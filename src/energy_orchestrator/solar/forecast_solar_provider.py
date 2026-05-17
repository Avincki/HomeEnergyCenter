"""Forecast.Solar provider — one HTTP call per configured plane.

Endpoint: ``https://api.forecast.solar/estimate/{lat}/{lon}/{decl}/{az}/{kwp}``
(paid tiers prefix the key as ``/{key}/estimate/...``). Free tier needs no
key. Rate limit info comes back inside ``result.message.ratelimit`` rather
than as an HTTP header — we surface ``remaining`` to the log so the user can
see if they're close to a throttle.

Timestamp handling: Forecast.Solar returns naive ``"YYYY-MM-DD HH:MM:SS"``
strings in the location's local timezone. We resolve "local" via
``zoneinfo`` (Europe/Brussels for BE coordinates) and convert to UTC
before yielding ``SolarPoint``s.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import structlog

from energy_orchestrator.config.models import SolarConfig, SolarPlaneConfig
from energy_orchestrator.solar.base import (
    SolarConfigurationError,
    SolarFetchError,
    SolarForecast,
    SolarParseError,
    SolarPoint,
    SolarProvider,
    sum_planes,
)

logger = structlog.stdlib.get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.forecast.solar"
_REQUEST_TIMEOUT_S = 30.0


def _last_sunday(year: int, month: int) -> datetime:
    """Last Sunday of the given month at 00:00 (naive)."""
    d = datetime(year, month, 28)
    while d.month == month:
        d += timedelta(days=1)
    d -= timedelta(days=1)  # last day of month
    while d.weekday() != 6:  # 6 = Sunday
        d -= timedelta(days=1)
    return d


class _EuropeBrusselsFallback(tzinfo):
    """Stdlib-only Brussels tz used when ``zoneinfo`` has no IANA database.

    Implements the EU-wide DST rule: CEST (UTC+2) from 02:00 local on the
    last Sunday of March to 03:00 local on the last Sunday of October;
    CET (UTC+1) otherwise. Good enough for parsing Forecast.Solar's
    local-time strings without depending on the optional ``tzdata`` wheel.
    """

    _CET = timedelta(hours=1)
    _CEST = timedelta(hours=2)

    def _is_dst_local(self, dt: datetime) -> bool:
        # Caller is in *local* (naive) time. DST starts at 02:00 local on
        # last-Sunday-March; ends at 03:00 local on last-Sunday-October.
        # Wall-clock comparison is fine for any unambiguous timestamp; the
        # ambiguous gap/overlap windows aren't relevant for hourly forecast
        # buckets (Forecast.Solar already returns a clean hourly grid).
        start = _last_sunday(dt.year, 3).replace(hour=2)
        end = _last_sunday(dt.year, 10).replace(hour=3)
        naive = dt.replace(tzinfo=None)
        return start <= naive < end

    def utcoffset(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return self._CET
        return self._CEST if self._is_dst_local(dt) else self._CET

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(0)
        return timedelta(hours=1) if self._is_dst_local(dt) else timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        if dt is None:
            return "CET"
        return "CEST" if self._is_dst_local(dt) else "CET"


def _resolve_local_tz(latitude: float, longitude: float) -> tzinfo:
    """Pick a tz for parsing Forecast.Solar's local-time strings.

    The API is opaque about which IANA name it uses internally; for Belgian
    coordinates Europe/Brussels matches in practice. We prefer ``zoneinfo``
    when the IANA db is available, otherwise fall back to a stdlib-only
    EU-DST implementation (or naive UTC for non-Belgian sites) so the
    service never crashes on a Python install without the ``tzdata`` wheel.
    """
    in_belgium = 49.0 <= latitude <= 52.0 and -1.0 <= longitude <= 7.0
    name = "Europe/Brussels" if in_belgium else "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if in_belgium:
            return _EuropeBrusselsFallback()
        return UTC


class ForecastSolarProvider(SolarProvider):
    def __init__(self, config: SolarConfig, *, base_url: str | None = None) -> None:
        super().__init__(config)
        if not config.planes:
            raise SolarConfigurationError("solar.planes must contain at least one entry")
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._tz = _resolve_local_tz(config.latitude, config.longitude)
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_S)
            )
        return self._session

    def _plane_url(self, plane: SolarPlaneConfig) -> str:
        prefix = ""
        if self.config.api_key is not None:
            prefix = f"/{self.config.api_key.get_secret_value()}"
        return (
            f"{self._base_url}{prefix}/estimate/"
            f"{self.config.latitude}/{self.config.longitude}/"
            f"{plane.declination}/{plane.azimuth}/{plane.kwp}"
        )

    def _query_params(self) -> dict[str, str]:
        params: dict[str, str] = {}
        if self.config.damping_morning > 0 or self.config.damping_evening > 0:
            # Combined "damping=morning,evening" syntax is what's documented.
            params["damping"] = (
                f"{self.config.damping_morning},{self.config.damping_evening}"
            )
        return params

    async def fetch_forecast(self) -> SolarForecast:
        session = self._ensure_session()
        params = self._query_params()
        # One call per plane — Forecast.Solar bills/limits per call.
        results = await asyncio.gather(
            *(self._fetch_one(session, plane, params) for plane in self.config.planes),
            return_exceptions=True,
        )

        per_plane: dict[str, tuple[SolarPoint, ...]] = {}
        wh_today_total = 0.0
        wh_tomorrow_total = 0.0
        any_today = False
        any_tomorrow = False
        first_error: BaseException | None = None
        for idx, (plane, res) in enumerate(zip(self.config.planes, results, strict=True)):
            label = plane.name or f"plane_{idx}"
            if isinstance(res, BaseException):
                first_error = first_error or res
                logger.warning(
                    "solar plane fetch failed",
                    plane=label,
                    error=str(res),
                )
                continue
            points, wh_today, wh_tomorrow = res
            per_plane[label] = points
            if wh_today is not None:
                wh_today_total += wh_today
                any_today = True
            if wh_tomorrow is not None:
                wh_tomorrow_total += wh_tomorrow
                any_tomorrow = True

        if not per_plane:
            assert first_error is not None
            raise SolarFetchError(f"all solar planes failed: {first_error}") from first_error

        return SolarForecast(
            points=sum_planes(per_plane),
            per_plane=per_plane,
            watt_hours_today=wh_today_total if any_today else None,
            watt_hours_tomorrow=wh_tomorrow_total if any_tomorrow else None,
        )

    async def _fetch_one(
        self,
        session: aiohttp.ClientSession,
        plane: SolarPlaneConfig,
        params: Mapping[str, str],
    ) -> tuple[tuple[SolarPoint, ...], float | None, float | None]:
        url = self._plane_url(plane)
        try:
            async with session.get(url, params=params) as resp:
                # Even non-200 responses include a JSON body with diagnostics.
                text = await resp.text()
                if resp.status != 200:
                    raise SolarFetchError(
                        f"Forecast.Solar HTTP {resp.status} for plane "
                        f"{plane.name or plane.azimuth}: {text[:200]}"
                    )
        except TimeoutError as e:
            raise SolarFetchError(f"Forecast.Solar request timed out: {url}") from e
        except aiohttp.ClientError as e:
            raise SolarFetchError(f"Forecast.Solar request failed: {e}") from e

        return self._parse_response(text, plane)

    def _parse_response(
        self, body: str, plane: SolarPlaneConfig
    ) -> tuple[tuple[SolarPoint, ...], float | None, float | None]:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise SolarParseError(f"Forecast.Solar response was not JSON: {e}") from e

        result = payload.get("result")
        if not isinstance(result, dict):
            raise SolarParseError("Forecast.Solar response missing 'result' object")

        watts_raw = result.get("watts")
        if not isinstance(watts_raw, dict):
            raise SolarParseError("Forecast.Solar 'result.watts' missing or wrong type")

        # Free-tier Forecast.Solar bakes in pessimistic system-loss and
        # temperature assumptions that can't be overridden via API params.
        # We apply a configurable multiplier here at the read site so every
        # downstream consumer (dashboard chart, day totals, future rules)
        # sees the corrected value rather than each having to recalibrate.
        cal = self.config.calibration_factor

        points: list[SolarPoint] = []
        for ts_text, w_value in watts_raw.items():
            try:
                local_dt = datetime.strptime(ts_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=self._tz)
            except ValueError as e:
                raise SolarParseError(
                    f"Forecast.Solar timestamp not parseable: {ts_text!r}"
                ) from e
            try:
                watts = float(w_value) * cal
            except (TypeError, ValueError) as e:
                raise SolarParseError(
                    f"Forecast.Solar watts value not numeric at {ts_text}: {w_value!r}"
                ) from e
            points.append(SolarPoint(timestamp=local_dt.astimezone(UTC), watts=watts))
        points.sort(key=lambda p: p.timestamp)

        wh_today, wh_tomorrow = self._extract_day_totals(result.get("watt_hours_day"))
        if wh_today is not None:
            wh_today *= cal
        if wh_tomorrow is not None:
            wh_tomorrow *= cal

        message = payload.get("message")
        if isinstance(message, dict):
            ratelimit = message.get("ratelimit")
            if isinstance(ratelimit, dict):
                logger.debug(
                    "forecast.solar ratelimit",
                    plane=plane.name or plane.azimuth,
                    remaining=ratelimit.get("remaining"),
                    limit=ratelimit.get("limit"),
                    period_s=ratelimit.get("period"),
                )

        return tuple(points), wh_today, wh_tomorrow

    def _extract_day_totals(self, raw: object) -> tuple[float | None, float | None]:
        """Pick out today + tomorrow from the ``watt_hours_day`` map.

        The API returns dates in the location's local timezone, so we use
        the same tz to derive "today" rather than UTC (otherwise a UTC
        midnight in the dashboard's first hours of operation could roll
        the date backwards).
        """
        if not isinstance(raw, dict):
            return None, None
        local_today = datetime.now(self._tz).date()
        local_tomorrow = local_today + timedelta(days=1)
        today_key = local_today.isoformat()
        tomorrow_key = local_tomorrow.isoformat()

        def _coerce(value: object) -> float | None:
            try:
                return float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        return _coerce(raw.get(today_key)), _coerce(raw.get(tomorrow_key))

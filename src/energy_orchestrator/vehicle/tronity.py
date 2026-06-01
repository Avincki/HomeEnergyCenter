"""Tronity cloud-API vehicle provider (EV state of charge).

Tronity bridges a manufacturer telemetry account (here a Mercedes EQS) to a
simple REST API. The flow, verified against evcc's Tronity adapter and the
Tronity docs:

1. **Token** — ``POST {base}/authentication`` with a JSON body
   ``{client_id, client_secret, grant_type: "app"}`` returns
   ``{access_token, token_type, expires_in}``. (Not interactive OAuth — a
   machine-to-machine app grant.) The token is cached and reused until it
   nears expiry.
2. **Vehicle** — ``GET {base}/tronity/vehicles`` lists the account's cars
   ``{data: [{id, vin, ...}]}``. The configured VIN selects one; a
   single-vehicle account needs no VIN. The resolved id is cached.
3. **Telemetry** — ``GET {base}/tronity/vehicles/{id}/last_record`` with
   ``Authorization: Bearer <token>`` returns the latest snapshot: ``level``
   (SoC %), ``range``, ``odometer``, ``charging``, ``plugged``,
   ``chargerPower``, ``latitude``/``longitude``, ``timestamp``.

Polling is slow and read-only by design (see ``TronityConfig``): each call
wakes the car and the data lags 30-40 min, so the tick loop refreshes on the
configured cadence, not every tick. Transient network/timeout failures retry
with backoff; an auth rejection does not retry (it won't fix itself).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from energy_orchestrator.config.models import TronityConfig
from energy_orchestrator.vehicle.base import (
    VehicleAuthError,
    VehicleConfigurationError,
    VehicleError,
    VehicleFetchError,
    VehicleParseError,
    VehicleProvider,
    VehicleRecord,
)

logger = structlog.stdlib.get_logger(__name__)

# Refresh the token this long before it actually expires, so an in-flight
# request never races the expiry boundary.
_TOKEN_REFRESH_MARGIN = timedelta(seconds=60)
# Fallback lifetime when the auth response omits ``expires_in``. Conservative
# so we re-auth sooner rather than carry a dead token.
_DEFAULT_TOKEN_LIFETIME = timedelta(minutes=30)
# Epoch values above this are milliseconds, not seconds (anything past ~2001 in
# ms, or year ~33000 in s — i.e. always ms in practice for a recent timestamp).
_EPOCH_MS_THRESHOLD = 1e12


class TronityProvider(VehicleProvider):
    """Reads one configured vehicle's telemetry from the Tronity cloud API."""

    def __init__(self, config: TronityConfig) -> None:
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        # Resolved once from the VIN (or the sole vehicle) and cached — the
        # account's vehicle list doesn't change at runtime.
        self._vehicle_id: str | None = None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout_s)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    @property
    def _base(self) -> str:
        return self.config.base_url.rstrip("/")

    # ----- token ---------------------------------------------------------------

    async def _ensure_token(self, now: datetime) -> str:
        if (
            self._token is not None
            and self._token_expires_at is not None
            and now < self._token_expires_at - _TOKEN_REFRESH_MARGIN
        ):
            return self._token
        return await self._authenticate(now)

    async def _authenticate(self, now: datetime) -> str:
        session = self._ensure_session()
        url = f"{self._base}/authentication"
        body = {
            "client_id": self.config.client_id.get_secret_value(),
            "client_secret": self.config.client_secret.get_secret_value(),
            "grant_type": "app",
        }
        try:
            async with session.post(url, json=body) as resp:
                if resp.status in (401, 403):
                    raise VehicleAuthError(f"tronity rejected credentials (HTTP {resp.status})")
                if resp.status >= 400:
                    raise VehicleFetchError(f"tronity auth HTTP {resp.status} at {url}")
                payload = await self._json(resp, "auth")
        except TimeoutError as e:
            raise VehicleFetchError(f"tronity auth timed out at {url}") from e
        except aiohttp.ClientConnectionError as e:
            raise VehicleFetchError(f"tronity auth connection error at {url}: {e}") from e

        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise VehicleParseError("tronity auth response missing access_token")
        lifetime = _DEFAULT_TOKEN_LIFETIME
        expires_in = _as_float(payload.get("expires_in"))
        if expires_in is not None and expires_in > 0:
            lifetime = timedelta(seconds=expires_in)
        self._token = token
        self._token_expires_at = now + lifetime
        logger.info("tronity authenticated", expires_in_s=lifetime.total_seconds())
        return token

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expires_at = None

    # ----- vehicle resolution --------------------------------------------------

    async def _ensure_vehicle_id(self, token: str) -> str:
        if self._vehicle_id is not None:
            return self._vehicle_id
        vehicles = await self._list_vehicles(token)
        self._vehicle_id = self._select_vehicle_id(vehicles)
        return self._vehicle_id

    async def _list_vehicles(self, token: str) -> list[dict[str, Any]]:
        payload = await self._authed_get(token, "/tronity/vehicles", "vehicles")
        # Tronity wraps the list in ``{data: [...]}``; tolerate a bare list too.
        data = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(data, list):
            raise VehicleParseError("tronity vehicles response was not a list")
        return [v for v in data if isinstance(v, dict)]

    def _select_vehicle_id(self, vehicles: list[dict[str, Any]]) -> str:
        if not vehicles:
            raise VehicleConfigurationError("tronity account exposes no vehicles")
        vin = self.config.vin
        if vin:
            for v in vehicles:
                if str(v.get("vin", "")).upper() == vin.upper():
                    vid = v.get("id")
                    if isinstance(vid, str) and vid:
                        return vid
                    raise VehicleParseError(f"tronity vehicle {vin} has no usable id")
            known = ", ".join(str(v.get("vin", "?")) for v in vehicles)
            raise VehicleConfigurationError(
                f"tronity VIN {vin} not found on the account (have: {known})"
            )
        if len(vehicles) > 1:
            known = ", ".join(str(v.get("vin", "?")) for v in vehicles)
            raise VehicleConfigurationError(
                f"tronity account has multiple vehicles ({known}); set tronity.vin to pick one"
            )
        vid = vehicles[0].get("id")
        if not isinstance(vid, str) or not vid:
            raise VehicleParseError("tronity sole vehicle has no usable id")
        return vid

    # ----- telemetry -----------------------------------------------------------

    async def fetch_record(self) -> VehicleRecord:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(VehicleFetchError),
            stop=stop_after_attempt(max(1, self.config.retry_count)),
            wait=wait_exponential(multiplier=0.3, min=0.3, max=3.0),
            reraise=True,
        ):
            with attempt:
                return await self._fetch_record_once()
        raise VehicleError("unreachable: AsyncRetrying(reraise=True) always re-raises")

    async def _fetch_record_once(self) -> VehicleRecord:
        now = datetime.now(UTC)
        token = await self._ensure_token(now)
        vehicle_id = await self._ensure_vehicle_id(token)
        path = f"/tronity/vehicles/{vehicle_id}/last_record"
        try:
            payload = await self._authed_get(token, path, "last_record")
        except VehicleAuthError:
            # Token was accepted at auth but rejected on use (rotated/expired
            # early). Drop it and re-auth once before giving up.
            self._invalidate_token()
            token = await self._ensure_token(datetime.now(UTC))
            payload = await self._authed_get(token, path, "last_record")
        return self._parse_record(payload, datetime.now(UTC))

    async def _authed_get(self, token: str, path: str, label: str) -> dict[str, Any]:
        session = self._ensure_session()
        url = f"{self._base}{path}"
        headers = {"Authorization": f"Bearer {token}", "Unit-System": "metric"}
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status in (401, 403):
                    raise VehicleAuthError(f"tronity {label} unauthorized (HTTP {resp.status})")
                if resp.status >= 400:
                    raise VehicleFetchError(f"tronity {label} HTTP {resp.status} at {url}")
                return await self._json(resp, label)
        except TimeoutError as e:
            raise VehicleFetchError(f"tronity {label} timed out at {url}") from e
        except aiohttp.ClientConnectionError as e:
            raise VehicleFetchError(f"tronity {label} connection error at {url}: {e}") from e

    @staticmethod
    async def _json(resp: aiohttp.ClientResponse, label: str) -> dict[str, Any]:
        try:
            payload = await resp.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError) as e:
            raise VehicleParseError(f"tronity {label} response was not JSON: {e}") from e
        if not isinstance(payload, dict):
            raise VehicleParseError(
                f"tronity {label} response was not a JSON object: {type(payload).__name__}"
            )
        return payload

    def _parse_record(self, payload: dict[str, Any], fetched_at: datetime) -> VehicleRecord:
        return VehicleRecord(
            fetched_at=fetched_at,
            soc_pct=_as_float(payload.get("level")),
            plugged=_as_bool(payload.get("plugged")),
            charging=_as_str(payload.get("charging")),
            range_km=_as_float(payload.get("range")),
            odometer_km=_as_float(payload.get("odometer")),
            charger_power_kw=_as_float(payload.get("chargerPower")),
            latitude=_as_float(payload.get("latitude")),
            longitude=_as_float(payload.get("longitude")),
            recorded_at=_epoch_to_utc(payload.get("timestamp")),
        )


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _epoch_to_utc(value: Any) -> datetime | None:
    """Parse Tronity's ``timestamp`` (epoch seconds or milliseconds) to UTC."""
    if value is None or isinstance(value, bool):
        return None
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        return None
    if epoch <= 0:
        return None
    if epoch >= _EPOCH_MS_THRESHOLD:
        epoch /= 1000.0
    try:
        return datetime.fromtimestamp(epoch, UTC)
    except (OverflowError, OSError, ValueError):
        return None

"""ENTSO-E Transparency Platform day-ahead price provider.

Calls the public REST-XML API at ``https://web-api.tp.entsoe.eu/api`` with
``documentType=A44`` (day-ahead prices). Wholesale prices are quoted in
EUR/MWh; we convert to EUR/kWh and apply ``injection_factor`` /
``injection_offset`` from config to derive the injection price.

ENTSO-E response convention: positions inside a Period that share the same
price as the previous position are *omitted*. We fill forward.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import datetime, timedelta

import aiohttp

from energy_orchestrator.config.models import PricesConfig
from energy_orchestrator.prices.base import (
    PriceConfigurationError,
    PriceFetchError,
    PriceParseError,
    PricePoint,
    PriceProvider,
)

_DEFAULT_BASE_URL = "https://web-api.tp.entsoe.eu/api"
_DAY_AHEAD_DOC_TYPE = "A44"

# Without this, aiohttp's default 5-minute total timeout lets a hung ENTSO-E
# response stall the entire tick loop — every read, decision, and actuation
# is held up until the request gives up. 30 s matches the solar provider.
_REQUEST_TIMEOUT_S = 30.0

# Bidding-zone EIC codes for areas users typically configure as 2-letter codes.
# Anything not in this map is passed through verbatim, so users can supply a
# raw EIC if their area isn't listed.
_AREA_TO_EIC: dict[str, str] = {
    "BE": "10YBE----------2",
    "NL": "10YNL----------L",
    "DE": "10Y1001A1001A82H",
    "FR": "10YFR-RTE------C",
    "AT": "10YAT-APG------L",
    "LU": "10YLU-CEGEDEL-NQ",
}

_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?$")


def _resolve_eic(area: str) -> str:
    return _AREA_TO_EIC.get(area.upper(), area)


def _parse_iso_duration_minutes(text: str) -> int:
    m = _DURATION_RE.match(text)
    if not m:
        raise PriceParseError(f"unsupported ENTSO-E resolution: {text!r}")
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    total = hours * 60 + minutes
    if total <= 0:
        raise PriceParseError(f"non-positive duration: {text!r}")
    return total


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.partition("}")[2]


class EntsoePriceProvider(PriceProvider):
    def __init__(self, config: PricesConfig, *, base_url: str | None = None) -> None:
        super().__init__(config)
        if config.api_key is None:
            raise PriceConfigurationError("api_key required for entsoe provider")
        self._eic = _resolve_eic(config.area)
        self._base_url = base_url or _DEFAULT_BASE_URL
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

    async def fetch_prices(self, start: datetime, end: datetime) -> Sequence[PricePoint]:
        assert self.config.api_key is not None  # checked in __init__
        params = {
            "securityToken": self.config.api_key.get_secret_value(),
            "documentType": _DAY_AHEAD_DOC_TYPE,
            "in_Domain": self._eic,
            "out_Domain": self._eic,
            "periodStart": start.strftime("%Y%m%d%H%M"),
            "periodEnd": end.strftime("%Y%m%d%H%M"),
        }
        session = self._ensure_session()
        try:
            async with session.get(self._base_url, params=params) as resp:
                if resp.status != 200:
                    raise PriceFetchError(f"ENTSO-E HTTP {resp.status} at {self._base_url}")
                xml_text = await resp.text()
        except TimeoutError as e:
            raise PriceFetchError(f"ENTSO-E request timed out at {self._base_url}") from e
        except aiohttp.ClientError as e:
            raise PriceFetchError(f"ENTSO-E request failed: {e}") from e
        return self._parse_xml(xml_text)

    def _parse_xml(self, xml_text: str) -> list[PricePoint]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            raise PriceParseError(f"ENTSO-E response was not valid XML: {e}") from e
        _strip_ns(root)

        factor = self.config.injection_factor
        offset = self.config.injection_offset

        points: list[PricePoint] = []
        for ts_el in root.findall("TimeSeries"):
            for period in ts_el.findall("Period"):
                points.extend(self._parse_period(period, factor, offset))
        return points

    @staticmethod
    def _parse_period(
        period: ET.Element, injection_factor: float, injection_offset: float
    ) -> list[PricePoint]:
        ti_start = period.findtext("timeInterval/start")
        ti_end = period.findtext("timeInterval/end")
        resolution_text = period.findtext("resolution")
        if not (ti_start and ti_end and resolution_text):
            raise PriceParseError("ENTSO-E Period missing timeInterval or resolution")

        try:
            start_dt = datetime.fromisoformat(ti_start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(ti_end.replace("Z", "+00:00"))
        except ValueError as e:
            raise PriceParseError(f"ENTSO-E Period timeInterval not parseable: {e}") from e

        resolution_min = _parse_iso_duration_minutes(resolution_text)
        total_minutes = (end_dt - start_dt).total_seconds() / 60.0
        num_positions = int(total_minutes // resolution_min)
        if num_positions <= 0:
            return []

        pos_to_price: dict[int, float] = {}
        for pt in period.findall("Point"):
            pos_text = pt.findtext("position")
            price_text = pt.findtext("price.amount")
            if pos_text is None or price_text is None:
                raise PriceParseError("ENTSO-E Point missing position or price.amount")
            try:
                pos = int(pos_text)
                price = float(price_text)
            except ValueError as e:
                raise PriceParseError(f"ENTSO-E Point has non-numeric value: {e}") from e
            pos_to_price[pos] = price

        out: list[PricePoint] = []
        last_wholesale: float | None = None
        for pos in range(1, num_positions + 1):
            if pos in pos_to_price:
                last_wholesale = pos_to_price[pos]
            if last_wholesale is None:
                continue  # gap at the start of the period — skip
            wholesale_kwh = last_wholesale / 1000.0  # EUR/MWh -> EUR/kWh
            injection = wholesale_kwh * injection_factor + injection_offset
            ts_for_pos = start_dt + timedelta(minutes=resolution_min * (pos - 1))
            out.append(
                PricePoint(
                    timestamp=ts_for_pos,
                    consumption_eur_per_kwh=wholesale_kwh,
                    injection_eur_per_kwh=injection,
                )
            )
        return out

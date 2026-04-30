"""sonnenBatterie HTTP client.

Talks to the local sonnen API in either v1 (no auth, port 8080) or v2
(``Auth-Token`` header, port 80) flavour. Network/timeout failures retry
with exponential backoff; auth (401) and protocol errors do not retry.
"""

from __future__ import annotations

from typing import Any

import aiohttp
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from energy_orchestrator.config.models import SonnenApiVersion, SonnenBatterieConfig
from energy_orchestrator.data.models import SourceName
from energy_orchestrator.devices.base import DeviceClient, DeviceReading
from energy_orchestrator.devices.errors import (
    DeviceConfigurationError,
    DeviceConnectionError,
    DeviceError,
    DeviceProtocolError,
    DeviceTimeoutError,
)
from energy_orchestrator.devices.registry import register_device

# Sonnen response field name -> our normalized name.
# Both v1 and v2 use the same field labels for the metrics we care about.
_FIELD_MAP: dict[str, str] = {
    "USOC": "soc_pct",
    "Pac_total_W": "battery_power_w",
    "Production_W": "production_w",
    "Consumption_W": "consumption_w",
    "GridFeedIn_W": "grid_feed_in_w",
}


@register_device(SonnenBatterieConfig)
class SonnenClient(DeviceClient[SonnenBatterieConfig]):
    source_name = SourceName.SONNEN

    def __init__(self, config: SonnenBatterieConfig) -> None:
        super().__init__(config)
        self._session: aiohttp.ClientSession | None = None

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
    def _url(self) -> str:
        path = (
            "/api/v2/status" if self.config.api_version is SonnenApiVersion.V2 else "/api/v1/status"
        )
        return f"http://{self.config.host}:{self.config.port}{path}"

    @property
    def _headers(self) -> dict[str, str]:
        if self.config.api_version is SonnenApiVersion.V2 and self.config.auth_token is not None:
            return {"Auth-Token": self.config.auth_token.get_secret_value()}
        return {}

    async def _fetch_once(self) -> dict[str, Any]:
        session = self._ensure_session()
        try:
            async with session.get(self._url, headers=self._headers) as resp:
                if resp.status == 401:
                    raise DeviceConfigurationError(
                        f"sonnen rejected token (HTTP 401) at {self._url}"
                    )
                if resp.status >= 400:
                    raise DeviceConnectionError(f"sonnen HTTP {resp.status} at {self._url}")
                try:
                    payload = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as e:
                    raise DeviceProtocolError(f"sonnen response was not JSON: {e}") from e
                if not isinstance(payload, dict):
                    raise DeviceProtocolError(
                        f"sonnen response was not a JSON object: {type(payload).__name__}"
                    )
                return payload
        except TimeoutError as e:
            raise DeviceTimeoutError(f"sonnen timed out at {self._url}") from e
        except aiohttp.ClientConnectionError as e:
            raise DeviceConnectionError(f"sonnen connection error at {self._url}: {e}") from e

    async def _fetch_with_retry(self) -> dict[str, Any]:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((DeviceConnectionError, DeviceTimeoutError)),
            stop=stop_after_attempt(max(1, self.config.retry_count)),
            wait=wait_exponential(multiplier=0.2, min=0.2, max=2.0),
            reraise=True,
        ):
            with attempt:
                return await self._fetch_once()
        raise DeviceError("unreachable: AsyncRetrying(reraise=True) always re-raises")

    @staticmethod
    def _normalize(payload: dict[str, Any]) -> tuple[dict[str, float], float]:
        normalized: dict[str, float] = {}
        for raw_key, our_key in _FIELD_MAP.items():
            value = payload.get(raw_key)
            if value is None:
                continue
            try:
                normalized[our_key] = float(value)
            except (TypeError, ValueError):
                continue
        soc = normalized.get("soc_pct")
        if soc is not None and not 0.0 <= soc <= 100.0:
            raise DeviceProtocolError(f"sonnen reported SoC out of range: {soc}")
        quality = len(normalized) / len(_FIELD_MAP)
        return normalized, quality

    async def read_data(self) -> DeviceReading | None:
        payload = await self._fetch_with_retry()
        normalized, quality = self._normalize(payload)
        if "soc_pct" not in normalized:
            # Essential field missing — caller skips the tick (per spec).
            return None
        return DeviceReading(
            device_id=self.device_id,
            data=dict(normalized),
            quality=quality,
        )

    async def health_check(self) -> bool:
        try:
            await self._fetch_once()
        except DeviceError:
            return False
        return True

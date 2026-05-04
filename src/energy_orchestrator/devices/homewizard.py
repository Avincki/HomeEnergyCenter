"""HomeWizard kWh-meter / P1 dongle HTTP client.

All three HomeWizard variants in this project (car-charger meter, P1 dongle,
small-solar meter) share the same ``GET /api/v1/data`` endpoint. The shared
``HomeWizardClient`` does the HTTP and parsing; thin subclasses below set
``source_name`` and register against the right config type.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

import aiohttp
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from energy_orchestrator.config.models import (
    CarChargerConfig,
    HomeWizardDeviceConfig,
    LargeSolarConfig,
    P1MeterConfig,
    SmallSolarConfig,
)
from energy_orchestrator.data.models import SourceName
from energy_orchestrator.devices.base import DeviceClient, DeviceReading
from energy_orchestrator.devices.errors import (
    DeviceConnectionError,
    DeviceError,
    DeviceProtocolError,
    DeviceTimeoutError,
)
from energy_orchestrator.devices.registry import register_device

HwConfigT = TypeVar("HwConfigT", bound=HomeWizardDeviceConfig)

_FIELDS: tuple[str, ...] = (
    "active_power_w",
    "total_power_import_kwh",
    "total_power_export_kwh",
)


class HomeWizardClient(DeviceClient[HwConfigT], Generic[HwConfigT]):
    """Shared HTTP/parse logic for HomeWizard meters. Not registered itself —
    instantiate one of the concrete subclasses below.
    """

    def __init__(self, config: HwConfigT) -> None:
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
        return f"http://{self.config.host}:{self.config.port}/api/v1/data"

    async def _fetch_once(self) -> dict[str, Any]:
        session = self._ensure_session()
        try:
            async with session.get(self._url) as resp:
                if resp.status >= 400:
                    raise DeviceConnectionError(
                        f"{self.source_name} HTTP {resp.status} at {self._url}"
                    )
                try:
                    payload = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as e:
                    raise DeviceProtocolError(
                        f"{self.source_name} response was not JSON: {e}"
                    ) from e
                if not isinstance(payload, dict):
                    raise DeviceProtocolError(
                        f"{self.source_name} response was not a JSON object: "
                        f"{type(payload).__name__}"
                    )
                return payload
        except TimeoutError as e:
            raise DeviceTimeoutError(f"{self.source_name} timed out at {self._url}") from e
        except aiohttp.ClientConnectionError as e:
            raise DeviceConnectionError(
                f"{self.source_name} connection error at {self._url}: {e}"
            ) from e

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
    def _normalize(payload: dict[str, Any]) -> dict[str, float]:
        data: dict[str, float] = {}
        for key in _FIELDS:
            v = payload.get(key)
            if v is None:
                continue
            try:
                data[key] = float(v)
            except (TypeError, ValueError):
                continue
        return data

    async def read_data(self) -> DeviceReading | None:
        payload = await self._fetch_with_retry()
        data = self._normalize(payload)
        if "active_power_w" not in data:
            return None
        return DeviceReading(device_id=self.device_id, data=data, quality=1.0)

    async def health_check(self) -> bool:
        try:
            await self._fetch_once()
        except DeviceError:
            return False
        return True


@register_device(CarChargerConfig)
class CarChargerClient(HomeWizardClient[CarChargerConfig]):
    source_name = SourceName.CAR_CHARGER


@register_device(P1MeterConfig)
class P1MeterClient(HomeWizardClient[P1MeterConfig]):
    source_name = SourceName.P1_METER


@register_device(SmallSolarConfig)
class SmallSolarClient(HomeWizardClient[SmallSolarConfig]):
    source_name = SourceName.SMALL_SOLAR


@register_device(LargeSolarConfig)
class LargeSolarClient(HomeWizardClient[LargeSolarConfig]):
    source_name = SourceName.LARGE_SOLAR

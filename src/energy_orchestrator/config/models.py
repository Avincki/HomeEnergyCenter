from __future__ import annotations

import ipaddress
import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    model_validator,
)

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)([a-zA-Z0-9-]{1,63}(?<!-)\.)*[a-zA-Z0-9-]{1,63}(?<!-)$"
)


def _validate_host(value: str) -> str:
    v = value.strip()
    if not v:
        raise ValueError("host must not be empty")
    try:
        ipaddress.ip_address(v)
    except ValueError:
        if not _HOSTNAME_RE.match(v):
            raise ValueError(f"{value!r} is not a valid IP address or hostname") from None
    return v


Host = Annotated[str, AfterValidator(_validate_host)]
Port = Annotated[int, Field(ge=1, le=65535)]
Percent = Annotated[float, Field(ge=0.0, le=100.0)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ----- devices -----------------------------------------------------------------


class DeviceConfig(_StrictModel):
    host: Host
    port: Port = 80
    timeout_s: float = Field(default=5.0, gt=0, le=30)
    retry_count: int = Field(default=3, ge=1, le=10)


class SonnenApiVersion(StrEnum):
    V1 = "v1"
    V2 = "v2"


class SonnenBatterieConfig(DeviceConfig):
    api_version: SonnenApiVersion = SonnenApiVersion.V2
    auth_token: SecretStr | None = None
    capacity_kwh: float = Field(..., gt=0, description="Battery capacity in kWh")

    @model_validator(mode="after")
    def _v2_requires_token(self) -> SonnenBatterieConfig:
        if self.api_version is SonnenApiVersion.V2 and self.auth_token is None:
            raise ValueError("sonnen api_version=v2 requires auth_token")
        return self


class HomeWizardDeviceConfig(DeviceConfig):
    """Base for HomeWizard devices — all use port 80 with the same /api/v1/data shape."""


class CarChargerConfig(HomeWizardDeviceConfig):
    charging_threshold_w: float = Field(
        default=500.0,
        gt=0,
        description="active_power_w above which the EV is considered charging",
    )


class P1MeterConfig(HomeWizardDeviceConfig):
    pass


class SmallSolarConfig(HomeWizardDeviceConfig):
    peak_w: float = Field(
        ...,
        gt=0,
        description="Nameplate peak output of the small (non-SolarEdge) PV string",
    )


class LargeSolarConfig(HomeWizardDeviceConfig):
    peak_w: float = Field(
        ...,
        gt=0,
        description="Nameplate peak output of the large PV string (e.g. east+west arrays)",
    )


class HomeWizardConfig(_StrictModel):
    car_charger: CarChargerConfig
    p1_meter: P1MeterConfig
    small_solar: SmallSolarConfig
    # Optional second HomeWizard kWh meter on a larger PV string. Omit the
    # whole subsection to disable.
    large_solar: LargeSolarConfig | None = None


class SolarEdgeConfig(DeviceConfig):
    port: Port = 1502
    modbus_port: Port = 1502
    unit_id: int = Field(default=1, ge=1, le=247)


# ----- prices ------------------------------------------------------------------


class PricesProvider(StrEnum):
    ENTSOE = "entsoe"
    TIBBER = "tibber"
    CSV = "csv"


class PricesConfig(_StrictModel):
    provider: PricesProvider
    api_key: SecretStr | None = None
    area: str = Field(
        default="BE",
        min_length=2,
        max_length=16,
        description="2-letter country code (resolved to an EIC) or a raw EIC string",
    )
    csv_path: Path | None = None
    base_url: str | None = Field(
        default=None,
        min_length=1,
        description="Override the provider's default REST endpoint (entsoe only)",
    )
    injection_factor: float = Field(default=1.0)
    injection_offset: float = Field(default=0.0, description="EUR/kWh added to derive injection")

    @model_validator(mode="after")
    def _provider_requirements(self) -> PricesConfig:
        if self.provider in (PricesProvider.ENTSOE, PricesProvider.TIBBER) and self.api_key is None:
            raise ValueError(f"prices.api_key is required for provider={self.provider.value}")
        if self.provider is PricesProvider.CSV and self.csv_path is None:
            raise ValueError("prices.csv_path is required for provider=csv")
        return self


# ----- solar forecast ----------------------------------------------------------


class SolarPlaneConfig(_StrictModel):
    """One PV array. Forecast.Solar treats each plane as a separate API call."""

    name: str = Field(default="", max_length=32, description="Display label, e.g. 'east'")
    declination: int = Field(..., ge=0, le=90, description="Tilt from horizontal in degrees")
    azimuth: int = Field(
        ...,
        ge=-180,
        le=180,
        description="Compass deviation from south: -90=east, 0=south, 90=west, 180=north",
    )
    kwp: float = Field(..., gt=0, le=1000, description="Peak nameplate output in kWp")


class SolarConfig(_StrictModel):
    """Forecast.Solar configuration. Free public tier needs no api_key."""

    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    api_key: SecretStr | None = None
    planes: tuple[SolarPlaneConfig, ...] = Field(
        default_factory=tuple,
        description="One entry per array. Forecast.Solar free tier supports up to 4.",
    )
    damping_morning: float = Field(default=0.0, ge=0.0, le=1.0)
    damping_evening: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _at_least_one_plane(self) -> SolarConfig:
        if len(self.planes) == 0:
            raise ValueError("solar.planes must contain at least one plane")
        if len(self.planes) > 4:
            raise ValueError("solar.planes: at most 4 planes (Forecast.Solar limit)")
        return self


# ----- decision / storage / logging / web -------------------------------------


class DecisionConfig(_StrictModel):
    battery_low_soc_pct: Percent = 60.0
    battery_full_soc_pct: Percent = 80.0
    hysteresis_pct: float = Field(default=5.0, ge=0.0, le=50.0)
    forecast_horizon_h: float = Field(default=12.0, gt=0.0, le=48.0)
    dry_run: bool = True

    @model_validator(mode="after")
    def _band_consistent(self) -> DecisionConfig:
        if self.battery_low_soc_pct >= self.battery_full_soc_pct:
            raise ValueError("battery_low_soc_pct must be < battery_full_soc_pct")
        if self.battery_low_soc_pct + self.hysteresis_pct > self.battery_full_soc_pct:
            raise ValueError(
                "battery_low_soc_pct + hysteresis_pct must be <= battery_full_soc_pct "
                "(otherwise the ON-band overruns the full threshold)"
            )
        return self


class StorageConfig(_StrictModel):
    sqlite_path: Path = Path("data/orchestrator.db")
    history_retention_days: int = Field(default=90, ge=1)


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class LoggingConfig(_StrictModel):
    log_dir: Path = Path("logs")
    level: LogLevel = "INFO"
    retention_days: int = Field(default=30, ge=1)


class WebConfig(_StrictModel):
    host: str = "0.0.0.0"
    port: Port = 8000


# ----- root --------------------------------------------------------------------


class AppConfig(_StrictModel):
    poll_interval_s: float = Field(default=30.0, gt=0.0, le=600.0)
    # How often the decision engine runs (and SolarEdge is actuated, if state
    # flipped). Decoupled from poll_interval_s so the dashboard can show fresh
    # readings while the inverter is left alone unless a real edge occurs.
    decision_interval_s: float = Field(default=60.0, gt=0.0, le=3600.0)
    sonnen: SonnenBatterieConfig
    homewizard: HomeWizardConfig
    solaredge: SolarEdgeConfig
    prices: PricesConfig
    solar: SolarConfig | None = None
    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    web: WebConfig = Field(default_factory=WebConfig)

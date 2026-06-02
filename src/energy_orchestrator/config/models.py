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


class EtrelInchConfig(DeviceConfig):
    """Etrel INCH Home/Pro EV charger over Modbus TCP (port 502 only).

    Cluster/Load-Guard port 503 is intentionally unused — the HomeWizard P1
    meter is the authoritative grid measurement.
    """

    port: Port = 502
    modbus_port: Port = 502
    unit_id: int = Field(default=1, ge=1, le=247)


# ----- vehicle (EV telemetry) --------------------------------------------------


class TronityConfig(_StrictModel):
    """Tronity cloud-API link for reading the EV (Mercedes EQS) state of charge.

    Tronity is an OAuth2 REST bridge to the *car's own* telemetry — not a local
    device — so it has no host/port and is polled on its own slow cadence
    (``poll_interval_s``) rather than every tick. Each poll wakes the car and
    draws on its 12 V battery, and the data itself lags 30-40 min, so the
    default cadence is deliberately long.

    SoC is read-only here: the EV is never commanded (the Etrel charger stays
    the sole actuator). The data is vehicle-centric, not socket-centric — the
    car reports a VIN wherever it physically is — so a consumer should confirm
    the EQS is at home (``home_latitude``/``home_longitude`` geofence) and the
    record is fresh (``stale_after_s``) before trusting it. No charging rule
    consumes the SoC yet; this section only feeds the dashboard for now.
    """

    client_id: SecretStr
    client_secret: SecretStr
    # VIN selects which vehicle on the Tronity account to read. Optional when
    # the account has exactly one vehicle (that one is then used); required to
    # disambiguate when the account exposes several.
    vin: str | None = Field(default=None, max_length=32)
    base_url: str = Field(
        default="https://api.tronity.tech",
        min_length=1,
        description="Override the Tronity API base (test/staging); production needs no change",
    )
    # Long by default: each poll wakes the car / drains its 12 V battery, and
    # Tronity data lags 30-40 min so a faster cadence buys nothing.
    poll_interval_s: float = Field(default=900.0, ge=60.0, le=86400.0)
    timeout_s: float = Field(default=10.0, gt=0.0, le=60.0)
    retry_count: int = Field(default=2, ge=1, le=10)
    # SoC older than this is treated as not trustworthy by any consumer (a
    # future charge-control gate). Display still shows it, flagged stale.
    stale_after_s: float = Field(default=3600.0, gt=0.0)
    # Home geofence — lets a consumer confirm the EQS is physically at home
    # before trusting its SoC. Optional; both coords must be set together, and
    # when unset "at home" can't be computed and is reported as null.
    home_latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    home_longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    geofence_radius_m: float = Field(default=250.0, gt=0.0, le=100000.0)

    @model_validator(mode="after")
    def _geofence_consistent(self) -> TronityConfig:
        if (self.home_latitude is None) != (self.home_longitude is None):
            raise ValueError(
                "tronity.home_latitude and home_longitude must be set together (or both omitted)"
            )
        return self


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
    calibration_factor: float = Field(
        default=1.56,
        gt=0.0,
        le=5.0,
        description=(
            "Multiplier applied to all Forecast.Solar watts/Wh values at the read "
            "site. Compensates for the free-tier model's conservative system-loss "
            "and temperature assumptions, which routinely under-forecast by 20-40% "
            "on clear days. Tune empirically against measured daily kWh."
        ),
    )

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


class ChargerControlConfig(_StrictModel):
    """Rule-based control of the Etrel EV charger during solar daytime.

    Every threshold is a tuning knob — expected to be adjusted empirically
    during the live-test window, not in code. See ``decision/charger_control.py``
    for how they combine.
    """

    # Master switch. Off => the controller never runs and the charger is left
    # to manual control (the /etrel/set-current endpoint). Turn on deliberately.
    enabled: bool = False
    # Compute + log the decision every tick but do NOT write to the charger.
    # Lets the behaviour be observed before it actuates. Default true so a fresh
    # ``enabled`` rollout is read-only until explicitly trusted.
    dry_run: bool = True

    # Home-battery SoC floor: below this the car must not charge (battery has
    # priority). Hysteresis re-enables only above floor + hysteresis to stop
    # boundary flapping.  [TUNABLE]
    battery_floor_soc_pct: Percent = 30.0
    battery_floor_hysteresis_pct: float = Field(default=3.0, ge=0.0, le=50.0)

    # Surplus-following dead-band. Up-tick when the available-power signal
    # exceeds export_threshold; down-tick when *measured* grid import exceeds
    # import_threshold. Keep their sum > ~700 W so one 3-phase step (~690 W)
    # can't jump across the band.  [TUNABLE]
    export_threshold_w: float = Field(default=500.0, ge=0.0)
    import_threshold_w: float = Field(default=500.0, ge=0.0)

    # Home-battery max discharge power, added as virtual headroom to the
    # measured export so the car can draw on the battery (down to the SoC
    # floor), not only on live solar export.  [TUNABLE]
    battery_max_output_w: float = Field(default=9000.0, gt=0.0)

    # SoC floor for the discharge-reserve *taper* — separate from
    # battery_floor_soc_pct (the charge-stop gate). The battery's contribution
    # to the available-power signal tapers linearly from battery_max_output_w at
    # 100% SoC down to 0 at this SoC. Set it below battery_floor_soc_pct to let
    # the signal lean harder on the battery above the charge-stop point.
    # [TUNABLE]
    taper_floor_soc_pct: Percent = 30.0

    # Resume from pause to min_charge_a only when the signal covers that draw,
    # so resuming doesn't immediately import and flap. ~ min_charge_a x 3 x
    # 230 V + margin on 3-phase.  [TUNABLE]
    resume_surplus_threshold_w: float = Field(default=4300.0, ge=0.0)

    # Charge-current envelope and per-decision step. min is the IEC/J1772 floor
    # (below it the charger pauses); max is the installation hard cap; step is
    # the ±adjustment per decision tick.  [TUNABLE]
    min_charge_a: float = Field(default=6.0, ge=0.0, le=16.0)
    max_charge_a: float = Field(default=16.0, gt=0.0, le=16.0)
    step_a: float = Field(default=1.0, gt=0.0, le=16.0)

    @model_validator(mode="after")
    def _envelope_consistent(self) -> ChargerControlConfig:
        if self.min_charge_a > self.max_charge_a:
            raise ValueError("charger_control.min_charge_a must be <= max_charge_a")
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
    # Optional Etrel INCH EV charger over Modbus TCP. Omit to disable.
    # The HomeWizard car_charger meter measures Tesla + Etrel together; this
    # entry lets the orchestrator subtract Etrel power to derive Tesla draw.
    etrel: EtrelInchConfig | None = None
    # Optional Tronity cloud link for the EV's state of charge (read-only,
    # display-only for now). Omit the whole section to disable.
    tronity: TronityConfig | None = None
    prices: PricesConfig
    solar: SolarConfig | None = None
    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    # Optional Etrel charger rule-based control. Inert unless ``enabled`` and an
    # ``etrel`` device + ``solar`` (for sunrise/sunset lat-lon) are configured.
    charger_control: ChargerControlConfig = Field(default_factory=ChargerControlConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    web: WebConfig = Field(default_factory=WebConfig)

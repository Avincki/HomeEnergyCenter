"""Form-section definitions for the web config editor.

Mirrors the grouping used by the tkinter editor (``gui/app.py``) but lives
in the web layer so the FastAPI process never imports tkinter. Field keys
are the same dotted paths consumed by ``gui.binding.form_to_config``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WebField:
    label: str
    key: str
    kind: str = "text"  # text | password | select | checkbox
    choices: tuple[str, ...] = ()
    hint: str = ""


@dataclass(frozen=True)
class WebSection:
    title: str
    fields: tuple[WebField, ...] = field(default_factory=tuple)


SECTIONS: tuple[tuple[str, tuple[WebSection, ...]], ...] = (
    (
        "Devices",
        (
            WebSection(
                "sonnenBatterie",
                (
                    WebField("Host", "sonnen.host"),
                    WebField("Port", "sonnen.port"),
                    WebField("API version", "sonnen.api_version", "select", ("v1", "v2")),
                    WebField(
                        "Auth token",
                        "sonnen.auth_token",
                        "password",
                        hint="required when api_version=v2",
                    ),
                    WebField("Capacity (kWh)", "sonnen.capacity_kwh"),
                    WebField("Timeout (s)", "sonnen.timeout_s"),
                    WebField("Retry count", "sonnen.retry_count"),
                ),
            ),
            WebSection(
                "HomeWizard — Car Charger",
                (
                    WebField("Host", "homewizard.car_charger.host"),
                    WebField("Port", "homewizard.car_charger.port"),
                    WebField(
                        "Charging threshold (W)",
                        "homewizard.car_charger.charging_threshold_w",
                    ),
                    WebField("Timeout (s)", "homewizard.car_charger.timeout_s"),
                    WebField("Retry count", "homewizard.car_charger.retry_count"),
                ),
            ),
            WebSection(
                "HomeWizard — P1 Meter",
                (
                    WebField("Host", "homewizard.p1_meter.host"),
                    WebField("Port", "homewizard.p1_meter.port"),
                    WebField("Timeout (s)", "homewizard.p1_meter.timeout_s"),
                    WebField("Retry count", "homewizard.p1_meter.retry_count"),
                ),
            ),
            WebSection(
                "HomeWizard — Small Solar",
                (
                    WebField("Host", "homewizard.small_solar.host"),
                    WebField("Port", "homewizard.small_solar.port"),
                    WebField("Peak (W)", "homewizard.small_solar.peak_w"),
                    WebField("Timeout (s)", "homewizard.small_solar.timeout_s"),
                    WebField("Retry count", "homewizard.small_solar.retry_count"),
                ),
            ),
            WebSection(
                "SolarEdge",
                (
                    WebField("Host", "solaredge.host"),
                    WebField("Modbus port", "solaredge.modbus_port"),
                    WebField("Unit ID", "solaredge.unit_id"),
                    WebField("Timeout (s)", "solaredge.timeout_s"),
                    WebField("Retry count", "solaredge.retry_count"),
                ),
            ),
        ),
    ),
    (
        "Decision",
        (
            WebSection(
                "Battery thresholds",
                (
                    WebField("Low SoC threshold (%)", "decision.battery_low_soc_pct"),
                    WebField("Full SoC threshold (%)", "decision.battery_full_soc_pct"),
                    WebField("Hysteresis (%)", "decision.hysteresis_pct"),
                    WebField("Forecast horizon (h)", "decision.forecast_horizon_h"),
                ),
            ),
            WebSection(
                "Pricing",
                (
                    WebField(
                        "Provider",
                        "prices.provider",
                        "select",
                        ("entsoe", "tibber", "csv"),
                    ),
                    WebField(
                        "API key",
                        "prices.api_key",
                        "password",
                        hint="required for entsoe / tibber",
                    ),
                    WebField(
                        "Area",
                        "prices.area",
                        hint="2-letter code (BE/NL/DE/FR/AT/LU) or raw EIC",
                    ),
                    WebField(
                        "CSV path", "prices.csv_path", hint="only when provider=csv"
                    ),
                    WebField(
                        "Base URL",
                        "prices.base_url",
                        hint="optional — override the entsoe REST endpoint",
                    ),
                    WebField("Injection factor", "prices.injection_factor"),
                    WebField("Injection offset (€/kWh)", "prices.injection_offset"),
                ),
            ),
            WebSection(
                "Safety",
                (
                    WebField(
                        "Dry-run (suppress SolarEdge writes)",
                        "decision.dry_run",
                        "checkbox",
                    ),
                ),
            ),
        ),
    ),
    (
        "System",
        (
            WebSection(
                "Operational",
                (
                    WebField("Poll interval (s)", "poll_interval_s"),
                    WebField("SQLite path", "storage.sqlite_path"),
                    WebField("History retention (days)", "storage.history_retention_days"),
                ),
            ),
            WebSection(
                "Logging",
                (
                    WebField("Log directory", "logging.log_dir"),
                    WebField(
                        "Level",
                        "logging.level",
                        "select",
                        ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
                    ),
                    WebField("Retention (days)", "logging.retention_days"),
                ),
            ),
            WebSection(
                "Web",
                (
                    WebField("Bind host", "web.host"),
                    WebField("Bind port", "web.port"),
                ),
            ),
        ),
    ),
)


__all__ = ["SECTIONS", "WebField", "WebSection"]

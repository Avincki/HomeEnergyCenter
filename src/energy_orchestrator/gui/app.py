"""tkinter config editor for ``config.yaml``.

Single window, four tabs (Devices / Decision / System / Validate). Every
field is a string-backed entry; we coerce types on save through Pydantic.
Save flow: form dict -> ``form_to_config`` -> ``save_with_backup``.

Connection-test buttons run ``health_check`` on a worker thread and post
results back to the tk main thread via ``root.after``.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk

from energy_orchestrator.config.loader import ConfigError, load_config
from energy_orchestrator.config.models import (
    AppConfig,
    CarChargerConfig,
    DeviceConfig,
    EtrelInchConfig,
    LargeSolarConfig,
    P1MeterConfig,
    SmallSolarConfig,
    SolarEdgeConfig,
    SonnenBatterieConfig,
)
from energy_orchestrator.gui.binding import (
    AppConfigForm,
    FormErrors,
    config_to_form,
    form_to_config,
    save_with_backup,
)
from energy_orchestrator.gui.probe import ProbeResult, probe_device

# ----- field definitions ------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """One labelled input on a tab. ``key`` is the dotted form path."""

    label: str
    key: str
    kind: str = "entry"  # entry | password | combobox | checkbox
    choices: tuple[str, ...] = ()
    hint: str = ""


# Devices tab: grouped by device.
_SONNEN_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("Host", "sonnen.host"),
    FieldSpec("Port", "sonnen.port"),
    FieldSpec("API version", "sonnen.api_version", "combobox", ("v1", "v2")),
    FieldSpec("Auth token", "sonnen.auth_token", "password", hint="required when api_version=v2"),
    FieldSpec("Capacity (kWh)", "sonnen.capacity_kwh"),
    FieldSpec("Timeout (s)", "sonnen.timeout_s"),
    FieldSpec("Retry count", "sonnen.retry_count"),
)
_CAR_CHARGER_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("Host", "homewizard.car_charger.host"),
    FieldSpec("Port", "homewizard.car_charger.port"),
    FieldSpec("Charging threshold (W)", "homewizard.car_charger.charging_threshold_w"),
    FieldSpec("Timeout (s)", "homewizard.car_charger.timeout_s"),
    FieldSpec("Retry count", "homewizard.car_charger.retry_count"),
)
_P1_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("Host", "homewizard.p1_meter.host"),
    FieldSpec("Port", "homewizard.p1_meter.port"),
    FieldSpec("Timeout (s)", "homewizard.p1_meter.timeout_s"),
    FieldSpec("Retry count", "homewizard.p1_meter.retry_count"),
)
_SMALL_SOLAR_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("Host", "homewizard.small_solar.host"),
    FieldSpec("Port", "homewizard.small_solar.port"),
    FieldSpec("Peak (W)", "homewizard.small_solar.peak_w"),
    FieldSpec("Timeout (s)", "homewizard.small_solar.timeout_s"),
    FieldSpec("Retry count", "homewizard.small_solar.retry_count"),
)
_LARGE_SOLAR_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("Host", "homewizard.large_solar.host", hint="leave blank to disable"),
    FieldSpec("Port", "homewizard.large_solar.port"),
    FieldSpec("Peak (W)", "homewizard.large_solar.peak_w"),
    FieldSpec("Timeout (s)", "homewizard.large_solar.timeout_s"),
    FieldSpec("Retry count", "homewizard.large_solar.retry_count"),
)
_SOLAREDGE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("Host", "solaredge.host"),
    FieldSpec("Modbus port", "solaredge.modbus_port"),
    FieldSpec("Unit ID", "solaredge.unit_id"),
    FieldSpec("Timeout (s)", "solaredge.timeout_s"),
    FieldSpec("Retry count", "solaredge.retry_count"),
)
_ETREL_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("Host", "etrel.host", hint="leave blank to disable"),
    FieldSpec("Modbus port", "etrel.modbus_port"),
    FieldSpec("Unit ID", "etrel.unit_id"),
    FieldSpec("Timeout (s)", "etrel.timeout_s"),
    FieldSpec("Retry count", "etrel.retry_count"),
)

# Decision tab: thresholds + pricing + safety.
_BATTERY_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("Low SoC threshold (%)", "decision.battery_low_soc_pct"),
    FieldSpec("Full SoC threshold (%)", "decision.battery_full_soc_pct"),
    FieldSpec("Hysteresis (%)", "decision.hysteresis_pct"),
    FieldSpec("Forecast horizon (h)", "decision.forecast_horizon_h"),
)
_PRICES_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("Provider", "prices.provider", "combobox", ("entsoe", "tibber", "csv")),
    FieldSpec("API key", "prices.api_key", "password", hint="required for entsoe / tibber"),
    FieldSpec("Area", "prices.area", hint="2-letter code (BE/NL/DE/FR/AT/LU) or raw EIC"),
    FieldSpec("CSV path", "prices.csv_path", hint="only when provider=csv"),
    FieldSpec("Base URL", "prices.base_url", hint="optional — override entsoe REST endpoint"),
    FieldSpec("Injection factor", "prices.injection_factor"),
    FieldSpec("Injection offset (€/kWh)", "prices.injection_offset"),
)
_SAFETY_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("Dry-run (suppress SolarEdge writes)", "decision.dry_run", "checkbox"),
)

# System tab.
_OPS_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("Poll interval (s)", "poll_interval_s"),
    FieldSpec("Decision interval (s)", "decision_interval_s"),
    FieldSpec("SQLite path", "storage.sqlite_path"),
    FieldSpec("History retention (days)", "storage.history_retention_days"),
)
_LOGGING_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("Log directory", "logging.log_dir"),
    FieldSpec(
        "Level",
        "logging.level",
        "combobox",
        ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    ),
    FieldSpec("Retention (days)", "logging.retention_days"),
)
_WEB_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("Bind host", "web.host"),
    FieldSpec("Bind port", "web.port"),
)


# ----- main app ---------------------------------------------------------------


class ConfigEditorApp:
    """tkinter front-end for editing ``config.yaml``.

    Construct, then call :meth:`run` to enter the tk main loop. Tests can
    instantiate without ``run()`` to inspect form state directly.
    """

    def __init__(
        self,
        config_path: Path,
        *,
        initial: AppConfig | None = None,
        root: tk.Tk | None = None,
    ) -> None:
        self.config_path = config_path
        self._vars: dict[str, tk.StringVar | tk.BooleanVar] = {}
        self._error_labels: dict[str, ttk.Label] = {}
        self._probe_status: dict[str, tk.StringVar] = {}

        self.root = root if root is not None else tk.Tk()
        self.root.title(f"Energy Orchestrator — {config_path.name}")
        self.root.geometry("760x640")

        self._status_var = tk.StringVar(value=f"loaded {config_path}")

        self._build_layout()
        if initial is not None:
            self._populate(config_to_form(initial))

    # ----- build --------------------------------------------------------------

    def _build_layout(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        notebook.add(self._build_devices_tab(notebook), text="Devices")
        notebook.add(self._build_decision_tab(notebook), text="Decision")
        notebook.add(self._build_system_tab(notebook), text="System")
        notebook.add(self._build_validate_tab(notebook), text="Validate & Save")

        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill="x", padx=8, pady=8)
        ttk.Button(toolbar, text="Reload from disk", command=self._on_reload).pack(side="left")
        ttk.Button(toolbar, text="Save", command=self._on_save).pack(side="left", padx=(8, 0))
        ttk.Label(toolbar, textvariable=self._status_var, anchor="w").pack(
            side="left", fill="x", expand=True, padx=(16, 0)
        )

    def _build_devices_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        tab = ttk.Frame(parent)
        self._add_device_section(tab, "sonnenBatterie", _SONNEN_FIELDS, sonnen_probe_factory)
        self._add_device_section(
            tab, "HomeWizard — Car Charger", _CAR_CHARGER_FIELDS, car_charger_probe_factory
        )
        self._add_device_section(tab, "HomeWizard — P1 Meter", _P1_FIELDS, p1_probe_factory)
        self._add_device_section(
            tab, "HomeWizard — Small Solar", _SMALL_SOLAR_FIELDS, small_solar_probe_factory
        )
        self._add_device_section(
            tab,
            "HomeWizard — Large Solar (optional)",
            _LARGE_SOLAR_FIELDS,
            large_solar_probe_factory,
        )
        self._add_device_section(tab, "SolarEdge", _SOLAREDGE_FIELDS, solaredge_probe_factory)
        self._add_device_section(
            tab, "Etrel INCH (optional)", _ETREL_FIELDS, etrel_probe_factory
        )
        return tab

    def _build_decision_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        tab = ttk.Frame(parent)
        self._add_section(tab, "Battery thresholds", _BATTERY_FIELDS)
        self._add_section(tab, "Pricing", _PRICES_FIELDS)
        self._add_section(tab, "Safety", _SAFETY_FIELDS)
        return tab

    def _build_system_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        tab = ttk.Frame(parent)
        self._add_section(tab, "Operational", _OPS_FIELDS)
        self._add_section(tab, "Logging", _LOGGING_FIELDS)
        self._add_section(tab, "Web", _WEB_FIELDS)
        return tab

    def _build_validate_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        tab = ttk.Frame(parent)
        ttk.Label(
            tab,
            text=(
                "Validate runs Pydantic against the current form values without "
                "saving. Save also validates first; either route shows per-field "
                "errors next to the offending input on the previous tabs."
            ),
            wraplength=700,
            justify="left",
        ).pack(anchor="w", padx=12, pady=12)
        ttk.Button(tab, text="Validate without saving", command=self._on_validate).pack(
            anchor="w", padx=12, pady=4
        )
        ttk.Button(tab, text="Save (with .bak backup)", command=self._on_save).pack(
            anchor="w", padx=12, pady=4
        )
        ttk.Label(
            tab,
            text=f"Editing file: {self.config_path}",
            foreground="#6b7280",
        ).pack(anchor="w", padx=12, pady=(20, 4))
        return tab

    # ----- section + field helpers --------------------------------------------

    def _add_section(
        self,
        parent: ttk.Frame,
        title: str,
        fields: Iterable[FieldSpec],
    ) -> ttk.LabelFrame:
        section = ttk.LabelFrame(parent, text=title)
        section.pack(fill="x", padx=8, pady=6)
        for row, spec in enumerate(fields):
            self._add_field(section, spec, row)
        return section

    def _add_device_section(
        self,
        parent: ttk.Frame,
        title: str,
        fields: Iterable[FieldSpec],
        probe_factory: Callable[[AppConfigForm], DeviceConfig | str],
    ) -> None:
        section = self._add_section(parent, title, fields)
        # Test row at the bottom of the section.
        n = len(self._field_keys(fields))
        status_var = tk.StringVar(value="")
        self._probe_status[title] = status_var
        ttk.Button(
            section,
            text="Test connection",
            command=lambda: self._on_probe(title, probe_factory),
        ).grid(row=n, column=0, sticky="w", padx=8, pady=(4, 8))
        ttk.Label(section, textvariable=status_var, foreground="#6b7280").grid(
            row=n, column=1, columnspan=2, sticky="w", padx=8, pady=(4, 8)
        )

    def _add_field(self, parent: ttk.LabelFrame, spec: FieldSpec, row: int) -> None:
        ttk.Label(parent, text=spec.label).grid(row=row, column=0, sticky="w", padx=8, pady=2)
        widget = self._create_widget(parent, spec)
        widget.grid(row=row, column=1, sticky="ew", padx=8, pady=2)
        parent.grid_columnconfigure(1, weight=1)

        hint_text = spec.hint
        err_label = ttk.Label(parent, text="", foreground="#dc2626", anchor="w")
        err_label.grid(row=row, column=2, sticky="w", padx=8, pady=2)
        self._error_labels[spec.key] = err_label
        if hint_text:
            ttk.Label(parent, text=hint_text, foreground="#6b7280", anchor="w").grid(
                row=row, column=3, sticky="w", padx=8, pady=2
            )

    def _create_widget(self, parent: ttk.LabelFrame, spec: FieldSpec) -> tk.Widget:
        if spec.kind == "checkbox":
            bvar = tk.BooleanVar(value=False)
            self._vars[spec.key] = bvar
            return ttk.Checkbutton(parent, variable=bvar)
        svar = tk.StringVar(value="")
        self._vars[spec.key] = svar
        if spec.kind == "combobox":
            return ttk.Combobox(
                parent, textvariable=svar, values=list(spec.choices), state="readonly"
            )
        show = "*" if spec.kind == "password" else ""
        return ttk.Entry(parent, textvariable=svar, show=show)

    @staticmethod
    def _field_keys(fields: Iterable[FieldSpec]) -> list[str]:
        return [f.key for f in fields]

    # ----- form <-> tk vars ---------------------------------------------------

    def _populate(self, form: AppConfigForm) -> None:
        for key, value in form.items():
            var = self._vars.get(key)
            if var is None:
                continue
            if isinstance(var, tk.BooleanVar):
                var.set(value.lower() in {"true", "1", "yes"})
            else:
                var.set(value)

    def current_form(self) -> AppConfigForm:
        out: AppConfigForm = {}
        for key, var in self._vars.items():
            if isinstance(var, tk.BooleanVar):
                out[key] = "true" if var.get() else "false"
            else:
                out[key] = var.get()
        return out

    def _clear_errors(self) -> None:
        for label in self._error_labels.values():
            label.configure(text="")

    def _show_errors(self, errors: FormErrors) -> None:
        self._clear_errors()
        for key, msg in errors.items():
            label = self._error_labels.get(key)
            if label is None:
                # Cross-field errors land on the model itself; surface in status.
                self._status_var.set(f"validation: {key}: {msg}")
                continue
            label.configure(text=msg)

    # ----- handlers -----------------------------------------------------------

    def _on_validate(self) -> AppConfig | None:
        config, errors = form_to_config(self.current_form())
        if errors:
            self._show_errors(errors)
            self._status_var.set(f"{len(errors)} validation error(s) — see red text")
            return None
        self._clear_errors()
        self._status_var.set("config is valid")
        return config

    def _on_save(self) -> None:
        config = self._on_validate()
        if config is None:
            return
        try:
            save_with_backup(config, self.config_path)
        except OSError as e:
            self._status_var.set(f"save failed: {e}")
            messagebox.showerror("Save failed", str(e))
            return
        self._status_var.set(f"saved to {self.config_path} (previous version kept as .bak)")

    def _on_reload(self) -> None:
        try:
            config = load_config(self.config_path)
        except ConfigError as e:
            self._status_var.set(f"reload failed: {e}")
            messagebox.showerror("Reload failed", str(e))
            return
        self._populate(config_to_form(config))
        self._clear_errors()
        self._status_var.set(f"reloaded from {self.config_path}")

    def _on_probe(
        self,
        title: str,
        probe_factory: Callable[[AppConfigForm], DeviceConfig | str],
    ) -> None:
        status_var = self._probe_status[title]
        result = probe_factory(self.current_form())
        if isinstance(result, str):
            status_var.set(f"can't probe: {result}")
            return
        status_var.set("probing…")

        def on_done(probe: ProbeResult) -> None:
            # Marshal back to the tk main thread.
            self.root.after(0, lambda: self._show_probe_result(status_var, probe))

        probe_device(result, on_done)

    @staticmethod
    def _show_probe_result(status_var: tk.StringVar, probe: ProbeResult) -> None:
        prefix = "OK" if probe.ok else "FAIL"
        status_var.set(f"{prefix}: {probe.message}")

    # ----- entry --------------------------------------------------------------

    def run(self) -> None:
        self.root.mainloop()


# ----- probe-config factories -------------------------------------------------
#
# Each factory takes the live form dict, extracts the fields for one device
# section, and tries to build the matching pydantic model. Returns the config
# on success or a short string describing why the test can't run yet.


def _try_build(cls: type[DeviceConfig], values: dict[str, object]) -> DeviceConfig | str:
    try:
        return cls.model_validate(values)
    except Exception as e:  # surfaced verbatim to the user
        return f"invalid config: {e}"


def sonnen_probe_factory(form: AppConfigForm) -> DeviceConfig | str:
    return _try_build(
        SonnenBatterieConfig,
        {
            "host": form.get("sonnen.host", ""),
            "port": form.get("sonnen.port", "80"),
            "api_version": form.get("sonnen.api_version", "v2"),
            "auth_token": form.get("sonnen.auth_token") or None,
            "capacity_kwh": form.get("sonnen.capacity_kwh", "10"),
            "timeout_s": form.get("sonnen.timeout_s", "5"),
            "retry_count": form.get("sonnen.retry_count", "3"),
        },
    )


def car_charger_probe_factory(form: AppConfigForm) -> DeviceConfig | str:
    return _try_build(
        CarChargerConfig,
        {
            "host": form.get("homewizard.car_charger.host", ""),
            "port": form.get("homewizard.car_charger.port", "80"),
            "charging_threshold_w": form.get("homewizard.car_charger.charging_threshold_w", "500"),
            "timeout_s": form.get("homewizard.car_charger.timeout_s", "5"),
            "retry_count": form.get("homewizard.car_charger.retry_count", "3"),
        },
    )


def p1_probe_factory(form: AppConfigForm) -> DeviceConfig | str:
    return _try_build(
        P1MeterConfig,
        {
            "host": form.get("homewizard.p1_meter.host", ""),
            "port": form.get("homewizard.p1_meter.port", "80"),
            "timeout_s": form.get("homewizard.p1_meter.timeout_s", "5"),
            "retry_count": form.get("homewizard.p1_meter.retry_count", "3"),
        },
    )


def small_solar_probe_factory(form: AppConfigForm) -> DeviceConfig | str:
    return _try_build(
        SmallSolarConfig,
        {
            "host": form.get("homewizard.small_solar.host", ""),
            "port": form.get("homewizard.small_solar.port", "80"),
            "peak_w": form.get("homewizard.small_solar.peak_w", "2000"),
            "timeout_s": form.get("homewizard.small_solar.timeout_s", "5"),
            "retry_count": form.get("homewizard.small_solar.retry_count", "3"),
        },
    )


def large_solar_probe_factory(form: AppConfigForm) -> DeviceConfig | str:
    if not form.get("homewizard.large_solar.host", "").strip():
        return "host is empty — large solar is disabled"
    return _try_build(
        LargeSolarConfig,
        {
            "host": form.get("homewizard.large_solar.host", ""),
            "port": form.get("homewizard.large_solar.port", "80"),
            "peak_w": form.get("homewizard.large_solar.peak_w", "4000"),
            "timeout_s": form.get("homewizard.large_solar.timeout_s", "5"),
            "retry_count": form.get("homewizard.large_solar.retry_count", "3"),
        },
    )


def solaredge_probe_factory(form: AppConfigForm) -> DeviceConfig | str:
    return _try_build(
        SolarEdgeConfig,
        {
            "host": form.get("solaredge.host", ""),
            "modbus_port": form.get("solaredge.modbus_port", "1502"),
            "unit_id": form.get("solaredge.unit_id", "1"),
            "timeout_s": form.get("solaredge.timeout_s", "5"),
            "retry_count": form.get("solaredge.retry_count", "3"),
        },
    )


def etrel_probe_factory(form: AppConfigForm) -> DeviceConfig | str:
    if not form.get("etrel.host", "").strip():
        return "host is empty — Etrel charger is disabled"
    return _try_build(
        EtrelInchConfig,
        {
            "host": form.get("etrel.host", ""),
            "modbus_port": form.get("etrel.modbus_port", "502"),
            "unit_id": form.get("etrel.unit_id", "1"),
            "timeout_s": form.get("etrel.timeout_s", "5"),
            "retry_count": form.get("etrel.retry_count", "3"),
        },
    )


__all__ = ["ConfigEditorApp"]

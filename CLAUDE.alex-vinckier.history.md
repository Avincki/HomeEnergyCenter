# History — alex-vinckier

## 2026-04-30 — Phases 1-10 + entry-point cleanup; project goes from empty to a running web app

Built the Energy Orchestrator from a single PyCharm stub up through a live
FastAPI dashboard on `http://0.0.0.0:8000`. 205 tests passing, all quality
gates green (ruff / ruff-format / black / mypy --strict / pytest).

### Phases completed

- **Phase 1 — Project structure & dev tooling.** `pyproject.toml` (hatchling,
  Python ≥3.11, all runtime + dev deps), `.gitignore`, `.pre-commit-config.yaml`,
  `.github/workflows/ci.yml` (matrix on 3.11/3.12), `src/energy_orchestrator/`
  package skeleton with seven sub-packages (`config`, `devices`, `decision`,
  `data`, `web`, `monitoring`, `utils`), `git init`. Used **src layout**, not
  the literal `energy_orchestrator/src/...` from the diagram in
  CLAUDE.alex-vinckier.md, because the literal nesting breaks hatchling.
- **Phase 2 — Pydantic v2 config models.** All `frozen=True`, `extra=forbid`.
  `DeviceConfig` base + sonnen / HomeWizard (3 sub-devices) / SolarEdge /
  Prices / Decision (with cross-field hysteresis-band rule) / Storage /
  Logging / Web. `SecretStr` on `auth_token` and `api_key`. YAML loader with
  classified errors (`ConfigError` family). `config.example.yaml` shipped.
- **Phase 3 — Async SQLAlchemy 2.0 + Alembic.** Three tables (`readings`,
  `decisions`, `source_status`) per spec. `BaseRepository[T]` + per-entity
  repos + `UnitOfWork` (commit/rollback, async-CM). Alembic env reads DB URL
  from `EO_DB_URL`/`EO_SQLITE_PATH` env vars rather than parsing config.yaml,
  so migrations don't require valid device tokens. `alembic check` confirms
  zero drift between ORM and the initial migration.
- **Phase 4 — DeviceClient ABC + registry.** Generic over config type;
  `@register_device(ConfigType)` with **exact-type lookup** (config subclasses
  must register explicitly — pinned by test). `DeviceReading` frozen
  dataclass with quality validation. Error hierarchy: `DeviceError` →
  `Connection` (→ `Timeout`), `Protocol`, `Configuration`, `UnknownType`.
- **Phase 5 — SonnenClient.** aiohttp + tenacity. v1/v2 API switch with
  `Auth-Token` header. Retries on transient (5xx, timeout, refused);
  401/protocol errors don't retry. `_normalize` extracts the 5 known fields,
  returns `None` if `USOC` missing.
- **Phase 6 — HomeWizardClient.** Shared base + three thin subclasses
  (`CarChargerClient`, `P1MeterClient`, `SmallSolarClient`). All hit
  `/api/v1/data`, no auth, same shape.
- **Phase 7 — SolarEdgeClient.** pymodbus async TCP. Reads + writes
  `0xF001` with **read-back verification** on every write. ValueError on
  out-of-range *before* I/O. Connection torn down on Modbus error so next
  call reconnects. **No** ramping / rate-limiting / circuit-breaker
  (workplan mentioned them; spec doesn't, so deferred).
- **Phase 8 — Price providers.** New `prices/` module. `PricePoint`,
  `PriceProvider` ABC, `CsvPriceProvider`, `EntsoePriceProvider` (XML parse
  with **fill-forward** position convention, EUR/MWh→kWh, factor+offset for
  injection). Tibber raises `NotImplementedError`. `area` accepts 2-letter
  codes (BE/NL/DE/FR/AT/LU mapped to EICs) or raw EICs.
- **Phase 9 — Decision engine + 4 rules + override.** `TickContext` includes
  pre-computed `car_is_charging` and `battery_capacity_kwh`.
  `BatteryLowRule` does hysteresis based on **previous overall state**, not
  previous-rule (simpler; sticky once ON for any reason, releases at
  `low + hysteresis`). `NegativeWindowForecastRule` always claims (fallback
  to OFF if no negative window). Engine layers manual override on top;
  `state_changed` reflects *applied* state vs previous, but `rule_fired` /
  `reason` always describe the auto computation so the audit trail is honest.
- **Phase 10 — FastAPI web layer.** `create_app(config)` factory + lifespan
  (opens SQLite engine, runs `init_schema`, mounts static, registers
  routers). `OverrideController` (in-memory, auto-expires on read). Six
  JSON endpoints under `/api`: state, history, sources, health, prices,
  POST override. HTML routes `/` and `/debug` with Jinja2 templates,
  dark-theme CSS, no chart libraries yet. Secret redaction in the debug
  config view (test-pinned). **Tick loop deferred** — the dashboard renders
  but tiles are empty until the orchestrator runs.

### Entry-point cleanup (post Phase 10)

Started with `__main__.py` + `run.py` split; user pushed back saying one
`main.py` is friendlier. Consolidated into a single `main.py` at project
root, deleted `run.py` + `__main__.py`, removed the now-orphan
`[project.scripts]` entry from `pyproject.toml`. So `python -m
energy_orchestrator` and the installed `energy-orchestrator` console script
no longer work — `python main.py` (or PyCharm's run button) is the only
entry point.

### Notable decisions worth remembering

- **Dropbox path is fragile.** The project lives at
  `C:\Users\AlexVinckier\Dropbox (Personal)\Python\HomeEnergyCenter\...`.
  Dropbox can sync partial writes / lock files / conflict-copy `.pyc` on
  Windows. If weird import errors appear later, this is a likely cause.
  Either move the project off Dropbox or exclude `.venv/` from sync.
- **PyCharm interpreter ended up pointing at the system Python**, not the
  venv. We installed deps into the system Python as a workaround so user
  could see the dashboard today. Long-term they should fix the project
  interpreter to use `.venv/Scripts/python.exe`.
- **SQLite drops tzinfo on round-trip** even with `DateTime(timezone=True)`.
  Added `_utc_aware()` helper in `web/api.py` for the one place that does
  arithmetic on loaded timestamps. If we ever add more such places, consider
  a SQLAlchemy `TypeDecorator` to normalise on load.
- **`init_schema()` runs at app startup** (idempotent). Production should
  still use `alembic upgrade head` for first install. The two are consistent
  per `alembic check`; future schema changes must be a new revision.
- **`create_engine` now mkdirs the SQLite parent directory** — without it,
  fresh installs failed with "unable to open database file" because
  `data/orchestrator.db` had no `data/` parent. Fix in
  `src/energy_orchestrator/data/database.py`.
- **Ruff `TCH` group disabled** because we use `from __future__ import
  annotations` everywhere — the payoff of `flake8-type-checking` is gone
  when annotations are already lazy strings.
- **Ruff `SIM117` disabled** because combining `async with server` +
  `async with client` in tests hurts readability without correctness gain.
- **pymodbus mypy override** uses `follow_imports = "skip"` because the
  bundled stubs disagree with runtime (e.g. `slave=` is rejected by stub
  but accepted at runtime).

### Workplan / spec updates today

- **`WORKPLAN.md`**: total bumped to 13.0d, added **Phase 15 — Dashboard
  Day-Ahead Price Chart** (0.5d) covering both the day-ahead injection bar
  chart and the SoC + injection-price overlay with ON/OFF zone shading.
- **`CLAUDE.alex-vinckier.md`**: new "Dashboard Visualizations" section
  between Logging/Monitoring and Development Standards. Pins the
  **no-CDN, vendored chart library** rule (home-LAN deployment may not
  have outbound internet).
- **`CLAUDE.md`** at project root left alone — auto-generated by
  ClaudeContextGenerator and currently has stale `hsbGeoLibExt` content
  (looks like the generator wasn't re-run for this project). Worth
  regenerating when convenient.

### State at end-of-session

- `python main.py` starts uvicorn on `0.0.0.0:8000`.
- Dashboard at `/`, debug at `/debug`, OpenAPI at `/docs`. All return 200.
- Tiles are empty / "No decision recorded yet" — there's no tick loop
  populating data.
- Override form on `/debug` is functional (POSTs to `/api/override`).
- `config.yaml` is a copy of `config.example.yaml` with placeholder IPs;
  user needs to edit in their real device IPs and ENTSO-E API key before
  any meaningful data appears.
- 205 tests pass, full quality gate clean.
- 35 source files under `src/`, all type-checked under `mypy --strict`.

### Next session

The natural next chunk is the **orchestrator tick loop** — a background
asyncio task in the FastAPI lifespan that every `poll_interval_s` reads all
five devices, gathers prices into a cache, builds a `TickContext`, runs the
decision engine, persists `Reading` + `Decision` + `SourceStatus`, and
(when `decision.dry_run = false`) calls `solaredge.set_active_power_limit()`.
Once that lands, Phase 15 (price chart) becomes feasible because the
`/api/prices` endpoint will have something to return.

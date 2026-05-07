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

## 2026-05-01 — Tick loop + Phase 15 dashboard charts

Implemented the orchestrator tick loop and Phase 15 (dashboard charts) in
one go. 232 tests pass; ruff/ruff-format/black clean. Dashboard renders end
to end on a real uvicorn instance (curl 200 on `/`, `/static/dashboard.js`,
`/static/vendor/chart.umd.min.js`, `/api/prices`).

### What landed

- **`prices/cache.py`** — `PriceCache`, single-writer in-memory store of
  `PricePoint`s. `is_stale(now)` triggers refresh after one hour OR when
  the cache no longer covers `now` (handles overnight pauses cleanly).
- **`orchestrator.py`** — `TickLoop`. Builds all five clients via the
  existing registry, holds the `PriceProvider`, owns one `DecisionEngine`.
  `start()` schedules a background task; `stop()` cancels it and closes
  every resource. One `tick()` method does the real work and is also the
  unit-test entry point (it accepts `now=` for determinism). Per-device
  reads run in parallel via `asyncio.gather`. Per-source success/error is
  recorded against `SourceStatus`. The decision step is **skipped** if
  sonnen SoC isn't available — partial reading still persists. SolarEdge
  is actuated only when `decision.dry_run=False` AND `state_changed=True`.
- **Lifespan wiring** — `create_app(start_tick_loop=True)`. Tests pass
  `start_tick_loop=False` so the test fixture doesn't hammer non-existent
  device IPs every time the integration suite spins up the app.
- **`/api/prices`** now serves `{last_refresh, window_start, window_end,
  prices: [...]}` from the cache (today midnight UTC + 2 days). The old
  hard-coded "tick loop not running" note is gone.
- **Phase 15** — vendored `Chart.js v4.4.6` UMD min build (~200 KB) under
  `web/static/vendor/`, no CDN. Ships an inline minimal date adapter so
  the time-axis line chart works without `chartjs-adapter-date-fns`. Two
  charts render from `/api/prices` and `/api/history?h=24`:
    - day-ahead injection-price bar chart (current hour outlined accent,
      negative-price hours filled red);
    - SoC + injection-price overlay line chart (SoC on left axis, dashed
      amber price line on right) with ON/OFF zone shading drawn by a
      custom Chart.js plugin that reads decision timestamps.
- **Tests** — `tests/unit/test_prices_cache.py` (5), `test_orchestrator.py`
  (7) using fakes that replace `loop._sonnen` etc. after construction;
  integration tests now check the chart canvases + vendored JS path +
  cache-populated `/api/prices`.

### Notable decisions

- **`# noqa` for `BLE001` removed.** That code is from `flake8-blind-except`
  which we don't enable; the bare comments were noise. Replaced with plain
  `except Exception:` and a one-line comment explaining the intent.
- **SolarEdge constructor narrowing.** `TickLoop.__init__` does an explicit
  `isinstance(solaredge, SolarEdgeClient)` check after registry lookup so
  the field is typed as `SolarEdgeClient` (not `DeviceClient[Any]`) for the
  one place that calls `set_active_power_limit`. Keeps mypy --strict happy
  without `# type: ignore` on the actuation call.
- **Small-solar sign convention is `abs()`.** HomeWizard kWh-meter wiring
  direction is unknown at design time; magnitude is the production rate.
  One-line fix if the user later confirms direction.
- **Price cache window = today 00:00 UTC + 2 days.** Aligns with ENTSO-E's
  publishing rhythm (today + tomorrow once tomorrow's prices land) and the
  forecast horizon used by rule 4.
- **Inline Chart.js date adapter.** `chartjs-adapter-date-fns` would have
  meant a second vendored file; we only need hour-level formatting, so a
  20-line adapter inside `dashboard.js` is enough. Smaller surface, no
  extra static asset.

### Pre-existing mypy warnings — NOT my regression

`mypy --strict src tests` reports **10 errors in 3 files I did not touch**:
- `test_config_models.py:139,140,141,etc.` — "unused `type: ignore`"
- `test_devices_homewizard.py:125,137,…` — variant configs vs concrete
  client `__init__` arg types
- `test_devices_solaredge.py:62` — MagicMock helper returns Any

`mypy --strict` runs cleanly on the 16 files I touched
(`src/energy_orchestrator/orchestrator.py`, `prices/`, `web/`,
`tests/unit/test_orchestrator.py`, `tests/unit/test_prices_cache.py`,
`tests/integration/test_web_app.py`). Yesterday's history claims gates were
green; either the mypy version drifted or the prior run wasn't full-strict.
Worth a separate cleanup pass — out of scope for this session.

### State at end-of-session

- Tick loop runs every 30s by default (config.poll_interval_s). It reads
  all five devices in parallel, refreshes prices when stale, persists a
  partial Reading on missing data, runs the engine when SoC is present,
  and would actuate SolarEdge if `decision.dry_run=False` (currently
  `True` in `config.example.yaml`).
- Dashboard at `/` shows the tile grid + day-ahead bar chart + 24h SoC
  overlay + recent decisions table.
- Until the user fills in real device IPs and an ENTSO-E token, the tick
  loop will record errors per source but the app stays up; charts show
  empty-state placeholder text.
- Vendored `chart.umd.min.js` is in `src/energy_orchestrator/web/static/
  vendor/` and gets served at `/static/vendor/chart.umd.min.js`.

### Next session

Phase 11 (config GUI, 2d) is the largest remaining item. Phase 12
(structlog) and Phase 13 (test coverage push) are smaller and would
strengthen the foundation. Also: the 10 pre-existing mypy errors should
be triaged before they accumulate.

## 2026-05-01 (later) — Chart merge + code review pass

Same day, follow-on work after the user smoke-tested the dashboard.
**232 → 220 tests pass, all four gates green** (ruff / ruff-format / black
/ mypy --strict — including the 10 pre-existing errors that were carried
forward).

### Two charts → one combined chart

Per user request, the day-ahead bar chart and the SoC overlay chart got
merged into a single `<canvas id="mainChart">` on the dashboard:
- **Bars** (left axis €/kWh): hourly day-ahead injection prices, red on
  negative hours, accent outline on the current hour, muted otherwise.
- **Line** (right axis 0–100 %): battery SoC over the last 24 h, smoothed.
- Shared time x-axis. Dual-axis tooltips report the right unit per series.

Dropped from the old design:
- **ON/OFF zone shading** (the custom Chart.js plugin from yesterday).
  The user didn't ask for it in the merged view; one less moving part.
- **Dashed amber price line** that used to overlay the SoC chart — now
  redundant since prices are bars.

Files touched: `templates/dashboard.html` (one section instead of two),
`static/dashboard.js` (single `renderCombined`, plus the inline date
adapter kept for the time scale), `static/style.css` (chart-card-tall is
the only height variant now), `tests/integration/test_web_app.py`
(asserts the merged canvas id, not the old two).

### Stray-byte glitch in `views.py` — Dropbox sync

While re-running gates, pytest collection failed with `SyntaxError` on
`src/energy_orchestrator/web/views.py:1`: a stray leading `2` had appeared
in front of the module docstring. Cause is the "Dropbox path is fragile"
issue called out in the 2026-04-30 entry. Removed the byte. Worth keeping
an eye out for similar corruptions on this filesystem.

### Code-review pass (user asked for "review the complete code")

Issues found and fixed:

1. **Duplicate helper.** `orchestrator.py` had its own
   `_current_hour_price` that was identical to
   `decision/forecast.py:get_current_hour_price`. Deleted, imported the
   existing one. -10 LOC, one less drift risk.
2. **Hardcoded source-name list (×2).** Both `web/api.py:get_health` and
   `web/views.py:debug_board` enumerated `[SourceName.SONNEN.value, ...]`
   by hand. Replaced with `for source in SourceName:` so adding a new
   source automatically picks up the panel.
3. **Pre-existing mypy errors (10) — finally cleaned up.** Yesterday's
   note flagged that `mypy --strict` was failing in three test files I
   hadn't touched. Fixed in this pass:
   - `test_config_models.py`: 3 of the 6 `# type: ignore` comments were
     genuinely unused (Pydantic 2's mypy plugin now accepts dict-coercion
     args without ignore for nested-model fields). Removed those three.
     The other three (frozen-write, missing required field, extra field)
     **are** load-bearing — restored.
   - `test_devices_homewizard.py:_make_config`: generified with a
     `TypeVar("HwConfigT", bound=HomeWizardDeviceConfig)`. Return type
     now tracks input class, so `CarChargerClient(_make_config(CarChargerConfig, …))`
     type-checks.
   - `test_devices_solaredge.py:_patch_modbus`: wrapped the return in
     `cast(MagicMock, instance)` — mypy can't infer that
     `mock_cls.return_value` is a `MagicMock`.

Considered and **rejected** during the review:
- Splitting `TickLoop` (~280 LOC) into smaller classes — current size is
  fine, premature.
- Parallelising `_close_resources` with `asyncio.gather` — serial closes
  are fast enough, not on a hot path.
- Rewriting `_classify_source_status` for clarity — dense but correct,
  pinned by tests.
- An `EO_DISABLE_TICK_LOOP` env var — the existing `start_tick_loop=`
  kwarg is the cleaner shape (one entry point, no env-var ladder).

### State at end-of-session

- Single combined dashboard chart, bars + line, dual axis. Renders
  correctly on a real uvicorn boot; verified `200` on `/`,
  `/static/dashboard.js`, `/static/vendor/chart.umd.min.js`,
  `/api/prices`.
- All four quality gates clean (ruff / ruff-format / black / mypy --strict
  / 220 tests).
- The user is currently away from home; tick loop will spam device-read
  errors against the unreachable LAN IPs. To populate the price chart
  while away, options documented in the chat: ENTSO-E token (slow signup,
  real prices) or CSV provider (immediate, hand-rolled file).

## 2026-05-01 (later still) — Phase 11: tkinter config editor

Phase 11 delivered as a pragmatic MVP, not the aspirational spec. **240
tests pass** (220 -> 240, +20 binding/probe tests), all four gates green
across 65 source files.

### What shipped

- `src/energy_orchestrator/gui/binding.py` — **pure** form-binding layer.
  Flat dotted-key dicts in/out (`"sonnen.host" -> "192.168.1.50"`). No
  tkinter imports, so all logic is unit-testable on a headless runner.
  Handles SecretStr unwrap, Path -> POSIX, enum -> .value, empty-string
  -> None for optional fields. Atomic YAML save with one `.bak` slot
  via `os.replace`.
- `src/energy_orchestrator/gui/probe.py` — async-to-tk bridge. Each
  "Test connection" button spawns a daemon thread, runs `health_check()`
  on a private event loop, posts a `ProbeResult` back to a callback
  (which the GUI marshals to the tk main thread via `root.after(0, …)`).
- `src/energy_orchestrator/gui/app.py` — `ConfigEditorApp` class with 4
  ttk.Notebook tabs (Devices / Decision / System / Validate & Save), 45
  form fields, per-device Test buttons, per-field error labels, status
  bar. Field definitions live as module-level `FieldSpec` tuples — easy
  to add/remove without touching the layout code.
- `gui.py` at project root — entry script mirroring the `main.py` shape.
  `python gui.py` (or `EO_CONFIG=foo.yaml python gui.py`) opens the
  editor on the chosen file. Opens cleanly even when the existing config
  fails Pydantic validation (so the user can fix it).
- Tests: `tests/unit/test_gui_binding.py` (16 tests) and
  `tests/unit/test_gui_probe.py` (4 tests).

### What was deliberately *not* done vs. CLAUDE.alex-vinckier.md spec

- **Encryption at rest** — the YAML file is the canonical source; encrypting
  it would break `git diff` and the existing `load_config` loader.
- **mDNS / SSDP auto-discovery** — separate concern, unrelated to config
  editing. Worth a follow-up phase if device IPs become a frequent edit.
- **Capability detection / firmware compatibility checks** — no APIs on
  these devices for that. Premature.
- **Multi-version rollback / config history** — one `.bak` slot is enough
  for the "I just broke it, undo" case. Anything heavier should use git.
- **Real-time validation as you type** — UX is identical with on-save
  validation that surfaces per-field errors next to the offending input
  (red label appears on Save click, clears on next valid Save).
- **Dry-run simulation with historical data** — huge separate feature,
  out of scope for "edit the config file".

Net: scope ≈ 30 % of the aspirational spec, but covers 100 % of what's
actually needed to edit `config.yaml` from a desktop instead of a text
editor.

### Notable design choices

- **Form values are all strings.** Tkinter `StringVar`s work uniformly;
  type coercion (`"30" -> 30.0`, `"true" -> True`) happens once at the
  Pydantic boundary in `form_to_config`. Avoids a thicket of
  `IntVar`/`DoubleVar`/`BooleanVar` plus per-widget conversion logic.
- **Booleans use BooleanVar, not StringVar.** Checkbutton needs a real
  bool. `current_form()` serialises it to `"true"`/`"false"` so the form
  dict stays uniformly stringy. Pydantic accepts both forms.
- **Probe factories are tiny pure functions** (`sonnen_probe_factory(form)
  -> DeviceConfig | str`). Easy to test, easy to add a sixth device.
- **45 fields** — that's every editable field on the AppConfig surface,
  not just the headline ones. Means a user can rebind the web port,
  tweak `forecast_horizon_h`, or change SQLite location without touching
  the YAML.

### State at end-of-session

- `python gui.py` opens the editor; saves go to `config.yaml`, previous
  version preserved as `config.yaml.bak`.
- Probe buttons fire async health-checks against the configured device
  IPs; while away from home all five will fail, but the GUI handles that
  gracefully (red status text, GUI stays responsive).
- All four gates clean: ruff / ruff-format / black / mypy --strict / 240
  pytest.

### Next session

Phase 12 (structlog) is now the smallest remaining item (1d) and the
most leverage per day for ops visibility. Phase 13 (test coverage push)
and Phase 14 (mkdocs) round out the workplan. The 4 phases left are
all relatively self-contained — no further dependencies between them.

## 2026-05-01 (yet later) — Phase 12: structlog wired in

Phase 12 done as a tight MVP. **247 tests pass** (240 -> 247, +7 logging
tests); all four gates green across 67 source files. Real uvicorn boot
verified — its own access + error logs land as JSON in
`logs/energy_orchestrator.log`.

### What shipped

- `src/energy_orchestrator/monitoring/logging_config.py` — single
  `configure_logging(config)` entry point. Does three things:
  1. Configures `structlog` to feed events into stdlib via
     `structlog.stdlib.LoggerFactory` (so `structlog.get_logger` and
     `logging.getLogger` produce coherent output).
  2. Installs a rotating JSON file handler under
     `config.logging.log_dir/energy_orchestrator.log` (10 MiB rotation,
     retention_days backups) plus a console handler on stderr using the
     `ConsoleRenderer`.
  3. Tags its handlers with a private attribute and removes only those
     on re-config — pytest's caplog and any other foreign handlers
     survive.
- `main.py` calls `configure_logging` BEFORE `uvicorn.run`, then passes
  `log_config=None` so uvicorn hooks into our root logger instead of
  installing its own.
- `web/app.py` lifespan calls it idempotently — covers direct factory
  paths used by tests and a future gui-launched server.
- `orchestrator.py` switched from `logging.getLogger` to
  `structlog.stdlib.get_logger`. Old `%s`-style format args replaced
  with kwargs (`logger.exception("device read unexpected error",
  source=client.source_name.value)`). `tick()` wraps its body in
  `structlog.contextvars.bound_contextvars(tick_at=…)` so every nested
  log line in the same tick carries the tick timestamp. Added an
  `info("decision state changed", ...)` event for state flips.
- `gui/app.py` had an unused `logger`; deleted along with the import.
- 7 new tests (`test_monitoring_logging.py`): file creation, ISO
  timestamp + level shape, contextvar binding, stdlib pass-through,
  idempotency, foreign-handler safety, level threshold filtering.

### Deferred from spec

- **Real-time log viewer UI** — separate skill. Would need an SSE/WS
  endpoint that tails the JSON file, plus a client page with filters.
- **Prometheus metrics integration** — different concern from logging;
  belongs in its own phase if/when ops tooling demands it.
- **Alert escalation, email/webhook notifications** — integration glue.
- **Per-component log levels** — single global level + structured
  fields lets you grep instead. Add per-component override only when
  there's a concrete need.

### Notable design choices

- **structlog feeds stdlib, not the other way around.** `LoggerFactory`
  + `ProcessorFormatter` is the structlog-recommended pattern when you
  also want third-party stdlib loggers (uvicorn, sqlalchemy) to format
  consistently. Verified: uvicorn access logs come out as JSON.
- **Idempotent reconfigure via handler tagging.** `setattr(handler,
  "_energy_orchestrator_handler", True)` lets the function strip *only*
  its own handlers on reinstall, not ones added by pytest's `caplog`
  fixture or other consumers.
- **Console renderer with `colors=False`** — ANSI sequences look ugly
  in PowerShell on Windows and journald, and the file handler is the
  primary read-path anyway.
- **bound_contextvars over .bind() returning a logger.** Using the
  context-manager form means helpers called from `tick()` automatically
  inherit the binding without having to thread a logger object down.

### Stray-byte glitch round 3 (Dropbox)

System reminder flagged a corruption at the top of `src/energy_orches
trator/web/views.py`: a `("yes" "")` snippet had appeared before the
docstring, which would have broken every test on collection. By the
time I ran `git diff` after fixing it, the file matched HEAD again —
either the Edit + clean state collapsed back to no-diff, or the
notification reflected a transient state. Either way: this is the
third time something has corrupted bytes near the top of `views.py`
(2026-05-01 chart merge, code review, today). Worth investigating
whether Dropbox's text-merge ever runs on .py files, and considering
moving the project off Dropbox.

### State at end-of-session

- All logs (orchestrator, FastAPI, uvicorn, sqlalchemy) flow through
  the same JSON pipeline.
- File at `logs/energy_orchestrator.log`; rotates at 10 MiB.
- All four quality gates clean.

### Next session

Phase 13 (comprehensive test suite, 1.5d) and Phase 14 (mkdocs, 0.5d)
remain. Phase 13 has the highest payoff if the user expects to extend
this in production — push coverage to >90%, add hypothesis property
tests for the rule engine, contract tests for the device clients.
Phase 14 is small but valuable for handoff.

## 2026-05-02 — Web config editor (/config page)

User asked what the "API" link in the navbar pointed at (FastAPI's
auto-generated `/docs` Swagger UI), then asked what it would take to
add a tab to edit device IPs / API keys. Picked option 2a from the
discussion: a `/config` page that writes `config.yaml` and prompts
for restart. Personal/iterative use, no auth needed. Shipped end-to-end.

### What landed

- `src/energy_orchestrator/web/config_form.py` — `WebField` /
  `WebSection` dataclasses + a `SECTIONS` constant that mirrors the
  tkinter editor's grouping (Devices / Decision / System). Pure data,
  no tkinter import — tkinter would otherwise have come in transitively
  if I'd reused `gui/app.py`'s `FieldSpec` list. Small duplication is
  the cheaper trade.
- `src/energy_orchestrator/web/views.py` — added `GET /config` (renders
  the form prefilled from the live `AppConfig` via
  `gui.binding.config_to_form`) and `POST /config` (form -> dotted-key
  dict -> `form_to_config` -> `save_with_backup`). Field-keyed
  validation errors render inline in red next to the offending input;
  successful save shows a green banner with the path and a
  "restart required" reminder.
- `src/energy_orchestrator/web/templates/config.html` — server-rendered
  HTML form, no JS. Each section is a `<fieldset>` with a `<legend>`;
  the three input kinds are text / password / select / checkbox.
  Pre-fills password fields with the current secret value (personal-use
  localhost; not a leak risk in this deployment).
- `src/energy_orchestrator/web/templates/base.html` — added "Config"
  to the navbar between Debug and API.
- `src/energy_orchestrator/web/static/style.css` — `.config-form`,
  `.config-row` (3-column grid: label / input / meta), `.banner-success`
  / `.banner-error`. ~85 lines of CSS appended.
- `src/energy_orchestrator/web/app.py` — `create_app()` now takes
  `config_path: str | Path | None = None`. When `config` is `None` it
  resolves the path from the kwarg or `EO_CONFIG` env var; when caller
  passes `config` directly (tests) the path stays None. Resolved path
  is stashed on `app.state.config_path`.
- `src/energy_orchestrator/web/dependencies.py` — `get_config_path`
  helper, returns `Path | None`.
- POST handler defends against the "no path bound" case with an error
  banner instead of writing to a default location.

### Reuse, not refactor

The biggest design choice was to **import `gui.binding`** rather than
re-implementing form -> config conversion. `binding.py`'s docstring
already declares "no tkinter imports" (line 1-10), and
`gui/__init__.py` only re-exports from binding — verified by reading.
So the web layer pulls `config_to_form` / `form_to_config` /
`save_with_backup` directly. One source of truth for SecretStr
unwrap, Path-as-POSIX serialisation, and `.bak` rotation. The tkinter
GUI and the web editor write identical YAML; users can flip between
them.

### Verified end-to-end

- `imports OK; sections: 3` — module loads.
- 38 tests pass (`tests/integration/test_web_app.py` +
  `tests/unit/test_gui_binding.py`).
- Live `GET /config` returns 200 with `<form method="post">` and a
  `name="sonnen.host"` input.
- `POST /config` with an empty `sonnen.host` returns 200 with
  `class="config-error"` rendered next to the field, no save.

### Deferred / not done

- **Hot reload.** Saves still require a process restart. The tick loop
  and device clients hold references to the loaded `AppConfig`; live
  reload would mean reinstantiating the device registry + price
  provider from app.state on a signal. Not a small refactor. Offered
  to schedule a follow-up agent in ~2 weeks; user response pending.
- **Authentication.** The dashboard is unauthenticated and the user
  confirmed personal use only — no auth added. If this ever escapes
  the home LAN, gate `/config` behind something before then.
- **Per-field "Test connection" buttons** like the tkinter editor.
  Out of scope; the `/debug` page already shows live source health.

### Known fragility (still)

The Dropbox stray-byte issue mentioned in three prior entries didn't
recur this session, but `views.py` is again the most-edited file in
the change set. If a syntax error appears at the top of the file on
the next pull, this is the cause.

### State at end-of-session

- Navbar: Dashboard / Debug / Config / API.
- `python main.py` -> open `http://localhost:8000/config` -> edit any
  field -> Save -> success banner with path -> restart `main.py` to
  apply. `config.yaml.bak` lives next to `config.yaml`.
- Commit `9f46b20` on `main`. Working tree clean apart from a
  screenshot the user dropped in for the question about the "API" tab
  (untracked, intentionally not committed).
- Tests still pass; no quality-gate run this session beyond the
  scoped pytest invocation.

### Next session

The hot-reload follow-up is the obvious next chunk if the iterative
config workflow proves annoying — config-change pubsub on app.state
that rebuilds the device registry + price provider, plus an "applied
at" indicator in the navbar. Otherwise: Phase 13 (test coverage) and
Phase 14 (mkdocs) are still the open workplan items.

## 2026-05-02 (later) — Web UI polish: 3-column config, navbar on /docs, /logs viewer

Same-day continuation. Three bite-sized UI features in one push:
config layout, embedded API docs, and a live log viewer.

### What landed

- **3-column config layout.** User asked the top-level Devices /
  Decision / System sections to sit side-by-side instead of stacked.
  Wrapped them in a `<div class="config-columns">` grid
  (`grid-template-columns: repeat(3, 1fr)`), `align-items: start` so
  the columns top-align even when content heights differ (Devices is
  much taller than Decision/System). Within each row, label/input/meta
  now stack vertically — the previous 220+280px three-column row
  layout didn't fit at ~330px column width. Bumped `main` max-width
  1100 -> 1400 so three columns aren't cramped on a typical monitor.
  Responsive: 3 cols → 2 cols at <1100px → 1 col at <720px.
- **Navbar on /docs.** The auto-generated FastAPI Swagger UI is its
  own template, so it didn't carry our nav. Disabled the default
  via `docs_url=None`, registered a custom `GET /docs` that renders
  a Jinja template extending `base.html`, with the swagger-ui CSS/JS
  loaded from the same `cdn.jsdelivr.net` URLs FastAPI defaults to.
  Same Swagger experience, now framed by our header + footer.
- **Live log viewer at /logs.** New nav tab between Config and API.
  Server side: `GET /api/logs/stream` is a `StreamingResponse` with
  `text/event-stream`. The async generator opens the rotating log
  file, seeks back ~16 KB on connect (so the page lands on context
  rather than empty), then `readline()`s in a loop with
  `asyncio.to_thread` to avoid blocking the event loop. Detects
  rotation by comparing `log_path.stat().st_size` vs `f.tell()` and
  reopens. Bails out on `request.is_disconnected()`. Client side:
  `EventSource` consumes the stream, parses JSON per line, renders as
  monospace rows with level coloring. Controls: level dropdown
  (debug+/info+/warning+/error+/critical, default info+), substring
  filter, follow toggle, pause/resume, clear. Auto-trims to 1000 rows
  in the DOM.

### Notable design choices

- **SSE, not WebSocket.** One-way server→client, auto-reconnect built
  into `EventSource`, no extra deps, plays nicely with the existing
  HTTP server. WebSockets would be overkill.
- **Three layout columns at the macro level + stacked rows at the
  micro level.** First instinct was to keep the existing tabular
  220+280px row layout and just put the sections side-by-side, but
  doing the math (1400px main / 3 cols / 1.5rem gap = ~440px each)
  it doesn't fit comfortably. Stacking label-above-input within each
  row reads cleaner at narrow column widths. The user signed off
  ("this is perfect").
- **Swagger UI still loads from CDN.** FastAPI's default `/docs` does
  too, so this isn't a regression — but per the project spec
  ("no CDN, home-LAN may not have outbound internet") it's a known
  deviation. Mentioned to the user; deferred vendoring
  swagger-ui-bundle.js + swagger-ui.css under `static/vendor/` until
  / if they go offline.
- **Initial `align-items: start` change to `.config-row` was based
  on misreading "3 columns" as the row's three sub-columns.** User
  clarified they meant the three top-level sections. Left the
  `align-items: start` change in (defensible improvement on its own,
  also irrelevant once rows became `flex-direction: column`).

### Stray hiccup — gh CLI vs browser auth

Pushed to `https://github.com/Avincki/HomeEnergyCenter` for the first
time today. `gh auth login` ran in the background but never completed
(user closed the browser flow), so `gh auth status` kept reporting not
logged in. Bypassed `gh` entirely — added the remote with
`git remote add origin <url>` and pushed via plain HTTPS. Git
Credential Manager (bundled with Git for Windows) handled the auth
prompt itself. Worth remembering: for first-push setups on Windows,
GCM is the path of least resistance; `gh` is only worth setting up
if you'll use the CLI for issues / PRs / releases.

### State at end-of-session

- Navbar: Dashboard / Debug / Config / Logs / API.
- Three commits since the last history entry: `f095a2f` (WORKPLAN
  tick Phase 15 + Phase 16), `fb1d46e` (this UI batch). Plus the
  history-update commit on top of those.
- All 22 web integration tests pass; ASGI httpx smoke test of the SSE
  endpoint hangs (httpx ASGITransport doesn't honor real-time
  streaming the way uvicorn does) but the structural correctness is
  visible in the code; user verifies in the browser.
- Repo lives at `https://github.com/Avincki/HomeEnergyCenter`,
  tracking origin/main.

### Next session

The hot-reload-on-config-save follow-up is still the highest-leverage
next chunk for the iterative config workflow. Beyond that: Phase 13
(test coverage push, including SSE generator tests now that there's
an async-streaming endpoint), Phase 14 (mkdocs), and possibly
vendoring Swagger UI assets if/when offline operation matters.

## 2026-05-04 — ENTSO-E endpoint migration + log viewer scoping fixes

User loaded the dashboard, added their ENTSO-E security token, and saw
an empty prices graph with `ENTSO-E request failed: Cannot connect to
host web-api.tso.entsoe.eu:443 ssl:default [getaddrinfo failed]`. Two
distinct things turned out to be wrong; this session fixed both plus a
log-viewer ergonomics issue surfaced along the way.

### What landed (commit `e61cb0d`)

- **ENTSO-E hostname migration.** `web-api.tso.entsoe.eu` no longer
  resolves anywhere — Google DNS (8.8.8.8), Cloudflare (1.1.1.1) and
  the OS resolver all return NODATA. Confirmed via `nslookup` and
  `Test-NetConnection`. The Transparency Platform retired that name in
  favor of `web-api.tp.entsoe.eu` (note `tp`, not `tso`). Verified the
  new name from two independent sources: a WebSearch survey and the
  `EnergieID/entsoe-py` library source
  (`URL = os.getenv("ENTSOE_ENDPOINT_URL") or "https://web-api.tp.entsoe.eu/api"`).
  Updated `_DEFAULT_BASE_URL` in `prices/entsoe_provider.py` and the
  module docstring.
- **`prices.base_url` config field as escape hatch.** Added an
  optional `str | None` field on `PricesConfig` (`min_length=1`,
  default `None`). `create_price_provider` threads it into
  `EntsoePriceProvider(config, base_url=...)`. Surfaced the field in
  both editors: the web `/config` page (Pricing section) and the
  tkinter `gui/app.py`. Next migration won't need a code edit.
- **Binding: `_OPTIONAL_STRING_FIELDS` set.** New third category in
  `gui/binding.py` beside `_SECRET_FIELDS` / `_PATH_FIELDS`. Empty
  form input maps to `None` so Pydantic's `min_length=1` doesn't
  reject the blank. Added two binding tests
  (`test_form_to_config_blank_base_url_becomes_none`,
  `test_form_to_config_preserves_custom_base_url`) and a factory test
  (`test_factory_threads_base_url_into_entsoe_provider`).
- **Logs page: scope to current server session.** Captured
  `app.state.session_started_at = datetime.now(UTC)` in the FastAPI
  lifespan. The SSE stream now replays from the *start* of the active
  log file and skips any line whose JSON `timestamp` is older than
  the session start (`_line_in_session` helper). Once we reach EOF
  the filter is a no-op since new lines necessarily belong to the
  current session. Replaced the previous 16 KB seek-back, which was
  cross-session leaky.
- **Logs page: render timestamps in browser local time.** Old code
  did `parsed.timestamp.replace('T', ' ').replace('Z', '')` — i.e.
  rendered UTC as if it were local. New `formatLocalTimestamp` parses
  via `new Date(...)` and formats `YYYY-MM-DD HH:MM:SS.mmm` from the
  local-timezone components.
- **Pre-existing dashboard x-axis lock.** `dashboard.js` had an
  uncommitted change locking the price chart to today's local
  00:00–24:00 window with a date title. Carried it into the same
  commit; called it out in the message rather than splitting.

### Notable diagnostic moments

- **The error wasn't on /logs.** First the user pasted the live log
  stream, which showed only sonnen / pymodbus warnings and no
  ENTSO-E line. The reason: `_refresh_prices_if_stale` catches
  `PriceError` and routes it through `_record_status_error`, which
  writes to the `source_status` table — visible on `/debug`, not in
  the rotating log file. So a fetch failure shows up on the Debug
  page only. Worth surfacing more loudly some day; for now it's
  documented context.
- **`/api/prices` returning 200 with `prices: []` is a red herring.**
  When the price cache is empty (because every fetch failed) the
  endpoint still returns 200 OK with `last_refresh: null` and an
  empty list. Status code is no signal of provider health.
- **`Test-NetConnection` takes a hostname only.** User pasted output
  with `web-api.tp.entsoe.eu/api` and `https://web-api.tp.entsoe.eu/api`,
  both resolved as literal hostnames (with slash and scheme), both
  failed. Once they re-ran with just the host + `-Port 443`, it
  succeeded against `20.23.37.29`. Worth remembering when triaging
  the next user.
- **`nslookup` returning a `Name:` line *without* an `Address:`
  line means NODATA.** That's how Windows nslookup expresses "the
  server replied but the host has no A/AAAA record." It's quiet
  enough that it reads like a successful lookup at first glance.

### State at end-of-session

- Branch `main` at `e61cb0d`, **1 commit ahead of origin**, not
  pushed yet. 13 files changed (+126 / −20).
- All 100 unit + integration tests in scope still pass
  (`tests/unit/test_prices_*`, `test_gui_binding.py`,
  `test_config_*` and `tests/integration/test_web_app.py`).
- Untracked files left intentionally: `keys.txt` (flagged to user as
  likely-secret — needs `.gitignore` confirmation),
  `config.yaml.bak` (runtime artifact from the editor), and a
  screenshot the user dropped during the troubleshooting.
- The price graph renders correctly again post-restart. User asked
  about the bar colors mid-session: grey = normal hour, cyan
  (with cyan border) = current local hour, red = negative injection
  price (`dashboard.js:57-64`).

### Next session

Same backlog as before plus a worthwhile small one: the orchestrator
swallows `PriceError` into `source_status` only, with no echo to the
log file. A single `logger.warning("price refresh failed", error=...)`
in `_refresh_prices_if_stale` would have made today's ENTSO-E
migration self-diagnosing from `/logs` alone. Otherwise: hot-reload
on config save (still highest leverage), Phase 13 (test coverage,
including SSE generator tests), Phase 14 (mkdocs), Swagger UI
vendoring if offline operation matters.

## 2026-05-04 (afternoon) — Forecast.Solar integration: provider, API, dashboard overlay, KPI tile

User's site has two PV arrays at 45° tilt — east 6.0 kWp (azimuth -90°) and
west 6.5 kWp (azimuth 90°), both in Gent. They wanted a daily-total kWh
estimate on the dashboard plus a power-curve overlay on the existing
price/SoC chart. Started from a "which web service?" question, ended
with a working overlay reading **25.4 kWh today / 21.5 kWh tomorrow**
on a live API call.

### Provider choice

Compared Forecast.Solar / Solcast / Open-Meteo. Picked **Forecast.Solar
free public tier** because it returns PV power directly (no irradiance
math), no key needed, and one URL per plane (`/estimate/{lat}/{lon}/
{decl}/{az}/{kwp}`). Rate limit is **rolling 60-min window**, ~12/hour
for the free tier — confirmed via `result.message.ratelimit` in the
response body (no HTTP header). 2 planes × 2 fetches/day = 4 calls,
well inside any threshold; the cache is set to 30 min anyway since
weather updates ~hourly and they ask not to poll < 15 min.

User initially wrote `6500 kWp` / `6000 kWp` — 6.5 MW utility scale —
caught and clarified to **6500 Wp / 6000 Wp** = 6.5 / 6.0 kWp.
Forecast.Solar's URL slot is in kWp so config stores `6.5` and `6.0`
verbatim.

### Architecture

New `solar/` sub-package mirroring `prices/`:

- `base.py` — `SolarPoint` (UTC tz-aware, watts), `SolarForecast`
  (summed series + per-plane breakdown + today/tomorrow Wh totals),
  `SolarProvider` ABC, `SolarError`/`SolarFetchError`/`SolarParseError`,
  and `sum_planes()` which buckets by timestamp so missing-plane
  hours just contribute 0 W.
- `cache.py` — `SolarCache` with the same single-writer pattern as
  `PriceCache`; `_MAX_AGE = 30 min`.
- `forecast_solar_provider.py` — `aiohttp.gather` over planes,
  `return_exceptions=True` so one failed plane doesn't poison the
  others (logs a warning, sums what's left). Surfaces remaining
  rate budget at debug level.

`config.SolarConfig` is **optional** (`AppConfig.solar: SolarConfig | None
= None`), so omitting the section disables the feature and existing
deployments aren't forced to take a dependency. `SolarPlaneConfig` has
`name`/`declination`/`azimuth`/`kwp` with the standard convention
(`-90 = east, 0 = south, 90 = west`); the model validates 1–4 planes
(Forecast.Solar's hard limit).

Tick loop took a fifth constructor arg (`solar_cache: SolarCache`) and
gained `_refresh_solar_if_stale(now)` running after the price refresh.
When `_solar_provider is None` the method short-circuits — that's how
the "no solar config" path stays a no-op. New `SourceName.SOLAR_FORECAST`
shows up in `/api/health` and the debug board, classified by the same
OK/DEGRADED/ERROR rules as the rest.

### Web layer

- `GET /api/solar` — returns `{last_refresh, window_start, window_end,
  watt_hours_today, watt_hours_tomorrow, points: [{timestamp, watts}],
  per_plane: {<name>: [...]}}`. Shape mirrors `/api/prices`.
- Dashboard view passes `solar_today_kwh = wh_today / 1000` to the
  template; new tile renders `25.4 kWh` (or `—` when the cache is
  empty). Hint text is `forecast.solar` so the source is obvious.
- Chart got a third dataset: filled curve in
  `rgba(251,146,60,0.30)` fill / `0.85` border, on a new
  `position: "right"` `ySolar` axis labelled "Solar kW". `kW` not `W`
  to keep numbers readable. Tooltip `label` cb branches by `yAxisID`
  for all three series (price / SoC / solar).

### Two issues caught + fixed mid-session

1. **Web-form save would wipe `solar:` from YAML.** The form-section
   list (`config_form.py`) intentionally doesn't include solar — too
   much complexity to build a UI for a list-of-planes. But
   `_config_to_plain_dict` re-emits the entire `AppConfig` to YAML,
   and the form-rebuilt config has `solar=None`, so any web save
   would silently drop the section. **Fix:** `form_to_config` now
   takes a `baseline: AppConfig | None` kwarg; when the form omits
   `solar`, the baseline's solar is grafted into the validation
   dict. The `/config` POST handler passes the current `ConfigDep`
   as the baseline.
2. **`_flatten` emitted `solar: ""` when solar was None.** Tripped the
   gui-binding round-trip test (Pydantic rejected the empty string as
   a `SolarConfig`). Fixed by popping the `solar` key from
   `model_dump()` in `config_to_form` before flattening — same idea
   as #1, just on the read path.

### tzdata gotcha (and the proper fix)

Forecast.Solar returns naive `"YYYY-MM-DD HH:MM:SS"` strings in the
**location's local timezone** (Brussels for our coords). First pass
used `ZoneInfo("Europe/Brussels")` which on Windows requires the
optional `tzdata` wheel (Linux/macOS pulls it from the system). User
ran uvicorn under their **system Python** rather than the project
`.venv` and got a startup crash:

```
zoneinfo._common.ZoneInfoNotFoundError: 'No time zone found with key
Europe/Brussels'
```

Adding `tzdata` to `pyproject.toml` covers `pip install -e .` users,
but doesn't help anyone running an arbitrary Python. **Proper fix:** a
20-line `_EuropeBrusselsFallback(tzinfo)` in `forecast_solar_provider.py`
implementing the EU DST rule directly (CEST/UTC+2 from last-Sunday-March
02:00 local to last-Sunday-October 03:00 local; CET/UTC+1 otherwise).
`_resolve_local_tz` prefers `ZoneInfo` and falls back to the custom tz
when `ZoneInfoNotFoundError` fires. Verified roundtrip:
- May 13:00 local → 11:00 UTC (CEST) ✓
- December 13:00 local → 12:00 UTC (CET) ✓

The `tzdata` declaration stays in `pyproject.toml` as belt-and-braces.

### Tests

All 250 pass. Three test fixups needed:

- `tests/integration/test_web_app.py` — `test_health_lists_all_expected_sources`
  and `test_health_ok_when_recent_success` had hardcoded source-name
  sets; added `solar_forecast`.
- `tests/unit/test_orchestrator.py` — `TickLoop` ctor gained an arg, so
  every call site (7 of them) now passes `SolarCache()` as the fifth
  positional. The "all SourceName values got recorded" assertion in
  `test_tick_persists_reading_and_decision` was relaxed to subtract
  `SOLAR_FORECAST` (test config has no solar, so the source is
  legitimately UNKNOWN).

### State at end-of-session

- 14 files modified, 1 new sub-package (`src/energy_orchestrator/solar/`),
  ~307 / −18 lines. All 250 tests pass.
- Live verified end-to-end against the real Forecast.Solar API:
  25,443 Wh today, 21,491 Wh tomorrow, 34 hourly points (today + part
  of tomorrow, sunrise to sunset). Peak summed power only ~2.9 kW
  because E/W arrays peak at different hours so they don't fully
  stack — that's geometry, not a bug.
- Pre-existing circular import (`orchestrator → web.override → web.__init__
  → web.app → orchestrator`) only manifests when `tests/unit/test_orchestrator.py`
  is loaded first by pytest. Not addressed this session.

### Reusable nuggets

- **Forecast.Solar puts rate-limit info in the JSON body**, not HTTP
  headers — `result.message.ratelimit.{period,limit,remaining}`.
  Easy to miss. Logged at debug so the throttle ceiling is visible
  without spamming INFO.
- **Forecast.Solar timestamps are local at the lat/lon, not UTC.**
  The "time_utc" key is only in the message envelope, not in
  per-point keys. So you must know the location's timezone to parse
  the data series at all. That's why a Belgium-without-tzdata install
  had to crash before the fallback existed.
- **`zoneinfo` on Windows needs `tzdata`** (PyPI wheel) unless the user
  has Python 3.13+ which started bundling it. Worth keeping a
  stdlib-only DST fallback for pinned-region features so deploy
  environments don't have to know.
- **Pydantic `frozen=True` models can't be mutated to "preserve a
  field through a save."** Either pass a baseline through the
  validation builder (what I did) or use `model_copy(update=...)`.
  The baseline approach keeps the failure mode in front of the
  validator instead of after it.

## 2026-05-04 (evening) — Live dashboard, decoupled cadences, large_solar device, classifier fix

Long session, many small wins that compound. Dashboard goes from "refresh
on tab-switch" to live-at-5s; orchestrator's tick loop splits into a fast
device poll and a slow decision step; a second optional HomeWizard kWh
meter (`large_solar`) lands end-to-end (config → schema migration → API
→ tile → chart curve); source-health classifier no longer holds ERROR
across recovered ticks. 253 tests passing.

### Time rendering — both halves of the rope

User reported "all times are UTC" on Debug tab and chart x-axis. Two
separate fixes were needed:

1. **Server-rendered Jinja times.** Added a `localtime(dt, fmt)` filter
   in `web/views.py` that re-attaches UTC to naive SQLite datetimes
   (round-trip drops tzinfo even with `DateTime(timezone=True)`) and
   returns `dt.astimezone().strftime(fmt)`. Applied across `debug.html`
   and `dashboard.html` for every `strftime` call.
2. **Client-rendered times via the API.** The polling JS overwrites
   server-rendered times within ~5–10 s. The serializers in `web/api.py`
   were calling `.isoformat()` on naive UTC datetimes, producing
   `"2026-05-04T14:00:00"` (no zone). JS `new Date(...)` on a no-zone
   string parses as **local**, shifting the values by the TZ offset.
   Added `_iso_utc(dt)` that re-attaches UTC before isoformat and routed
   every `Reading` / `Decision` / `SourceStatus` / `OverrideController`
   timestamp through it. Now JS sees `"+00:00"` and converts correctly.

### Auto-refresh dashboard (5 s polling, in-place updates)

User: "the dashboard is not updated. Only when I go to another tab and
back do I see new values." Tiles, state card, recent-decisions table,
and chart datasets all needed live updates. Approach:

- Gave every live element a stable id (`tile-soc`, `state-text`,
  `state-when`, `recent-decisions-body`, …) and reworked the conditional
  branches so the same DOM survives empty → populated transitions
  without re-rendering (e.g. `state-empty-headline` is hidden, not
  conditionally absent).
- `dashboard.js` now keeps the `Chart` instance, runs `refreshAll()`
  every `REFRESH_MS` (5000), updates DOM via small helpers (`setText`,
  `setHidden`, `fmtTime*`), and calls `chart.update("none")` to redraw
  without animation. Pauses on `visibilitychange` so a hidden tab stops
  hammering the API.
- Footer hint reads "Live — last refresh HH:MM:SS" so the user can see
  when the connection drops.

### Decoupled poll vs decision (split option B)

Asked the user: "speed up the dashboard only, or also split device-poll
from decisioning?" They picked the split. Added
**`AppConfig.decision_interval_s`** (default 60 s, range 0 < x ≤ 3600).
`TickLoop` keeps `_last_decision_at`; every tick polls devices + writes a
`Reading`, but the engine + SolarEdge actuation only run when
`(now − _last_decision_at) >= decision_interval_s` (or first tick after
startup, so we don't sit idle for a minute). Importantly, when sonnen
SoC is unavailable we **don't** advance `_last_decision_at` — retry on
the next poll instead of waiting another full minute.

Added `test_decision_interval_gates_subsequent_ticks` covering first
tick / gated tick / gate-elapsed tick. Anchored its `base = now()` to
mid-hour (`replace(minute=30, second=0, microsecond=0)`) — without that,
runs near an hour boundary cross into the next hour and
`get_current_hour_price` returns None → engine flips to OFF for an
unrelated reason. Took one false-positive failure to spot.

End of session, set the user's `config.yaml` to `poll_interval_s: 5`,
`decision_interval_s: 60`. Their tile data now changes on every browser
poll instead of every 6th.

### Wider price-fetch window (so chart bars cover local-day fully)

Chart x-axis is locked to local 00:00 → 24:00 today. East-of-UTC zones
(Brussels DST = UTC+2) mean the first 2 hours of local-today live in
**yesterday's UTC date**, which the old `[today-UTC-00, +2 days)` window
didn't cover — left a visible gap. Replaced `_PRICE_LOOKAHEAD = 2 days`
with `_PRICE_PAST_DAYS = 1 day` + `_PRICE_FUTURE_DAYS = 2 days`; both
the orchestrator's `_refresh_prices_if_stale` and `/api/prices` now
return `[today-UTC-00 − 1 d, today-UTC-00 + 2 d)` (3 days). Chart already
clips outside its visible range, so the extras are visually free; the
decision engine filters by `p.timestamp <= now < +1h` so the historical
prices in the cache are inert. ENTSO-E happily returns yesterday's
prices, so no provider-side changes.

### Sign flips + tile renames (display-only, schema unchanged)

User wanted positive numbers to mean "the right thing":

- **Battery power**: positive = **charging** (was discharging). Hint
  updated.
- **Small / large solar**: positive = **generating** (later the explicit
  hint was removed at user request — the sign convention reads cleaner
  without the disclaimer cluttering the tile).
- **Grid feed-in → Grid import**: positive = **importing**. "Import" beat
  "Usage" (overlaps with House consumption) and "Consumption" (verbose).

The DB columns keep their existing semantics — flip happens in the
template (`{{ "%.0f" | format(-reading.foo_w) }}`) and in the JS poller
(`fmtInt(neg(reading.foo_w), " W")`). No migration, no historical-data
reinterpretation.

### large_solar — second optional HomeWizard kWh meter, end-to-end

User has a second PV string they want measured (and which the SolarEdge
inverter actuates ON/OFF). Added the device entirely as **optional** so
existing `config.yaml` files keep working unchanged:

- `LargeSolarConfig` (mirrors `SmallSolarConfig`, same `peak_w` field).
- `HomeWizardConfig.large_solar: LargeSolarConfig | None = None` —
  `None` means "no device".
- `SourceName.LARGE_SOLAR`, `Reading.large_solar_w` (Float | None).
- New alembic migration `0002_add_large_solar_w.py` — single
  `ALTER TABLE readings ADD COLUMN large_solar_w FLOAT`.
- `LargeSolarClient(HomeWizardClient[LargeSolarConfig])` registered.
- `TickLoop.__init__` builds the client conditionally; `tick()` parallel-
  reads it via a new `_read_optional(client)` helper that no-ops on
  `None`; `_close_resources` closes it if present.
- API `_reading_to_dict` includes `large_solar_w`.
- `web/config_form.py` + `gui/app.py` got a "HomeWizard — Large Solar
  (optional)" section with the hint "leave blank to disable".
- **Form-binding round-trip needed care.** When `large_solar=None`,
  `model_dump()` produces `large_solar: None`; `_flatten` sees
  `obj=None`, which is **not** a Mapping, so it emits
  `flat["homewizard.large_solar"] = ""` — that empty-string would later
  fail Pydantic's `LargeSolarConfig` validation. Two-part fix:
  (a) `config_to_form` pops `homewizard.large_solar` when `None`, same
  pattern as the existing `solar` pop; (b) `form_to_config` prunes
  `homewizard.large_solar` back to `None` if the user left `host` blank
  (or the placeholder slipped through as a non-dict). Round-trip
  stable in both directions now.
- `_config_view` (debug page) and `_config_to_plain_dict` (YAML save)
  emit `large_solar` only when configured.

Three test files needed updates: the `/api/health` source-name set
expects `large_solar`; the orchestrator test that asserts every
`SourceName` got recorded subtracts `LARGE_SOLAR` (test config has no
device so it's legitimately absent).

### Migration gotcha: pre-alembic database

User's live DB was created via `init_schema` (`Base.metadata.create_all`)
on first run, so it had no `alembic_version` row. `alembic upgrade head`
would try to run `0001_initial` against an existing schema and fail. The
fix is two commands:

```
alembic stamp 0001_initial    # record "DB is at rev 1" without running it
alembic upgrade head          # apply 0002 only
```

I ran them for the user from the orchestrator working dir while their
process was stopped. Future migrations are just `alembic upgrade head`.
Worth surfacing to anyone deploying a fresh Pi: either stamp on first
boot, or stop using `init_schema` for non-test bootstrapping.

### pymodbus 3.7+ rename: slave → device_id

Trying to actuate SolarEdge surfaced
`TypeError: ModbusClientMixin.read_holding_registers() got an unexpected
keyword argument 'slave'`. pymodbus renamed `slave=` → `device_id=` in
3.7 and removed the deprecated alias by 3.11+. User's installed version
is 3.13. Renamed both call sites in `solaredge.py`, fixed three
`assert_awaited_once_with` mocks in `test_devices_solaredge.py`, bumped
the floor in `pyproject.toml` from `>=3.6,<4.0` → `>=3.7,<4.0`, and
removed the now-stale comment about `slave=` being rejected by stubs.

### Source-status classifier: most-recent event wins

User noted SolarEdge stayed ERROR on the Debug tab even though the error
message was from the previous session and recent polls were succeeding.
The old classifier had a bug: any error within 5 minutes returned ERROR
**regardless** of whether a successful poll happened after it. Rewrote
`_classify_source_status` so the most recent of `last_success_at` /
`last_error_at` decides the state:

- Latest is success, age ≤ 5 min → OK
- Latest is success, age 5–30 min → DEGRADED
- Latest is success, age > 30 min → ERROR
- Latest is error, age ≤ 30 min → ERROR
- Latest is error, age > 30 min → UNKNOWN

The "Last error" column still displays the most recent failure for
diagnostics, but it no longer drives the badge. Added
`test_health_ok_when_success_follows_recent_error` to lock it in.

### "Clear all errors" button on Debug tab

User: "while we're here, give me a button to wipe stale error messages."
Added `SourceStatusRepository.clear_all_errors()` — bulk
`UPDATE source_status SET last_error_at=NULL, last_error_message=NULL`.
New endpoint `POST /api/source-status/clear-errors` that calls it inside
a UoW. Button + JS in `debug.html` POSTs and reloads. Returns
`{"cleared": rowcount}`. `last_success_at`, `last_payload`, `updated_at`
left intact — pure ack-and-clear, not a polling pause. Test
`test_clear_errors_nulls_error_columns` verifies the column subset and
that other fields are preserved.

### Solar tile redesign + chart total-solar curve

The two solar tiles became cramped quickly. Merged into one wide tile
(`.tile-wide` with `grid-column: span 2`) holding three labeled cells —
**small + large = total** — separated by `+` and `=` operators. Cell
labels `solar-cell-label` are uppercase muted; values are `1.4 rem 600`;
the total is rendered in the same green as the SoC line so it reads as
the headline of the tile. Sign-flip and "missing meter" fallback are
shared between Jinja initial render and the JS polling path
(`applySolarTile` in `dashboard.js`).

User then asked for a **measured total-solar curve on the chart**, in
yellow with the same contrast treatment as the SoC. Added
`COLOR_TOTAL_SOLAR = "#fde047"` (yellow-300) and two datasets — a
6-px-wide dark halo (`COLOR_SOC_HALO`) underneath, a 3-px-wide bright
yellow line on top — both bound to the existing `ySolar` (kW) axis so
the new line shares scale with the orange forecast curve.
`buildChartData` now derives `totalSolarLine` from `readings` (same
sign-flip + missing-meter fallback as the tile); points where neither
meter reported are dropped and `spanGaps: true` reconnects.
Stacking order, top → bottom: SoC line → SoC halo → total-solar line →
total-solar halo → forecast solar (filled) → price bars. Tooltip uses
the dataset's `label` directly so hovers read "Forecast solar 2.41 kW"
vs "Total solar 1.93 kW" instead of an ambiguous "Solar".

### SoC line contrast pass (mid-session)

Before the yellow line landed, user asked to make the SoC line "stand
out more". Bumped `borderWidth` 2 → 3, brightened the green
`#22c55e → #4ade80`, dropped the dataset to `order: 0` (top), and added
the dark halo dataset (`_soc_halo`, `borderWidth: 6`) drawn just behind.
Halo is hidden from legend/tooltip via the `_`-prefix label filter. The
total-solar curve reuses the exact same trick.

### Reusable nuggets

- **SQLite drops tzinfo even on `DateTime(timezone=True)`.** The fix
  has to happen at the serialization boundary on every read, not via
  the column type. A `_utc_aware()` re-attach + a paired `_iso_utc()`
  helper kept things consistent.
- **JS `new Date("2026-05-04T14:00:00")` parses as local**, not UTC. If
  your API emits naive ISO strings, your client side-shifts silently by
  the TZ offset. `.isoformat()` on a tz-naive datetime is a serializer
  bug, even when the Python side treats the value as UTC by convention.
- **Chart.js bars are clipped against `scales.x.{min,max}`**, so over-
  fetching to fill local-day windows in non-UTC zones is free. No
  client-side filtering needed.
- **A "stamp before upgrade"** dance is necessary any time a database
  was created via `Base.metadata.create_all` rather than the first
  migration. Easy to forget on Pi-style deploys where the dev path is
  `init_schema` and the prod path is alembic.
- **pymodbus moves fast.** `slave=` → `device_id=` is the kind of
  rename that's silently okay until the deprecated alias is dropped a
  few minor versions later. Keep the floor pinned to the version that
  actually exposes the new keyword, not just "latest 3.x".
- **Holding ERROR badges on a fixed cooldown after success** is worse
  than a "latest event wins" rule, because successful polls during the
  cooldown look like nothing changed. The cooldown idea felt right at
  Phase-10 time but operating the dashboard exposed how confusing it is.
- **Optional sub-models in pydantic + dotted-key form serializers**
  need explicit prune logic on both sides of the round-trip
  (`config_to_form` strips when `None`, `form_to_config` re-strips when
  the visible host field is empty). Otherwise `_flatten` and
  `_set_nested` happily produce a placeholder dict that fails
  validation with a confusing message.

## 2026-05-06 — Persisted prices/solar, date-navigable chart, line-gap polish

Two-feature session: the dashboard chart now lets you walk back through
past days, and the SoC / total-solar curves break across genuine data
gaps instead of bridging them. Also a brief diagnostic detour where the
user thought yesterday's predicted-solar was identical to today's.

### Persistence: prices + forecast.solar points

Until now `/api/prices` and `/api/solar` were served only from the
in-memory caches the tick loop refreshes — fine for "today" but the
moment you rolled past midnight, yesterday's bars/curve disappeared.
Added a new alembic migration `0003_persist_prices_and_solar.py` plus
two SQLAlchemy models:

- `price_points` — PK `(timestamp,)`, `consumption_eur_per_kwh` and
  `injection_eur_per_kwh` nullable. Each provider refresh upserts
  today/tomorrow rows in place.
- `solar_forecast_points` — PK `(timestamp, plane)`, `watts` non-null.
  The summed-across-planes series is **derived on read** by summing
  rows that share a timestamp; that lets a future config add/remove
  planes without a schema change. When a provider only returns the
  aggregate, the orchestrator stores it under a synthetic
  `_total` plane so the same read path still works.

Wired `PricePointsRepository` and `SolarForecastRepository` (with
`upsert_many` / `upsert_per_plane` using sqlite's
`on_conflict_do_update`) into `UnitOfWork`, and added two
`_persist_*` helpers in the tick loop that fire after each successful
cache refresh. Both are wrapped in their own `try/except Exception`
that **only logs** — bookkeeping must never kill the tick. New repos
also got `prune(retention_days)` so the same retention story applies.

### Date-navigable chart (Prev / Today / Next)

`/api/{prices,solar,history}` all gained a `?date=YYYY-MM-DD` query
param interpreted as a **server-local calendar day**. New helper
`_local_day_window(date_str)` parses the date with `astimezone()` (no
arg attaches the system tz), shifts to UTC at the storage boundary,
and returns `[start, end)`. Without the param, the cache-backed code
path stays exactly as before — the date branch is purely additive.

For prices specifically the persisted-window read widens by ±1 day
to mirror the cache's "yesterday + today + tomorrow" slop, otherwise
chart bars near midnight go missing in non-UTC zones (we already
learned this lesson client-side in the previous session). Solar
historic days don't have meaningful `watt_hours_today` totals, so
those fields come back as `null` and the tile shows `—`.

Dashboard side: three small buttons under the chart heading
(`#chart-prev`, `#chart-today`, `#chart-next`) plus a date label
(`#chart-date`). State lives in a single mutable
`viewedDate = startOfLocalDay(new Date())`. `buildChartUrls()` returns
the cache-backed URLs only when `isViewingToday()`; otherwise it
appends the encoded date. `navigateTo(newDate)` stops the polling
timer (history is immutable), refreshes once, then restarts polling
**only** if the new date is today and the tab is visible. The Next
button is disabled at today since there's nothing recorded ahead.
Existing chart `scales.x.{min,max}` already locks to the viewed day's
00:00→24:00, so reusing it for past days was a one-line free win.

### Solar forecast "double check" detour

User: "the predicted-solar chart of yesterday looks identical to
today's." Dumped `solar_forecast_points` for both days side-by-side:
30 hourly points, mean ratio today/yesterday **1.019 ± 0.028** —
small but real per-hour deltas, and tomorrow's curve is +27 % higher
at peak so the upsert path is clearly hitting separate days. The
visual identity is just Forecast.Solar predicting near-identical
weather two days running, plus the natural ~0.5 %/day clear-sky
drift in May. No code change. Worth keeping in mind for later: if
someone reports "stuck" historic forecasts again, log raw API
responses for a couple of days first before assuming a bug.

### SoC + total-solar line gap-breaks

The two measured curves used `spanGaps: true`, which silently
bridges across offline windows. Switched both (and their dark
contrast halos) to `spanGaps: MAX_LINE_GAP_MS = 90 * 1000` — three
times the orchestrator's 30 s poll, so a single dropped sample
doesn't flicker but a real outage shows. Also stopped
`.filter()`-ing missing samples out of the data array;
`buildChartData` now emits `{x, y: null}` for `battery_soc_pct == null`
and for readings where neither solar meter reported. Forecast solar
keeps `spanGaps: true` since it's a smooth hourly model curve, not a
sampled measurement, and gaps there are expected at sunrise/sunset.

While in the area, the user also asked to clamp `ySolar` to a fixed
**0–10 kW** range so the line height is comparable across days
regardless of which kWp peak that day actually hit.

### Reusable nuggets

- **"Stuck data" is usually similar weather.** Before chasing a bug
  in a forecast/prediction pipeline, dump the underlying numeric
  series across days and look at the per-point delta. A ~1 %
  consistent ratio is what near-identical clear-sky forecasts look
  like; a 0 % delta on every point would be the actual bug
  signature.
- **`spanGaps` accepts a number for time-axis line charts.** Reads
  as "max ms gap to bridge" and coexists with the null-y break, so
  you get *both* "single missing field → break" and "no reading at
  all for ≥ N ms → break" out of one knob. Pick the threshold off
  the source data's cadence (~3× the poll interval is a good
  default).
- **Free additive-only API params.** `?date=YYYY-MM-DD` slots in
  next to `h=` on `/api/history` without breaking any existing
  caller — the cache-backed path is the default branch and the
  date branch is purely opt-in. Same shape, same fields, just the
  data window changes.
- **Bookkeeping persistence must be belt-and-braces.** Every new
  `_persist_*` in the tick loop is wrapped in a top-level
  `except Exception` that *only logs*; a sqlite hiccup or a
  schema mismatch on a stale DB must never block the next device
  poll or actuation. Pattern repeats `_persist_prices` and
  `_persist_solar_forecast` verbatim.
- **Per-plane stored, summed on read.** Storing the aggregate
  series only would have saved one DB write but made it impossible
  to add or rename a plane without rewriting historic rows. The
  composite `(timestamp, plane)` PK + sum-on-read keeps schema
  stable through config changes.

## 2026-05-07 — Etrel INCH integration + Sonnen Smart-E-Grid lockdown discovery + Solar 429 backoff

Phase: add the Etrel INCH Home/Pro EV charger as a new device source
similar to SolarEdge, with dashboard tiles for live charger status and
the installer-configured current cap. Adjacent fix: a real bug surfaced
during the diagnostic phase where Forecast.Solar 429s caused an infinite
retry loop hammering the API every poll.

### Scaffolding the Etrel client

Modelled on `SolarEdgeClient`: `EtrelInchConfig` (host, modbus_port=502,
unit_id=1) wired into `AppConfig` as an *optional* `etrel: ... | None`
section so the orchestrator runs unchanged for users without an Etrel.
`@register_device(EtrelInchConfig)` decorator picks up the registry the
same way the other Modbus client does.

Per-field reads from the start (status reg 0, setpoint reg 4, voltage
reg 8, power-total reg 26, custom-max reg 1028). The 30-register block
read I started with would have made failure logs less actionable —
rejecting one read pinpoints the offending register; a block read leaves
you guessing. Pymodbus's default `retries=3` is also lethal on a 5 s
poll budget for a silent device, so we cap at `retries=1`. Every failure
emits a structured warning with `host / modbus_port / unit_id / address /
count / field` so the live log viewer is self-diagnosing.

Word-order detection happens once per session, not per tick. Read the
voltage registers once, decode locally in both endiannesses, pick
whichever is in the mains envelope (80–300 V) and make it sticky. The
earlier "flip every tick when implausible" implementation generated 12
warnings per minute when the device returned bogus values — useless
noise.

### New `Reading.etrel_power_w` column + alembic 0004

Added one persisted column (`etrel_power_w` only) so historical Tesla =
`car_charger - etrel` decomposition is possible without bloating the
table with status/setpoint fields that are inherently latest-only and
already live in `SourceStatus.last_payload`. Status, setpoint, voltage
and `custom_max_a` go through the source-status JSON payload to the
dashboard; only the power flux gets a column.

### Configurator + dashboard wiring

Mirrored the existing optional-section pattern (`large_solar`):
configurator section in both `web/config_form.py` and `gui/app.py`,
binding handles blank-host as "disabled". Dashboard got two new pieces:

- **Charger tile rebuilt to match the solar tile.** Three cells
  (Tesla / Etrel / Total) with `+` and `=` operators between them.
  Tesla is derived as `max(0, car_total - etrel)` — clamped at 0 so
  sub-watt rounding doesn't briefly flip negative. Reuses the
  `.solar-breakdown / .solar-cell / .solar-op / .solar-cell-total`
  CSS classes verbatim — zero new styling needed for the layout.
- **State card top tile split into two columns.** SolarEdge state on
  the left, Etrel status on the right, both using `.state-headline`
  for identical font sizing. `state-card` becomes a flex container
  with a thin `border-left` on the right column as a divider, plus
  a `@media (max-width: 720px)` rule that flips it to a stacked layout
  with a horizontal divider. The Etrel column reuses `.state-rule`
  for the setpoint and max lines so they read as a parallel of the
  SolarEdge rule line.

### Diagnostic odyssey

Goal: get Modbus reads through. Path through the brick wall:

1. **Test-NetConnection 192.168.1.250:502** → success. So TCP works.
2. Modbus reads time out silently every tick. By spec a server stays
   silent on either (a) Modbus disabled, (b) wrong unit ID, (c) Modbus
   listener not actually running.
3. Asked Opteco; somewhere along the way someone briefly enabled
   Modbus and we got responses for ~10 minutes — but every register
   except reg 8 returned 0, and reg 8 decoded to 16.0 (a current value
   in volts? at "L1 voltage register"?). Weird.
4. Added a one-shot **diagnostic register dump** that runs on the first
   successful read after restart: regs 0..47 (live block) + regs
   990..1039 (info block — serial, model, HW/SW versions, custom max).
   Both blocks logged with raw uint16 + float32_big + float32_little
   side-by-side so a layout shift is eyeballable.
5. **The dump was the smoking gun.** Out of 97 registers, exactly
   *one* held a non-zero value (reg 8 = 0x4180 = 16.0 BE-float32).
   The device-info block came back **100% zero** — model/firmware/
   serial are static manufacturer strings burned in at production,
   so them returning 0 means the responding side isn't the Etrel
   itself. Sonnen's Smart-E-Grid is intercepting Modbus reads and
   only forwarding one register through their facade.
6. Plugged in the Tesla anyway — connector status reg 0 still read 0
   ("Available"). The live block doesn't reflect actual device state
   either. End of story for this firmware: only reg 8 is exposed,
   probably the Sonnen-imposed current cap (16 A → ~3.7 kW @ 1ph or
   ~11 kW @ 3ph 230/400 V, typical Belgian residential).

The `red→green→red` source-status flapping during this phase had a
secondary cause: `SourceStatus` rows persist across restarts. After a
clean shutdown, the most recent error from the previous run is
displayed until the new run's first successful tick lands. Briefly
considered auto-clearing on startup but decided against — the existing
"Clear errors" button on `/debug` is enough for the rare case.

### Pymodbus log-spam silenced

`pymodbus.logging` emits the raw send/recv hex frames at ERROR with
`get_last_frames()` appended into the same record on every retry.
With Modbus down for 12 ticks/minute that floods the live log viewer
with content the operator can't act on — our device clients log their
own structured warnings with the actionable context. Bumped the
internal `pymodbus.logging` logger to `CRITICAL` (suppresses the
hex-blob ERROR), left the rest of `pymodbus` at `WARNING` so genuine
library-level problems still surface.

### Solar 429 backoff (separate bug, found during this session)

Symptom: every tick logged
`Forecast.Solar HTTP 429 Rate limit for API calls reached`. Root cause:
in-memory `SolarCache` starts empty after each restart → `is_stale()`
returns True → fetch fires → 429 → cache stays empty → next tick
re-fetches. With ~30 restarts during the Etrel debugging session we
burned the daily quota and got stuck in the loop.

Fix: `SolarCache.mark_failed(now)` writes a `_next_attempt_at` 60
minutes ahead (matching Forecast.Solar's hourly quota reset).
`is_stale()` honours the cooldown first. On success, `replace()` sets
the cooldown to `_MAX_AGE` (30 min) as before — success overrides
failure backoff. Existing forecast remains visible during cooldown so
the dashboard doesn't blank out from a transient API blip. Wired
`mark_failed` into both `SolarError` and the catch-all `Exception`
branches in `_refresh_solar_if_stale` so pre-startup cache wipes never
hit Forecast.Solar more than once per hour. 5 unit tests for the
backoff state machine.

### Reusable nuggets

- **One-shot register dump on first connect.** When integrating any
  Modbus device whose documented register layout might not match the
  firmware actually running, log a full sweep of the live block AND
  the info block (manufacturer strings, version registers) on the
  first successful tick. The info block is the Rosetta Stone — if it
  returns garbage or zeros, you're not talking to the device you
  think you are. ~30 lines of code, paid for itself in literally one
  diagnostic round.
- **Per-field reads beat block reads for diagnostics.** A single
  block-read failure tells you "the device didn't respond to a read of
  N registers". A per-field-read failure tells you "the device didn't
  respond when asked for `voltage_l1_v` at `addr=8 count=2`". Same
  Modbus traffic on success, vastly more actionable on failure.
- **Cap pymodbus retries at 1 for ticked polling.** Default 3 means
  every silent timeout costs 3× the configured `timeout`. On a 5 s
  poll that's ~15 s blocking the gather, which silently degrades the
  dashboard freshness for *every* device, not just the failing one.
- **TCP-accepts-but-app-silent ≠ device unreachable.** The Etrel was
  reachable on every probed port (80, 443, 502) yet none of them
  served any application data — Sonnen's network setup pinholes ports
  but the embedded server behind them speaks selectively. When the
  layer-4 test passes, you still need a layer-7 test (HTTP fetch,
  Modbus read) to know if anything's actually behind it.
- **Failure backoff in caches.** Any cache that gates a rate-limited
  API call needs `mark_failed`-style cooldown, not just
  `replace()`-on-success. Otherwise `is_stale()` comes back True on
  every poll after a failure and you hammer the API harder than
  any legitimate cadence would. Cooldown duration should match the
  upstream's quota window (Forecast.Solar resets hourly → 60 min).
- **Show em-dash, not zero, for unavailable values.** Rendering "0 W"
  / "0 A" on the dashboard when the underlying read returned a
  default zero is actively misleading — looks like a real reading.
  Only relevant on devices like this where zero is genuinely "no
  data" rather than a real measurement (zero charging current is a
  meaningful state on a working charger). Trade-off noted, not
  implemented this session — gated on Opteco unblocking the
  registers first.
- **Persisted `SourceStatus` flaps across restarts.** Stale errors
  from the previous run are displayed until the new run's first
  successful tick lands. Mention this once when teaching the system
  to a user; don't auto-clear on startup (it would mask "the device
  has been down since yesterday" — a real signal).

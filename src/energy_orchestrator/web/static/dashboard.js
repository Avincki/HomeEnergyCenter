/* Dashboard chart: day-ahead injection-price bars + battery SoC line on
 * one canvas with dual axes. Reads /api/prices and /api/history?h=24.
 * No CDN — the orchestrator runs on a home LAN, so all assets ship locally.
 *
 * After initial render, polls /api/state + /api/history + /api/prices +
 * /api/solar every REFRESH_MS to update tiles, state card, recent-decisions
 * table, and chart datasets without reloading the page.
 */
(() => {
    "use strict";

    // Price bars: muted grey when injection price is positive, bright green
    // when negative. Bar heights are plotted as |price| so negative hours
    // still rise from zero — the sign is conveyed by colour, not direction.
    // The negative bars are the ones the user acts on, so they're drawn at
    // near-full opacity (and full width — see priceBorderWidths) to stand out.
    const COLOR_PRICE_POS = "rgba(148, 163, 184, 0.55)";
    const COLOR_PRICE_NEG = "rgba(74, 222, 128, 0.95)";
    // SoC: blue-400 — pops against the muted gray price bars, the green
    // negative-price bars, and the translucent orange solar fill.
    const COLOR_SOC = "#60a5fa";
    // Halo drawn underneath the SoC line so it stays legible where it crosses
    // bars or the solar fill.
    const COLOR_SOC_HALO = "rgba(2, 6, 23, 0.85)";
    // Car (EV) SoC: red-500 — shares the right-hand SoC % axis with the blue
    // battery line but is unmistakably distinct from it.
    const COLOR_EV_SOC = "#ef4444";
    const COLOR_SOLAR_FILL = "rgba(251, 146, 60, 0.30)";
    const COLOR_SOLAR_LINE = "rgba(251, 146, 60, 0.85)";
    // Measured total solar (small + large): yellow-300, distinct from the
    // orange forecast and the green SoC line; same dark halo treatment as
    // SoC for legibility against the price bars.
    const COLOR_TOTAL_SOLAR = "#fde047";
    const TEXT_MUTED = "#94a3b8";
    const TEXT_BODY = "#e2e8f0";
    const GRID_FAINT = "rgba(148, 163, 184, 0.15)";

    const REFRESH_MS = 5000;
    // Largest x-axis gap (in ms) the SoC and total-solar lines will bridge.
    // Beyond this the line breaks — the orchestrator polls every 5 s, so
    // 90 s catches genuine offline windows (≥ 18 missed samples) without
    // flickering on a single skipped read.
    const MAX_LINE_GAP_MS = 90 * 1000;

    let chart = null;
    // Local-midnight Date for the day currently shown on the chart. Mutated
    // by the prev/today/next nav buttons; also used to derive the URL params
    // sent to /api/{prices,history,solar} and the x-axis bounds in
    // renderCombined / updateChart.
    let viewedDate = startOfLocalDay(new Date());
    // True while the chart should auto-follow "today" — the default until the
    // user manually navigates to a past day via prev/next. Flipped back on
    // when they click Today (or Next back to today). Drives the midnight
    // rollover in `refreshAll`.
    let autoTrackToday = true;
    // Polling timer handle so the nav code can pause refreshes when looking
    // at a non-today day (the data is frozen there).
    let pollTimer = null;
    // True once tomorrow's day-ahead prices are present in the live cache
    // window — gates whether the Next button can advance to tomorrow (a
    // price-only preview used for night-charging planning). Recomputed only
    // on the today view (the no-date /api/prices returns the full
    // yesterday+today+tomorrow window); left untouched while browsing other
    // days, whose ?date= payload is clipped to that day.
    let tomorrowAvailable = false;

    function startOfLocalDay(d) {
        const out = new Date(d);
        out.setHours(0, 0, 0, 0);
        return out;
    }

    function fmtDateYMD(d) {
        return d.getFullYear() + "-" +
               pad2(d.getMonth() + 1) + "-" +
               pad2(d.getDate());
    }

    function isViewingToday() {
        return viewedDate.getTime() === startOfLocalDay(new Date()).getTime();
    }

    // Furthest day the chart may navigate to: today normally, or tomorrow once
    // tomorrow's day-ahead prices have loaded. Day-ahead never reaches beyond
    // tomorrow, so this is the hard ceiling for the Next button.
    function maxNavigableDay() {
        const d = startOfLocalDay(new Date());
        if (tomorrowAvailable) d.setDate(d.getDate() + 1);
        return d;
    }

    function canGoNext() {
        return viewedDate.getTime() < maxNavigableDay().getTime();
    }

    // Whether the given price rows contain any point falling on tomorrow's
    // local day — i.e. tomorrow's day-ahead prices have been published and
    // pulled into the live cache window.
    function pricesCoverTomorrow(prices) {
        if (!prices || !prices.length) return false;
        const tomorrow = startOfLocalDay(new Date());
        tomorrow.setDate(tomorrow.getDate() + 1);
        const dayAfter = new Date(tomorrow);
        dayAfter.setDate(dayAfter.getDate() + 1);
        const lo = tomorrow.getTime();
        const hi = dayAfter.getTime();
        return prices.some((p) => {
            const t = new Date(p.timestamp).getTime();
            return t >= lo && t < hi;
        });
    }

    function dateQuery() {
        // No date param when viewing today — falls through to the cache-backed
        // endpoints, which stay fresh between tick refreshes.
        return isViewingToday() ? "" : "&date=" + encodeURIComponent(fmtDateYMD(viewedDate));
    }

    function currentHourMatches(iso, now) {
        const d = new Date(iso);
        return d.getFullYear() === now.getFullYear() &&
               d.getMonth() === now.getMonth() &&
               d.getDate() === now.getDate() &&
               d.getHours() === now.getHours();
    }

    async function fetchJson(url) {
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`${url}: HTTP ${resp.status}`);
        return resp.json();
    }

    function showEmpty(canvas, msg) {
        const parent = canvas.parentElement;
        if (!parent) return;
        const note = document.createElement("p");
        note.className = "empty";
        note.textContent = msg;
        parent.replaceChild(note, canvas);
    }

    function pad2(n) { return String(n).padStart(2, "0"); }

    // Pin displayed timestamps to the install's timezone (Brussels) regardless
    // of the viewing browser's TZ, so the text times match the server logs.
    const DISPLAY_TZ = "Europe/Brussels";
    function tzParts(d) {
        const parts = new Intl.DateTimeFormat("en-GB", {
            timeZone: DISPLAY_TZ, hourCycle: "h23",
            year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit", second: "2-digit",
        }).formatToParts(d);
        const m = {};
        for (const p of parts) m[p.type] = p.value;
        return m;
    }

    function fmtTimeFull(iso) {
        if (!iso) return "";
        const d = new Date(iso);
        if (isNaN(d.getTime())) return String(iso);
        const p = tzParts(d);
        return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
    }

    function fmtTimeShort(iso) {
        if (!iso) return "";
        const d = new Date(iso);
        if (isNaN(d.getTime())) return String(iso);
        const p = tzParts(d);
        return `${p.hour}:${p.minute}:${p.second}`;
    }

    function fmtTimeHM(iso) {
        if (!iso) return "";
        const d = new Date(iso);
        if (isNaN(d.getTime())) return String(iso);
        const p = tzParts(d);
        return `${p.hour}:${p.minute}`;
    }

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
        }[c]));
    }

    function setText(id, value) {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    }

    function setHidden(id, hidden) {
        const el = document.getElementById(id);
        if (!el) return;
        if (hidden) el.setAttribute("hidden", "");
        else el.removeAttribute("hidden");
    }

    function fmtNum(v, digits, suffix) {
        return (v === null || v === undefined)
            ? "—"
            : v.toFixed(digits) + (suffix || "");
    }

    function fmtInt(v, suffix) {
        return (v === null || v === undefined)
            ? "—"
            : Math.round(v) + (suffix || "");
    }

    // Solar tile populates three cells: small / large / total. Inputs are
    // raw API values (sign convention: negative = generating); each cell
    // flips the sign so positive = generating, like the other tiles. Total
    // falls back to whichever single value is present when one meter is
    // missing or the device is unconfigured.
    function applySolarTile(smallRaw, largeRaw) {
        const s = smallRaw == null ? null : -smallRaw;
        const l = largeRaw == null ? null : -largeRaw;
        const fmt = (v) => v == null ? "—" : Math.round(v) + " W";
        let total;
        if (s != null && l != null) total = s + l;
        else if (s != null) total = s;
        else if (l != null) total = l;
        else total = null;
        setText("tile-solar-small", fmt(s));
        setText("tile-solar-large", fmt(l));
        setText("tile-solar-total", fmt(total));
    }

    // Charger tile: Tesla + Etrel = total, mirroring the solar tile layout.
    // Total = HomeWizard car_charger meter (measures both vehicles together).
    // Etrel power: prefer the persisted reading, fall back to the live source
    // payload so the cell populates on the very first refresh after enabling
    // the device, before a Reading row has been written.
    // Tesla = total - Etrel, clamped at 0 (sub-watt rounding can flip sign).
    // Below the breakdown we surface the Etrel charger's live status and
    // current setpoint — needed to know what the charger is actually doing.
    function applyChargerTile(state, reading) {
        const etrel = sourcePayload(state, "etrel");
        const totalW = reading.car_charger_w;
        const etrelW = reading.etrel_power_w != null
            ? reading.etrel_power_w
            : (etrel.power_w != null ? etrel.power_w : null);
        const teslaW = (totalW != null && etrelW != null)
            ? Math.max(0, totalW - etrelW)
            : null;

        setText("tile-charger-tesla", fmtInt(teslaW, " W"));
        setText("tile-charger-etrel", fmtInt(etrelW, " W"));
        setText("tile-charger-total", fmtInt(totalW, " W"));

        const status = etrel.status ? String(etrel.status) : null;
        const setpointPart = etrel.setpoint_a != null
            ? `setpoint ${etrel.setpoint_a.toFixed(1)} A`
            : null;
        const detailParts = [status, setpointPart].filter((p) => p != null);
        setText(
            "tile-charger-etrel-detail",
            detailParts.length > 0 ? `Etrel: ${detailParts.join(" · ")}` : "Etrel: —"
        );
    }

    // Top-of-page state card sub-line: live Etrel charger status, the
    // currently-applied current setpoint (reg 4) and the installer-configured
    // ceiling (reg 1028). Always visible alongside the SolarEdge state so
    // the user can see the charger's max-power limit at a glance, with
    // em-dashes when individual fields haven't been read yet. Hidden only
    // when Etrel isn't configured at all — i.e. no source row exists.
    function applyEtrelStateLine(state) {
        const sources = (state && state.sources) || [];
        const isConfigured = sources.some((s) => s && s.source_name === "etrel");
        if (!isConfigured) {
            setHidden("state-etrel", true);
            return;
        }
        setHidden("state-etrel", false);
        const etrel = sourcePayload(state, "etrel");
        setText("state-etrel-status", etrel.status ? String(etrel.status) : "—");
        setText("state-etrel-setpoint",
            etrel.setpoint_a != null ? etrel.setpoint_a.toFixed(1) : "—");
        setText("state-etrel-max",
            etrel.custom_max_a != null ? Math.round(etrel.custom_max_a) : "—");
    }

    // Charger-control decision, shown in the Etrel column of the state card so
    // it mirrors SolarEdge's rule/reason/when on the left: the rule engine's
    // latest charger command (target current or "Paused"), the descriptive
    // reason, and the decision time. The Rule line stays visible even when
    // there's no live decision — charger control disabled, outside solar
    // daytime, essential inputs missing, or nothing computed yet all surface a
    // fallback reason rather than hiding, like SolarEdge's always-visible card.
    // (The whole Etrel column is hidden only when the charger isn't configured
    // at all — handled by applyEtrelStateLine.) A "dry-run" badge shows while
    // the engine logs decisions without writing to the charger.
    function applyChargerControlDecision(state) {
        const c = state && state.charger;
        if (!c) {
            setText("state-charger-target", "—");
            setText("state-charger-reason", "charger control inactive or no decision yet");
            setText("state-charger-when", "");
            setHidden("state-charger-when", true);
            setHidden("state-charger-dry-run", true);
            setHidden("state-charger-mode", true);
            applyChargerModeButtons(null);
            return;
        }
        const target = c.paused
            ? "Paused"
            : (c.target_a != null ? `Charging ${c.target_a.toFixed(0)} A` : "—");
        setText("state-charger-target", target);
        setText("state-charger-reason", c.reason || "—");
        setText("state-charger-when",
            c.timestamp ? `decided ${fmtTimeFull(c.timestamp)}` : "");
        setHidden("state-charger-when", !c.timestamp);
        setHidden("state-charger-dry-run", !c.dry_run);
        // Mode badge + active-button highlight, so it's obvious whether the
        // algorithm or a forced setpoint is in control.
        const forced = c.mode === "forced";
        setText("state-charger-mode", forced ? "FORCED — ignoring solar/battery" : "");
        setHidden("state-charger-mode", !forced);
        applyChargerModeButtons(c.mode || "optimized");
    }

    function applyChargerModeButtons(mode) {
        const forceBtn = document.getElementById("etrel-force-btn");
        const optBtn = document.getElementById("etrel-optimized-btn");
        if (forceBtn) forceBtn.classList.toggle("is-active", mode === "forced");
        if (optBtn) optBtn.classList.toggle("is-active", mode === "optimized");
    }

    function buildChartData(prices, readings, solarPoints) {
        // Bars plot |price| so negative hours rise from zero like positive
        // ones; sign is conveyed by colour (grey = positive, green = negative).
        // _raw carries the signed value through to the tooltip.
        const priceBars = prices.map(p => ({
            x: new Date(p.timestamp).valueOf(),
            y: Math.abs(p.injection_eur_per_kwh),
            _raw: p.injection_eur_per_kwh,
        }));
        const priceColors = prices.map(p =>
            p.injection_eur_per_kwh < 0 ? COLOR_PRICE_NEG : COLOR_PRICE_POS
        );
        // Positive bars keep the 3 px transparent border that slims them;
        // negative (green) bars drop it to 0 so they render full-width and
        // stay clearly visible.
        const priceBorderWidths = prices.map(p =>
            p.injection_eur_per_kwh < 0 ? 0 : 3
        );

        // Keep readings without a SoC sample but emit a null y so the line
        // breaks at the gap (spanGaps with MAX_LINE_GAP_MS additionally
        // breaks across time gaps where no reading exists at all).
        const socLine = readings.map(r => ({
            x: new Date(r.timestamp).valueOf(),
            y: r.battery_soc_pct == null ? null : r.battery_soc_pct,
        }));

        // Car (EV) SoC from Tronity — same right-hand % axis as the battery
        // SoC. Null y where no sample so the line breaks across gaps (the EV
        // is polled far less often than the battery, so expect a stepped line).
        const evSocLine = readings.map(r => ({
            x: new Date(r.timestamp).valueOf(),
            y: r.ev_soc_pct == null ? null : r.ev_soc_pct,
        }));

        const solarLine = (solarPoints || []).map(p => ({
            x: new Date(p.timestamp).valueOf(),
            y: p.watts / 1000.0,  // chart axis is in kW for readable numbers
        }));

        // Measured total solar = small + large (with the same sign flip used
        // by the tile so positive = generating). Emit null y when neither
        // meter reported, so the line breaks at the missing sample instead
        // of bridging across it.
        const totalSolarLine = readings.map(r => {
            const s = r.small_solar_w == null ? null : -r.small_solar_w;
            const l = r.large_solar_w == null ? null : -r.large_solar_w;
            let total = null;
            if (s != null && l != null) total = s + l;
            else if (s != null) total = s;
            else if (l != null) total = l;
            return {
                x: new Date(r.timestamp).valueOf(),
                y: total == null ? null : total / 1000.0,
            };
        });

        return {
            priceBars, priceColors, priceBorderWidths, socLine, evSocLine, solarLine, totalSolarLine,
        };
    }

    function renderCombined(canvas, prices, readings, solarPoints) {
        // Lock the x-axis to the viewed local-day's 00:00 -> 24:00 window.
        const startOfDay = new Date(viewedDate);
        const endOfDay = new Date(startOfDay);
        endOfDay.setDate(endOfDay.getDate() + 1);
        const dayLabel = fmtDateYMD(startOfDay);

        const {
            priceBars, priceColors, priceBorderWidths, socLine, evSocLine, solarLine, totalSolarLine,
        } = buildChartData(prices, readings, solarPoints);

        return new Chart(canvas, {
            data: {
                datasets: [
                    {
                        type: "bar",
                        label: "Injection €/kWh",
                        data: priceBars,
                        backgroundColor: priceColors,
                        // Transparent border eats into each side of the bar to
                        // slim it — kept for positive hours, set to 0 for
                        // negative (green) ones so they render full-width.
                        borderColor: "transparent",
                        borderWidth: priceBorderWidths,
                        yAxisID: "yPrice",
                        // 1 hour wide — matches the price-point hour resolution.
                        barThickness: "flex",
                        order: 5,
                    },
                    {
                        type: "line",
                        label: "Forecast solar (kW)",
                        data: solarLine,
                        borderColor: COLOR_SOLAR_LINE,
                        backgroundColor: COLOR_SOLAR_FILL,
                        borderWidth: 1.5,
                        pointRadius: 0,
                        tension: 0.35,
                        fill: "origin",
                        spanGaps: true,
                        yAxisID: "ySolar",
                        order: 4,
                    },
                    {
                        // Dark halo drawn behind the total-solar line for contrast.
                        type: "line",
                        label: "_total_solar_halo",
                        data: totalSolarLine,
                        borderColor: COLOR_SOC_HALO,
                        borderWidth: 6,
                        pointRadius: 0,
                        tension: 0.2,
                        spanGaps: MAX_LINE_GAP_MS,
                        yAxisID: "ySolar",
                        order: 3,
                    },
                    {
                        type: "line",
                        label: "Total solar (kW)",
                        data: totalSolarLine,
                        borderColor: COLOR_TOTAL_SOLAR,
                        backgroundColor: COLOR_TOTAL_SOLAR,
                        borderWidth: 3,
                        pointRadius: 0,
                        tension: 0.2,
                        spanGaps: MAX_LINE_GAP_MS,
                        yAxisID: "ySolar",
                        order: 2,
                    },
                    {
                        // Dark halo drawn behind the SoC line for contrast.
                        type: "line",
                        label: "_soc_halo",
                        data: socLine,
                        borderColor: COLOR_SOC_HALO,
                        borderWidth: 6,
                        pointRadius: 0,
                        tension: 0.2,
                        spanGaps: MAX_LINE_GAP_MS,
                        yAxisID: "ySoc",
                        order: 1,
                    },
                    {
                        type: "line",
                        label: "Battery SoC %",
                        data: socLine,
                        borderColor: COLOR_SOC,
                        backgroundColor: COLOR_SOC,
                        borderWidth: 3,
                        pointRadius: 0,
                        tension: 0.2,
                        spanGaps: MAX_LINE_GAP_MS,
                        yAxisID: "ySoc",
                        order: 0,
                    },
                    {
                        // Dark halo behind the car-SoC line for contrast.
                        type: "line",
                        label: "_ev_soc_halo",
                        data: evSocLine,
                        borderColor: COLOR_SOC_HALO,
                        borderWidth: 5,
                        pointRadius: 0,
                        tension: 0.2,
                        spanGaps: MAX_LINE_GAP_MS,
                        yAxisID: "ySoc",
                        order: 1,
                    },
                    {
                        type: "line",
                        label: "Car SoC %",
                        data: evSocLine,
                        borderColor: COLOR_EV_SOC,
                        backgroundColor: COLOR_EV_SOC,
                        borderWidth: 2.5,
                        pointRadius: 0,
                        tension: 0.2,
                        spanGaps: MAX_LINE_GAP_MS,
                        yAxisID: "ySoc",
                        order: 0,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: "nearest", axis: "x", intersect: false },
                plugins: {
                    legend: {
                        labels: {
                            color: TEXT_BODY,
                            // Hide internal "_"-prefixed datasets (e.g. SoC halo).
                            filter: (item) => !String(item.text).startsWith("_"),
                        },
                    },
                    tooltip: {
                        // Same suppression for tooltip lines.
                        filter: (item) => !String(item.dataset.label || "").startsWith("_"),
                        callbacks: {
                            title: (items) => {
                                const d = new Date(items[0].parsed.x);
                                return d.getFullYear() + "-" +
                                       String(d.getMonth() + 1).padStart(2, "0") + "-" +
                                       String(d.getDate()).padStart(2, "0") + " " +
                                       String(d.getHours()).padStart(2, "0") + ":" +
                                       String(d.getMinutes()).padStart(2, "0");
                            },
                            label: (ctx) => {
                                if (ctx.dataset.yAxisID === "yPrice") {
                                    const signed = (ctx.raw && ctx.raw._raw != null)
                                        ? ctx.raw._raw : ctx.parsed.y;
                                    return `Injection ${signed.toFixed(4)} €/kWh`;
                                }
                                if (ctx.dataset.yAxisID === "ySolar") {
                                    const name = ctx.dataset.label.replace(/ \(kW\)$/, "");
                                    return `${name} ${ctx.parsed.y.toFixed(2)} kW`;
                                }
                                // Battery/car SoC share this axis; use the
                                // dataset label (minus its trailing " %") so the
                                // tooltip names which line you're hovering.
                                const socName = String(ctx.dataset.label || "SoC").replace(/ %$/, "");
                                return `${socName} ${ctx.parsed.y.toFixed(1)} %`;
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        type: "time",
                        time: { unit: "hour", tooltipFormat: "HH:mm" },
                        min: startOfDay.valueOf(),
                        max: endOfDay.valueOf(),
                        title: {
                            display: true,
                            text: dayLabel,
                            color: TEXT_MUTED,
                            padding: { top: 6 },
                        },
                        ticks: { color: TEXT_MUTED, maxRotation: 0, autoSkip: true },
                        grid: { color: "rgba(148, 163, 184, 0.10)" },
                    },
                    yPrice: {
                        position: "left",
                        title: { display: true, text: "€/kWh", color: TEXT_MUTED },
                        ticks: { color: TEXT_MUTED },
                        // Gridlines are anchored to the 0–100% SoC axis instead,
                        // so the horizontal lines read as SoC levels, not prices.
                        grid: { display: false },
                    },
                    ySoc: {
                        position: "right",
                        min: 0,
                        max: 100,
                        title: { display: true, text: "SoC %", color: TEXT_MUTED },
                        ticks: { color: TEXT_MUTED },
                        // Owns the horizontal gridlines (0/20/.../100%) — the
                        // price axis no longer draws them.
                        grid: { display: true, color: GRID_FAINT },
                    },
                    ySolar: {
                        position: "right",
                        min: 0,
                        max: 10,
                        title: { display: true, text: "Solar kW", color: TEXT_MUTED },
                        ticks: { color: TEXT_MUTED },
                        grid: { display: false },
                    },
                },
            },
        });
    }

    function updateChart(prices, readings, solarPoints) {
        if (!chart) return;
        const {
            priceBars, priceColors, priceBorderWidths, socLine, evSocLine, solarLine, totalSolarLine,
        } = buildChartData(prices, readings, solarPoints);
        // Datasets:
        //   0 = price bars
        //   1 = forecast solar (filled)
        //   2 = total-solar halo
        //   3 = total-solar line
        //   4 = SoC halo
        //   5 = battery SoC line
        //   6 = car-SoC halo
        //   7 = car SoC line
        chart.data.datasets[0].data = priceBars;
        chart.data.datasets[0].backgroundColor = priceColors;
        chart.data.datasets[0].borderWidth = priceBorderWidths;
        chart.data.datasets[1].data = solarLine;
        chart.data.datasets[2].data = totalSolarLine;
        chart.data.datasets[3].data = totalSolarLine;
        chart.data.datasets[4].data = socLine;
        chart.data.datasets[5].data = socLine;
        chart.data.datasets[6].data = evSocLine;
        chart.data.datasets[7].data = evSocLine;
        // Re-bind the x-axis window to the viewed day so prev/next nav
        // actually pans the chart instead of stretching today's bars.
        const startOfDay = new Date(viewedDate);
        const endOfDay = new Date(startOfDay);
        endOfDay.setDate(endOfDay.getDate() + 1);
        chart.options.scales.x.min = startOfDay.valueOf();
        chart.options.scales.x.max = endOfDay.valueOf();
        chart.options.scales.x.title.text = fmtDateYMD(startOfDay);
        chart.update("none");
    }

    // Pull the latest payload for one source name out of the /api/state
    // response. Returns {} when the source hasn't reported yet so callers
    // can do `.power_w ?? null` without null-checking the wrapper.
    function sourcePayload(state, name) {
        const sources = (state && state.sources) || [];
        const row = sources.find((s) => s && s.source_name === name);
        return (row && row.last_payload) || {};
    }

    function fmtAge(seconds) {
        if (seconds == null) return null;
        const m = Math.round(seconds / 60);
        if (m < 1) return "just now";
        if (m < 60) return m + " min ago";
        const h = Math.floor(m / 60);
        return h + "h" + String(m % 60).padStart(2, "0") + " ago";
    }

    // Tronity (EQS) SoC — the third main tile in the state card. Prefers the
    // live cache (state.vehicle); falls back to the last-known value persisted
    // on the reading. The meta line exposes the trust signals — record age
    // (flagged when stale) and whether the car is within the home geofence — so
    // a laggy/away SoC reads as exactly that.
    function applyVehicleTile(vehicle, reading) {
        const soc = (vehicle && vehicle.soc_pct != null)
            ? vehicle.soc_pct
            : (reading && reading.ev_soc_pct != null ? reading.ev_soc_pct : null);
        // Estimated range rides alongside the SoC (only the live cache carries
        // it — the persisted reading has SoC only), e.g. "75% · 320 km".
        const rangeKm = vehicle && vehicle.range_km != null ? vehicle.range_km : null;
        const socText = soc != null ? Math.round(soc) + "%" : "—";
        setText("state-ev-soc",
            rangeKm != null ? `${socText} · ${Math.round(rangeKm)} km` : socText);
        const metaEl = document.getElementById("state-ev-meta");
        if (!metaEl) return;
        if (!vehicle) {
            metaEl.textContent = reading && reading.ev_soc_pct != null ? "" : "not configured";
            return;
        }
        const parts = [];
        const age = fmtAge(vehicle.age_s);
        if (age) parts.push(vehicle.fresh ? age : age + " (stale)");
        if (vehicle.at_home === true) parts.push("At home");
        else if (vehicle.at_home === false) parts.push("Away");
        if (vehicle.charging) parts.push(String(vehicle.charging).toLowerCase());
        metaEl.textContent = parts.length ? parts.join(" · ") : "—";
    }

    function applyState(state) {
        const reading = state.reading || {};
        const decision = state.decision;
        const override = state.override || { mode: "auto", expires_at: null };

        // Display-side sign flip — the API's storage convention is unchanged
        // (battery_power_w positive = discharging, grid_feed_in_w positive =
        // exporting), but the dashboard tiles render the inverse so positive
        // means: charging / generating / importing from grid.
        const neg = (v) => (v == null ? null : -v);
        setText("tile-soc", reading.battery_soc_pct != null
            ? reading.battery_soc_pct.toFixed(1) + "%" : "—");
        applyVehicleTile(state.vehicle, reading);
        setText("tile-batt-power", fmtInt(neg(reading.battery_power_w), " W"));
        setText("tile-house", fmtInt(reading.house_consumption_w, " W"));
        applyChargerTile(state, reading);
        applyEtrelStateLine(state);
        applyChargerControlDecision(state);
        applySolarTile(reading.small_solar_w, reading.large_solar_w);
        setText("tile-grid", fmtInt(neg(reading.grid_feed_in_w), " W"));

        const card = document.getElementById("state-card");
        if (card) {
            card.classList.remove("state-on", "state-off", "state-unknown");
            const cls = decision && decision.state === "on" ? "state-on"
                : decision && decision.state === "off" ? "state-off" : "state-unknown";
            card.classList.add(cls);
        }
        if (decision) {
            setHidden("state-empty-headline", true);
            setHidden("state-headline", false);
            setHidden("state-rule", false);
            setHidden("state-when", false);
            setText("state-text", String(decision.state || "").toUpperCase());
            setText("state-rule", decision.rule_fired || "");
            setText("state-reason", decision.reason || "");
            setText("state-when", "decided " + fmtTimeFull(decision.timestamp));
        } else {
            setHidden("state-empty-headline", false);
            setHidden("state-headline", true);
            setHidden("state-rule", true);
            setHidden("state-when", true);
            setText("state-reason",
                "The orchestrator tick loop hasn't produced a decision — " +
                "populate config.yaml and start the service.");
        }

        const flag = document.getElementById("state-override-flag");
        if (flag) {
            const active = override.mode && override.mode !== "auto";
            if (active) {
                setText("state-override-mode", override.mode);
                flag.removeAttribute("hidden");
            } else {
                flag.setAttribute("hidden", "");
            }
        }

        applySolarEdgeLimit(state, decision);
    }

    // Read-only "is the inverter actually obeying us?" indicator. Surfaces the
    // live SolarEdge Active Power Limit register (0xF001) and whether it matches
    // what the engine commanded. SolarEdge quirk: 0xF001 has a sub-second
    // watchdog and reverts to 100 % unless refreshed continuously, so while the
    // decision is OFF this currently reads "reverted to 100 % — not curtailing".
    // Once the inverter is configured to hold the limit it flips to "holding".
    //
    // `actuated` distinguishes a live read from the value the engine just *wrote*:
    // an actuation status carries the target (not a read-back), so we never claim
    // "holding" off a write we haven't re-read — that would flash green falsely on
    // every decision tick.
    function applySolarEdgeLimit(state, decision) {
        const line = document.getElementById("se-limit-line");
        const badge = document.getElementById("se-limit-status");
        if (!line) return;
        const payload = sourcePayload(state, "solaredge");
        const pct = payload.active_power_limit_pct;
        if (pct == null) {
            setText("se-limit-pct", "—");
            if (badge) badge.setAttribute("hidden", "");
            line.removeAttribute("hidden");
            return;
        }
        const p = Math.round(pct);
        setText("se-limit-pct", p + " %");
        line.removeAttribute("hidden");
        if (!badge) return;
        badge.classList.remove("se-limit-ok", "se-limit-warn", "se-limit-muted");
        const st = decision && decision.state;
        if (payload.actuated === true) {
            badge.textContent = "commanded — read pending";
            badge.classList.add("se-limit-muted");
            badge.removeAttribute("hidden");
            return;
        }
        if (!st) { badge.setAttribute("hidden", ""); return; }
        const expected = st === "off" ? 0 : 100;
        const holding = Math.abs(p - expected) <= 1;
        if (st === "off" && holding) {
            badge.textContent = "✓ holding (curtailed)";
            badge.classList.add("se-limit-ok");
        } else if (st === "off") {
            badge.textContent = "⚠ reverted to " + p + " % — not curtailing";
            badge.classList.add("se-limit-warn");
        } else if (holding) {
            badge.textContent = "released";
            badge.classList.add("se-limit-muted");
        } else {
            badge.textContent = "⚠ limited to " + p + " % — engine wants 100 %";
            badge.classList.add("se-limit-warn");
        }
        badge.removeAttribute("hidden");
    }

    function applySolarToday(solarJson) {
        const wh = solarJson && solarJson.watt_hours_today;
        setText("chart-title-solar-today",
            wh != null ? (wh / 1000.0).toFixed(1) + " kWh" : "—");
    }

    // Pull "now"'s injection price straight from the day-ahead price array
    // instead of the latest Reading row. A stale tick loop (network glitch on
    // the Pi) would otherwise leave the title pinned to whatever hour the
    // last reading was written in.
    function applyCurrentHourPrice(prices) {
        const now = new Date();
        const point = (prices || []).find(p => currentHourMatches(p.timestamp, now));
        setText("chart-title-injection-price",
            fmtNum(point ? point.injection_eur_per_kwh : null, 4, " €/kWh"));
    }

    function buildChartUrls() {
        // /api/state always reflects the live device snapshot — independent
        // of the chart's date selector, since the tiles + state card show
        // current readings even while the user is browsing history.
        if (isViewingToday()) {
            return {
                history: "/api/history?h=24",
                prices: "/api/prices",
                solar: "/api/solar",
            };
        }
        const d = encodeURIComponent(fmtDateYMD(viewedDate));
        return {
            history: `/api/history?h=24&date=${d}`,
            prices: `/api/prices?date=${d}`,
            solar: `/api/solar?date=${d}`,
        };
    }

    async function refreshAll() {
        // Midnight rollover: if we're auto-tracking and the wall clock has
        // moved into a new local day, advance viewedDate before fetching so
        // the new day's data is requested (and so the x-axis re-bounds).
        if (autoTrackToday && !isViewingToday()) {
            viewedDate = startOfLocalDay(new Date());
            applyNavUi();
        }
        const urls = buildChartUrls();
        try {
            const [state, history, priceJson, solarJson] = await Promise.all([
                fetchJson("/api/state"),
                fetchJson(urls.history),
                fetchJson(urls.prices),
                fetchJson(urls.solar),
            ]);
            applyState(state);
            applySolarToday(solarJson);
            applyCurrentHourPrice(priceJson.prices || []);
            updateChart(priceJson.prices || [], history.readings || [],
                        (solarJson && solarJson.points) || []);
            // Only the today view's /api/prices returns the full window that
            // includes tomorrow; recompute availability there and refresh the
            // Next button so a freshly-published tomorrow unlocks it without a
            // manual nav action.
            if (isViewingToday()) {
                tomorrowAvailable = pricesCoverTomorrow(priceJson.prices || []);
                applyNavUi();
            }
            const status = document.getElementById("dashboard-refresh-status");
            if (status) {
                if (isViewingToday()) {
                    status.textContent = `Live — last refresh ${fmtTimeShort(new Date().toISOString())}.`;
                } else {
                    status.textContent = `Showing ${fmtDateYMD(viewedDate)} — live polling paused.`;
                }
            }
        } catch (e) {
            console.warn("Dashboard refresh failed", e);
            const status = document.getElementById("dashboard-refresh-status");
            if (status) status.textContent = "Refresh failed — retrying.";
        }
    }

    /* Chart.js 4 has a built-in time scale but needs a date adapter. To stay
     * vendored / no-CDN, ship a minimal Date-based adapter inline — enough
     * for hour-resolution tick formatting. */
    function installDateAdapter() {
        if (!window.Chart || !window.Chart._adapters || !window.Chart._adapters._date) return;
        window.Chart._adapters._date.override({
            formats: () => ({
                datetime: "HH:mm",
                hour: "HH:mm",
                minute: "HH:mm",
                second: "HH:mm:ss",
                millisecond: "HH:mm:ss.SSS",
                day: "yyyy-MM-dd",
                week: "yyyy-MM-dd",
                month: "yyyy-MM",
                quarter: "yyyy-Q",
                year: "yyyy",
            }),
            parse: (v) => {
                if (v instanceof Date) return v.valueOf();
                if (typeof v === "string") return new Date(v).valueOf();
                if (typeof v === "number") return v;
                return null;
            },
            format: (ts, fmt) => {
                const d = new Date(ts);
                if (fmt === "HH:mm") {
                    return d.getHours().toString().padStart(2, "0") + ":" +
                           d.getMinutes().toString().padStart(2, "0");
                }
                if (fmt === "yyyy-MM-dd") {
                    return d.getFullYear() + "-" +
                           (d.getMonth() + 1).toString().padStart(2, "0") + "-" +
                           d.getDate().toString().padStart(2, "0");
                }
                return d.toISOString();
            },
            add: (ts, amount, unit) => {
                const d = new Date(ts);
                if (unit === "hour") d.setHours(d.getHours() + amount);
                else if (unit === "minute") d.setMinutes(d.getMinutes() + amount);
                else if (unit === "day") d.setDate(d.getDate() + amount);
                else if (unit === "month") d.setMonth(d.getMonth() + amount);
                else if (unit === "year") d.setFullYear(d.getFullYear() + amount);
                else d.setTime(d.getTime() + amount);
                return d.valueOf();
            },
            diff: (a, b, unit) => {
                const ms = a - b;
                if (unit === "hour") return ms / 3_600_000;
                if (unit === "minute") return ms / 60_000;
                if (unit === "day") return ms / 86_400_000;
                return ms;
            },
            startOf: (ts, unit) => {
                const d = new Date(ts);
                if (unit === "hour") { d.setMinutes(0, 0, 0); }
                else if (unit === "day") { d.setHours(0, 0, 0, 0); }
                return d.valueOf();
            },
            endOf: (ts, unit) => {
                const d = new Date(ts);
                if (unit === "hour") { d.setMinutes(59, 59, 999); }
                else if (unit === "day") { d.setHours(23, 59, 59, 999); }
                return d.valueOf();
            },
        });
    }

    function startPolling() {
        if (pollTimer == null) pollTimer = setInterval(refreshAll, REFRESH_MS);
    }
    function stopPolling() {
        if (pollTimer != null) { clearInterval(pollTimer); pollTimer = null; }
    }

    function applyNavUi() {
        const dateEl = document.getElementById("chart-date");
        if (dateEl) dateEl.textContent = fmtDateYMD(viewedDate);
        const nextBtn = document.getElementById("chart-next");
        if (nextBtn) {
            // Enabled up to maxNavigableDay() — today, or tomorrow once its
            // day-ahead prices have loaded. Beyond that there's no data, so the
            // button is disabled rather than navigating into an empty future.
            nextBtn.disabled = !canGoNext();
        }
        const todayBtn = document.getElementById("chart-today");
        if (todayBtn) todayBtn.disabled = isViewingToday();
    }

    async function navigateTo(newDate) {
        viewedDate = startOfLocalDay(newDate);
        // Resume midnight auto-rollover only when the destination is today —
        // a manual jump to a past day means the user wants to stay there
        // until they click Today (or Next back to today).
        autoTrackToday = isViewingToday();
        applyNavUi();
        // Polling only makes sense while looking at today — past days are
        // immutable. Stop it explicitly before fetching, then restart only
        // if the new date is today.
        stopPolling();
        await refreshAll();
        if (isViewingToday() && !document.hidden) {
            startPolling();
        }
    }

    function wireChartNav() {
        const prev = document.getElementById("chart-prev");
        const today = document.getElementById("chart-today");
        const next = document.getElementById("chart-next");
        if (prev) prev.addEventListener("click", () => {
            const d = new Date(viewedDate);
            d.setDate(d.getDate() - 1);
            navigateTo(d);
        });
        if (today) today.addEventListener("click", () => {
            navigateTo(new Date());
        });
        if (next) next.addEventListener("click", () => {
            const d = new Date(viewedDate);
            d.setDate(d.getDate() + 1);
            // Cap at maxNavigableDay() (today, or tomorrow once its prices are
            // in) — the disabled-state check on the button is belt-and-braces;
            // this is the actual guard.
            if (d.getTime() > maxNavigableDay().getTime()) return;
            navigateTo(d);
        });
        applyNavUi();
    }

    // The Etrel tile's two control buttons both POST /api/charger/mode:
    //   Force     → { mode:"forced", amps }  sticky setpoint, immediate write
    //   Optimized → { mode:"optimized" }     hand control to the rule engine
    // FORCED ignores solar/battery (16 A cap only) and holds until Optimized.
    function wireChargerModeControls() {
        const forceBtn = document.getElementById("etrel-force-btn");
        const optBtn = document.getElementById("etrel-optimized-btn");
        const input = document.getElementById("etrel-set-current-input");
        const status = document.getElementById("etrel-set-current-status");
        if (!forceBtn || !optBtn || !input) return;

        async function postMode(body, busyLabel) {
            forceBtn.disabled = true;
            optBtn.disabled = true;
            if (status) status.textContent = busyLabel;
            try {
                const resp = await fetch("/api/charger/mode", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                });
                if (!resp.ok) {
                    const detail = await resp.text();
                    throw new Error(`HTTP ${resp.status}: ${detail}`);
                }
                return await resp.json();
            } finally {
                forceBtn.disabled = false;
                optBtn.disabled = false;
            }
        }

        forceBtn.addEventListener("click", async () => {
            const amps = Number(input.value);
            // Safety hard cap at 16 A — the API also enforces this; the
            // client-side check is just for a friendlier error.
            if (!Number.isFinite(amps) || amps < 0 || amps > 16) {
                if (status) status.textContent = "0–16 A only";
                return;
            }
            try {
                const data = await postMode({ mode: "forced", amps }, "Forcing…");
                if (status) status.textContent = `Forced · ${formatSetCurrentResult(data)}`;
            } catch (e) {
                if (status) status.textContent = `Failed: ${e.message}`;
            }
        });

        optBtn.addEventListener("click", async () => {
            try {
                await postMode({ mode: "optimized" }, "Switching…");
                if (status) status.textContent = "Optimized — algorithm in control";
            } catch (e) {
                if (status) status.textContent = `Failed: ${e.message}`;
            }
        });
    }

    // Render the structured /api/etrel/set-current response as a one-liner.
    // Three cases the user actually cares about:
    //   1. Write ACKed and readback agrees → "Sent N A (readback M A)".
    //   2. Write ACK was lost but readback shows our value → "ACK timed out
    //      but readback N A — write took silently". This is the case Etrel
    //      firmware exhibits when the Sonnen cluster channel is active.
    //   3. Write failed and readback disagrees → "Write failed; readback M A".
    // Tolerance below matches the device's float-precision noise floor; the
    // API enforces a hard 16 A cap and rounds to whole amps in practice.
    function formatSetCurrentResult(data) {
        const requested = data.amps_requested;
        const after = data.set_current_a_after;
        const readbackTxt = data.readback_error
            ? `readback failed: ${data.readback_error}`
            : (after != null ? `readback ${after.toFixed(2)} A` : "readback —");
        if (data.write_succeeded) {
            return `Sent ${requested} A · ${readbackTxt}`;
        }
        const tookSilently = (
            after != null && Math.abs(after - requested) < 0.1
        );
        if (tookSilently) {
            return `ACK lost but ${readbackTxt} — write took silently`;
        }
        return `Write failed: ${data.write_error || "unknown"} · ${readbackTxt}`;
    }

    // SolarEdge tile probe button: POST /api/solaredge/test-toggle, which
    // writes the inverter's active-power-limit register directly (0 %/100 %),
    // bypassing the engine and dry-run, so the user can confirm the hardware
    // physically obeys curtailment. Not sticky — the tick loop re-asserts the
    // engine's decision on its next decision tick when dry-run is off.
    function wireSolarEdgeTestControl() {
        const btn = document.getElementById("se-toggle-btn");
        const status = document.getElementById("se-test-status");
        if (!btn) return;

        btn.addEventListener("click", async () => {
            btn.disabled = true;
            if (status) status.textContent = "Sending…";
            try {
                const resp = await fetch("/api/solaredge/test-toggle", { method: "POST" });
                if (!resp.ok) {
                    const detail = await resp.text();
                    throw new Error(`HTTP ${resp.status}: ${detail}`);
                }
                const data = await resp.json();
                if (status) status.textContent = formatSolarEdgeTestResult(data);
            } catch (e) {
                if (status) status.textContent = `Failed: ${e.message}`;
            } finally {
                btn.disabled = false;
            }
        });
    }

    // TEMPORARY (remove me): summarise the one-shot Advanced Power Control
    // enable that the probe runs before curtailing. Drop this helper and its
    // call below once the installer commits APC in SetApp — see solaredge.py.
    //   already on → "APC on"; just turned on → "APC enabled+committed";
    //   failed/rejected → "APC enable failed: …".
    function formatApcSegment(apc) {
        if (!apc) return "";
        if (apc.error) return ` · APC enable failed: ${apc.error}`;
        if (apc.already_enabled) return " · APC on";
        if (apc.committed && apc.enabled_now) return " · APC enabled+committed";
        if (!apc.enabled_now) return " · APC still off (enable rejected)";
        return "";
    }

    // Render the structured /api/solaredge/test-toggle response as a one-liner.
    //   1. Write OK, register reads the target → "Sent OFF (0 %) · register now 0 %".
    //      If the panels keep producing despite this, Advanced Power Control
    //      isn't committed on the inverter (writes land but aren't enforced).
    //   2. Write failed (read-back mismatch / unreachable) → "Tried … write failed: …".
    function formatSolarEdgeTestResult(data) {
        const stateTxt = String(data.target_state || "").toUpperCase();
        const target = data.target_pct;
        const after = data.active_power_limit_pct_after;
        const readbackTxt = data.readback_error
            ? `readback failed: ${data.readback_error}`
            : (after != null ? `register now ${after} %` : "register —");
        // TEMPORARY: APC enable status — remove with the one-shot APC probe.
        const apcTxt = formatApcSegment(data.advanced_power_control);
        if (data.write_succeeded) {
            // The write arms a short hold so the engine's self-healing
            // reconciliation doesn't snap the register back before you can
            // watch production respond.
            const holdTxt = data.hold_seconds ? ` · holding ${data.hold_seconds}s` : "";
            return `Sent ${stateTxt} (${target} %) · ${readbackTxt}${holdTxt}${apcTxt}`;
        }
        return `Tried ${stateTxt} (${target} %) · write failed: ${data.write_error || "unknown"} · ${readbackTxt}${apcTxt}`;
    }

    async function init() {
        installDateAdapter();
        const canvas = document.getElementById("mainChart");
        if (!canvas) return;

        wireChartNav();
        wireChargerModeControls();
        wireSolarEdgeTestControl();

        const urls = buildChartUrls();
        let prices = [];
        let history = { readings: [] };
        let solarPoints = [];
        try {
            const [priceJson, historyJson, solarJson] = await Promise.all([
                fetchJson(urls.prices),
                fetchJson(urls.history),
                fetchJson(urls.solar),
            ]);
            prices = priceJson.prices || [];
            history = historyJson;
            solarPoints = (solarJson && solarJson.points) || [];
        } catch (e) {
            console.warn("Dashboard chart data fetch failed", e);
        }

        const readings = history.readings || [];
        if (prices.length > 0 || readings.length > 0 || solarPoints.length > 0) {
            chart = renderCombined(canvas, prices, readings, solarPoints);
        } else {
            showEmpty(canvas, "No prices, SoC history, or solar forecast yet — waiting for tick loop.");
        }

        // Pause polling while the tab is hidden — the user already gets
        // a fresh render on their next focus via the manual `refreshAll`
        // we trigger on visibilitychange.
        document.addEventListener("visibilitychange", () => {
            if (document.hidden) {
                stopPolling();
            } else {
                refreshAll();
                if (isViewingToday()) startPolling();
            }
        });
        if (isViewingToday()) startPolling();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();

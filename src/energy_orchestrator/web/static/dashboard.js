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

    // Price bars: muted grey when injection price is positive, light green
    // when negative. Bar heights are plotted as |price| so negative hours
    // still rise from zero — the sign is conveyed by colour, not direction.
    const COLOR_PRICE_POS = "rgba(148, 163, 184, 0.55)";
    const COLOR_PRICE_NEG = "rgba(134, 239, 172, 0.75)";
    // SoC: brighter green-400 instead of green-500 — pops against the muted
    // gray price bars and the translucent orange solar fill.
    const COLOR_SOC = "#4ade80";
    // Halo drawn underneath the SoC line so it stays legible where it crosses
    // bars or the solar fill.
    const COLOR_SOC_HALO = "rgba(2, 6, 23, 0.85)";
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
    // Polling timer handle so the nav code can pause refreshes when looking
    // at a non-today day (the data is frozen there).
    let pollTimer = null;

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

    function fmtTimeFull(iso) {
        if (!iso) return "";
        const d = new Date(iso);
        if (isNaN(d.getTime())) return String(iso);
        return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
               `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
    }

    function fmtTimeShort(iso) {
        if (!iso) return "";
        const d = new Date(iso);
        if (isNaN(d.getTime())) return String(iso);
        return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
    }

    function fmtTimeHM(iso) {
        if (!iso) return "";
        const d = new Date(iso);
        if (isNaN(d.getTime())) return String(iso);
        return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
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

    function buildChartData(prices, readings, solarPoints) {
        // Bars plot |price| so negative hours rise from zero like positive
        // ones; sign is conveyed by colour (red = positive, green = negative).
        // _raw carries the signed value through to the tooltip.
        const priceBars = prices.map(p => ({
            x: new Date(p.timestamp).valueOf(),
            y: Math.abs(p.injection_eur_per_kwh),
            _raw: p.injection_eur_per_kwh,
        }));
        const priceColors = prices.map(p =>
            p.injection_eur_per_kwh < 0 ? COLOR_PRICE_NEG : COLOR_PRICE_POS
        );

        // Keep readings without a SoC sample but emit a null y so the line
        // breaks at the gap (spanGaps with MAX_LINE_GAP_MS additionally
        // breaks across time gaps where no reading exists at all).
        const socLine = readings.map(r => ({
            x: new Date(r.timestamp).valueOf(),
            y: r.battery_soc_pct == null ? null : r.battery_soc_pct,
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

        return { priceBars, priceColors, socLine, solarLine, totalSolarLine };
    }

    function renderCombined(canvas, prices, readings, solarPoints) {
        // Lock the x-axis to the viewed local-day's 00:00 -> 24:00 window.
        const startOfDay = new Date(viewedDate);
        const endOfDay = new Date(startOfDay);
        endOfDay.setDate(endOfDay.getDate() + 1);
        const dayLabel = fmtDateYMD(startOfDay);

        const { priceBars, priceColors, socLine, solarLine, totalSolarLine } =
            buildChartData(prices, readings, solarPoints);

        return new Chart(canvas, {
            data: {
                datasets: [
                    {
                        type: "bar",
                        label: "Injection €/kWh",
                        data: priceBars,
                        backgroundColor: priceColors,
                        // Transparent border eats 2 px from each side of the
                        // bar — keeps the slim look we had before the colour
                        // refactor without re-adding a visible outline.
                        borderColor: "transparent",
                        borderWidth: 3,
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
                                return `SoC ${ctx.parsed.y.toFixed(1)} %`;
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
                        grid: { color: GRID_FAINT },
                    },
                    ySoc: {
                        position: "right",
                        min: 0,
                        max: 100,
                        title: { display: true, text: "SoC %", color: TEXT_MUTED },
                        ticks: { color: TEXT_MUTED },
                        grid: { display: false },
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
        const { priceBars, priceColors, socLine, solarLine, totalSolarLine } =
            buildChartData(prices, readings, solarPoints);
        // Datasets:
        //   0 = price bars
        //   1 = forecast solar (filled)
        //   2 = total-solar halo
        //   3 = total-solar line
        //   4 = SoC halo
        //   5 = SoC line
        chart.data.datasets[0].data = priceBars;
        chart.data.datasets[0].backgroundColor = priceColors;
        chart.data.datasets[1].data = solarLine;
        chart.data.datasets[2].data = totalSolarLine;
        chart.data.datasets[3].data = totalSolarLine;
        chart.data.datasets[4].data = socLine;
        chart.data.datasets[5].data = socLine;
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
        setText("chart-title-soc", reading.battery_soc_pct != null
            ? reading.battery_soc_pct.toFixed(1) + "%" : "—");
        setText("chart-title-injection-price",
            fmtNum(reading.injection_price_eur_per_kwh, 4, " €/kWh"));
        setText("tile-batt-power", fmtInt(neg(reading.battery_power_w), " W"));
        setText("tile-house", fmtInt(reading.house_consumption_w, " W"));
        applyChargerTile(state, reading);
        applyEtrelStateLine(state);
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
    }

    function applySolarToday(solarJson) {
        const wh = solarJson && solarJson.watt_hours_today;
        setText("chart-title-solar-today",
            wh != null ? (wh / 1000.0).toFixed(1) + " kWh" : "—");
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
            updateChart(priceJson.prices || [], history.readings || [],
                        (solarJson && solarJson.points) || []);
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
            // Past today there's no recorded data — disable the Next button
            // so users don't navigate into an empty future. They can still
            // jump back via Today / Prev.
            nextBtn.disabled = isViewingToday();
        }
        const todayBtn = document.getElementById("chart-today");
        if (todayBtn) todayBtn.disabled = isViewingToday();
    }

    async function navigateTo(newDate) {
        viewedDate = startOfLocalDay(newDate);
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
            // Cap at today — the disabled-when-today check on the button is
            // belt-and-braces; this is the actual guard.
            const startOfToday = startOfLocalDay(new Date());
            if (d.getTime() > startOfToday.getTime()) return;
            navigateTo(d);
        });
        applyNavUi();
    }

    function wireEtrelSetCurrent() {
        const btn = document.getElementById("etrel-set-current-btn");
        const input = document.getElementById("etrel-set-current-input");
        const status = document.getElementById("etrel-set-current-status");
        if (!btn || !input) return;
        btn.addEventListener("click", async () => {
            const amps = Number(input.value);
            // Safety hard cap at 16 A — the API also enforces this; the
            // client-side check is just for a friendlier error.
            if (!Number.isFinite(amps) || amps < 0 || amps > 16) {
                if (status) status.textContent = "0–16 A only";
                return;
            }
            btn.disabled = true;
            if (status) status.textContent = "Sending…";
            try {
                const resp = await fetch("/api/etrel/set-current", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ amps }),
                });
                if (!resp.ok) {
                    const detail = await resp.text();
                    throw new Error(`HTTP ${resp.status}: ${detail}`);
                }
                const data = await resp.json();
                if (status) status.textContent = formatSetCurrentResult(data);
            } catch (e) {
                if (status) status.textContent = `Failed: ${e.message}`;
            } finally {
                btn.disabled = false;
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

    function wireEtrelDump() {
        const btn = document.getElementById("etrel-dump-btn");
        const status = document.getElementById("etrel-set-current-status");
        if (!btn) return;
        btn.addEventListener("click", async () => {
            btn.disabled = true;
            if (status) status.textContent = "Dumping…";
            try {
                const resp = await fetch("/api/etrel/diagnostic-dump", { method: "POST" });
                if (!resp.ok) {
                    const detail = await resp.text();
                    throw new Error(`HTTP ${resp.status}: ${detail}`);
                }
                if (status) status.textContent = "Dumped — see log";
            } catch (e) {
                if (status) status.textContent = `Dump failed: ${e.message}`;
            } finally {
                btn.disabled = false;
            }
        });
    }

    async function init() {
        installDateAdapter();
        const canvas = document.getElementById("mainChart");
        if (!canvas) return;

        wireChartNav();
        wireEtrelSetCurrent();
        wireEtrelDump();

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

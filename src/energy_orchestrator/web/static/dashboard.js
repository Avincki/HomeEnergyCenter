/* Dashboard chart: day-ahead injection-price bars + battery SoC line on
 * one canvas with dual axes. Reads /api/prices and /api/history?h=24.
 * No CDN — the orchestrator runs on a home LAN, so all assets ship locally.
 */
(() => {
    "use strict";

    const COLOR_ACCENT = "#38bdf8";
    const COLOR_MUTED = "rgba(148, 163, 184, 0.55)";
    const COLOR_NEG = "rgba(239, 68, 68, 0.75)";
    const COLOR_CURRENT = "rgba(56, 189, 248, 0.85)";
    const COLOR_SOC = "#22c55e";
    const COLOR_SOLAR_FILL = "rgba(251, 146, 60, 0.30)";
    const COLOR_SOLAR_LINE = "rgba(251, 146, 60, 0.85)";
    const TEXT_MUTED = "#94a3b8";
    const TEXT_BODY = "#e2e8f0";
    const GRID_FAINT = "rgba(148, 163, 184, 0.15)";

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

    function renderCombined(canvas, prices, readings, solarPoints) {
        const now = new Date();

        // Lock the x-axis to today's local 00:00 -> 24:00 window.
        const startOfToday = new Date(now);
        startOfToday.setHours(0, 0, 0, 0);
        const endOfToday = new Date(startOfToday);
        endOfToday.setDate(endOfToday.getDate() + 1);
        const todayLabel =
            startOfToday.getFullYear() + "-" +
            String(startOfToday.getMonth() + 1).padStart(2, "0") + "-" +
            String(startOfToday.getDate()).padStart(2, "0");

        const priceBars = prices.map(p => ({
            x: new Date(p.timestamp).valueOf(),
            y: p.injection_eur_per_kwh,
        }));
        const priceColors = prices.map(p => {
            if (p.injection_eur_per_kwh < 0) return COLOR_NEG;
            if (currentHourMatches(p.timestamp, now)) return COLOR_CURRENT;
            return COLOR_MUTED;
        });
        const priceBorders = prices.map(p =>
            currentHourMatches(p.timestamp, now) ? COLOR_ACCENT : "transparent"
        );

        const socLine = readings
            .filter(r => r.battery_soc_pct !== null && r.battery_soc_pct !== undefined)
            .map(r => ({
                x: new Date(r.timestamp).valueOf(),
                y: r.battery_soc_pct,
            }));

        const solarLine = (solarPoints || []).map(p => ({
            x: new Date(p.timestamp).valueOf(),
            y: p.watts / 1000.0,  // chart axis is in kW for readable numbers
        }));

        return new Chart(canvas, {
            data: {
                datasets: [
                    {
                        type: "bar",
                        label: "Injection €/kWh",
                        data: priceBars,
                        backgroundColor: priceColors,
                        borderColor: priceBorders,
                        borderWidth: 2,
                        yAxisID: "yPrice",
                        // 1 hour wide — matches the price-point hour resolution.
                        barThickness: "flex",
                        order: 3,
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
                        order: 2,
                    },
                    {
                        type: "line",
                        label: "Battery SoC %",
                        data: socLine,
                        borderColor: COLOR_SOC,
                        backgroundColor: COLOR_SOC,
                        borderWidth: 2,
                        pointRadius: 0,
                        tension: 0.2,
                        spanGaps: true,
                        yAxisID: "ySoc",
                        order: 1,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: "nearest", axis: "x", intersect: false },
                plugins: {
                    legend: { labels: { color: TEXT_BODY } },
                    tooltip: {
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
                                    return `Injection ${ctx.parsed.y.toFixed(4)} €/kWh`;
                                }
                                if (ctx.dataset.yAxisID === "ySolar") {
                                    return `Solar ${ctx.parsed.y.toFixed(2)} kW`;
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
                        min: startOfToday.valueOf(),
                        max: endOfToday.valueOf(),
                        title: {
                            display: true,
                            text: todayLabel,
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
                        title: { display: true, text: "Solar kW", color: TEXT_MUTED },
                        ticks: { color: TEXT_MUTED },
                        grid: { display: false },
                    },
                },
            },
        });
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

    async function init() {
        installDateAdapter();
        const canvas = document.getElementById("mainChart");
        if (!canvas) return;

        let prices = [];
        let history = { readings: [] };
        let solarPoints = [];
        try {
            const [priceJson, historyJson, solarJson] = await Promise.all([
                fetchJson("/api/prices"),
                fetchJson("/api/history?h=24"),
                fetchJson("/api/solar"),
            ]);
            prices = priceJson.prices || [];
            history = historyJson;
            solarPoints = (solarJson && solarJson.points) || [];
        } catch (e) {
            console.warn("Dashboard chart data fetch failed", e);
        }

        const readings = history.readings || [];
        if (prices.length === 0 && readings.length === 0 && solarPoints.length === 0) {
            showEmpty(canvas, "No prices, SoC history, or solar forecast yet — waiting for tick loop.");
            return;
        }
        renderCombined(canvas, prices, readings, solarPoints);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();

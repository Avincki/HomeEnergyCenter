/* HomeEnergyCenter service worker.
 *
 * Bumps Chrome over the PWA-installability bar (manifest + SW + secure
 * context) and gives the dashboard shell a single-cache offline fallback
 * so the kiosk doesn't show a blank page during a brief LAN blip.
 *
 * v2 fixes a Windows-Chrome bug: when the SW's network fetch timed out,
 * the old fallback returned the '/' HTML for CSS/JS/SVG subresources,
 * which Chrome refused as a stylesheet/script (MIME mismatch) and the
 * page rendered unstyled. The fetch handler only serves the HTML shell
 * to navigation requests; subresources fail cleanly (never the '/' HTML)
 * if both cache and network miss.
 *
 * v3 switches subresources from stale-while-revalidate to network-first
 * (cache fallback). Stale-while-revalidate served the cached CSS/JS first
 * and only refreshed the cache in the background, so a fresh deploy showed
 * up a load late — on an iOS standalone PWA (no easy reload) that read as
 * "the app never updates" even though live /api data kept flowing.
 * Network-first means a deploy appears on the next load; the cached copy
 * remains as an offline fallback. Bumping CACHE forces this worker to
 * install and drop the v2 cache.
 *
 * The live data API is never cached — it must always reach the server.
 */

const CACHE = "eo-shell-v4";
const PRECACHE = [
    "/",
    "/static/style.css",
    "/static/dashboard.js",
    "/static/vendor/chart.umd.min.js",
    "/static/icon.svg",
    "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(CACHE)
            .then((c) => c.addAll(PRECACHE))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener("activate", (event) => {
    event.waitUntil((async () => {
        const keys = await caches.keys();
        await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
        await self.clients.claim();
    })());
});

self.addEventListener("fetch", (event) => {
    const req = event.request;
    if (req.method !== "GET") return;
    const url = new URL(req.url);
    if (url.origin !== self.location.origin) return;
    // Live data must always hit the network — don't intercept /api/*.
    if (url.pathname.startsWith("/api/")) return;

    if (req.mode === "navigate") {
        // Network-first for the document so live decisions stay fresh;
        // fall back to the cached shell during a LAN blip.
        event.respondWith(
            fetch(req)
                .then((resp) => {
                    if (resp.ok) {
                        const copy = resp.clone();
                        caches.open(CACHE).then((c) => c.put("/", copy)).catch(() => {});
                    }
                    return resp;
                })
                .catch(() => caches.match("/"))
        );
        return;
    }

    // Subresources (CSS/JS/SVG): network-first so a fresh deploy shows up on
    // the next load instead of lagging a load behind. Falls back to the
    // cached copy keyed to the exact request — never the '/' HTML, which
    // Chrome would refuse as CSS/JS and render the page unstyled — so the
    // offline shell still renders during a LAN blip.
    event.respondWith(
        fetch(req)
            .then((resp) => {
                if (resp.ok) {
                    const copy = resp.clone();
                    caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
                }
                return resp;
            })
            .catch(() => caches.match(req))
    );
});

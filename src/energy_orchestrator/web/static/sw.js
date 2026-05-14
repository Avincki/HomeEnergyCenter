/* HomeEnergyCenter service worker.
 *
 * Bumps Chrome over the PWA-installability bar (manifest + SW + secure
 * context) and gives the dashboard shell a single-cache offline fallback
 * so the kiosk doesn't show a blank page during a brief LAN blip.
 *
 * v2 fixes a Windows-Chrome bug: when the SW's network fetch timed out,
 * the old fallback returned the '/' HTML for CSS/JS/SVG subresources,
 * which Chrome refused as a stylesheet/script (MIME mismatch) and the
 * page rendered unstyled. The new fetch handler only serves the HTML
 * shell to navigation requests; subresources use stale-while-revalidate
 * against a precached shell and fail cleanly if both cache and network
 * miss.
 *
 * The live data API is never cached — it must always reach the server.
 */

const CACHE = "eo-shell-v2";
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

    // Subresources: stale-while-revalidate. Never fall back to '/' HTML —
    // Chrome would refuse text/html as CSS/JS and render the page unstyled.
    event.respondWith(
        caches.match(req).then((cached) => {
            const networkFetch = fetch(req).then((resp) => {
                if (resp.ok) {
                    const copy = resp.clone();
                    caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
                }
                return resp;
            });
            if (cached) {
                networkFetch.catch(() => {});
                return cached;
            }
            return networkFetch;
        })
    );
});

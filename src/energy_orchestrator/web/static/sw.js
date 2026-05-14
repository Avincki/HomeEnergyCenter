/* HomeEnergyCenter service worker.
 *
 * Bumps Chrome over the PWA-installability bar (manifest + SW + secure
 * context) and gives the dashboard shell a single-cache offline fallback,
 * so the kiosk doesn't show a blank page during a brief LAN blip. The live
 * data API is never cached — it must always reach the server.
 */

const CACHE = "eo-shell-v1";
const SHELL = ["/"];

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(CACHE)
            .then((c) => c.addAll(SHELL))
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
    // Live data must always hit the network — don't intercept /api/*.
    if (url.pathname.startsWith("/api/")) return;
    event.respondWith(
        fetch(req)
            .then((resp) => {
                // Cache same-origin successful GETs so the shell survives
                // a brief offline window. Skip opaque responses.
                if (resp.ok && url.origin === self.location.origin) {
                    const copy = resp.clone();
                    caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
                }
                return resp;
            })
            .catch(() => caches.match(req).then((r) => r || caches.match("/")))
    );
});

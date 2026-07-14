/* Vivo Count service worker.
 *
 * Strategy:
 *   - Shell + static JS/CSS: cache-first (works fully offline).
 *   - API calls (/vivo-count/api/*): network-only; offline handling is
 *     IndexedDB queueing in app.js, not response caching.
 *
 * Cache version is bumped per release to force shell update.
 */
const VERSION = 'vivo-count-v6';
const SHELL = [
    '/vivo-count/pwa',
    '/vivo-count/pwa/manifest.webmanifest',
    '/vivo_stock_count/static/pwa/styles.css',
    '/vivo_stock_count/static/pwa/app.js',
    '/vivo_stock_count/static/pwa/api.js',
    '/vivo_stock_count/static/pwa/idb.js',
    '/vivo_stock_count/static/pwa/scanner.js',
    '/vivo_stock_count/static/pwa/icon.svg',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)))
        ).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);
    // Never intercept API calls — they must surface network errors so the
    // app can queue them in IndexedDB.
    if (url.pathname.startsWith('/vivo-count/api/')) {
        return;
    }
    if (event.request.method !== 'GET') {
        return;
    }
    event.respondWith(
        caches.match(event.request).then((cached) => {
            if (cached) return cached;
            return fetch(event.request).then((resp) => {
                if (
                    resp.ok &&
                    (url.pathname.startsWith('/vivo_stock_count/static/pwa/') ||
                        url.pathname === '/vivo-count/pwa')
                ) {
                    const clone = resp.clone();
                    caches.open(VERSION).then((c) => c.put(event.request, clone));
                }
                return resp;
            }).catch(() => cached || new Response('Offline', { status: 503 }));
        })
    );
});

// Paydown Service Worker — root scope ('/').
// Pre-caches the static app shell and serves GET /api/* with network-first +
// offline cache fallback while strictly bypassing mutations and Plaid OAuth flows.

const VERSION = '__VERSION__';
const SHELL_CACHE = `paydown-shell-${VERSION}`;
const DATA_CACHE = `paydown-data-${VERSION}`;

const PRECACHE_URLS = [
  '/',
  '/index.html',
  '/offline.html',
  '/manifest.webmanifest',
  '/app.css',
  '/components.css',
  '/pages.css',
  '/app.js',
  '/api.js',
  '/util.js',
  '/charts.js',
  '/plaid.js',
  '/views/today.js',
  '/views/month.js',
  '/views/accounts.js',
  '/views/budget.js',
  '/views/subscriptions.js',
  '/views/plan.js',
  '/views/settings.js',
  '/icons/icon.svg',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/icon-maskable-512.png',
  '/icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => Promise.all(
        PRECACHE_URLS.map((u) =>
          fetch(u, { credentials: 'include' })
            .then((res) => (res.ok ? cache.put(u, res) : null))
            .catch(() => null)
        )
      ))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys
        .filter((k) => k !== SHELL_CACHE && k !== DATA_CACHE)
        .map((k) => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Never intercept Plaid OAuth redirect or link-token/exchange endpoints.
  if (url.pathname.startsWith('/plaid-oauth') || url.pathname.startsWith('/api/plaid/')) {
    return;
  }

  // Read-only API calls: network-first with cached fallback for offline viewing.
  if (url.pathname.startsWith('/api/')) {
    if (url.pathname === '/api/export') return;
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(DATA_CACHE).then((cache) => cache.put(req, copy));
          }
          return res;
        })
        .catch(async () => {
          const cached = await caches.match(req);
          if (cached) return cached;
          return new Response(
            JSON.stringify({ detail: 'Offline — no cached data available yet.' }),
            { status: 503, headers: { 'Content-Type': 'application/json' } }
          );
        })
    );
    return;
  }

  // HTML navigations: network-first, fall back to cached '/' shell, then '/offline.html'.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(SHELL_CACHE).then((cache) => cache.put('/', copy));
          }
          return res;
        })
        .catch(async () => {
          return (await caches.match(req)) ||
                 (await caches.match('/')) ||
                 (await caches.match('/index.html')) ||
                 (await caches.match('/offline.html'));
        })
    );
    return;
  }

  // Static shell assets (JS, CSS, icons, manifest): network-first so updates land immediately,
  // falling back to precache when offline.
  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(SHELL_CACHE).then((cache) => cache.put(req, copy));
        }
        return res;
      })
      .catch(async () => {
        const cached = await caches.match(req);
        if (cached) return cached;
        throw new Error('offline');
      })
  );
});

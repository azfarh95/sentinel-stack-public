// Sentinel Finance — service worker
// Network-first cache for the major Mini App pages so they stay viewable
// when the device is briefly offline (e.g. lift, basement, plane).
//
// Cached path prefixes:
//   /                           — home
//   /balance_sheet              — IAS 1 statement
//   /income_statement           — YTD + prior year
//   /cash_forecast              — 90-day projection
//   /drill/                     — every drill page
//   /static/                    — icons, manifest, privacy.js/css
//
// Strategy: network-first. On success: serve fresh + populate cache.
// On failure: serve cached copy. Navigation fallback: last cached home.

const CACHE = 'sentinel-v16';
const FALLBACK_URL = '/balance_sheet';
// Paths cached for offline use. NOTE: '/' (home) is intentionally excluded —
// Home shows live counts (Pending Reconciliation glance) that must never go
// stale. Same for the pending drill; the server now emits Cache-Control:
// no-store on those endpoints, but we belt-and-suspenders here too.
const CACHED_PREFIXES = [
  '/static/',
  '/balance_sheet',
  '/income_statement',
  '/cash_forecast',
];
const NEVER_CACHE_EXACT = ['/'];
const NEVER_CACHE_PREFIXES = ['/income_statement/category', '/drill/pending'];

function shouldCache(url) {
  if (NEVER_CACHE_EXACT.includes(url.pathname)) return false;
  for (const p of NEVER_CACHE_PREFIXES) {
    if (url.pathname.startsWith(p)) return false;
  }
  for (const p of CACHED_PREFIXES) {
    if (url.pathname.startsWith(p)) return true;
  }
  return false;
}

self.addEventListener('install', (e) => {
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;
  if (!shouldCache(url)) return;

  e.respondWith((async () => {
    try {
      const network = await fetch(e.request);
      if (network.ok) {
        const cache = await caches.open(CACHE);
        cache.put(e.request, network.clone());
      }
      return network;
    } catch (err) {
      const cached = await caches.match(e.request);
      if (cached) return cached;
      if (e.request.mode === 'navigate') {
        const fallback = await caches.match(FALLBACK_URL);
        if (fallback) return fallback;
      }
      throw err;
    }
  })());
});

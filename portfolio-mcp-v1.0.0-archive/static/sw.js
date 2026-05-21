// Sentinel Finance — service worker
// Caches the last balance_sheet response for offline view.

const CACHE = 'sentinel-v10';
const FALLBACK_URL = '/balance_sheet';

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
  const url = new URL(e.request.url);
  // Only cache same-origin GETs for the balance sheet
  if (e.request.method !== 'GET') return;
  if (url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith('/balance_sheet') && !url.pathname.startsWith('/static/')) return;

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
      // Fall back to cached balance sheet for navigation requests
      if (e.request.mode === 'navigate') {
        const fallback = await caches.match(FALLBACK_URL);
        if (fallback) return fallback;
      }
      throw err;
    }
  })());
});

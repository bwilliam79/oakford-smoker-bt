const CACHE_NAME = 'bt-smoker-v2';
const urlsToCache = [
  '/',
  '/index.html',
  '/favicon.svg',
  '/icon-192.png',
  '/icon-512.png',
  '/apple-touch-icon.png',
  '/manifest.json'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(urlsToCache))
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => Promise.all(
      cacheNames.map(name => name !== CACHE_NAME ? caches.delete(name) : null)
    ))
  );
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;

  const url = new URL(event.request.url);

  // WebSocket upgrades are not handled by service workers; let them pass.
  if (url.pathname === '/ws') return;

  // API: always go to network so live temperature data is fresh. No cache
  // fallback — serving a stale /api/state would make the UI lie about the
  // current smoker state.
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // Static shell: cache-first, refresh in background.
  event.respondWith(
    caches.match(event.request).then(cached => {
      const networkFetch = fetch(event.request).then(response => {
        if (response && response.status === 200) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone))
            .catch(err => console.warn('cache put failed', err));
        }
        return response;
      }).catch(() => cached);
      return cached || networkFetch;
    })
  );
});

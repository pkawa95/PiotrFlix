/* Piotrflix SW – proste strategie i offline fallback */
const VERSION = 'pf-v1.0.0';
const CORE = [
  '/', '/offline.html',
  '/static/favicon.ico',
  '/static/icon-192.png', '/static/icon-512.png'
];

self.addEventListener('install', e => {
  self.skipWaiting();
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(CORE)));
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    // usuń stare cache
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k)));
    // włącz navigation preload (szybsze pierwsze wejście)
    if ('navigationPreload' in self.registration) {
      await self.registration.navigationPreload.enable();
    }
    await self.clients.claim();
  })());
});

/* Strategie:
 * - nawigacja: network-first -> cache -> offline.html
 * - statyki (img/script/style/font): cache-first
 * - API GET: network-first z fallbackiem do cache, POST/PUT itp. idą w sieć
 */
self.addEventListener('fetch', e => {
  const req = e.request;
  const url = new URL(req.url);
  const isGET = req.method === 'GET';

  // nie dotykaj innych originów
  if (url.origin !== location.origin) return;

  // Nawigacje (klik w link/refresh/adres)
  if (req.mode === 'navigate') {
    e.respondWith((async () => {
      try {
        const prel = await e.preloadResponse;
        if (prel) return prel;
        const net = await fetch(req);
        const cache = await caches.open(VERSION);
        cache.put('/', net.clone());
        return net;
      } catch {
        const cache = await caches.open(VERSION);
        return (await cache.match('/offline.html')) || new Response('Offline', {status: 503});
      }
    })());
    return;
  }

  // Statyki
  const dest = req.destination;
  if (['style','script','image','font'].includes(dest)) {
    e.respondWith((async () => {
      const cache = await caches.open(VERSION);
      const hit = await cache.match(req);
      if (hit) return hit;
      const net = await fetch(req);
      cache.put(req, net.clone());
      return net;
    })());
    return;
  }

  // API GET
  if (isGET && url.pathname.startsWith('/')) {
    e.respondWith((async () => {
      try {
        const net = await fetch(req);
        // cache tylko małe/krótkie odpowiedzi
        const clone = net.clone();
        const cache = await caches.open(VERSION);
        cache.put(req, clone);
        return net;
      } catch {
        const cache = await caches.open(VERSION);
        const hit = await cache.match(req);
        return hit || new Response(JSON.stringify({offline:true}), {headers:{'Content-Type':'application/json'}});
      }
    })());
  }
});

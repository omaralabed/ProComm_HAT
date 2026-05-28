/* ProComm Phone — Service Worker
   Caches the phone UI for fast loads on the local network.
   Network-first strategy so the Pi always serves fresh JS/CSS. */

const CACHE = 'procomm-phone-v5';
const PRECACHE = [
  '/phone',
  '/static/css/phone.css',
  '/static/js/phone.js',
  '/static/js/socket.io.min.js',
  '/static/manifest.json'
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => Promise.allSettled(PRECACHE.map(url => c.add(url))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  // Pass through non-GET and socket.io polling/websocket requests
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/socket.io')) return;
  if (url.pathname.startsWith('/api/')) return;
  if (url.pathname === '/qr') return;

  // Network-first: try live Pi, fall back to cache
  e.respondWith(
    fetch(e.request)
      .then(resp => {
        const clone = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return resp;
      })
      .catch(() => caches.match(e.request))
  );
});

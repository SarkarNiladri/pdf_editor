/* PDF Suite service worker.
 *
 * Caches the app shell (pages, CSS, JS, icons, fonts) so pages you have already
 * opened still load without a connection. It never caches PDF uploads, API calls
 * or anything that is not a plain GET, so documents stay on the device and in
 * memory only.
 *
 * CACHE_VERSION is filled in by the server from a hash of the shell files, so
 * every deploy that changes them gets a fresh cache and old ones are deleted.
 */
const CACHE_VERSION = '__CACHE_VERSION__';
const SHELL_CACHE = `pdf-suite-shell-${CACHE_VERSION}`;
const FONT_CACHE = 'pdf-suite-fonts-v1';
const OFFLINE_URL = '/static/offline.html';

const SHELL_URLS = [
  '/',
  '/edit-existing-text',
  '/converter',
  '/static/theme.css',
  '/static/pwa.js',
  '/static/image-tools.js',
  '/static/offline.html',
  '/manifest.webmanifest',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png',
  '/static/icons/favicon-32.png',
];

const FONT_HOSTS = ['fonts.googleapis.com', 'fonts.gstatic.com'];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then(cache => cache.addAll(SHELL_URLS.map(url => new Request(url, { cache: 'reload' }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys
        .filter(key => key.startsWith('pdf-suite-shell-') && key !== SHELL_CACHE)
        .map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

// Never cache: anything that is not GET (uploads are POST), API routes, and
// responses that are not plain successful ones.
function isCacheable(request, url) {
  if (request.method !== 'GET') return false;
  if (url.origin === self.location.origin) {
    return !url.pathname.startsWith('/api/') && url.pathname !== '/sw.js';
  }
  return FONT_HOSTS.includes(url.hostname);
}

self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);
  if (!isCacheable(request, url)) return;   // let the browser handle it normally

  if (FONT_HOSTS.includes(url.hostname)) {
    event.respondWith(cacheFirst(request, FONT_CACHE));
  } else if (request.mode === 'navigate') {
    event.respondWith(networkFirstPage(request));
  } else {
    event.respondWith(staleWhileRevalidate(request));
  }
});

// Pages: always try the network so a deploy shows up straight away; fall back to
// the cached copy, then to the offline page.
async function networkFirstPage(request) {
  const cache = await caches.open(SHELL_CACHE);
  try {
    const response = await fetch(request);
    if (response.ok && response.type === 'basic') {
      cache.put(stripSearch(request), response.clone());
    }
    return response;
  } catch (err) {
    return (await cache.match(request, { ignoreSearch: true }))
      || (await cache.match(OFFLINE_URL))
      || Response.error();
  }
}

// CSS, JS and icons: answer from the cache at once, refresh it in the background.
async function staleWhileRevalidate(request) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(request);
  const network = fetch(request).then(response => {
    if (response.ok && response.type === 'basic') cache.put(request, response.clone());
    return response;
  }).catch(() => cached || Response.error());
  return cached || network;
}

// Web fonts never change for a given URL.
async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok || response.type === 'opaque') cache.put(request, response.clone());
    return response;
  } catch (err) {
    return Response.error();
  }
}

function stripSearch(request) {
  const url = new URL(request.url);
  url.search = '';
  return url.toString();
}

self.addEventListener('install', (event) => {
    self.skipWaiting();
    event.waitUntil(
        caches.open('energi-cache').then((cache) => {
            return cache.addAll(['/']);
        })
    );
});

self.addEventListener('fetch', (event) => {
    event.respondWith(
        fetch(event.request).catch(() => caches.match(event.request))
    );
});

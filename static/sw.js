// Minimal service worker for PWA standalone mode.
// No caching strategy — agentchattr requires live connectivity.
self.addEventListener('fetch', function(event) {
  event.respondWith(fetch(event.request));
});

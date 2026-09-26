// Minimal service worker — no offline caching, just a pass-through fetch
// handler. Chrome requires one (even trivial) for the "Add to Home Screen"
// install prompt to appear on Android; iOS Safari doesn't need it (manual
// "Add to Home Screen" via the Share menu works without it), but having it
// doesn't hurt there either.
self.addEventListener("fetch", (event) => {
  event.respondWith(fetch(event.request));
});

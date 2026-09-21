// Line Lab service worker — cache the app shell so it installs and works offline.
const CACHE = "line-lab-v7";
const ASSETS = [
  "./",
  "./index.html",
  "./manifest.json",
  "./icon-192.png",
  "./icon-512.png",
  "./apple-touch-icon.png",
  "./splash-1170x2532.png",
  "./splash-1290x2796.png",
  "./splash-1125x2436.png",
  "./splash-750x1334.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  // Only handle same-origin requests; let fonts/CDN go to network.
  if (url.origin !== self.location.origin) return;
  const isDocument =
    event.request.mode === "navigate" ||
    url.pathname === "/" ||
    url.pathname.endsWith("/index.html");
  event.respondWith(
    (async () => {
      if (isDocument) {
        // Network-first for the page itself: always render the latest UI.
        // (Stale-while-revalidate here left the installed app one deploy behind.)
        try {
          const fresh = await fetch(new Request(event.request, { cache: "no-store" }));
          if (fresh && fresh.ok) {
            const copy = fresh.clone();
            caches.open(CACHE).then((cache) => cache.put(event.request, copy));
          }
          return fresh;
        } catch (e) {
          return caches.match(event.request, { ignoreSearch: true });
        }
      }
      // Stale-while-revalidate for static assets (icons, manifest).
      const cached = await caches.match(event.request, { ignoreSearch: false });
      const network = fetch(event.request).then((resp) => {
        if (resp && resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        }
        return resp;
      }).catch(() => cached);
      return cached || network;
    })()
  );
});

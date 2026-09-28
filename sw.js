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

/* ---------- web push (product layer 2026-09-28) ----------
   Caching behavior above is untouched. Payloads come from the Cloudflare
   Worker (built in parallel); the in-app bell feed is the full history,
   push is just the timely ping. data.url is a deep link (./#game-<id>). */
self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = {}; }
  const title = d.title || "Line Lab";
  const body = d.body || "Something moved on the board.";
  const url = d.url || "./";
  event.waitUntil(
    self.registration.showNotification(title, {
      body: body,
      icon: "./icon-192.png",
      badge: "./icon-192.png",
      data: { url: url },
      tag: d.tag || "line-lab",
      renotify: !!d.renotify
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "./";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        if ("focus" in c) {
          c.focus();
          if ("navigate" in c && url !== "./") return c.navigate(url);
          return;
        }
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});

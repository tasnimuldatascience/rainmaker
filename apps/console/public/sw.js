/**
 * Service worker: the app shell must survive a cold reload with no network.
 *
 * WHY THIS IS NOT OPTIONAL. Everything else in this console is built so a rep can keep
 * working offline — the CRDT, IndexedDB, the outbox. All of it is worthless if reloading the
 * tab on a train shows the browser's dinosaur, because the data is right there on the device
 * and the app simply refuses to start. This was caught by the screenshot script, which tried
 * to reload while offline and got ERR_INTERNET_DISCONNECTED.
 *
 * STRATEGY, per request class:
 *
 *   navigation   cache-first on the shell. A local-first app must boot from disk; going to
 *                the network first means every cold start is gated on a round trip.
 *   static asset stale-while-revalidate. Serve instantly, refresh in the background.
 *   /api/*       NETWORK ONLY, never cached. A cached deal list is a stale deal list, and the
 *                local replica is already the offline answer. Caching the API would give the
 *                user a second, worse, silently-outdated source of truth.
 *
 * The API rule is the important one. It is tempting to cache GETs "so offline works", and it
 * is exactly wrong: offline already works, through the replica. A cache here would surface
 * data the CRDT has since superseded.
 */

const VERSION = "rainmaker-v1";
const SHELL = `${VERSION}-shell`;
const ASSETS = `${VERSION}-assets`;

// Only the entry points. Hashed build assets are picked up on first use rather than listed,
// because a hardcoded manifest goes stale the moment the bundle is rebuilt.
const SHELL_URLS = ["/", "/index.html"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL)
      .then((cache) => cache.addAll(SHELL_URLS))
      // Take over immediately. Waiting for every tab to close means a fix ships "eventually",
      // and in a tool people leave open for days that is never.
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Never cache the API. See the module comment: the replica is the offline answer.
  if (url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    event.respondWith(
      caches.match("/index.html").then(
        (cached) =>
          cached ??
          fetch(request).catch(
            () =>
              new Response(
                "<!doctype html><title>Offline</title><p>Rainmaker could not start offline.</p>",
                { headers: { "Content-Type": "text/html" }, status: 503 },
              ),
          ),
      ),
    );
    return;
  }

  event.respondWith(
    caches.open(ASSETS).then(async (cache) => {
      const cached = await cache.match(request);
      const network = fetch(request)
        .then((response) => {
          if (response.ok) cache.put(request, response.clone());
          return response;
        })
        // Offline with nothing cached: let the failure propagate rather than returning a
        // fake 200, so the browser's own error handling applies.
        .catch(() => cached ?? Promise.reject(new Error("offline and uncached")));
      return cached ?? network;
    }),
  );
});

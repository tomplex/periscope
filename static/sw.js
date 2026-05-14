// Minimal service worker.
//
// Existence + reachability of a SW is an installability gate for most
// PWA flows (Chrome's address-bar install button, PWAsForFirefox, Safari
// Add-to-Dock). The runtime behavior we want is: do nothing. periscope
// is a thin client over a local server — caching the shell would only
// serve stale HTML/JS when the server has updated, and offline use is
// meaningless without the FastAPI process running. So this SW intercepts
// no fetches; every request goes to the network exactly as it would
// without a SW.
//
// `skipWaiting` + `clients.claim` make updates land on the next reload
// instead of requiring two reloads (the second reload is normally where
// a waiting SW takes over). With no caching layer this is purely a
// "newer SW replaces older SW promptly" knob.

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

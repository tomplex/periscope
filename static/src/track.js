// UI instrumentation emitter. track(name, detail) buffers a usage event;
// a 5s interval and the unload listeners flush the buffer to POST /api/events
// (navigator.sendBeacon, with a fetch keepalive fallback). Fire-and-forget:
// every failure path is swallowed — instrumentation must never disrupt the UI.
// See docs/superpowers/specs/2026-06-05-ui-instrumentation-design.md.

let buf = [];

export function track(name, detail) {
  buf.push({ name, detail: detail || null, t: Math.floor(Date.now() / 1000) });
  if (buf.length > 500) buf = buf.slice(-500); // cap if the server is down
}

function flush(beacon) {
  if (!buf.length) return;
  const body = JSON.stringify({ events: buf });
  buf = [];
  if (beacon && navigator.sendBeacon) {
    navigator.sendBeacon("/api/events", new Blob([body], { type: "application/json" }));
  } else {
    fetch("/api/events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      keepalive: true,
    }).catch(() => {});
  }
}

// Guarded so importing this module (via util.js) doesn't blow up in
// node-environment vitest, where there's no global addEventListener.
if (typeof addEventListener === "function") {
  setInterval(() => flush(false), 5000);
  addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush(true);
  });
  addEventListener("pagehide", () => flush(true));
}

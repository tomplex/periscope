// The single /api/state poll loop. Any surface (Detail mutation handler,
// Modal action) can force a refresh by calling `poll()`.
//
// Writes the transient signals (`windows`, `projects`, `usage`); the poll
// does NOT commit while a rename or drag is in flight (`editingTarget` /
// `dragState`) — that preserves the user's in-flight input. Connection-
// banner / disconnected handling is preserved (threshold of 2 ≈ 6s).
import {
  windows,
  projects,
  usage,
  editingTarget,
  dragState,
  openFileTabForPane,
} from "./store.js";

const POLL_MS = 3000;

// MCP open_document request ids already applied. The server expires
// requests by TTL rather than acks, so the same id can show up on several
// consecutive polls — dedupe here so a tab opens (and re-activates) once.
const appliedOpenDocs = new Set();

function consumeOpenDocs(ws) {
  for (const w of ws) {
    for (const d of w.channel_open_docs || []) {
      if (appliedOpenDocs.has(d.id)) continue;
      appliedOpenDocs.add(d.id);
      openFileTabForPane(w.pid, w.target, { path: d.path, line: d.line ?? null });
    }
  }
}

// Consecutive failed /api/state polls. Banner shows at ≥2 (≈6s of
// detection) to avoid flicker on a single transient hiccup.
let consecutivePollFails = 0;

function bannerEl() {
  return document.getElementById("connection-banner");
}
function lastUpdateEl() {
  return document.getElementById("last-update");
}

export async function poll() {
  // user is mid-rename / mid-drag; a commit would blow away their input or
  // destroy the drag source.
  if (editingTarget.value) return;
  if (dragState.value) return;
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    windows.value = data.windows || [];
    projects.value = data.projects || [];
    consumeOpenDocs(windows.value);
    // UsagePill reads { scraped, fallback }.
    usage.value = { scraped: data.usage_scrape, fallback: data.usage };
    const lu = lastUpdateEl();
    if (lu) lu.textContent = `updated ${new Date().toLocaleTimeString()}`;
    if (consecutivePollFails > 0) {
      consecutivePollFails = 0;
      const b = bannerEl();
      if (b) b.hidden = true;
      document.body.classList.remove("disconnected");
    }
  } catch (e) {
    consecutivePollFails += 1;
    if (consecutivePollFails >= 2) {
      const b = bannerEl();
      if (b) b.hidden = false;
      document.body.classList.add("disconnected");
    }
    const lu = lastUpdateEl();
    if (lu) lu.textContent = `poll failed: ${e.message}`;
  }
}

// Start the single interval. Returns a teardown that clears it. Guarded so a
// double-mount (StrictMode-style) never spins up two loops.
let started = false;
export function startPolling() {
  if (started) return () => {};
  started = true;
  poll();
  const handle = setInterval(poll, POLL_MS);
  return () => {
    clearInterval(handle);
    started = false;
  };
}

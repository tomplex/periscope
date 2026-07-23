// The single /api/state poll loop. Any surface (Detail mutation handler,
// Modal action) can force a refresh by calling `poll()`.
//
// Writes the transient signals (`windows`, `projects`, `usage`); the poll
// does NOT commit while a rename or drag is in flight (`editingTarget` /
// `dragState`) — that preserves the user's in-flight input. Connection-
// banner / disconnected handling is preserved (threshold of 2 ≈ 6s).
import {
  dragState,
  editingTarget,
  projects,
  syncTabsFromWindows,
  tracks,
  usage,
  windows,
  workspaces,
} from "./store.js";

const POLL_MS = 3000;

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
    workspaces.value = data.workspaces || [];
    tracks.value = data.tracks || [];
    syncTabsFromWindows(windows.value);
    // UsagePill reads { plan, fallback }.
    usage.value = { plan: data.usage_plan, fallback: data.usage };
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

// Start the single loop. Returns a teardown. Chained setTimeout, NOT
// setInterval: the next poll is scheduled only after the current one resolves,
// so a slow response can never overlap — and thus never commit its (older)
// snapshot on top of a newer one. Guarded so a double-mount (StrictMode-style)
// never spins up two loops.
let started = false;
export function startPolling() {
  if (started) return () => {};
  started = true;
  let handle = null;
  let stopped = false;
  async function tick() {
    await poll();   // poll() has its own try/catch — never rejects
    if (!stopped) handle = setTimeout(tick, POLL_MS);
  }
  tick();
  return () => {
    stopped = true;
    if (handle) clearTimeout(handle);
    started = false;
  };
}

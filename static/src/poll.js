// Dashboard state transport. Steady-state updates arrive pushed over
// /ws/state (the server's state hub computes /api/state once and fans it out
// to every tab). The REST /api/state endpoint stays as the fallback when the
// socket is down and as the one-shot forced refresh `poll()`.
//
// Writes the transient signals (`windows`, `projects`, `usage`); never commits
// while a rename or drag is in flight (`editingTarget` / `dragState`) — that
// preserves the user's in-flight input. Connection-banner shows at ≥2
// consecutive REST failures (≈6s); a WS drop alone doesn't trip it because the
// REST fallback keeps data flowing.
import {
  alerts,
  dragState,
  editingTarget,
  projects,
  syncTabsFromWindows,
  tracks,
  updateInfo,
  usage,
  windows,
  workspaces,
} from "./store.js";

const POLL_MS = 3000;
const WS_RETRY_MS = 2000;

// Consecutive failed REST polls. Banner shows at ≥2 to avoid flicker on a
// single transient hiccup.
let consecutivePollFails = 0;

function bannerEl() {
  return document.getElementById("connection-banner");
}
function lastUpdateEl() {
  return document.getElementById("last-update");
}

function applyState(data) {
  // Alerts commit ahead of the rename/drag guard below: they share none of
  // that state, and stalling them would stall the dock badge and native
  // notifications for as long as a drag is held.
  alerts.value = data.alerts || [];
  // user is mid-rename / mid-drag; a commit would blow away their input or
  // destroy the drag source.
  if (editingTarget.value) return;
  if (dragState.value) return;
  windows.value = data.windows || [];
  projects.value = data.projects || [];
  workspaces.value = data.workspaces || [];
  tracks.value = data.tracks || [];
  syncTabsFromWindows(windows.value);
  // UsagePill reads { plan, fallback }.
  usage.value = { plan: data.usage_plan, fallback: data.usage };
  updateInfo.value = data.update || null;
}

function markOk() {
  if (consecutivePollFails > 0) {
    consecutivePollFails = 0;
    const b = bannerEl();
    if (b) b.hidden = true;
    document.body.classList.remove("disconnected");
  }
  const lu = lastUpdateEl();
  if (lu) lu.textContent = `updated ${new Date().toLocaleTimeString()}`;
}

function markFail(msg) {
  consecutivePollFails += 1;
  if (consecutivePollFails >= 2) {
    const b = bannerEl();
    if (b) b.hidden = false;
    document.body.classList.add("disconnected");
  }
  const lu = lastUpdateEl();
  if (lu && msg) lu.textContent = msg;
}

// One-shot REST refresh. Kept for the forced-refresh path and used as the
// fallback poll while the websocket is down.
export async function poll() {
  if (editingTarget.value) return;
  if (dragState.value) return;
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    applyState(await res.json());
    markOk();
  } catch (e) {
    markFail(`poll failed: ${e.message}`);
  }
}

let started = false;
let stopped = false;
let ws = null;
let fallbackTimer = null;
let retryTimer = null;

// REST fallback while the socket is down. Chained setTimeout, NOT setInterval:
// the next poll is scheduled only after the current one resolves, so a slow
// response can't overlap and commit its (older) snapshot over a newer one —
// the T0.8 guarantee, preserved here on the fallback path.
function startFallbackPoll() {
  if (fallbackTimer) return;
  const tick = async () => {
    await poll(); // poll() has its own try/catch — never rejects
    if (fallbackTimer !== null) fallbackTimer = setTimeout(tick, POLL_MS);
  };
  fallbackTimer = setTimeout(tick, 0); // poll now, then self-chain
}
function stopFallbackPoll() {
  if (fallbackTimer) {
    clearTimeout(fallbackTimer);
    fallbackTimer = null;
  }
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const sock = new WebSocket(`${proto}//${location.host}/ws/state`);
  ws = sock;
  sock.onmessage = (e) => {
    stopFallbackPoll(); // socket healthy — drop the REST fallback interval
    try {
      applyState(JSON.parse(e.data));
      markOk();
    } catch (err) {
      markFail(`bad frame: ${err.message}`);
    }
  };
  sock.onclose = () => {
    if (stopped) return;
    ws = null;
    // Keep data flowing via REST while the socket is down, and retry the WS.
    startFallbackPoll();
    if (!retryTimer) {
      retryTimer = setTimeout(() => {
        retryTimer = null;
        connectWs();
      }, WS_RETRY_MS);
    }
  };
  sock.onerror = () => {
    try {
      sock.close();
    } catch {}
  };
}

// Start the state stream. Returns a teardown. Guarded so a double-mount
// (StrictMode-style) never spins up two connections.
export function startPolling() {
  if (started) return () => {};
  started = true;
  stopped = false;
  connectWs();
  return () => {
    stopped = true;
    started = false;
    if (ws) {
      try {
        ws.close();
      } catch {}
      ws = null;
    }
    stopFallbackPoll();
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
  };
}

// "N behind → update" pill in the dashboard header. Hidden entirely when the
// checkout is current, so it costs nothing in the normal case.
//
// THE NAG IS THE POINT. `bin/periscope update` existed as a command long
// before anyone ran it; a coworker daily-driving periscope went many commits
// stale purely because nothing ever surfaced that he was. The pill is the part
// that closes that gap — the click is a convenience on top.
//
// Clicking POSTs /api/update, which spawns a DETACHED updater and returns
// immediately. It cannot report success, because a successful update kills the
// server mid-request: the sequence ends in `launchctl bootout` + `bootstrap`.
// So the two outcomes are read differently:
//
//   success — the server dies, the connection banner appears, and the next
//             successful poll carries behind:0 and the pill vanishes.
//   failure — `git pull --ff-only` aborts BEFORE launchd is touched, so the
//             server is still alive; /api/update/status reports running:false
//             with the reason in the log tail (dirty tree, diverged branch).
//
// That asymmetry is why failure polls the status endpoint and success doesn't
// need to: the absence of the pill IS the success signal.
import { useState } from "preact/hooks";
import { updateInfo } from "../store.js";
import { apiCall } from "../util.js";

const POLL_MS = 2000;
// The updater re-provisions (plist rewrite, bootout/bootstrap, hook install)
// then waits for healthz. Past this, stop polling and let the banner speak.
const GIVE_UP_MS = 120_000;

export function UpdatePill() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const info = updateInfo.value;

  // No info yet (dev instance, or pre-first-check), current, and not mid-run.
  if (!info || (!info.behind && !info.running && !busy && !error)) return null;

  // Poll the status endpoint until the updater exits. If it exits with the
  // server still answering, the update FAILED — surface the log tail.
  async function watch() {
    const deadline = Date.now() + GIVE_UP_MS;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, POLL_MS));
      let st;
      try {
        const res = await fetch("/api/update/status");
        if (!res.ok) continue;
        st = await res.json();
      } catch (_) {
        // Server is down — the expected path for a SUCCESSFUL update. Keep
        // waiting; the restarted server will answer with behind:0.
        continue;
      }
      if (st.running) continue;
      if (st.behind === 0) return setBusy(false);      // landed
      // Exited, still behind, server alive → aborted before touching launchd.
      setBusy(false);
      return setError((st.log || []).filter(Boolean).slice(-3).join("\n")
        || "update exited without applying — see ~/.config/periscope/update.log");
    }
    setBusy(false);
  }

  async function start() {
    setError(null);
    setBusy(true);
    // apiCall already toasts on failure (409 for a dev instance or an update
    // already in flight); a null result means we never started.
    const ok = await apiCall("update", "/api/update", { method: "POST" });
    if (!ok) return setBusy(false);
    watch();
  }

  const running = busy || info.running;
  const title = error
    ? `update failed:\n${error}`
    : running
      ? "updating — periscope will restart itself"
      : `${info.behind} commit${info.behind === 1 ? "" : "s"} behind origin — click to pull, re-provision and restart`;

  return (
    <button
      type="button"
      class={`update-pill${running ? " is-running" : ""}${error ? " is-error" : ""}`}
      title={title}
      disabled={running}
      onClick={running ? undefined : start}
    >
      {running ? "updating…" : error ? "⚠ update failed" : `↑ ${info.behind} behind`}
    </button>
  );
}

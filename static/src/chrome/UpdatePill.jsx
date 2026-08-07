// "N behind → update" pill. Hidden entirely when the checkout is current, so
// it costs nothing in the normal case. See CLAUDE.md > "Updating" for the
// design; the consequence that shapes THIS file is that POST /api/update
// cannot report success — a successful update kills the server mid-request —
// so the two outcomes are read from opposite signals:
//
//   success — server dies, connection banner shows, next poll carries
//             behind:0, pill vanishes. Its ABSENCE is the success signal.
//   failure — the pull aborts before launchd is touched, so the server is
//             still alive and /api/update/status has the reason.
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
    // Never came back. Reverting to "↑ N behind" would imply a healthy server
    // that simply didn't update; the honest reading is that we don't know, and
    // the install may be down (bootout succeeded, bootstrap didn't).
    setBusy(false);
    setError(
      "no response for 2 minutes — periscope may not have come back up.\n" +
        "check `bin/periscope status` and ~/.config/periscope/update.log",
    );
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

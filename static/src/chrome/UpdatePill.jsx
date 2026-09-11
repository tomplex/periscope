// "N behind" pill → popover listing what the update would pull → Update.
// Hidden entirely when the checkout is current, so it costs nothing in the
// normal case. See CLAUDE.md > "Updating" for the design; the consequence that
// shapes THIS file is that POST /api/update cannot report success — a
// successful update kills the server mid-request — so the two outcomes are
// read from opposite signals:
//
//   success — server dies, connection banner shows, next poll carries
//             behind:0, pill vanishes. Its ABSENCE is the success signal.
//   failure — the pull aborts before launchd is touched, so the server is
//             still alive and /api/update/status has the reason.
//
// The commit list is fetched when the popover opens, not carried on
// /api/state: it's only wanted at the moment of deciding, and the 3s poll
// shouldn't grow by thirty subjects.
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import { updateInfo } from "../store.js";
import { apiCall } from "../util.js";

const POLL_MS = 2000;
// The updater re-provisions (plist rewrite, bootout/bootstrap, hook install)
// then waits for healthz. Past this, stop polling and let the banner speak.
const GIVE_UP_MS = 120_000;

// `commits` null = still loading; "error" = the status request failed;
// [] = the server never got a git-log answer (its count came from rev-list,
// so with behind > 0 an empty list is only ever a failed probe, not "nothing").
export function CommitList({ commits, behind }) {
  if (!commits) return <div class="update-commit is-muted">loading…</div>;
  if (commits === "error") return <div class="update-commit is-muted">couldn't load the commit list</div>;
  if (!commits.length) return <div class="update-commit is-muted">no commit list recorded — git log failed on the last check</div>;
  const more = behind - commits.length;
  return (
    <>
      {commits.map((c) => (
        <div class="update-commit" key={c.sha} title={c.subject}>
          <code>{c.sha}</code>
          <span>{c.subject}</span>
        </div>
      ))}
      {more > 0 && <div class="update-commit is-muted">…and {more} more</div>}
    </>
  );
}

export function UpdatePill() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [open, setOpen] = useState(false);
  const [commits, setCommits] = useState(null);
  const ref = useRef(null);
  const close = useCallback(() => setOpen(false), []);
  useEscape(close, open);
  useEffect(() => {
    if (!open) return;
    function onDocClick(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);
  const info = updateInfo.value;
  const behind = info?.behind || 0;
  // The pill can vanish while the popover is open (an external pull lands,
  // behind drops to 0). Close it, or the Escape handler + document listener
  // stay registered against an invisible popover and eat one Escape meant
  // for whatever overlay is underneath.
  useEffect(() => {
    if (!behind) setOpen(false);
  }, [behind]);

  // No info yet (dev instance, or pre-first-check), current, and not mid-run.
  if (!info || (!behind && !info.running && !busy && !error)) return null;

  async function toggle() {
    if (open) return setOpen(false);
    setOpen(true);
    setCommits(null);
    try {
      const res = await fetch("/api/update/status");
      setCommits(res.ok ? (await res.json()).commits || [] : "error");
    } catch (_) {
      setCommits("error");
    }
  }

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
    setOpen(false);
    setError(null);
    setBusy(true);
    // apiCall already toasts on failure (409 for a dev instance or an update
    // already in flight); a null result means we never started.
    const ok = await apiCall("update", "/api/update", { method: "POST" });
    if (!ok) return setBusy(false);
    watch();
  }

  const running = busy || info.running;
  const n = info.behind;
  const plural = `${n} commit${n === 1 ? "" : "s"} behind origin`;
  const title = error
    ? `update failed:\n${error}`
    : running
      ? "updating — periscope will restart itself"
      : `${plural} — click to see what's changing`;

  return (
    <div class="update-dd" ref={ref}>
      <button
        type="button"
        class={`update-pill${running ? " is-running" : ""}${error ? " is-error" : ""}`}
        title={title}
        aria-haspopup="dialog"
        aria-expanded={open ? "true" : "false"}
        disabled={running}
        onClick={running ? undefined : toggle}
      >
        {running ? "updating…" : error ? "⚠ update failed" : `↑ ${n} behind`}
      </button>
      <div class="tb-dd-menu update-menu" role="dialog" hidden={!open}>
        <div class="update-menu-head">{plural}</div>
        {error && <pre class="update-menu-error">{error}</pre>}
        <div class="update-commits">
          <CommitList commits={commits} behind={n} />
        </div>
        <button type="button" class="update-pill update-menu-go" onClick={start}>
          {error ? "retry — " : ""}pull, re-provision and restart
        </button>
      </div>
    </div>
  );
}

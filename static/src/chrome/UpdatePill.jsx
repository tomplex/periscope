// "↑ vX.Y.Z" pill → popover with that release's changelog prose → Update.
// The target is the newest release tag, never main's tip, so the pill only
// appears when someone has cut a release (bin/release) with notes.
// Hidden entirely when the checkout is current, so it costs nothing in the
// normal case. See docs/updating.md for the design; the consequence that
// shapes THIS file is that POST /api/update cannot report success — a
// successful update kills the server mid-request — so the two outcomes are
// read from opposite signals:
//
//   success — server dies, connection banner shows, next poll carries
//             behind:0, pill vanishes. Its ABSENCE is the success signal.
//   failure — the pull aborts before launchd is touched, so the server is
//             still alive and /api/update/status has the reason.
//
// The release notes are fetched when the popover opens, not carried on
// /api/state: they're only wanted at the moment of deciding, and the 3s poll
// shouldn't grow by a changelog.
//
// `ahead` rides on /api/state alongside `behind` because it decides whether the
// button can work AT ALL: the update fast-forwards to the release, which
// refuses once the checkout has commits the release doesn't AND the release
// has moved. Behind-only, the pill
// armed a button that was arithmetically incapable of succeeding and gave no
// reason.
// The button stays ENABLED anyway — the count is up to an hour stale, so an
// external rebase may already have fixed it, and a refused pull aborts before
// launchd is touched (see docs/updating.md > "Ordering is the safety
// property"). Warn, don't block.
import { signal } from "@preact/signals";
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { usePopoverDismiss } from "../hooks/usePopoverDismiss.js";
import { renderMarkdown } from "../split/markdown.jsx";
import { updateInfo } from "../store.js";
import { apiCall, plural } from "../util.js";

const POLL_MS = 2000;

// The popover's open state, at module level so Clawd's parcel
// (chrome/clawdClick.js) opens this same popover — the same commit list and
// button as the pill, never a second, unreviewed way to start an update.
const popoverOpen = signal(false);
let focusOnOpen = false;

// Opened from the mascot, focus moves to the update button: the pill sits far
// earlier in tab order, and the dialog announces nothing.
export function openUpdatePopover({ focus = false } = {}) {
  focusOnOpen = focus;
  popoverOpen.value = true;
}
// The updater re-provisions (plist rewrite, bootout/bootstrap, hook install)
// then waits for healthz. Past this, stop polling and let the banner speak.
const GIVE_UP_MS = 120_000;

// `releases` null = still loading; "error" = the status request failed;
// [] = the server couldn't read the release's CHANGELOG.md (bin/release
// refuses to tag without a section, so with behind > 0 an empty list is only
// ever a failed probe, not "nothing to say").
export function ReleaseNotes({ releases }) {
  if (!releases) return <div class="update-note-muted">loading…</div>;
  if (releases === "error") return <div class="update-note-muted">couldn't load the release notes</div>;
  if (!releases.length) {
    return <div class="update-note-muted">no release notes recorded — CHANGELOG.md was unreadable on the last check</div>;
  }
  return releases.map((r) => (
    <div class="update-release" key={r.heading}>
      <div class="update-release-head">{r.heading}</div>
      <div class="update-release-body">{renderMarkdown(r.body)}</div>
    </div>
  ));
}

export function UpdatePill() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const open = popoverOpen.value;
  const setOpen = (v) => { popoverOpen.value = v; };
  const [releases, setReleases] = useState(null);
  const ref = useRef(null);
  const goRef = useRef(null);
  const close = useCallback(() => { popoverOpen.value = false; }, []);
  usePopoverDismiss(ref, open, close);
  const info = updateInfo.value;
  const behind = info?.behind || 0;
  const ahead = info?.ahead || 0;
  // The pill can vanish while the popover is open (an external pull lands,
  // behind drops to 0). Close it, or the Escape handler + document listener
  // stay registered against an invisible popover and eat one Escape meant
  // for whatever overlay is underneath.
  useEffect(() => {
    if (!behind) setOpen(false);
  }, [behind]);

  // The notes are fetched each time the popover opens, however it was
  // opened (the pill or Clawd).
  useEffect(() => {
    if (!open) return;
    let live = true;
    setReleases(null);
    if (focusOnOpen) {
      focusOnOpen = false;
      goRef.current?.focus();
    }
    fetch("/api/update/status")
      .then(async (res) => { if (live) setReleases(res.ok ? (await res.json()).releases || [] : "error"); })
      .catch(() => { if (live) setReleases("error"); });
    return () => { live = false; };
  }, [open]);

  // No info yet (dev instance, or pre-first-check), current, and not mid-run.
  if (!info || (!behind && !info.running && !busy && !error)) return null;

  function toggle() {
    setOpen(!open);
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
  const target = info.target;
  const behindText = `${target} is out — ${plural(n, "commit")} newer than this checkout`;
  // Only meaningful while the pill is up (behind > 0): ahead alone is a
  // checkout past the newest release, which has nothing to do.
  const blocked = n > 0 && ahead > 0;
  const warn = `${plural(ahead, "local commit")} not in ${target} — the update's fast-forward will refuse. `
    + "Get them into a release, or off this checkout's main.";
  const title = error
    ? `update failed:\n${error}`
    : running
      ? "updating — periscope will restart itself"
      : blocked
        ? `${behindText}, but ${warn}`
        : `${behindText} — click to see what's new`;

  return (
    <div class="pill-dd" ref={ref}>
      <button
        type="button"
        class={`update-pill${running ? " is-running" : ""}${error ? " is-error" : ""}`}
        title={title}
        aria-haspopup="dialog"
        aria-expanded={open ? "true" : "false"}
        disabled={running}
        onClick={running ? undefined : toggle}
      >
        {running ? "updating…" : error ? "⚠ update failed" : `↑ ${target}${blocked ? " ⚠" : ""}`}
      </button>
      <div class="tb-dd-menu update-menu" role="dialog" hidden={!open}>
        <div class="update-menu-head">{behindText}</div>
        {blocked && <div class="update-menu-warn">{warn}</div>}
        {error && <pre class="update-menu-error">{error}</pre>}
        <div class="update-notes">
          <ReleaseNotes releases={releases} />
        </div>
        <button type="button" class="update-pill update-menu-go" ref={goRef} onClick={start}>
          {error ? "retry — " : ""}pull, re-provision and restart
        </button>
      </div>
    </div>
  );
}

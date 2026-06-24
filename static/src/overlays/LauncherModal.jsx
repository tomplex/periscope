// Per-worktree "+ New tab" launcher. Reads prefs.getCommands() and lets the
// user pick one; POSTs to /api/window/new with the worktree's session as the
// target. Ported from static/launcher-modal.js.
//
// /api/window/new takes URL query parameters (not a JSON body): `session` and
// `exec`. The "label" in prefs is purely UI text; the `exec` field is the
// actual shell command to run.
//
// The opener (__periscopeOpenLauncher) takes the worktree key — the Preact
// rail's "+ New tab" row already calls window.__periscopeOpenLauncher(wtKey)
// (see Rail.jsx). This replaces the vanilla bridge of the same name.
//
// Behavior change (per Task 8): the vanilla launcher had NO Escape handling.
// The unified useEscape hook adds Escape-to-close here (noted in the commit).
//
// CSS contract preserved: #launcher-modal / .launcher-modal-overlay / -card /
// -head / -sub / .launcher-list / .launcher-row / .launcher-empty,
// #launcher-session-name.
import { signal } from "@preact/signals";
import { useEffect } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import * as prefs from "../prefs.js";
import { track } from "../track.js";
import { apiCall } from "../util.js";

// The open worktree key (or null). A signal so the singleton modal reacts.
const target = signal(null);

export function openLauncher(worktreeKey) {
  target.value = worktreeKey;
  track("overlay.open", { which: "launcher" });
}
function close() {
  target.value = null;
}

export function LauncherModal() {
  useEscape(close, target.value != null);

  // Register the window bridge so the rail's "+ New tab" row can open it.
  useEffect(() => {
    window.__periscopeOpenLauncher = openLauncher;
    return () => {
      if (window.__periscopeOpenLauncher === openLauncher) delete window.__periscopeOpenLauncher;
    };
  }, []);

  const worktreeKey = target.value;
  if (worktreeKey == null) return null;

  const commands = prefs.getCommands();

  async function run(cmd) {
    const exec = cmd?.exec || "";
    const qs = new URLSearchParams({ session: worktreeKey });
    if (exec) qs.set("exec", exec);
    await apiCall("new window", `/api/window/new?${qs.toString()}`, { method: "POST" });
    close();
  }

  return (
    <div
      id="launcher-modal"
      class="launcher-modal-overlay"
      onClick={(e) => { if (e.target.id === "launcher-modal") close(); }}
    >
      <div class="launcher-modal-card">
        <header class="launcher-modal-head">
          <h2>+ New tab</h2>
          <button id="launcher-close" title="close" onClick={close}>×</button>
        </header>
        <p class="launcher-modal-sub" id="launcher-session-name">Add to session: {worktreeKey}</p>
        <div id="launcher-list">
          {commands.length === 0 ? (
            <div class="launcher-empty">No commands configured. Use Commands settings to add some.</div>
          ) : (
            commands.map((c) => (
              <button key={c.label} class="launcher-row" data-label={c.label} onClick={() => run(c)}>
                {c.label}
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

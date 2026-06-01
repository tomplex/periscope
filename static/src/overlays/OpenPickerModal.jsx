// + open picker for the split-view rail. Lists tmux sessions whose worktree
// isn't already in the rail, grouped by repo. Multi-select; submit calls
// prefs.addWorktreeToRail() once per selected session. Ported from
// static/open-picker-modal.js.
//
// The vanilla version re-rendered the rail with a dynamic import on submit;
// the Preact rail re-renders reactively when addWorktreeToRail mutates the
// prefs signal, so no explicit renderRail() is needed.
//
// Behavior change (per Task 8): the vanilla picker had NO Escape handling.
// The unified useEscape hook adds Escape-to-close here (noted in the commit).
//
// The opener is registered on window (__periscopeOpenPicker) so the rail can
// open it — mirrors the __periscopeOpenLauncher bridge. CSS contract
// preserved: #open-picker-modal / .open-picker-modal-overlay / -card / -head /
// -sub / -actions, .open-picker-repo / -repo-head / -row / -branch / -empty.
import { signal } from "@preact/signals";
import { useEffect, useState } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import * as prefs from "../prefs.js";
import { windows } from "../store.js";

const open = signal(false);

export function openPicker() {
  open.value = true;
}
function close() {
  open.value = false;
}

export function OpenPickerModal() {
  useEscape(close, open.value);
  // selected session names.
  const [selected, setSelected] = useState(() => new Set());

  // Register the window bridge so the rail (or any caller) can open it.
  useEffect(() => {
    window.__periscopeOpenPicker = openPicker;
    return () => {
      if (window.__periscopeOpenPicker === openPicker) delete window.__periscopeOpenPicker;
    };
  }, []);

  useEffect(() => {
    if (open.value) setSelected(new Set());
  }, [open.value]);

  if (!open.value) return null;

  // Group live windows by repo, skipping non-git sessions and already-railed.
  const railed = new Set(Object.values(prefs.getWorktreesByRepo()).flat());
  const grouped = {}; // repo_label → [{session, branch, repo_key}, ...]
  for (const w of (windows.value || [])) {
    if (railed.has(w.session)) continue;
    if (!w.repo_key) continue; // skip non-git sessions for v1
    const k = w.repo_label || w.repo_key;
    (grouped[k] = grouped[k] || []).push({
      session: w.session,
      branch: w.branch,
      repo_key: w.repo_key,
    });
  }
  // De-dupe sessions within a repo group (multiple panes share a session).
  for (const k of Object.keys(grouped)) {
    const seen = new Set();
    grouped[k] = grouped[k].filter((s) => (seen.has(s.session) ? false : seen.add(s.session)));
  }

  function toggle(session, checked) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(session); else next.delete(session);
      return next;
    });
  }

  async function submit() {
    for (const session of selected) {
      // Collect ALL panes for this session, not just one.
      const win = (windows.value || []).find((w) => w.session === session);
      const repoKey = win?.repo_key;
      const sessionPanes = (windows.value || [])
        .filter((w) => w.session === session)
        .map((w) => w.pid);
      await prefs.addWorktreeToRail({
        repoKey,
        worktreeKey: session,
        paneIds: sessionPanes,
        hasReview: true,
      });
    }
    close();
  }

  const groups = Object.entries(grouped);

  return (
    <div
      id="open-picker-modal"
      class="open-picker-modal-overlay"
      onClick={(e) => { if (e.target.id === "open-picker-modal") close(); }}
    >
      <div class="open-picker-modal-card">
        <header class="open-picker-modal-head">
          <h2>+ open</h2>
          <button id="open-picker-close" title="close" onClick={close}>×</button>
        </header>
        <p class="open-picker-modal-sub">
          Pick tmux sessions to add to the rail. Already-railed sessions are hidden.
        </p>
        <div id="open-picker-list">
          {groups.length === 0 ? (
            <div class="open-picker-empty">
              No sessions available to add — every git session is already railed.
            </div>
          ) : (
            groups.map(([label, sessions]) => (
              <div key={label} class="open-picker-repo">
                <div class="open-picker-repo-head">{label}</div>
                {sessions.map((s) => (
                  <label key={s.session} class="open-picker-row">
                    <input
                      type="checkbox"
                      data-session={s.session}
                      data-repo-key={s.repo_key}
                      checked={selected.has(s.session)}
                      onChange={(e) => toggle(s.session, e.currentTarget.checked)}
                    />
                    <span>{s.session}</span>
                    <span class="open-picker-branch">{s.branch || ""}</span>
                  </label>
                ))}
              </div>
            ))
          )}
        </div>
        <div class="open-picker-modal-actions">
          <button type="button" id="open-picker-cancel" onClick={close}>cancel</button>
          <button type="button" id="open-picker-submit" disabled={selected.size === 0} onClick={submit}>
            add ({selected.size})
          </button>
        </div>
      </div>
    </div>
  );
}

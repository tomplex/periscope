// Per-track "+ New tab" launcher. One launcher per track (see Rail.jsx); the
// user first picks WHICH branch the new tab lands in — an existing branch in
// the track, or a brand-new branch (spawns a worktree off the repo default) —
// then the command (claude is the default; shell remains selectable).
//
// /api/window/new takes URL query parameters (not a JSON body): `session` (a
// TRACK id, not a tmux session name), `exec` (the shell command), and two
// optional cwd hints the branch picker drives:
//   - new_branch=<name>  → backend spawns a worktree off the track's repo
//   - cwd=<path>         → land the tab in an existing branch's worktree path
// With neither, the backend uses the track's repo (or ~/dev for a loose track).
//
// The opener (__periscopeOpenLauncher) takes the track id — the rail's "+ New
// tab" row calls window.__periscopeOpenLauncher(trackId) (see Rail.jsx).
//
// CSS contract preserved: #launcher-modal / .launcher-modal-overlay / -card /
// -head / -sub / .launcher-list / .launcher-row / .launcher-empty /
// #launcher-session-name. Branch-picker classes (.launcher-branches /
// .launcher-branch / .launcher-branch-new / -input / .launcher-section) are
// added in styles.css.
import { computed, signal } from "@preact/signals";
import { useEffect } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import * as prefs from "../prefs.js";
import { tracks, windows } from "../store.js";
import { track } from "../track.js";
import { apiCall } from "../util.js";
import { trackLabel } from "../split/railTree.js";

// The open track id (or null). A signal so the singleton modal reacts.
const target = signal(null);
// The picked existing branch (its worktree path is the cwd we POST), or null.
const pickedBranch = signal(null);
// Non-null once the user opts to create a new branch: the typed name (may be "").
const newBranchName = signal(null);

// Distinct branches in `trackId`, each with a representative worktree cwd.
// Pure: exported for unit tests. Skips windows without a branch.
export function trackBranches(trackId, wins) {
  const seen = new Map();   // branch → cwd (first non-empty cwd wins)
  for (const w of (wins || [])) {
    if (w.track_id !== trackId) continue;
    const branch = w.branch;
    if (!branch) continue;
    if (!seen.has(branch)) seen.set(branch, w.cwd || "");
  }
  return [...seen.entries()].map(([branch, cwd]) => ({ branch, cwd }));
}

const branches = computed(() => trackBranches(target.value, windows.value));

export function openLauncher(trackId) {
  target.value = trackId;
  const bs = trackBranches(trackId, windows.value);
  pickedBranch.value = bs.length ? bs[0].branch : null;
  newBranchName.value = null;
  track("overlay.open", { which: "launcher" });
}
function close() {
  target.value = null;
  pickedBranch.value = null;
  newBranchName.value = null;
}

function pickExisting(branch) {
  pickedBranch.value = branch;
  newBranchName.value = null;
}
function startNewBranch() {
  pickedBranch.value = null;
  newBranchName.value = "";
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

  const trackId = target.value;
  if (trackId == null) return null;

  const commands = prefs.getCommands();
  const bs = branches.value;
  // Branch picker shows when the track has live branches OR a repo on its
  // registry row (an EMPTY repo-backed goal track can still "+ new branch…" —
  // the backend spawns the worktree off the track's repo). Loose / repo-less
  // track: command list only.
  const trackRepo = (tracks.value || []).find((t) => t.id === trackId)?.repo || null;
  const showBranchPicker = bs.length > 0 || newBranchName.value != null || !!trackRepo;

  async function run(cmd) {
    const exec = cmd?.exec || "";
    const qs = new URLSearchParams({ session: trackId });
    if (exec) qs.set("exec", exec);
    const nb = newBranchName.value;
    if (nb?.trim()) {
      qs.set("new_branch", nb.trim());
    } else if (pickedBranch.value != null) {
      const hit = bs.find((b) => b.branch === pickedBranch.value);
      if (hit?.cwd) qs.set("cwd", hit.cwd);
    }
    await apiCall("new window", `/api/window/new?${qs.toString()}`, { method: "POST" });
    close();
  }

  // The default command (first in the list — claude by convention); Enter on
  // the new-branch input launches it.
  const defaultCmd = commands[0] || null;

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
        <p class="launcher-modal-sub" id="launcher-session-name">Add to track: {trackLabel(trackId, windows.value, tracks.value)}</p>

        {showBranchPicker && (
          <div class="launcher-section">
            <div class="launcher-section-label">Branch</div>
            <div class="launcher-branches">
              {bs.map((b) => (
                <button
                  key={b.branch}
                  class={`launcher-branch${pickedBranch.value === b.branch ? " is-active" : ""}`}
                  onClick={() => pickExisting(b.branch)}
                >
                  ⎇ {b.branch}
                </button>
              ))}
              {newBranchName.value == null ? (
                <button class="launcher-branch launcher-branch-new" onClick={startNewBranch}>
                  + new branch…
                </button>
              ) : (
                <input
                  class="launcher-branch-input is-active"
                  type="text"
                  placeholder="new-branch-name"
                  // Callback ref instead of the autoFocus attribute (Biome
                  // a11y/noAutofocus): focus the field the moment it mounts so
                  // the user can type the branch name without a second click.
                  ref={(el) => el?.focus()}
                  value={newBranchName.value}
                  onInput={(e) => { newBranchName.value = e.target.value; }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && defaultCmd) run(defaultCmd);
                  }}
                />
              )}
            </div>
          </div>
        )}

        <div class="launcher-section">
          {showBranchPicker && <div class="launcher-section-label">Command</div>}
          <div id="launcher-list">
            {commands.length === 0 ? (
              <div class="launcher-empty">No commands configured. Use Commands settings to add some.</div>
            ) : (
              commands.map((c, i) => (
                <button
                  key={c.label}
                  class={`launcher-row${i === 0 ? " is-default" : ""}`}
                  data-label={c.label}
                  onClick={() => run(c)}
                >
                  {c.label}
                </button>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

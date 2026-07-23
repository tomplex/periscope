// Per-track "+ New tab" launcher. One launcher per track (see Rail.jsx); the
// user first picks WHICH branch the new tab lands in, then the command (claude
// is the default; shell remains selectable).
//
// The branch list spans the track's whole repo, not just what's running:
// live branches (badged), then the repo's other worktrees, then remaining git
// branches with no worktree yet — all from GET /api/open/catalog. Restricting
// it to live branches was the "can't open anything that isn't already open"
// dead end; the omnibox could reach those repos but couldn't aim at a track.
//
// /api/window/new takes URL query parameters (not a JSON body): `session` (a
// TRACK id, not a tmux session name), `exec` (the shell command), and two
// optional cwd hints the branch picker drives:
//   - branch=<name>  → backend resolves it to a worktree, creating one if the
//                      branch has none (checkout when the branch exists, fork
//                      when it doesn't)
//   - cwd=<path>     → land the tab in a worktree path we already know
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
import { trackLabel } from "../split/railTree.js";
import { tracks, windows } from "../store.js";
import { track } from "../track.js";
import { apiCall } from "../util.js";

// The open track id (or null). A signal so the singleton modal reacts.
const target = signal(null);
// The picked existing branch (its worktree path is the cwd we POST), or null.
const pickedBranch = signal(null);
// Non-null once the user opts to create a new branch: the typed name (may be "").
const newBranchName = signal(null);

// Catalog payload (GET /api/open/catalog), or null until it loads. Lets the
// picker offer branches that are NOT currently running — the whole point of
// the launcher: without it, "existing branches" meant only live ones.
const catalog = signal(null);

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

// The branch list the picker renders: live branches first (they carry a real
// cwd), then the repo's other worktrees, then remaining git branches with no
// worktree yet. `live` drives the badge; `cwd` is passed straight through when
// known, otherwise the backend resolves the branch to a worktree.
// Pure: exported for unit tests.
export function pickerBranches(trackId, wins, repo, cat) {
  const out = [];
  const seen = new Set();
  for (const b of trackBranches(trackId, wins)) {
    seen.add(b.branch);
    out.push({ ...b, live: true });
  }
  if (!repo || !cat) return out;
  for (const w of (cat.worktrees || [])) {
    if (w.repo !== repo || !w.branch || seen.has(w.branch)) continue;
    seen.add(w.branch);
    out.push({ branch: w.branch, cwd: w.path, live: false });
  }
  const repoRow = (cat.repos || []).find((r) => r.repo === repo);
  for (const name of (repoRow?.branches || [])) {
    if (!name || seen.has(name)) continue;
    seen.add(name);
    out.push({ branch: name, cwd: "", live: false });
  }
  return out;
}

const branches = computed(() => pickerBranches(
  target.value, windows.value, trackRepoOf(target.value), catalog.value,
));

function trackRepoOf(trackId) {
  return (tracks.value || []).find((t) => t.id === trackId)?.repo || null;
}

export function openLauncher(trackId) {
  target.value = trackId;
  const bs = trackBranches(trackId, windows.value);
  pickedBranch.value = bs.length ? bs[0].branch : null;
  newBranchName.value = null;
  track("overlay.open", { which: "launcher" });
  // Fetch fresh each open: worktrees and branches change outside periscope.
  catalog.value = null;
  apiCall("open catalog", "/api/open/catalog").then((data) => {
    if (target.value === trackId) catalog.value = data;
  });
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
  const trackRepo = trackRepoOf(trackId);
  const showBranchPicker = bs.length > 0 || newBranchName.value != null || !!trackRepo;

  async function run(cmd) {
    const exec = cmd?.exec || "";
    const qs = new URLSearchParams({ session: trackId });
    if (exec) qs.set("exec", exec);
    const nb = newBranchName.value;
    if (nb?.trim()) {
      qs.set("branch", nb.trim());
    } else if (pickedBranch.value != null) {
      const hit = bs.find((b) => b.branch === pickedBranch.value);
      // A known worktree path goes straight through as cwd; a branch without
      // one (never checked out here, or its worktree was removed) is resolved
      // server-side — reused if it exists, created otherwise.
      if (hit?.cwd) qs.set("cwd", hit.cwd);
      else if (hit) qs.set("branch", hit.branch);
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
                  class={`launcher-branch${pickedBranch.value === b.branch ? " is-active" : ""}${b.live ? "" : " is-dormant"}`}
                  title={b.live ? "running now" : (b.cwd || "no worktree yet")}
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

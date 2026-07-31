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
// An optional `account` picks the Claude subscription (see accountQuery).
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
import { ACCOUNTS } from "../accounts.js";
import { bestAccount } from "../chrome/usageSummary.js";
import { useEscape } from "../hooks/useEscape.js";
import * as prefs from "../prefs.js";
import { trackLabel } from "../split/railTree.js";
import { tracks, usage, windows } from "../store.js";
import { track } from "../track.js";
import { apiCall } from "../util.js";

// The open track id (or null). A signal so the singleton modal reacts.
const target = signal(null);
// The picked existing branch (its worktree path is the cwd we POST), or null.
const pickedBranch = signal(null);
// Non-null once the user opts to create a new branch: the typed name (may be "").
const newBranchName = signal(null);
// Branch search text. Empty → the shortlist; non-empty → matches from the full
// list. Transient, cleared on every open.
const branchQuery = signal("");

// Which Claude subscription the new pane runs on ("default" | "b").
const account = signal("default");

// account id → the `account` query param, or null to omit it entirely.
// The server fails OPEN on an unknown id (store.account_config_dir), so a
// param is the risky direction: omitting it keeps the default launch
// byte-identical to the pre-accounts URL.
// Pure: exported for unit tests.
export function accountQuery(acct) {
  return !acct || acct === "default" ? null : acct;
}

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

// The SHORT branch list: the repo's default branch, then whatever is running
// in this track, then the most recent of the rest, capped at `limit`.
//
// The full list is everything `git for-each-ref --sort=-committerdate` returned
// (up to 100). Rendering all of it was the bug this replaces — a repo with 130
// branches filled the entire viewport with chips, so the picker was unusable
// exactly on the repos where it mattered most. Everything not in the shortlist
// stays reachable through the search box.
// Pure: exported for unit tests.
export function shortlistBranches(all, defaultBranch, limit = 5) {
  const out = [];
  const taken = new Set();
  const take = (b) => {
    if (!b || taken.has(b.branch)) return;
    taken.add(b.branch);
    out.push(b);
  };
  // The default branch always holds a slot, even if it's stale and nothing is
  // running on it — it's the one branch you always want one click away.
  take(all.find((b) => b.branch === defaultBranch));
  // A branch with a live pane outranks commit recency: it's what you're on
  // right now, and a long-running pane can sit on an old commit for days.
  for (const b of all) if (b.live) take(b);
  for (const b of all) {
    if (out.length >= limit) break;
    take(b);
  }
  return out;
}

// Substring match over the full branch list, for the search box. Case- and
// separator-insensitive so "qa tool" finds "tc/attribute-qa-tooling".
// Pure: exported for unit tests.
export function filterBranches(all, query) {
  const terms = String(query || "").toLowerCase().split(/[\s/]+/).filter(Boolean);
  if (!terms.length) return [];
  return all.filter((b) => {
    const hay = b.branch.toLowerCase();
    return terms.every((t) => hay.includes(t));
  });
}

// The RUN list: the two built-in agents, then the user's configured commands.
//
// Agent and command used to be separate sections — an AGENT pill plus a COMMAND
// list — which read as two decisions but was really one, and picking Codex
// REPLACED the command list rather than filtering it, so custom commands
// vanished. They're all just launch targets; a command whose label collides
// with a built-in is dropped so "claude" doesn't appear twice.
// Pure: exported for unit tests.
export function launchTargets(commands) {
  const builtins = [
    { id: "claude", label: "Claude", mode: "agent", agent: "claude" },
    { id: "codex", label: "Codex", mode: "agent", agent: "codex" },
  ];
  const reserved = new Set(["claude", "codex"]);
  const custom = (commands || [])
    .filter((c) => !reserved.has((c.label || "").trim().toLowerCase()))
    .map((c) => ({ id: `cmd:${c.label}`, label: c.label, mode: "shell", exec: c.exec || "" }));
  return [...builtins, ...custom];
}

const branches = computed(() => pickerBranches(
  target.value, windows.value, trackRepoOf(target.value), catalog.value,
));

function trackRepoOf(trackId) {
  return (tracks.value || []).find((t) => t.id === trackId)?.repo || null;
}

function trackDefaultBranchOf(trackId) {
  const repo = trackRepoOf(trackId);
  if (!repo) return null;
  return (catalog.value?.repos || []).find((r) => r.repo === repo)?.default_branch || null;
}

export function openLauncher(trackId) {
  target.value = trackId;
  const bs = trackBranches(trackId, windows.value);
  pickedBranch.value = bs.length ? bs[0].branch : null;
  newBranchName.value = null;
  branchQuery.value = "";
  // Preselect whichever subscription has the most headroom, re-derived on
  // every open rather than remembered: the answer changes as limits burn
  // down, and a stale sticky value would keep routing work at an account
  // that filled up since. Falls back to the default account when no usage
  // has been fetched yet, which is the pre-accounts behaviour.
  account.value = bestAccount(usage.value?.plan, Date.now() / 1000) || "default";
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
  branchQuery.value = "";
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

  // `t` is a launchTargets() row. mode="agent" lets the server build the argv
  // via config.build_agent_command(agent) for BOTH agents; mode="shell" carries
  // a literal exec. Previously Claude went through the shell path with
  // exec="claude" while only Codex used the agent path — same launch, two
  // mechanisms.
  async function run(t) {
    const qs = new URLSearchParams({
      session: trackId,
      agent: t.agent || "claude",
      mode: t.mode,
    });
    if (t.mode === "shell" && t.exec) qs.set("exec", t.exec);
    const acct = accountQuery(account.value);
    if (acct) qs.set("account", acct);
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

  const targets = launchTargets(commands);
  // Enter (in the new-branch field or the branch search) launches the first
  // target — Claude.
  const defaultTarget = targets[0];
  const query = branchQuery.value.trim();
  const shown = query
    ? filterBranches(bs, query).slice(0, 24)
    : shortlistBranches(bs, trackDefaultBranchOf(trackId));
  // A branch picked from search then searched away again would silently drop
  // out of the rendered set while staying selected — keep it visible.
  const pinned = pickedBranch.value != null && !shown.some((b) => b.branch === pickedBranch.value)
    ? bs.find((b) => b.branch === pickedBranch.value)
    : null;
  const visible = pinned ? [pinned, ...shown] : shown;

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
            <div class="launcher-section-head">
              <div class="launcher-section-label">Branch</div>
              <input
                class="launcher-branch-search"
                type="text"
                placeholder={bs.length > 1 ? `search ${bs.length} branches…` : "search branches…"}
                value={branchQuery.value}
                onInput={(e) => { branchQuery.value = e.target.value; }}
                onKeyDown={(e) => {
                  if (e.key === "Escape" && branchQuery.value) {
                    // Clear the query first; a second Escape closes the modal.
                    e.stopPropagation();
                    branchQuery.value = "";
                  } else if (e.key === "Enter") {
                    const first = visible[0];
                    if (first) pickExisting(first.branch);
                  }
                }}
              />
            </div>
            <div class="launcher-branches">
              {visible.map((b) => (
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
                    if (e.key === "Enter" && defaultTarget) run(defaultTarget);
                  }}
                />
              )}
            </div>
            {query && visible.length === 0 && (
              <div class="launcher-empty">No branch matches “{query}”.</div>
            )}
          </div>
        )}

        <div class="launcher-section">
          <div class="launcher-section-label">Account</div>
          <div class="launcher-branches">
            {ACCOUNTS.map((a) => (
              <button
                key={a.id}
                class={`launcher-branch${account.value === a.id ? " is-active" : ""}`}
                onClick={() => { account.value = a.id; }}
              >
                {a.label}
              </button>
            ))}
          </div>
        </div>

        <div class="launcher-section">
          <div class="launcher-section-label">Run</div>
          <div id="launcher-list">
            {targets.map((t, i) => (
              <button
                key={t.id}
                class={`launcher-row${i === 0 ? " is-default" : ""}`}
                data-label={t.label}
                onClick={() => run(t)}
              >
                <span class="launcher-row-label">{t.label}</span>
                {i === 0 && <span class="launcher-row-hint">⏎</span>}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

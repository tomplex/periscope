"""Worktree creation primitive.

Caller passes a repo path + new branch name + optional base. We:
  1. Acquire per-repo lock (repo_locks.repo_lock).
  2. Resolve default branch via `git symbolic-ref refs/remotes/origin/HEAD`,
     fallback `main`/`master`.
  3. `git fetch origin <base>` (non-fatal — proceed with stale tracking
     ref on failure, surface a warning).
  4. `git worktree add -b <branch> <path> origin/<base>`. Path layout
     is hardcoded sibling: `~/dev/worktrees/<repo-basename>/<branch>`
     (with `/` in branch → `-` for path safety).
  5. Invalidate the worktrees cache for the repo so the next
     /api/state poll re-runs `git worktree list`.

Local default-branch ref is NOT touched. Local main-checkout HEAD is
NOT touched. See the workflow-management spec §Verb 1 + the v1
worktree-integration spec §"Pre-spawn fetch".
"""

import os
import re
import time
from pathlib import Path

from fastapi import HTTPException

from periscope import worktrees
from periscope.gitutil import detect_default_branch
from periscope.log import log
from periscope.repo_locks import repo_lock
from periscope.tmux import _run, _tmux_mutate, tmux

WORKTREES_DIR = Path.home() / "dev" / "worktrees"


def _slug_for_path(branch: str) -> str:
    """`/` → `-` so `tc/foo` becomes `tc-foo` on disk. Strips any
    characters that aren't safe for a directory name; collapses repeats.
    """
    s = re.sub(r"[^A-Za-z0-9._/-]", "-", branch)
    s = s.replace("/", "-")
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "branch"


def _resolve_layout(repo: str) -> str:
    """Return the worktree-layout string for `repo`. Order:
      1. settings.worktree_layout_overrides[repo] (sticky once set)
      2. Auto-detect from existing worktrees: if all non-main worktrees
         match the sibling pattern → 'sibling'; if all match inline →
         'inline'; mixed or zero → fall back to default.
      3. settings.worktree_layout_default (= 'sibling' if unset).

    Auto-detect runs ONCE per repo per process — once a layout is
    written to overrides, we never re-detect (Tom's design call: the
    first spawn determines the convention).

    Always-writes to `settings.worktree_layout_overrides[realpath(repo)]`
    after deciding, so subsequent spawns are O(1) settings lookups.
    """
    from periscope.store import get_settings, update_settings

    repo_real = os.path.realpath(repo)
    s = get_settings()
    overrides = s.get("worktree_layout_overrides") or {}
    if repo_real in overrides:
        return overrides[repo_real]

    default = s.get("worktree_layout_default") or "sibling"

    # Auto-detect.
    detected: set[str] = set()
    for wt_path, _branch in worktrees._cached_worktrees(repo_real):
        wt_real = os.path.realpath(wt_path)
        if wt_real == repo_real:
            continue  # skip main checkout
        if wt_real.startswith(str(WORKTREES_DIR) + "/"):
            detected.add("sibling")
        elif wt_real.startswith(os.path.join(repo_real, ".worktrees") + "/"):
            detected.add("inline")
    layout = next(iter(detected)) if len(detected) == 1 else default

    # Record + persist.
    new_overrides = {**overrides, repo_real: layout}
    update_settings({"worktree_layout_overrides": new_overrides})
    return layout


def worktree_path(repo: str, slug: str) -> str:
    """Absolute on-disk path for a worktree of `repo` identified by `slug`.

    Honors the repo's layout (see `_resolve_layout`): `inline` places
    worktrees at `<repo>/.worktrees/<slug>`, the default `sibling` layout
    at `~/dev/worktrees/<repo-basename>/<slug>`. `slug` is path-slugged.
    Shared by `spawn_worktree` and the PR-review route so a layout change
    reaches both.
    """
    safe = _slug_for_path(slug)
    if _resolve_layout(repo) == "inline":
        return str(Path(repo) / ".worktrees" / safe)
    repo_name = os.path.basename(repo.rstrip("/"))
    return str(WORKTREES_DIR / repo_name / safe)


def _branch_exists(repo: str, branch: str) -> bool:
    """True when `branch` is already a local ref in `repo`."""
    code, _ = _run(
        ["git", "-C", repo, "rev-parse", "--verify", "--quiet",
         f"refs/heads/{branch}"],
        timeout=10.0,
    )
    return code == 0


def spawn_worktree(
    repo: str,
    branch: str,
    base_branch: str | None = None,
    fetch: bool = True,
) -> dict:
    """Create a worktree of `repo` at branch `branch`, forked from
    `origin/<base_branch>` (or the detected default branch).

    When `fetch=True` (default), fetches origin/<base> first. When
    `fetch=False`, skips the network call and forks from the local
    <base_branch> ref directly, intended for new-worktree-tab callers
    where `base_branch` is the project's own (typically unpushed)
    feature branch.

    Returns:
      {
        "path": <absolute worktree path>,
        "base_branch": <resolved base branch name>,
        "branch": <new branch name as created>,
        "warning": <optional message about non-fatal fetch failure>,
      }

    Raises:
      ValueError if `branch` is empty, `repo` doesn't exist, the
      computed worktree path already exists, or `git worktree add`
      fails.
    """
    if not branch:
        raise ValueError("branch is required")
    repo = os.path.realpath(repo)
    if not os.path.isdir(os.path.join(repo, ".git")) and not os.path.isfile(
        os.path.join(repo, ".git")
    ):
        # .git can be a dir (normal checkout) or a file (worktree itself).
        # Either way it must exist.
        raise ValueError(f"not a git repo: {repo}")

    base = base_branch or detect_default_branch(repo)

    wt_path_str = worktree_path(repo, branch)
    wt_path = Path(wt_path_str)

    if wt_path.exists():
        raise ValueError(f"worktree path already exists: {wt_path_str}")

    # Branch-name safety: reject anything that would be interpreted as a
    # git flag. `--` after the flag/path positional arguments doesn't help
    # here because -b takes the branch as its value — a leading `-` in
    # the branch name still trips git. Reject it.
    if branch.startswith("-"):
        raise ValueError(f"branch name cannot start with '-': {branch!r}")

    warning: str | None = None

    # Fetch runs OUTSIDE the per-repo lock (network op, idempotent vs.
    # concurrent fetches). Skipped when `fetch=False` — callers spawning
    # off a local-only ref (e.g. an unpushed project branch) don't want
    # to fetch and don't need the remote to be up to date.
    # Phase-1's repo_locks.py:33-35 documents this: "Callers should hold
    # the lock only across the git mutation itself, not surrounding work."
    if fetch:
        fetch_code, fetch_out = _run(
            ["git", "-C", repo, "fetch", "origin", base], timeout=30.0
        )
        if fetch_code != 0:
            warning = f"fetch failed: origin/{base} may be stale ({fetch_out!r})"
            log.warning("worktree_spawn: %s", warning)

    with repo_lock(repo):
        # Ensure parent dir exists for both layouts. mkdir(parents=True)
        # handles arbitrary depth.
        wt_path.parent.mkdir(parents=True, exist_ok=True)

        # An already-existing local branch is CHECKED OUT, not re-created:
        # `-b` fails outright on a name git already knows. Without this, any
        # branch that exists but has no worktree was unreachable from both the
        # launcher and the omnibox's BranchTarget — the "open something that
        # isn't currently open" gap.
        if _branch_exists(repo, branch):
            add_args = ["worktree", "add", wt_path_str, branch]
        else:
            # With fetch=True the fresh remote ref is the source of truth.
            # With fetch=False the local ref is what we want — typically
            # the project's own feature branch with the user's unpushed work.
            base_ref = f"origin/{base}" if fetch else base
            add_args = ["worktree", "add", "-b", branch, wt_path_str, base_ref]
        code, out = _run(["git", "-C", repo, *add_args], timeout=30.0)
        if code != 0:
            raise ValueError(f"git worktree add failed: {out}")

    worktrees.invalidate(repo)

    result = {"path": wt_path_str, "base_branch": base, "branch": branch}
    if warning:
        result["warning"] = warning
    return result


def _layout_two_window(tmux_session: str, pinned_dir: str) -> tuple[str, str]:
    """Add the trellis-style 2-window pair (a 'claude' window + a 'shell'
    window) into `tmux_session`, creating the session on first use. The
    session is the single shared `config.MANAGED_SESSION` — many such pairs
    coexist in it, so every target is a stable `#{window_id}` (e.g. `@7`),
    NEVER `session:claude` (ambiguous — it resolves to the first match and
    would send-keys/stamp/select the WRONG window). Ends with the new claude
    window active. The user is NOT attached — periscope is a dashboard, not a
    terminal client.

    The 100ms sleep before send-keys lets the shell finish loading its rc
    file before `claude` lands (see CLAUDE.md "Key invariants" note 5).
    Without it, `claude` can land mid-rc and either get echoed as text or
    fail silently.

    Returns `(claude_pid, shell_pid)` — both windows stamped (by window id).
    Phase 4's PR-review endpoint uses claude_pid to write
    state.windows[pid].linked_pr synchronously; other callers can ignore the
    return.

    Raises HTTPException(500) on any tmux failure — this layout primitive
    is deliberately coupled to FastAPI so its callers (the project-CRUD
    route handlers) can let the exception propagate as an HTTP error.
    """
    from periscope.panes import note_action, note_focus
    from periscope.pids import stamp_new_window

    # Create the shared session lazily (first pane), or add a claude window to
    # the existing one. Either way capture the new window's stable id from
    # `-P -F "#{window_id}"` so subsequent targets can't drift onto a
    # same-named sibling window.
    if not _tmux_mutate("has-session", "-t", tmux_session)[0]:
        ok, claude_win = _tmux_mutate(
            "new-session", "-d", "-s", tmux_session, "-c", pinned_dir,
            "-n", "claude", "-P", "-F", "#{window_id}",
        )
        if not ok:
            raise HTTPException(500, f"tmux new-session failed: {claude_win}")
    else:
        ok, claude_win = _tmux_mutate(
            "new-window", "-t", f"{tmux_session}:", "-c", pinned_dir,
            "-n", "claude", "-P", "-F", "#{window_id}",
        )
        if not ok:
            raise HTTPException(500, f"tmux new-window (claude) failed: {claude_win}")

    # Send `claude` into the captured claude window, with the periscope
    # channels flag so the spawned Claude connects to periscope's MCP socket.
    from periscope.channels import dismiss_dev_channels_consent_bg
    from periscope.config import claude_exec
    exec_cmd = claude_exec()
    time.sleep(0.1)
    _tmux_mutate("send-keys", "-t", claude_win, exec_cmd, "Enter")
    if "--dangerously-load-development-channels" in exec_cmd:
        dismiss_dev_channels_consent_bg(claude_win)

    # Second window: shell.
    ok, shell_win = _tmux_mutate(
        "new-window", "-t", f"{tmux_session}:", "-c", pinned_dir,
        "-n", "shell", "-P", "-F", "#{window_id}",
    )
    if not ok:
        # Session + claude window already exist; don't roll back.
        log.warning("new-project: failed to create shell window: %s", shell_win)
        shell_win = ""

    # Stamp the shell window too — server-side rail placement needs the
    # complete pane list synchronously (it would otherwise only learn the
    # shell pid on the next /api/state poll's resolve_pids). `set-option -w`
    # accepts a `@id` target, so the captured window id works directly.
    shell_pid = stamp_new_window(shell_win) if shell_win else ""

    # Park focus on the claude window.
    _tmux_mutate("select-window", "-t", claude_win)

    # Stamp focus + action so the new project sorts to the top on the next
    # poll. Match the pattern in routes/sessions.py for `+ session`. The
    # recency map is keyed by session:index (window_view.py), NOT window id —
    # so these two stamps must resolve the claude window's index, even though
    # everything else targets the unambiguous window id. The index is stable
    # between creation and the next poll (no kills in between), so it matches
    # what update_focus_from_windows / window_view compute.
    claude_idx = tmux("display-message", "-t", claude_win, "-p", "#{window_index}").strip()
    claude_si = f"{tmux_session}:{claude_idx}"
    note_focus(claude_si)
    note_action(claude_si)
    claude_pid = stamp_new_window(claude_win)
    return claude_pid, shell_pid

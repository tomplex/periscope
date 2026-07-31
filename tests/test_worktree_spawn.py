"""Tests for periscope.worktree_spawn."""

import pytest

from tests.conftest import needs_tmux


@pytest.fixture
def tmp_worktrees(tmp_path, monkeypatch):
    """Redirect the sibling-layout worktree root into tmp.

    WORKTREES_DIR defaults to ~/dev/worktrees — the USER'S real directory. A
    spawn test without this fixture creates worktrees there for real, and then
    fails on the next run because `wt_path.exists()` short-circuits. (Two such
    strays from May 2026 are still sitting in that directory.)
    """
    from periscope import worktree_spawn
    root = tmp_path / "worktrees"
    monkeypatch.setattr(worktree_spawn, "WORKTREES_DIR", root)
    return root


def _patch_layout(monkeypatch, *, has_session: bool = True) -> list[tuple]:
    """Capture the tmux argv `_layout_two_window` issues, stubbing every real
    tmux / thread side effect.

    `_tmux_mutate` is patched in BOTH modules: worktree_spawn bound the name at
    import time, while `tmux.scrub_session_env` calls its own module-level
    binding — patching one alone lets the scrub reach the real tmux server.
    """
    from periscope import channels, pids, worktree_spawn
    from periscope import tmux as tmux_mod
    calls: list[tuple] = []

    def fake_mutate(*args):
        calls.append(args)
        if args[0] == "has-session":
            return has_session, ""
        return True, "@9"

    monkeypatch.setattr(worktree_spawn, "_tmux_mutate", fake_mutate)
    monkeypatch.setattr(tmux_mod, "_tmux_mutate", fake_mutate)
    monkeypatch.setattr(worktree_spawn, "tmux", lambda *a, **k: "9")
    monkeypatch.setattr(pids, "stamp_new_window", lambda target: f"pid{target}")
    monkeypatch.setattr(channels, "dismiss_dev_channels_consent_bg", lambda *a: None)
    return calls


def _created(calls: list[tuple]) -> list[tuple]:
    return [c for c in calls if c and c[0] in ("new-window", "new-session")]


def test_layout_two_window_passes_account_env(monkeypatch):
    """The unified-open surface (⌘K omnibox, /api/open, PR review) lands here —
    without the env every pane it creates is silently on the default account."""
    from periscope.worktree_spawn import _layout_two_window
    calls = _patch_layout(monkeypatch)

    _layout_two_window("sess", "/tmp", account="b")

    created = _created(calls)
    assert created, "no window created"
    # Both the claude window and its sibling shell window carry the binding, so
    # a hand-run `claude` in the shell stays on the same account.
    for call in created:
        flat = list(call)
        assert "-e" in flat, flat
        assert any(a.startswith("CLAUDE_CONFIG_DIR=") and a.endswith("/.claude-b")
                   for a in flat), flat


def test_layout_two_window_default_account_sends_no_env(monkeypatch):
    from periscope.worktree_spawn import _layout_two_window
    calls = _patch_layout(monkeypatch)

    _layout_two_window("sess", "/tmp")

    created = _created(calls)
    assert created
    assert all("-e" not in list(c) for c in created)


def test_layout_two_window_scrubs_new_session_env(monkeypatch):
    """`new-session -e` sets the SESSION env — every later window in the one
    shared session would otherwise inherit this account."""
    from periscope.worktree_spawn import _layout_two_window
    calls = _patch_layout(monkeypatch, has_session=False)

    _layout_two_window("sess", "/tmp", account="b")

    assert _created(calls) and _created(calls)[0][0] == "new-session"
    assert any(c[0] == "set-environment" and "-u" in c and "CLAUDE_CONFIG_DIR" in c
               for c in calls), f"session env never scrubbed: {calls}"


@needs_tmux
def test_layout_two_window_stamps_both_windows(tmp_git_repo, tmux_test_server):
    from periscope import config
    from periscope.tmux import tmux
    from periscope.worktree_spawn import _layout_two_window
    session = config.MANAGED_SESSION
    claude_pid, shell_pid = _layout_two_window(session, str(tmp_git_repo))
    assert claude_pid and shell_pid and claude_pid != shell_pid
    # Under one shared session there can be many windows named "claude"/"shell",
    # so look up the stamped ids by window id, not by session:name (ambiguous).
    stamped = {}
    for row in tmux("list-windows", "-t", session, "-F",
                    "#{window_id} #{@periscope_id}").split("\n"):
        if not row.strip():
            continue
        wid, _, pid = row.partition(" ")
        stamped[wid] = pid.strip()
    pids = [p for p in stamped.values() if p]
    assert claude_pid in pids and shell_pid in pids


@needs_tmux
def test_layout_two_window_stamps_recency_by_session_index(tmp_git_repo, tmux_test_server):
    """The new claude window's recency must land on the session:index key the
    rail sort reads (window_view.py) — not the window id. A window-id-keyed
    note_action would silently lose the action bump (focused_at self-heals on
    the next poll; acted_at never re-derives)."""
    from periscope import config, panes
    from periscope.tmux import tmux
    from periscope.worktree_spawn import _layout_two_window
    session = config.MANAGED_SESSION
    claude_pid, _ = _layout_two_window(session, str(tmp_git_repo))
    # Find the freshly-stamped claude window's index via its @periscope_id.
    idx = ""
    for row in tmux("list-windows", "-t", session, "-F",
                    "#{window_index} #{@periscope_id}").split("\n"):
        wi, _, pid = row.partition(" ")
        if pid.strip() == claude_pid:
            idx = wi.strip()
            break
    assert idx, "claude window not found by periscope id"
    stamps = panes.recency_stamps_for(f"{session}:{idx}")
    assert stamps["acted_at"] > 0 and stamps["focused_at"] > 0


def test_spawn_worktree_checks_out_an_existing_branch(tmp_git_repo, tmp_worktrees):
    """A branch that already exists but has no worktree must be CHECKED OUT.

    `git worktree add -b <name>` fails outright on a known branch name, which
    left every existing-but-not-open branch unreachable from the launcher and
    from the omnibox's BranchTarget.
    """
    import subprocess

    from periscope.worktree_spawn import spawn_worktree
    repo = str(tmp_git_repo)
    subprocess.run(["git", "-C", repo, "branch", "already-here"], check=True)

    res = spawn_worktree(repo, "already-here", fetch=False)

    assert res["branch"] == "already-here"
    head = subprocess.run(
        ["git", "-C", res["path"], "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "already-here"


def test_spawn_worktree_still_creates_a_brand_new_branch(tmp_git_repo, tmp_worktrees):
    import subprocess

    from periscope.worktree_spawn import spawn_worktree
    repo = str(tmp_git_repo)

    res = spawn_worktree(repo, "brand-new", fetch=False)

    head = subprocess.run(
        ["git", "-C", res["path"], "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "brand-new"

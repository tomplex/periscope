"""Direct tests for periscope/projects.py (resolve_project_for_window).

CLAUDE.md flags projects.py as indirectly-covered-only; this starts the
direct mirror file. Route-level behavior stays in tests/routes/test_projects.py.
"""

import pytest

from periscope.projects import (
    MAIN_KEY,
    archive_project,
    create_project,
    resolve_project_for_window,
)


@pytest.fixture(autouse=True)
def _state(clean_state):
    # Isolation: without this, create_project persists into the REAL
    # state.json (clean_state is not autouse in tests/conftest.py).
    return clean_state


def test_resolve_matched_session_returns_pinned_dir():
    create_project("/Users/foo/dev/myproj", name="myproj", tmux_session="myproj")
    assert resolve_project_for_window({"session": "myproj"}) == "/Users/foo/dev/myproj"


def test_resolve_unknown_session_folds_to_main():
    # The fold rule: every non-empty session resolves to SOMETHING.
    assert resolve_project_for_window({"session": "adhoc-scratch"}) == MAIN_KEY


def test_resolve_empty_session_returns_none():
    assert resolve_project_for_window({"session": ""}) is None
    assert resolve_project_for_window({}) is None


def test_resolve_archived_project_still_matches():
    # Archived rows still resolve (the frontend folds them to dev via the
    # no-row-in-projects_view fallback; the resolver itself doesn't filter).
    create_project("/Users/foo/dev/oldproj", name="old", tmux_session="oldproj")
    archive_project("/Users/foo/dev/oldproj")
    assert resolve_project_for_window({"session": "oldproj"}) == "/Users/foo/dev/oldproj"


def test_resolve_tag_wins_over_session_match(fresh_activity_db):
    # A window living in sess_a but tagged for /repo/b — the tag wins.
    create_project("/repo/a", name="a", tmux_session="sess_a")
    create_project("/repo/b", name="b", tmux_session="sess_b")
    fresh_activity_db.set_pane_project("%9", "/repo/b")
    assert resolve_project_for_window(
        {"session": "sess_a", "pane_id": "%9"}) == "/repo/b"


def test_resolve_untagged_pane_falls_back_to_session(fresh_activity_db):
    create_project("/repo/a", name="a", tmux_session="sess_a")
    assert resolve_project_for_window(
        {"session": "sess_a", "pane_id": "%untagged"}) == "/repo/a"


def test_resolve_external_session_with_pane_is_main(fresh_activity_db):
    assert resolve_project_for_window(
        {"session": "random", "pane_id": "%x"}) == MAIN_KEY


def test_placement_kill_set_excludes_live_ws_pane(clean_state, fresh_activity_db):
    from periscope.projects import placement_kill_set
    create_project("/repo/a", name="a", tmux_session="sess_a")
    clean_state["workspaces"]["ws_goal"] = {"id": "ws_goal", "archived_at": None}
    fresh_activity_db.set_pane_workspace("%claude", "ws_goal")
    windows = [
        {"session": "sess_a", "index": 0, "pane_id": "%claude"},
        {"session": "sess_a", "index": 1, "pane_id": "%shell"},
    ]
    assert placement_kill_set("/repo/a", windows) == [("sess_a:1", "%shell")]


def test_placement_kill_set_includes_archived_ws_pane(clean_state, fresh_activity_db):
    # Pane tagged into an ARCHIVED workspace folds back to its worktree row,
    # so close must kill it (matches the rail's resolve_workspace_for_window).
    from periscope.projects import placement_kill_set
    create_project("/repo/a", name="a", tmux_session="sess_a")
    clean_state["workspaces"]["ws_old"] = {"id": "ws_old", "archived_at": 123}
    fresh_activity_db.set_pane_workspace("%c", "ws_old")
    windows = [{"session": "sess_a", "index": 0, "pane_id": "%c"}]
    assert placement_kill_set("/repo/a", windows) == [("sess_a:0", "%c")]


def test_placement_kill_set_includes_untagged_managed_pane(clean_state, fresh_activity_db):
    from periscope.projects import placement_kill_set
    create_project("/repo/a", name="a", tmux_session="sess_a")
    windows = [{"session": "sess_a", "index": 2, "pane_id": "%new"}]
    assert placement_kill_set("/repo/a", windows) == [("sess_a:2", "%new")]


def test_placement_kill_set_refuses_main(clean_state, fresh_activity_db):
    from periscope.projects import placement_kill_set, MAIN_KEY as MK
    with pytest.raises(ValueError):
        placement_kill_set(MK, [])

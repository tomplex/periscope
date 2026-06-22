import time

import periscope.store as store
from periscope.pids import _gc_workspaces
from periscope.workspaces import (
    create_workspace, get_workspace, all_workspaces,
    update_workspace, archive_workspace, resolve_workspace_for_window,
)


def test_create_and_get(clean_state):
    ws = create_workspace(name="Auth refactor", base_repo="/dev/fdy")
    assert ws["id"].startswith("ws_")
    assert ws["name"] == "Auth refactor"
    assert ws["base_repo"] == "/dev/fdy"
    assert ws["archived_at"] is None
    assert get_workspace(ws["id"])["name"] == "Auth refactor"


def test_id_is_slugged_and_unique(clean_state):
    a = create_workspace(name="Auth refactor")
    b = create_workspace(name="Auth refactor")
    assert a["id"] != b["id"]
    assert a["id"].startswith("ws_auth-refactor")


def test_all_excludes_nothing_returns_snapshot(clean_state):
    create_workspace(name="One")
    create_workspace(name="Two")
    assert len(all_workspaces()) == 2


def test_update(clean_state):
    ws = create_workspace(name="X")
    assert update_workspace(ws["id"], name="Y", base_worktree="/dev/fdy-x") is True
    assert get_workspace(ws["id"])["name"] == "Y"
    assert get_workspace(ws["id"])["base_worktree"] == "/dev/fdy-x"
    assert update_workspace("ws_nope", name="Z") is False


def test_archive(clean_state):
    ws = create_workspace(name="X")
    assert archive_workspace(ws["id"]) is True
    assert get_workspace(ws["id"])["archived_at"] is not None
    assert archive_workspace("ws_nope") is False


def test_gc_drops_old_archived(clean_state):
    ws = create_workspace(name="Old")
    archive_workspace(ws["id"])
    clean_state["workspaces"][ws["id"]]["archived_at"] = int(time.time()) - 31 * 86400
    dirty = _gc_workspaces(clean_state["workspaces"], int(time.time()))
    assert dirty is True
    assert ws["id"] not in clean_state["workspaces"]


def test_gc_keeps_recent_archived_and_live(clean_state):
    live = create_workspace(name="Live")
    recent = create_workspace(name="Recent")
    archive_workspace(recent["id"])
    dirty = _gc_workspaces(clean_state["workspaces"], int(time.time()))
    assert dirty is False
    assert live["id"] in clean_state["workspaces"]
    assert recent["id"] in clean_state["workspaces"]

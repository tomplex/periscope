"""Tests for periscope/tracks.py — entity + resolution ladder."""
import pytest

import periscope.tracks as tracks
from periscope import activity


@pytest.fixture(autouse=True)
def fresh_db(fresh_activity_db):
    """Every test here hits the DB — make the shared isolation autouse."""


def test_resolve_explicit_tag(monkeypatch):
    activity.insert_track({"id": "tk_x", "name": "X", "repo": None,
                           "created_at": 1, "archived_at": None})
    activity.set_pane_track("aaaa0001", "tk_x")
    assert tracks.resolve_track_for_window({"pid": "aaaa0001"}) == "tk_x"


def test_resolve_archived_tag_falls_through_to_loose():
    activity.insert_track({"id": "tk_x", "name": "X", "repo": None,
                           "created_at": 1, "archived_at": 999})
    activity.set_pane_track("aaaa0001", "tk_x")
    # archived track + no repo → loose
    assert tracks.resolve_track_for_window({"pid": "aaaa0001"}) == tracks.LOOSE_KEY


def test_resolve_repo_default_get_or_create(monkeypatch):
    monkeypatch.setattr(tracks, "_repo_for_window", lambda w: "/repos/fdy")
    tid = tracks.resolve_track_for_window({"pid": "aaaa0009"})
    row = activity.get_track(tid)
    assert row["repo"] == "/repos/fdy" and row["name"] == "fdy"
    # second call is idempotent — same id, no duplicate row
    assert tracks.resolve_track_for_window({"pid": "aaaa0008"}) == tid
    assert len([t for t in activity.all_tracks() if t["repo"] == "/repos/fdy"]) == 1


def test_resolve_non_git_is_loose(monkeypatch):
    monkeypatch.setattr(tracks, "_repo_for_window", lambda w: None)
    assert tracks.resolve_track_for_window({"pid": "beef0005"}) == tracks.LOOSE_KEY


def test_resolve_track_reads_pid_then_pid_raw(fresh_activity_db):
    """Raw list_windows() rows carry only pid_raw (narrator, teardown,
    sessions-route callers); resolved rows carry pid. Both must resolve."""
    activity.insert_track({"id": "tk_goal", "name": "goal", "repo": None,
                           "created_at": 1, "archived_at": None})
    activity.set_pane_track("cafe1234", "tk_goal")
    assert tracks.resolve_track_for_window(
        {"pid": "cafe1234", "cwd": "/tmp"}) == "tk_goal"
    assert tracks.resolve_track_for_window(
        {"pid_raw": "cafe1234", "cwd": "/tmp"}) == "tk_goal"


def test_seed_tracks_keys_by_pid_raw(fresh_activity_db, mocker):
    mocker.patch.object(tracks, "_repo_for_window", return_value=None)
    windows = [{"pid_raw": "dead0001", "pane_id": "%1", "cwd": "/x"},
               {"pid_raw": "", "pane_id": "%2", "cwd": "/y"}]   # unstamped: skipped
    written = tracks.seed_tracks(windows)
    assert written == 1
    assert activity.get_pane_track("dead0001") == tracks.LOOSE_KEY


def test_track_label_prefers_row_name_then_basename():
    activity.insert_track({"id": "tk_goal", "name": "Ship It", "repo": None,
                           "created_at": 1, "archived_at": None})
    assert tracks.track_label("tk_goal") == "Ship It"
    # repo-default row: name is basename(repo)
    tid = tracks.repo_default_track("/repos/fdy")
    assert tracks.track_label(tid) == "fdy"
    # loose catchall
    assert tracks.track_label(tracks.LOOSE_KEY) == "loose"
    # no row yet → fall back to the id's path basename
    assert tracks.track_label("/repos/unseen") == "unseen"


def test_track_kind(clean_state):
    """The rail's only signal for which row is a catchall — a repo-default's
    label is basename(repo), identical to a goal track named after the repo."""
    activity.insert_track({"id": "tk_myproj", "name": "myproj", "repo": "/repos/myproj",
                           "created_at": 1, "archived_at": None})
    tid = tracks.repo_default_track("/repos/myproj")
    assert tracks.track_label(tid) == tracks.track_label("tk_myproj")  # collide
    assert tracks.track_kind(tid) == "repo"
    assert tracks.track_kind("tk_myproj") == "goal"
    assert tracks.track_kind(tracks.LOOSE_KEY) == "loose"
    # no row → "repo": the rail hides the menu rather than offering actions
    # that refuse server-side
    assert tracks.track_kind("/repos/unseen") == "repo"


def test_dissolve_refuses_repo_default(clean_state):
    """Archiving a repo-default is a silent no-op — repo_default_track only
    inserts when get_track is None, and get_track doesn't filter archived, so
    the row keeps resolving. Refuse it instead of leaving a zombie."""
    tid = tracks.repo_default_track("/repos/myproj")
    with pytest.raises(ValueError):
        tracks.dissolve_track(tid)
    assert activity.get_track(tid)["archived_at"] is None
    goal = tracks.create_track(name="Ship It", repo="/repos/myproj")
    tracks.dissolve_track(goal["id"])
    assert activity.get_track(goal["id"])["archived_at"] is not None


def test_teardown_targets_refuses_loose_and_repo_default(monkeypatch):
    monkeypatch.setattr(tracks, "_repo_for_window", lambda w: "/repos/fdy")
    tid = tracks.repo_default_track("/repos/fdy")
    with pytest.raises(ValueError):
        tracks.teardown_targets(tracks.LOOSE_KEY, [])
    with pytest.raises(ValueError):
        tracks.teardown_targets(tid, [])  # repo-default is a catchall, never mass-kill


def _live(monkeypatch, pane_to_pid: dict):
    """pane_workspace_map is %N-keyed legacy data; the migration converts
    through a live %N → pid_raw map from list_windows()."""
    rows = [{"pane_id": k, "pid_raw": v} for k, v in pane_to_pid.items()]
    monkeypatch.setattr("periscope.panes.list_windows", lambda: rows)


def test_migrate_workspaces_to_tracks(clean_state, monkeypatch):
    """A legacy workspace folds into a goal track (id == ws id), its members
    get tagged BY PID, an already-track-tagged pane is preserved, a pane with
    no live window (or no stamp) is dropped, and re-runs no-op."""
    from periscope import workspaces
    ws = workspaces.create_workspace(name="Auth", base_repo="/r/fdy")
    activity.set_pane_workspace("%1", ws["id"])
    activity.set_pane_workspace("%2", ws["id"])
    activity.set_pane_workspace("%9", ws["id"])  # pane gone — unresolvable
    activity.set_pane_track("aaaa0002", "tk_other")  # user already moved %2 elsewhere
    _live(monkeypatch, {"%1": "aaaa0001", "%2": "aaaa0002"})

    n = tracks.migrate_workspaces_to_tracks()
    assert n == 1  # only %1 newly tagged
    row = activity.get_track(ws["id"])
    assert row and row["name"] == "Auth" and row["repo"] == "/r/fdy"
    assert activity.get_pane_track("aaaa0001") == ws["id"]
    assert activity.get_pane_track("aaaa0002") == "tk_other"  # move preserved
    # Every handled row is CONSUMED — folded (%1), preserved (%2), and
    # unresolvable (%9) alike. Left behind, a %N-keyed row would match an
    # unrelated future pane that drew the same %N after a tmux restart.
    assert activity.pane_workspace_map() == {}
    assert tracks.migrate_workspaces_to_tracks() == 0  # idempotent


def test_migrate_workspaces_skips_archived(clean_state, monkeypatch):
    from periscope import workspaces
    ws = workspaces.create_workspace(name="Gone", base_repo="/r/x")
    activity.set_pane_workspace("%9", ws["id"])
    workspaces.archive_workspace(ws["id"])
    _live(monkeypatch, {"%9": "aaaa0009"})
    assert tracks.migrate_workspaces_to_tracks() == 0
    assert activity.get_track(ws["id"]) is None
    assert activity.get_pane_track("aaaa0009") is None
    # Archived-workspace rows are skipped, not consumed — they never write a
    # tag, so lingering is harmless (unlike live-workspace rows).
    assert activity.pane_workspace_map() == {"%9": ws["id"]}


def test_migrate_workspaces_overrides_repo_default(clean_state, monkeypatch):
    """Workspace membership overrides a repo-default tag (the lazy fallback /
    a prior seed), but a goal-track move still wins (see the test above)."""
    from periscope import workspaces
    ws = workspaces.create_workspace(name="Auth", base_repo="/r/fdy")
    # %3's pane was repo-default-seeded into the fdy track (id == repo path).
    activity.insert_track({"id": "/r/fdy", "name": "fdy", "repo": "/r/fdy",
                           "created_at": 1, "archived_at": None})
    activity.set_pane_track("aaaa0003", "/r/fdy")
    activity.set_pane_workspace("%3", ws["id"])
    _live(monkeypatch, {"%3": "aaaa0003"})

    assert tracks.migrate_workspaces_to_tracks() == 1
    assert activity.get_pane_track("aaaa0003") == ws["id"]  # repo-default overridden

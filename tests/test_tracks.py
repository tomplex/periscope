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
    activity.set_pane_track("%1", "tk_x")
    assert tracks.resolve_track_for_window({"pane_id": "%1"}) == "tk_x"


def test_resolve_archived_tag_falls_through_to_loose():
    activity.insert_track({"id": "tk_x", "name": "X", "repo": None,
                           "created_at": 1, "archived_at": 999})
    activity.set_pane_track("%1", "tk_x")
    # archived track + no repo → loose
    assert tracks.resolve_track_for_window({"pane_id": "%1"}) == tracks.LOOSE_KEY


def test_resolve_repo_default_get_or_create(monkeypatch):
    monkeypatch.setattr(tracks, "_repo_for_window", lambda w: "/repos/fdy")
    tid = tracks.resolve_track_for_window({"pane_id": "%9"})
    row = activity.get_track(tid)
    assert row["repo"] == "/repos/fdy" and row["name"] == "fdy"
    # second call is idempotent — same id, no duplicate row
    assert tracks.resolve_track_for_window({"pane_id": "%8"}) == tid
    assert len([t for t in activity.all_tracks() if t["repo"] == "/repos/fdy"]) == 1


def test_resolve_non_git_is_loose(monkeypatch):
    monkeypatch.setattr(tracks, "_repo_for_window", lambda w: None)
    assert tracks.resolve_track_for_window({"pane_id": "%5"}) == tracks.LOOSE_KEY


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


def test_teardown_targets_refuses_loose_and_repo_default(monkeypatch):
    monkeypatch.setattr(tracks, "_repo_for_window", lambda w: "/repos/fdy")
    tid = tracks.repo_default_track("/repos/fdy")
    with pytest.raises(ValueError):
        tracks.teardown_targets(tracks.LOOSE_KEY, [])
    with pytest.raises(ValueError):
        tracks.teardown_targets(tid, [])  # repo-default is a catchall, never mass-kill

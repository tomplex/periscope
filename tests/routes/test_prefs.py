"""Tests for /api/prefs/*."""

import re
from pathlib import Path

import pytest

import build_critters
from periscope.routes.prefs import FONT_IDS, TAB_COLOURS, TAB_TINTS, TAMA_CRITTERS


def test_get_prefs_returns_state(client, clean_state):
    clean_state["ui"]["view"] = "grid"
    r = client.get("/api/prefs")
    assert r.status_code == 200
    body = r.json()
    assert body["ui"]["view"] == "grid"


def test_patch_prefs_ui_valid_view(client, clean_state):
    r = client.patch("/api/prefs/ui", json={"view": "stream"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["ui"]["view"] == "stream"


def test_patch_prefs_ui_rejects_bogus_view(client, clean_state):
    r = client.patch("/api/prefs/ui", json={"view": "weird"})
    assert r.status_code == 400
    assert "invalid view" in r.json()["detail"]


def test_patch_prefs_ui_detail_mode_by_pid(client, clean_state):
    r = client.patch(
        "/api/prefs/ui",
        json={"detail_mode_by_pid": {"abc123": "transcript", "def456": "terminal"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ui"]["detail_mode_by_pid"] == {
        "abc123": "transcript",
        "def456": "terminal",
    }


def test_patch_prefs_ui_pinned_pids(client, clean_state):
    # Rail conversation pinning: the star toggle PATCHes pinned_pids; the
    # field must round-trip or the client's optimistic pin reverts on the
    # server response.
    r = client.patch("/api/prefs/ui", json={"pinned_pids": ["abc123", "def456"]})
    assert r.status_code == 200
    assert r.json()["ui"]["pinned_pids"] == ["abc123", "def456"]
    # unpin (empty list) round-trips too — must not be dropped as falsy
    r = client.patch("/api/prefs/ui", json={"pinned_pids": []})
    assert r.status_code == 200
    assert r.json()["ui"]["pinned_pids"] == []


def test_patch_prefs_ui_rejects_bogus_detail_mode(client, clean_state):
    r = client.patch(
        "/api/prefs/ui",
        json={"detail_mode_by_pid": {"abc123": "weird"}},
    )
    assert r.status_code == 400
    assert "invalid detail_mode" in r.json()["detail"]


def test_patch_prefs_ui_pane_widths(client, clean_state):
    # Splitter drags persist the rail and inspector widths. Undeclared on
    # UIPatch, Pydantic drops them silently and every reload snaps back.
    r = client.patch("/api/prefs/ui", json={"rail_width": 360, "side_width": 420})
    assert r.status_code == 200
    ui = r.json()["ui"]
    assert ui["rail_width"] == 360
    assert ui["side_width"] == 420


def test_patch_prefs_ui_rejects_absurd_pane_width(client, clean_state):
    r = client.patch("/api/prefs/ui", json={"side_width": 5})
    assert r.status_code == 400
    assert "invalid side_width" in r.json()["detail"]
    r = client.patch("/api/prefs/ui", json={"rail_width": 99999})
    assert r.status_code == 400


def test_patch_prefs_ui_panel_visibility_round_trips(client, clean_state):
    # The navigator / context-column toggles. False must survive too: it is
    # how a hidden panel comes back, and exclude_none keeps it (None drops).
    r = client.patch("/api/prefs/ui", json={"rail_hidden": True, "side_hidden": True})
    assert r.status_code == 200
    assert (r.json()["ui"]["rail_hidden"], r.json()["ui"]["side_hidden"]) == (True, True)
    r = client.patch("/api/prefs/ui", json={"rail_hidden": False})
    assert r.json()["ui"]["rail_hidden"] is False
    assert r.json()["ui"]["side_hidden"] is True


@pytest.mark.parametrize("critter", TAMA_CRITTERS)
def test_patch_prefs_ui_tama_critter_round_trips(client, clean_state, critter):
    r = client.patch("/api/prefs/ui", json={"tama_critter": critter})
    assert r.status_code == 200
    assert r.json()["ui"]["tama_critter"] == critter


def test_patch_prefs_ui_rejects_unknown_tama_critter(client, clean_state):
    r = client.patch("/api/prefs/ui", json={"tama_critter": "dragon"})
    assert r.status_code == 400
    assert "tama_critter" in r.json()["detail"]


def test_tama_critters_match_the_generated_sprites():
    # The client builds its list from build_critters.py's; a critter added
    # there but not here would 400 on pick.
    assert ("clawd", *build_critters.CRITTERS) == TAMA_CRITTERS


@pytest.mark.parametrize("key", ["ui_font", "code_font"])
def test_patch_prefs_ui_font_round_trips(client, clean_state, key):
    r = client.patch("/api/prefs/ui", json={key: "jetbrains-mono"})
    assert r.status_code == 200
    assert r.json()["ui"][key] == "jetbrains-mono"


def test_patch_prefs_ui_rejects_unknown_font(client, clean_state):
    r = client.patch("/api/prefs/ui", json={"ui_font": "comic-sans"})
    assert r.status_code == 400
    assert "ui_font" in r.json()["detail"]


def test_font_ids_match_the_client_list():
    # The picker offers static/src/fonts.js's list; an id there but not here
    # would 400 on pick.
    src = (Path(__file__).resolve().parents[2] / "static/src/fonts.js").read_text()
    assert tuple(re.findall(r'\bid: "([a-z0-9-]+)"', src)) == FONT_IDS


@pytest.mark.parametrize("tint", TAB_TINTS)
def test_patch_prefs_ui_tab_tint_round_trips(client, clean_state, tint):
    r = client.patch("/api/prefs/ui", json={"tab_tint": tint})
    assert r.status_code == 200
    assert r.json()["ui"]["tab_tint"] == tint


def test_patch_prefs_ui_rejects_unknown_tab_tint(client, clean_state):
    r = client.patch("/api/prefs/ui", json={"tab_tint": "loud"})
    assert r.status_code == 400
    assert "tab_tint" in r.json()["detail"]


def test_patch_prefs_ui_tab_colour_names_are_trimmed_and_empties_dropped(client, clean_state):
    r = client.patch("/api/prefs/ui", json={"tab_colour_names": {"iris": "  ci ", "rose": "  "}})
    assert r.status_code == 200
    assert r.json()["ui"]["tab_colour_names"] == {"iris": "ci"}


def test_patch_prefs_ui_tab_colour_names_replace_the_whole_map(client, clean_state):
    # The client sends the whole map, so a name it dropped is gone.
    client.patch("/api/prefs/ui", json={"tab_colour_names": {"iris": "ci", "teal": "ui"}})
    r = client.patch("/api/prefs/ui", json={"tab_colour_names": {"teal": "ui"}})
    assert r.json()["ui"]["tab_colour_names"] == {"teal": "ui"}


def test_patch_prefs_ui_rejects_a_name_for_an_unknown_colour(client, clean_state):
    r = client.patch("/api/prefs/ui", json={"tab_colour_names": {"red": "urgent"}})
    assert r.status_code == 400
    assert "red" in r.json()["detail"]


def test_patch_prefs_ui_rejects_an_over_long_colour_name(client, clean_state):
    r = client.patch("/api/prefs/ui", json={"tab_colour_names": {"iris": "x" * 25}})
    assert r.status_code == 400


def test_tab_colour_and_tint_ids_match_the_client_list():
    # The menu and Settings offer static/src/tabColours.js's lists; an id there
    # but not here would 400 on pick.
    src = (Path(__file__).resolve().parents[2] / "static/src/tabColours.js").read_text()
    assert tuple(re.findall(r'\bid: "([a-z]+)"', src)) == TAB_COLOURS + TAB_TINTS


def test_patch_prefs_ui_notify_round_trips(client, clean_state):
    prefs = {
        "needs_input": True,
        "done": False,
        "ci_failed": True,
        "pr_merged": True,
        "usage": False,
        "quiet_when_focused": True,
        "group": True,
        "badge": False,
    }
    r = client.patch("/api/prefs/ui", json={"notify": prefs})
    assert r.status_code == 200
    assert r.json()["ui"]["notify"] == prefs


def test_patch_prefs_ui_notify_rejects_a_non_bool(client, clean_state):
    r = client.patch("/api/prefs/ui", json={"notify": {"done": "sometimes"}})
    assert r.status_code == 422


def test_put_window_annotation(client, clean_state):
    r = client.put("/api/prefs/windows/abc123", json={"notes": "hi", "tags": ["a", "b", "a"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["annotation"]["notes"] == "hi"
    assert body["annotation"]["tags"] == ["a", "b"]
    assert body["annotation"]["pinned_files"] == []


def test_put_window_annotation_pinned_files(client, clean_state):
    # Dedupe + preserve insertion order; trim blanks.
    r = client.put(
        "/api/prefs/windows/abc123",
        json={
            "pinned_files": ["/a/b.md", "  ", "/a/b.md", "/c/d.html"],
        },
    )
    assert r.status_code == 200
    assert r.json()["annotation"]["pinned_files"] == ["/a/b.md", "/c/d.html"]
    # Empty list clears the field.
    r2 = client.put("/api/prefs/windows/abc123", json={"pinned_files": []})
    assert r2.json()["annotation"]["pinned_files"] == []
    assert "pinned_files" not in clean_state["windows"]["abc123"]


def test_put_window_annotation_colour_sets_and_clears(client, clean_state):
    r = client.put("/api/prefs/windows/abc123", json={"colour": "teal"})
    assert r.status_code == 200
    assert r.json()["annotation"]["colour"] == "teal"
    assert clean_state["windows"]["abc123"]["colour"] == "teal"
    # Setting the colour leaves the notes alone, and "" removes the colour.
    client.put("/api/prefs/windows/abc123", json={"notes": "hi"})
    r = client.put("/api/prefs/windows/abc123", json={"colour": ""})
    assert r.json()["annotation"]["colour"] is None
    assert clean_state["windows"]["abc123"] == {"notes": "hi"}


def test_put_window_annotation_rejects_an_unknown_colour(client, clean_state):
    r = client.put("/api/prefs/windows/abc123", json={"colour": "red"})
    assert r.status_code == 400
    assert "colour" in r.json()["detail"]


def test_put_window_annotation_invalid_pid(client, clean_state):
    r = client.put("/api/prefs/windows/abc-123", json={"notes": "hi"})
    assert r.status_code == 400
    assert "invalid pid" in r.json()["detail"]


def test_delete_window_annotation(client, clean_state):
    clean_state["windows"]["abc123"] = {"notes": "x", "tags": ["t"], "pinned_files": ["/x"], "colour": "rose"}
    r = client.delete("/api/prefs/windows/abc123")
    assert r.json()["ok"] is True
    assert "notes" not in clean_state["windows"]["abc123"]
    assert "tags" not in clean_state["windows"]["abc123"]
    assert "pinned_files" not in clean_state["windows"]["abc123"]
    assert "colour" not in clean_state["windows"]["abc123"]


def test_add_and_delete_command(client, clean_state):
    r = client.post("/api/prefs/commands", json={"label": "deploy", "exec": "make deploy"})
    assert r.json()["ok"] is True
    # Duplicate rejected.
    r2 = client.post("/api/prefs/commands", json={"label": "deploy"})
    assert r2.status_code == 409
    assert "duplicate" in r2.json()["detail"]
    # Delete.
    r3 = client.delete("/api/prefs/commands/deploy")
    assert r3.json()["ok"] is True


def test_update_command(client, clean_state):
    clean_state["commands"].append({"label": "old", "exec": "cmd"})
    r = client.put("/api/prefs/commands/old", json={"label": "new"})
    body = r.json()
    assert body["ok"] is True
    assert body["commands"][0]["label"] == "new"


def test_reorder_commands(client, clean_state):
    clean_state["commands"].extend(
        [
            {"label": "a", "exec": ""},
            {"label": "b", "exec": ""},
            {"label": "c", "exec": ""},
        ]
    )
    r = client.put("/api/prefs/commands", json={"labels": ["c", "a", "b"]})
    body = r.json()
    assert body["ok"] is True
    assert [c["label"] for c in body["commands"]] == ["c", "a", "b"]


def test_ui_patch_accepts_split_view(client, clean_state):
    r = client.patch("/api/prefs/ui", json={"view": "split"})
    assert r.status_code == 200, r.text
    assert r.json()["ui"]["view"] == "split"


def test_ui_patch_rejects_unknown_view(client, clean_state):
    r = client.patch("/api/prefs/ui", json={"view": "kanban"})
    assert r.status_code == 400


def test_ui_patch_accepts_rail_state_keys(client, clean_state):
    body = {
        "repo_order": ["/home/tom/dev/foo"],
        "worktrees_by_repo": {"/home/tom/dev/foo": ["session-a"]},
        "panes_by_worktree": {"session-a": ["pane:abc", "review"]},
        "rail_collapsed": {"repo:/home/tom/dev/foo": False},
        "last_selected": {"kind": "pane", "pid": "abc"},
    }
    r = client.patch("/api/prefs/ui", json=body)
    assert r.status_code == 200, r.text
    ui = r.json()["ui"]
    for key in body:
        assert ui[key] == body[key]


def test_patch_prefs_ui_clawd_notes_round_trip(client, clean_state):
    # Clawd's evening reads what happened today (a merged PR, a furious usage
    # day); kept server-side so a reload at 5pm, or a second window, agrees.
    # The minute it was noted decides which evening half hours it can book.
    r = client.patch("/api/prefs/ui", json={"clawd_merged": "2026-10-21T17:40", "clawd_furious": "2026-10-20T11:05"})
    assert r.status_code == 200
    ui = r.json()["ui"]
    assert (ui["clawd_merged"], ui["clawd_furious"]) == ("2026-10-21T17:40", "2026-10-20T11:05")


def test_patch_prefs_ui_clawd_notes_are_independent(client, clean_state):
    # One key per kind, so a window noting a furious day can't drop the merge
    # another window noted: update_ui merges only at the top level.
    client.patch("/api/prefs/ui", json={"clawd_merged": "2026-10-21T11:00"})
    r = client.patch("/api/prefs/ui", json={"clawd_furious": "2026-10-21T14:30"})
    assert r.status_code == 200
    ui = r.json()["ui"]
    assert (ui["clawd_merged"], ui["clawd_furious"]) == ("2026-10-21T11:00", "2026-10-21T14:30")


def test_patch_prefs_ui_rejects_bogus_clawd_notes(client, clean_state):
    for key, bad in (
        ("clawd_merged", "yesterday"),
        ("clawd_furious", "2026-10-21"),
        ("clawd_merged", "2026-10-21 12:00"),
    ):
        r = client.patch("/api/prefs/ui", json={key: bad})
        assert r.status_code == 400, (key, bad)
        assert key in r.json()["detail"]


def test_patch_prefs_ui_clawd_notes_keep_the_days_first(client, clean_state):
    # Every window notes merges and furious moods from its own prefs cache,
    # loaded at boot: a second window that missed the first note mustn't move
    # the minute the evening is booked from. The reply carries the kept note,
    # which that window's cache adopts.
    client.patch("/api/prefs/ui", json={"clawd_merged": "2026-10-21T17:29"})
    r = client.patch("/api/prefs/ui", json={"clawd_merged": "2026-10-21T17:31", "clawd_furious": "2026-10-21T17:31"})
    assert r.status_code == 200
    ui = r.json()["ui"]
    assert (ui["clawd_merged"], ui["clawd_furious"]) == ("2026-10-21T17:29", "2026-10-21T17:31")
    # An earlier minute from the same day (a write that arrived late) wins;
    # a new day's note replaces yesterday's.
    assert client.patch("/api/prefs/ui", json={"clawd_furious": "2026-10-21T14:05"}).json()["ui"]["clawd_furious"] == (
        "2026-10-21T14:05"
    )
    assert client.patch("/api/prefs/ui", json={"clawd_merged": "2026-10-22T09:10"}).json()["ui"]["clawd_merged"] == (
        "2026-10-22T09:10"
    )

"""The boot copy of state.json and periscope.db."""

import sqlite3
from contextlib import closing

from periscope import config
from periscope.backup import KEEP, back_up_stores


def _backups(xdg_home):
    return sorted((xdg_home / "periscope" / "backups").iterdir())


def test_copies_both_stores_with_rows_still_in_the_wal(tmp_xdg_home, fresh_activity_db):
    state = tmp_xdg_home / "periscope" / "state.json"
    state.parent.mkdir()
    state.write_text('{"windows": {}}', encoding="utf-8")
    fresh_activity_db.set_pane_track("9b1aca3f", "/repo")
    assert (tmp_xdg_home / "periscope.db-wal").stat().st_size > 0

    back_up_stores()

    [copy] = _backups(tmp_xdg_home)
    assert (copy / "state.json").read_text(encoding="utf-8") == '{"windows": {}}'
    with closing(sqlite3.connect(copy / "periscope.db")) as c:
        assert c.execute("SELECT pid, track_id FROM pane_tracks").fetchall() == [("9b1aca3f", "/repo")]


def test_drops_all_but_the_newest(tmp_xdg_home, fresh_activity_db):
    old = [tmp_xdg_home / "periscope" / "backups" / f"20260101T0000{i:02}" for i in range(KEEP)]
    for d in old:
        d.mkdir(parents=True)
        (d / "state.json").touch()

    back_up_stores()

    kept = _backups(tmp_xdg_home)
    assert len(kept) == KEEP
    assert kept[:-1] == old[1:]


def test_first_boot_has_no_state_json(tmp_xdg_home, fresh_activity_db):
    back_up_stores()

    [copy] = _backups(tmp_xdg_home)
    assert not (copy / "state.json").exists()


def test_dev_makes_no_backup(tmp_xdg_home, fresh_activity_db, monkeypatch):
    monkeypatch.setattr(config, "PORT", config.DEV_PORT)

    back_up_stores()

    assert not (tmp_xdg_home / "periscope").exists()

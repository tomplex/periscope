"""Tests for periscope.resurrect — rewriting tmux-resurrect save files so
Claude panes resume their specific session on restore.

The regression guard for the next time Claude's TUI/process format shifts: the
old standalone hook silently no-op'd because it keyed claude-detection off
pane_current_command (now a version string) instead of the full command field.
"""
import sqlite3

import pytest

import periscope.resurrect as resurrect
from periscope import config

SID = "33923c83-441e-4395-bac3-bfc9554b37a2"
OLD_SID = "f80a8a28-94c8-4bb3-a153-9a35c399a599"

# A realistic --system-prompt body: multi-token, contains text, the channel
# flags follow it (as in the real save file).
SYS = "--system-prompt # Environment\\012\\012- cwd: /x\\012 you are claude --resume not-a-flag"


def _pane_line(session, window, pane, command, *, title="t", cwd="/x", cur="2.1.150"):
    """Build a resurrect pane line (11 tab-separated fields, command last)."""
    return "\t".join([
        "pane", session, window, "0", ":", pane, title,
        f":{cwd}", "1", cur, f":{command}",
    ])


def _both_channels():
    return f"claude {SYS} --dangerously-load-development-channels server:lgtm --dangerously-load-development-channels server:periscope"


# --- _rewrite_line (pure) ------------------------------------------------

def _maps():
    return {"s:1.1": "%56"}, {"%56": SID}


def test_both_channels_preserved_in_order():
    pane_map, sess_map = _maps()
    line = _pane_line("s", "1", "1", _both_channels())
    out, changed = resurrect._rewrite_line(line, pane_map, sess_map)
    assert changed
    assert out.split("\t")[10] == (
        f":claude --resume {SID}"
        " --dangerously-load-development-channels server:lgtm"
        " --dangerously-load-development-channels server:periscope"
    )
    assert "--system-prompt" not in out


def test_single_channel_kept_no_periscope_assumed():
    pane_map, sess_map = _maps()
    cmd = f"claude {SYS} --dangerously-load-development-channels server:lgtm"
    out, changed = resurrect._rewrite_line(_pane_line("s", "1", "1", cmd), pane_map, sess_map)
    assert changed
    assert out.split("\t")[10] == (
        f":claude --resume {SID} --dangerously-load-development-channels server:lgtm"
    )


def test_resume_in_the_middle_is_stripped():
    pane_map, sess_map = _maps()
    cmd = (
        f"claude {SYS} --dangerously-load-development-channels server:lgtm"
        f" --resume {OLD_SID}"
        " --dangerously-load-development-channels server:periscope"
    )
    out, changed = resurrect._rewrite_line(_pane_line("s", "1", "1", cmd), pane_map, sess_map)
    assert changed
    assert OLD_SID not in out
    assert out.split("\t")[10] == (
        f":claude --resume {SID}"
        " --dangerously-load-development-channels server:lgtm"
        " --dangerously-load-development-channels server:periscope"
    )


def test_no_channels():
    pane_map, sess_map = _maps()
    out, changed = resurrect._rewrite_line(_pane_line("s", "1", "1", "claude"), pane_map, sess_map)
    assert changed
    assert out.split("\t")[10] == f":claude --resume {SID}"


def test_non_claude_untouched():
    pane_map, sess_map = _maps()
    for cmd in ("nvim /tmp/foo.md", "zsh", ""):
        line = _pane_line("s", "1", "1", cmd)
        out, changed = resurrect._rewrite_line(line, pane_map, sess_map)
        assert not changed
        assert out == line


def test_unresolved_session_untouched():
    # pane exists in tmux but has no pane_sessions row
    line = _pane_line("s", "1", "1", _both_channels())
    out, changed = resurrect._rewrite_line(line, {"s:1.1": "%99"}, {"%56": SID})
    assert not changed
    assert out == line

    # position not present in the live tmux map at all
    out, changed = resurrect._rewrite_line(line, {}, {"%56": SID})
    assert not changed
    assert out == line


def test_non_pane_lines_untouched():
    pane_map, sess_map = _maps()
    for line in (
        "window\ts\t1\t:win\t0\t:\tfa1c,187x57,0,0,2465\toff",
        "state\ts\ts",
        "",
    ):
        out, changed = resurrect._rewrite_line(line, pane_map, sess_map)
        assert not changed
        assert out == line


def test_idempotent():
    pane_map, sess_map = _maps()
    line = _pane_line("s", "1", "1", _both_channels())
    once, _ = resurrect._rewrite_line(line, pane_map, sess_map)
    twice, changed = resurrect._rewrite_line(once, pane_map, sess_map)
    assert changed  # still a claude line we resolve
    assert twice == once


# --- rewrite_save_file (integration) -------------------------------------

@pytest.fixture
def seeded_db(tmp_xdg_home, monkeypatch):
    """Point config.ACTIVITY_DB at a temp DB seeded with one pane_sessions row."""
    db = tmp_xdg_home / "periscope" / "periscope.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE pane_sessions (pane_id TEXT PRIMARY KEY, "
        "session_id TEXT NOT NULL, updated_at INTEGER NOT NULL)"
    )
    con.execute("INSERT INTO pane_sessions VALUES (?,?,?)", ("%56", SID, 1))
    con.commit()
    con.close()
    monkeypatch.setattr(config, "ACTIVITY_DB", db)
    return db


def test_rewrite_save_file_end_to_end(tmp_path, seeded_db, monkeypatch):
    monkeypatch.setattr(resurrect, "_live_pane_map", lambda: {"s:1.1": "%56"})
    save = tmp_path / "last"
    save.write_text(
        "\n".join([
            _pane_line("s", "1", "1", _both_channels()),     # rewritten
            _pane_line("s", "2", "1", "zsh"),                 # untouched
            "window\ts\t1\t:win\t0\t:\tfa1c,187x57,0,0,1\toff",
        ]) + "\n"
    )
    n = resurrect.rewrite_save_file(save)
    assert n == 1
    lines = save.read_text().splitlines()
    assert f"--resume {SID}" in lines[0]
    assert lines[1].endswith(":zsh")
    assert lines[2].startswith("window\t")


def test_rewrite_save_file_missing_db_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACTIVITY_DB", tmp_path / "nope" / "periscope.db")
    monkeypatch.setattr(resurrect, "_live_pane_map", lambda: {"s:1.1": "%56"})
    save = tmp_path / "last"
    original = _pane_line("s", "1", "1", _both_channels()) + "\n"
    save.write_text(original)
    n = resurrect.rewrite_save_file(save)
    assert n == 0
    assert save.read_text() == original

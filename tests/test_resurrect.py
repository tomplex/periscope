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
    return "\t".join(
        [
            "pane",
            session,
            window,
            "0",
            ":",
            pane,
            title,
            f":{cwd}",
            "1",
            cur,
            f":{command}",
        ]
    )


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
    assert out.split("\t")[10] == (f":claude --resume {SID} --dangerously-load-development-channels server:lgtm")


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


# --- second-subscription account prefix ---------------------------------
#
# resurrect captures commands from ps argv, and a shell-level `VAR=x cmd`
# prefix never reaches argv — so the config dir is absent from every captured
# line and must be re-emitted here. Without it a second-subscription pane
# restores onto the DEFAULT account and bills the wrong subscription silently.

ALT = "/Users/tom/.claude-b"


def test_account_prefix_emitted_with_resume():
    pane_map, sess_map = _maps()
    line = _pane_line("s", "1", "1", _both_channels())
    out, changed = resurrect._rewrite_line(line, pane_map, sess_map, {"%56": ALT})
    assert changed
    assert out.split("\t")[10] == (
        f":CLAUDE_CONFIG_DIR={ALT} claude --resume {SID}"
        " --dangerously-load-development-channels server:lgtm"
        " --dangerously-load-development-channels server:periscope"
    )


def test_account_prefix_emitted_without_session():
    """No pane_sessions row: --resume is lost, but the account must NOT be."""
    line = _pane_line("s", "1", "1", "claude --dangerously-load-development-channels server:periscope")
    out, changed = resurrect._rewrite_line(line, {"s:1.1": "%56"}, {}, {"%56": ALT})
    assert changed
    assert out.split("\t")[10] == (
        f":CLAUDE_CONFIG_DIR={ALT} claude --dangerously-load-development-channels server:periscope"
    )


def test_default_account_gets_no_prefix():
    pane_map, sess_map = _maps()
    line = _pane_line("s", "1", "1", "claude")
    out, changed = resurrect._rewrite_line(line, pane_map, sess_map, {"%99": ALT})
    assert changed
    assert out.split("\t")[10] == f":claude --resume {SID}"


def test_no_session_and_no_account_still_untouched():
    line = _pane_line("s", "1", "1", "claude")
    out, changed = resurrect._rewrite_line(line, {"s:1.1": "%56"}, {}, {})
    assert not changed
    assert out == line


def test_account_prefix_not_doubled():
    pane_map, sess_map = _maps()
    line = _pane_line("s", "1", "1", _both_channels())
    once, _ = resurrect._rewrite_line(line, pane_map, sess_map, {"%56": ALT})
    twice, _ = resurrect._rewrite_line(once, pane_map, sess_map, {"%56": ALT})
    assert twice == once
    assert twice.count("CLAUDE_CONFIG_DIR") == 1


# --- wrapper profile prefix ---------------------------------------------
#
# Same argv-invisibility as the account, one step worse: the `claude` wrapper
# consumes a typed `full` and execs `command claude --settings '{...}'`, so the
# profile is absent from argv even when it WAS typed. Without re-emitting it a
# full pane restores on the default plugin set, silently.


def test_profile_prefix_emitted_with_resume():
    pane_map, sess_map = _maps()
    line = _pane_line("s", "1", "1", _both_channels())
    out, changed = resurrect._rewrite_line(line, pane_map, sess_map, {}, {"%56": "full"})
    assert changed
    assert out.split("\t")[10] == (
        f":CLAUDE_WRAPPER_PROFILE=full claude --resume {SID}"
        " --dangerously-load-development-channels server:lgtm"
        " --dangerously-load-development-channels server:periscope"
    )


def test_profile_prefix_emitted_without_session():
    """No pane_sessions row: --resume is lost, but the profile must NOT be."""
    line = _pane_line("s", "1", "1", "claude")
    out, changed = resurrect._rewrite_line(line, {"s:1.1": "%56"}, {}, {}, {"%56": "full"})
    assert changed
    assert out.split("\t")[10] == ":CLAUDE_WRAPPER_PROFILE=full claude"


def test_account_and_profile_prefixes_coexist():
    """The two axes are independent — a full pane on account B carries both."""
    pane_map, sess_map = _maps()
    line = _pane_line("s", "1", "1", "claude")
    out, changed = resurrect._rewrite_line(line, pane_map, sess_map, {"%56": ALT}, {"%56": "full"})
    assert changed
    assert out.split("\t")[10] == (f":CLAUDE_CONFIG_DIR={ALT} CLAUDE_WRAPPER_PROFILE=full claude --resume {SID}")


def test_default_profile_gets_no_prefix():
    pane_map, sess_map = _maps()
    line = _pane_line("s", "1", "1", "claude")
    out, changed = resurrect._rewrite_line(line, pane_map, sess_map, {}, {"%99": "full"})
    assert changed
    assert out.split("\t")[10] == f":claude --resume {SID}"


# --- model prefix (ANTHROPIC_MODEL) ------------------------------------------
# A resumed session restores its OWN model, and ANTHROPIC_MODEL at launch would
# override it — clobbering a later in-session /model switch with the spawn-time
# choice. So the prefix is emitted only when there is no session to resume.


def test_model_prefix_emitted_without_session():
    line = _pane_line("s", "1", "1", "claude")
    out, changed = resurrect._rewrite_line(line, {"s:1.1": "%56"}, {}, {}, {}, {"%56": "opus"})
    assert changed
    assert out.split("\t")[10] == ":ANTHROPIC_MODEL=opus claude"


def test_model_prefix_NOT_emitted_with_resume():
    pane_map, sess_map = _maps()
    line = _pane_line("s", "1", "1", _both_channels())
    out, changed = resurrect._rewrite_line(line, pane_map, sess_map, {}, {}, {"%56": "opus"})
    assert changed
    assert "ANTHROPIC_MODEL" not in out
    assert out.split("\t")[10].startswith(f":claude --resume {SID}")


def test_model_prefix_orders_after_account_and_profile():
    line = _pane_line("s", "1", "1", "claude")
    out, _ = resurrect._rewrite_line(line, {"s:1.1": "%56"}, {}, {"%56": ALT}, {"%56": "full"}, {"%56": "opus"})
    assert out.split("\t")[10] == (f":CLAUDE_CONFIG_DIR={ALT} CLAUDE_WRAPPER_PROFILE=full ANTHROPIC_MODEL=opus claude")


# --- effort flag (--effort) ----------------------------------------------------
# Launch effort is a flag on the captured argv. It is kept on the session-lost
# path (the original command is restored as-is) and dropped on --resume, like
# the model: /effort can change a running session, and re-emitting the launch
# level after a reboot would undo it.


def test_effort_flag_kept_without_session():
    line = _pane_line("s", "1", "1", "claude --effort high")
    out, changed = resurrect._rewrite_line(line, {"s:1.1": "%56"}, {}, {"%56": ALT})
    assert changed
    assert out.split("\t")[10] == f":CLAUDE_CONFIG_DIR={ALT} claude --effort high"


def test_effort_flag_dropped_on_resume():
    pane_map, sess_map = _maps()
    line = _pane_line("s", "1", "1", _both_channels() + " --effort max")
    out, changed = resurrect._rewrite_line(line, pane_map, sess_map)
    assert changed
    assert "--effort" not in out
    assert out.split("\t")[10].startswith(f":claude --resume {SID}")


def test_profile_prefix_not_doubled():
    pane_map, sess_map = _maps()
    line = _pane_line("s", "1", "1", _both_channels())
    once, _ = resurrect._rewrite_line(line, pane_map, sess_map, {}, {"%56": "full"})
    twice, _ = resurrect._rewrite_line(once, pane_map, sess_map, {}, {"%56": "full"})
    assert twice == once
    assert twice.count("CLAUDE_WRAPPER_PROFILE") == 1


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
        "CREATE TABLE pane_sessions (pane_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, updated_at INTEGER NOT NULL)"
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
        "\n".join(
            [
                _pane_line("s", "1", "1", _both_channels()),  # rewritten
                _pane_line("s", "2", "1", "zsh"),  # untouched
                "window\ts\t1\t:win\t0\t:\tfa1c,187x57,0,0,1\toff",
            ]
        )
        + "\n"
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


# --- _duplicate_resumes: one pane per session keeps --resume -------------


def _active_line(session, window, pane, command, active):
    parts = _pane_line(session, window, pane, command).split("\t")
    parts[8] = active
    return "\t".join(parts)


def test_duplicate_resume_kept_only_on_the_pane_in_use():
    # Regression: racing restores left three panes in one window all resuming
    # one session; each restart restored every one of them plus another.
    cmd = f"claude --resume {SID} --dangerously-load-development-channels server:periscope"
    lines = [_active_line("p", "6", str(i), cmd, "0") for i in range(3)]
    pane_map = {"p:6.0": "%3", "p:6.1": "%4", "p:6.2": "%5"}
    activity = {"%3": (SID, 10.0), "%4": (SID, 11.0), "%5": (SID, 99.0)}
    assert resurrect._duplicate_resumes(lines, pane_map, {}, activity) == {0, 1}


def test_duplicate_resume_falls_back_to_active_then_first():
    cmd = f"claude --resume {SID}"
    lines = [_active_line("p", "6", "0", cmd, "0"), _active_line("p", "6", "1", cmd, "1")]
    assert resurrect._duplicate_resumes(lines, {}, {}, {}) == {0}
    lines = [_active_line("p", "6", "0", cmd, "0"), _active_line("p", "6", "1", cmd, "0")]
    assert resurrect._duplicate_resumes(lines, {}, {}, {}) == {1}


def test_duplicate_resume_activity_beats_the_active_flag():
    # The pane in use keeps the session even when another pane is the active one.
    cmd = f"claude --resume {SID}"
    lines = [_active_line("p", "6", "0", cmd, "1"), _active_line("p", "6", "1", cmd, "0")]
    pane_map = {"p:6.0": "%3", "p:6.1": "%4"}
    assert resurrect._duplicate_resumes(lines, pane_map, {}, {"%3": (SID, 10.0), "%4": (SID, 99.0)}) == {0}


def test_duplicate_resume_goes_to_the_pane_live_on_that_session():
    # The newest file can belong to a claude that has moved to another session
    # (a /clear); the pane whose live claude reports THIS session is on it.
    cmd = f"claude --resume {SID}"
    lines = [_active_line("p", "6", "0", cmd, "0"), _active_line("p", "6", "1", cmd, "0")]
    pane_map = {"p:6.0": "%3", "p:6.1": "%4"}
    activity = {"%3": (SID, 10.0), "%4": (OLD_SID, 99.0)}
    assert resurrect._duplicate_resumes(lines, pane_map, {}, activity) == {1}


def test_duplicate_resume_grouped_by_the_session_each_line_renders_with():
    # The pane_sessions row wins over captured argv, as in _rewrite_line: both
    # panes resume SID, though only one argv ever said so.
    lines = [_pane_line("p", "1", "0", f"claude --resume {OLD_SID}"), _pane_line("p", "2", "0", "claude")]
    pane_map = {"p:1.0": "%3", "p:2.0": "%4"}
    assert resurrect._duplicate_resumes(lines, pane_map, {"%3": SID, "%4": SID}, {}) == {1}


@pytest.mark.parametrize(
    ("cmd", "row"),
    [
        (f"claude --resume {OLD_SID} --effort high", SID),  # the row wins over argv
        (f"claude --resume {OLD_SID} --effort high", None),  # argv kept whole
        ("claude --effort high", SID),
        ("claude", None),
        (f"claude --session-id {OLD_SID} --effort high", None),  # a spawn's id, no row
        (f"claude --session-id {OLD_SID}", SID),  # the row wins over the spawn's id
        (f"claude {SYS}", None),  # "--resume not-a-flag" is prompt text
    ],
)
# A pane with an account, profile or spawn model leaves _rewrite_line by the
# env-prefix route, not the early return — the usual periscope-spawned pane,
# and a row-less one keeps its captured --resume there too.
@pytest.mark.parametrize("envs", [({}, {}, {}), ({"%56": ALT}, {"%56": "full"}, {"%56": "opus"})], ids=["bare", "env"])
def test_a_line_renders_the_session_it_is_grouped_by(cmd, row, envs):
    # _duplicate_resumes groups by _line_session; _rewrite_line renders by its
    # own route. Let them disagree and a duplicate escapes the grouping, or a
    # line that is not one loses its session.
    line = _pane_line("s", "1", "1", cmd)
    pane_map, sess_map = {"s:1.1": "%56"}, ({"%56": row} if row else {})
    out, _ = resurrect._rewrite_line(line, pane_map, sess_map, *envs)
    m = resurrect._RESUME_ARG_RE.search(out.split("\t")[10])
    assert (m.group(1) if m else None) == resurrect._line_session(resurrect._claude_pane(line), "%56", sess_map)


def test_demoted_line_takes_the_session_lost_shape():
    # Prefixes incl. the spawn-time model, captured flags incl. --effort, and
    # its own captured --resume gone — like any pane with no session to resume.
    cmd = f"claude --resume {SID} --effort high --dangerously-load-development-channels server:periscope"
    pane_map, sess_map = _maps()
    out, changed = resurrect._rewrite_line(
        _pane_line("s", "1", "1", cmd), pane_map, sess_map, {"%56": ALT}, {}, {"%56": "opus"}, demote=True
    )
    assert changed
    assert out.split("\t")[10] == (
        f":CLAUDE_CONFIG_DIR={ALT} ANTHROPIC_MODEL=opus claude --effort high"
        " --dangerously-load-development-channels server:periscope"
    )


def test_demoted_line_with_nothing_to_change_is_untouched():
    line = _pane_line("s", "1", "1", "claude --dangerously-load-development-channels server:periscope")
    assert resurrect._rewrite_line(line, *_maps(), demote=True) == (line, False)


def test_captured_session_id_resumes_without_a_row():
    line = _pane_line("s", "1", "1", f"claude --session-id {SID} --effort high")
    out, changed = resurrect._rewrite_line(line, {"s:1.1": "%56"}, {})
    assert changed
    assert out.split("\t")[10] == f":claude --resume {SID} --effort high"


def test_demoted_line_loses_its_session_id():
    line = _pane_line("s", "1", "1", f"claude --session-id {SID} --effort high")
    out, changed = resurrect._rewrite_line(line, *_maps(), demote=True)
    assert changed
    assert out.split("\t")[10] == ":claude --effort high"


def test_a_captured_session_id_counts_as_a_resume_for_duplicates():
    lines = [
        _pane_line("p", "1", "0", f"claude --resume {SID}"),
        _pane_line("p", "2", "0", f"claude --session-id {SID}"),
    ]
    assert resurrect._duplicate_resumes(lines, {}, {}, {}) == {1}


def test_distinct_sessions_and_prompt_text_untouched():
    lines = [
        _pane_line("p", "1", "0", f"claude --resume {SID}"),
        _pane_line("p", "2", "0", f"claude --resume {OLD_SID}"),
        _pane_line("p", "3", "0", f"claude {SYS}"),  # "--resume not-a-flag" in text
        _pane_line("p", "4", "0", f"claude {SYS}"),
    ]
    assert resurrect._duplicate_resumes(lines, {}, {}, {}) == set()


def test_rewrite_save_file_dedupes_captured_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACTIVITY_DB", tmp_path / "nope" / "periscope.db")
    monkeypatch.setattr(resurrect, "_live_pane_map", dict)
    monkeypatch.setattr(resurrect.session_status, "pane_session_activity", dict)
    save = tmp_path / "last"
    save.write_text(
        "\n".join(_active_line("p", "6", str(i), f"claude --resume {SID}", "1" if i == 1 else "0") for i in range(3))
        + "\n"
    )
    assert resurrect.rewrite_save_file(save) == 2
    assert [SID in line for line in save.read_text().splitlines()] == [False, True, False]


@pytest.mark.parametrize(
    "cmd",
    [
        "claude --effort high --dangerously-load-development-channels server:periscope",
        # a restored zombie: its argv already says --resume
        f"claude --resume {SID} --effort high --dangerously-load-development-channels server:periscope",
    ],
)
def test_rewrite_save_file_demotes_db_resolved_duplicates_in_the_session_lost_shape(
    tmp_path, seeded_db, monkeypatch, cmd
):
    # The incident's path: pane_sessions maps both panes to one session, so
    # both lines came out of the resume branch (model and --effort dropped)
    # before the duplicate lost its --resume — it restored fresh on the default
    # model and effort, and was counted twice.
    con = sqlite3.connect(seeded_db)
    con.execute("INSERT INTO pane_sessions VALUES (?,?,?)", ("%57", SID, 1))
    con.commit()
    con.close()
    monkeypatch.setattr(resurrect, "_live_pane_map", lambda: {"p:6.0": "%56", "p:6.1": "%57"})
    monkeypatch.setattr(resurrect.session_status, "pane_config_dirs", lambda: {"%56": ALT, "%57": ALT})
    monkeypatch.setattr(resurrect.session_status, "pane_models", lambda: {"%56": "opus", "%57": "opus"})
    monkeypatch.setattr(
        resurrect.session_status, "pane_session_activity", lambda: {"%56": (SID, 99.0), "%57": (SID, 10.0)}
    )
    save = tmp_path / "last"
    save.write_text(_active_line("p", "6", "0", cmd, "0") + "\n" + _active_line("p", "6", "1", cmd, "1") + "\n")
    assert resurrect.rewrite_save_file(save) == 2
    keeper, loser = (line.split("\t")[10] for line in save.read_text().splitlines())
    assert (
        keeper
        == f":CLAUDE_CONFIG_DIR={ALT} claude --resume {SID} --dangerously-load-development-channels server:periscope"
    )
    assert loser == (
        f":CLAUDE_CONFIG_DIR={ALT} ANTHROPIC_MODEL=opus claude --effort high"
        " --dangerously-load-development-channels server:periscope"
    )

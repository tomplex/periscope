"""POST /api/pane/permission and /api/pane/reply — the agent-acting banner
buttons. Each re-checks the pane when pressed; these tests pin every refusal,
because a wrong yes approves a tool call nobody saw, and a wrong reply runs in
a shell or picks a dialog option. Nothing here reaches tmux: capture and
_send_to_target (or, to pin the tmux arguments, routes.send.tmux) are mocked,
and the rows are real ones in a per-test DB."""

import re
from collections import deque
from pathlib import Path
from unittest.mock import call

import pytest
from fastapi import HTTPException

import pane_permission_hook as hook
from periscope.panes import composer_ready, dialog_open, parse_pane
from periscope.routes import pane_actions, send

_DIALOGS = Path(__file__).resolve().parent.parent / "fixtures" / "permission_dialogs"
_COMPOSER = Path(__file__).resolve().parent.parent / "fixtures" / "composer"
_IDLE = "⏺ Done.\n\n" + "─" * 60 + "\n❯ \n" + "─" * 60 + "\n  ⏵⏵ auto mode on (shift+tab to cycle)\n"
# An idle Claude an unmapped pane still reads as Claude: its composer (_IDLE,
# whatever the status line, THI-434), and the status line STATUS_RE knows.
_CLAUDE_ON_SCREEN = (
    "⏺ Done.\n\n" + "─" * 60 + "\n❯ \n" + "─" * 60 + "\n  17% | ↑30k ↓74 | $0.50 | Opus 4.7 (1M context)\n"
)
_SHELL = "tom@mac periscope % git status\nnothing to commit, working tree clean\ntom@mac periscope % \n"
_ASK = (
    "─" * 60 + "\nWhich database?\n\n❯ 1. Postgres\n  2. SQLite\n\nEnter to select · ↑/↓ to navigate · Esc to cancel\n"
)
_WAITING = {"state": "needs-input", "waiting_for": "permission prompt"}
_REQUEST = "a1b2c3d4e5f60718"

# The keys the hook stores for each fixture's request (pane_permission_hook.match_key).
_BASH_KEY = hook.match_key("Bash", {"command": "touch probe.txt", "description": "Create empty probe.txt file"})
_MULTILINE_KEY = hook.match_key("Bash", {"command": "touch a.txt\ntouch b.txt", "description": "Create two files"})
_PY_KEY = hook.match_key(
    "Bash",
    {
        "command": 'python3 -c "open(\\"py.txt\\",\\"w\\").close()"',
        "description": "Create empty py.txt file with Python",
    },
)
_NOTES = "/tmp/probe/notes.md"


def _screen(name: str) -> str:
    return (_DIALOGS / name).read_text()


def _row(act, request_id=_REQUEST, tool="Bash", key=_BASH_KEY, summary="touch probe.txt", created_at=999.4, pane="%9"):
    """A row as pane_permission_hook.py writes it."""
    with act._LOCK:
        c = act._conn()
        c.execute(
            "INSERT INTO pane_permissions "
            "(pane_id, session_id, agent_id, request_id, tool, summary, match_key, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (pane, "sid-1", None, request_id, tool, summary, key, created_at),
        )
        c.commit()


@pytest.fixture(autouse=True)
def _fresh_answers(monkeypatch):
    # The answered-once memory is module state; each test starts with none.
    monkeypatch.setattr(pane_actions, "_ANSWERED", deque(maxlen=256))
    # The reply's wait for its paste to show; the captures here are canned.
    monkeypatch.setattr(pane_actions, "_PASTE_SETTLE_S", 0)


@pytest.fixture
def pane(mocker):
    """A live Claude pane ab12cd34 (%9, s:4) that has waited on a permission
    prompt since t=1000, showing the Bash dialog. Returns the _send_to_target
    spy."""
    window = {"session": "s", "index": 4, "pane_id": "%9", "current_command": "claude.exe"}
    mocker.patch.object(pane_actions, "window_by_pid", return_value=("ab12cd34", "%9", window))
    mocker.patch.object(pane_actions, "session_id_for_pane", return_value="sid-1")
    mocker.patch.object(pane_actions, "live_session_id_for_pane", return_value="sid-1")
    mocker.patch.object(pane_actions, "session_state_for", return_value=dict(_WAITING))
    mocker.patch.object(pane_actions, "needs_since_for", return_value=1000)
    mocker.patch.object(pane_actions, "capture", return_value=_screen("bash.txt"))
    return mocker.patch.object(pane_actions, "_send_to_target", return_value={"ok": True})


def _permission(client, **body):
    return client.post("/api/pane/permission", json={"pid": "ab12cd34", "request_id": _REQUEST, **body})


def _reply(client, text="Postgres, please"):
    return client.post("/api/pane/reply", json={"pid": "ab12cd34", "text": text})


# --- /api/pane/permission: the presses that go through ------------------------


@pytest.mark.parametrize(("choice", "keys"), [("allow", ["1"]), ("deny", ["Escape"])])
def test_permission_sends_the_choices_key(client, pane, fresh_activity_db, choice, keys):
    _row(fresh_activity_db)
    r = _permission(client, choice=choice)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    pane.assert_called_once_with("s:4", None, keys, address="%9")
    # Looked up by the pane the pid resolves to now, never by the banner's target.
    pane_actions.session_id_for_pane.assert_called_with("%9")
    pane_actions.needs_since_for.assert_called_with("ab12cd34")
    # Read and sent by %id: tmux re-resolves `s:4` at send time.
    pane_actions.capture.assert_called_with("%9")


@pytest.mark.parametrize(
    ("fixture", "tool", "key"),
    [
        ("bash.txt", "Bash", _BASH_KEY),
        ("bash_narrow.txt", "Bash", _BASH_KEY),
        ("bash_multiline.txt", "Bash", _MULTILINE_KEY),
        # "This command requires approval" under the description is chrome.
        ("bash_requires_approval.txt", "Bash", _PY_KEY),
        # The dialog names notes.md; the hook stored the absolute path.
        ("write.txt", "Write", _NOTES),
        ("edit.txt", "Edit", _NOTES),
    ],
)
def test_allow_answers_each_dialog_its_row_names(client, pane, fresh_activity_db, mocker, fixture, tool, key):
    _row(fresh_activity_db, tool=tool, key=key)
    mocker.patch.object(pane_actions, "capture", return_value=_screen(fixture))
    r = _permission(client, choice="allow")
    assert r.status_code == 200, r.text
    pane.assert_called_once_with("s:4", None, ["1"], address="%9")


def test_the_keys_pane_actions_sends_fit_the_dialogs_options():
    # `1` is Yes in every dialog; No's number moves (4 for Bash, 3 for Write),
    # so Deny is Escape, which the footer offers.
    assert pane_actions._PERMISSION_KEYS == {"allow": ["1"], "deny": ["Escape"]}
    for name, no in (("bash.txt", 4), ("write.txt", 3)):
        screen = _screen(name)
        assert re.search(r"^\s*❯ 1\. Yes\s*$", screen, re.M), name
        assert re.search(rf"^\s*{no}\. No\s*$", screen, re.M), name
        footer = [line for line in screen.splitlines() if line.strip()][-1]
        assert footer.strip().startswith("Esc to cancel"), name


# --- /api/pane/permission: refusals before anything is read --------------------


def test_permission_rejects_any_other_choice(client, pane, fresh_activity_db):
    _row(fresh_activity_db)
    r = _permission(client, choice="always")
    assert r.status_code == 400
    pane.assert_not_called()


def test_permission_404s_for_a_pane_that_is_gone(client, pane, fresh_activity_db, mocker):
    _row(fresh_activity_db)
    mocker.patch.object(pane_actions, "window_by_pid", return_value=("", "", {}))
    assert _permission(client, choice="allow").status_code == 404
    pane.assert_not_called()


def test_permission_404s_without_a_pane_id_to_address(client, pane, fresh_activity_db, mocker):
    _row(fresh_activity_db)
    window = {"session": "s", "index": 4, "pane_id": "", "current_command": "claude.exe"}
    mocker.patch.object(pane_actions, "window_by_pid", return_value=("ab12cd34", "", window))
    assert _permission(client, choice="allow").status_code == 404
    pane.assert_not_called()


@pytest.mark.parametrize("headers", [{}, {"content-type": "text/plain"}], ids=["no-type", "text-plain"])
def test_permission_refuses_a_body_that_is_not_json(client, pane, fresh_activity_db, headers):
    # A POST without a JSON content type is a CORS simple request: any page could send it.
    _row(fresh_activity_db)
    body = f'{{"pid": "ab12cd34", "request_id": "{_REQUEST}", "choice": "allow"}}'
    r = client.post("/api/pane/permission", content=body, headers=headers)
    assert r.status_code == 415
    pane.assert_not_called()


# --- /api/pane/permission: the row must still be this request ------------------


def test_permission_409s_on_a_different_request(client, pane, fresh_activity_db):
    _row(fresh_activity_db, request_id="ffff000000000000")
    assert _permission(client, choice="allow").status_code == 409
    pane.assert_not_called()


@pytest.mark.parametrize(
    "sess",
    [
        {"state": "working", "waiting_for": None},
        {"state": "idle", "waiting_for": None},
        {"state": "needs-input", "waiting_for": "approve AskUserQuestion"},
        None,
    ],
    ids=["working", "idle", "another-dialog", "no-live-session"],
)
def test_permission_409s_unless_the_session_waits_on_a_permission_prompt(client, pane, fresh_activity_db, mocker, sess):
    # The dialog is still on screen in each case; the session file says otherwise (or nothing).
    _row(fresh_activity_db)
    mocker.patch.object(pane_actions, "session_state_for", return_value=sess)
    assert _permission(client, choice="allow").status_code == 409
    pane.assert_not_called()


def test_permission_409s_when_no_poll_has_seen_the_pane_waiting(client, pane, fresh_activity_db, mocker):
    _row(fresh_activity_db)
    mocker.patch.object(pane_actions, "needs_since_for", return_value=0)
    assert _permission(client, choice="allow").status_code == 409
    pane.assert_not_called()


@pytest.mark.parametrize(
    "rows",
    [
        # Parallel tool calls, or back-to-back prompts inside one poll: can't be told apart.
        [{"request_id": _REQUEST}, {"request_id": "b2b2b2b2b2b2b2b2", "created_at": 999.8}],
        # The row is from an earlier wait.
        [{"created_at": 990.0}],
        # Another pane's row.
        [{"pane": "%8"}],
        # A tool whose dialog can't be verified gets no key, so no Allow.
        [{"tool": "WebFetch", "key": None, "summary": "https://example.com"}],
    ],
    ids=["two-rows", "earlier-wait", "another-pane", "no-match-key"],
)
def test_permission_409s_unless_exactly_one_keyed_row_falls_in_this_wait(client, pane, fresh_activity_db, rows):
    for row in rows:
        _row(fresh_activity_db, **row)
    assert _permission(client, choice="allow").status_code == 409
    pane.assert_not_called()


def test_a_request_is_answered_once(client, pane, fresh_activity_db):
    # A second press (the banner and the rail card, a double click) could pass
    # every check before Claude redraws, and its key would land on whatever is
    # up next.
    _row(fresh_activity_db)
    assert _permission(client, choice="allow").status_code == 200
    assert _permission(client, choice="allow").status_code == 409
    assert _permission(client, choice="deny").status_code == 409
    pane.assert_called_once_with("s:4", None, ["1"], address="%9")


def test_a_failed_send_is_not_retried_from_a_banner(client, pane, fresh_activity_db):
    # The key may or may not have gone in; the retry's 409 reveals the pane instead.
    _row(fresh_activity_db)
    pane.side_effect = HTTPException(500, "tmux: no such window")
    assert _permission(client, choice="allow").status_code == 500
    pane.side_effect = None
    assert _permission(client, choice="allow").status_code == 409
    assert pane.call_count == 1


# --- /api/pane/permission: the screen must show exactly that request ----------


@pytest.mark.parametrize(
    ("tool", "key"),
    [
        # A stale row for another command: another fixture's key, then a made-up one.
        ("Bash", _MULTILINE_KEY),
        ("Bash", hook.match_key("Bash", {"command": "rm -rf build", "description": "Clean the build"})),
        # The same command, described differently.
        ("Bash", hook.match_key("Bash", {"command": "touch probe.txt", "description": "Make a file"})),
        # A file tool's row against a Bash dialog, whose command mentions the file.
        ("Write", "/tmp/probe/probe.txt"),
    ],
    ids=["other-fixture", "other-command", "other-description", "file-tool-row"],
)
def test_permission_409s_when_the_screen_shows_another_request(client, pane, fresh_activity_db, tool, key):
    _row(fresh_activity_db, tool=tool, key=key)
    assert _permission(client, choice="allow").status_code == 409
    pane.assert_not_called()


@pytest.mark.parametrize(
    ("fixture", "tool", "key"),
    [
        ("write.txt", "Edit", _NOTES),
        ("edit.txt", "Write", _NOTES),
        ("write.txt", "Write", "/tmp/probe/old-notes.md"),
        ("write.txt", "Write", "/tmp/probe/docs/other.md"),
    ],
    ids=["edit-row-on-write", "write-row-on-edit", "no-slash-boundary", "other-file"],
)
def test_permission_409s_when_a_file_dialog_is_for_another_tool_or_file(
    client, pane, fresh_activity_db, mocker, fixture, tool, key
):
    _row(fresh_activity_db, tool=tool, key=key)
    mocker.patch.object(pane_actions, "capture", return_value=_screen(fixture))
    assert _permission(client, choice="allow").status_code == 409
    pane.assert_not_called()


def test_permission_never_takes_the_tool_or_key_from_the_caller(client, pane, fresh_activity_db):
    # The row is for rm; a body naming the dialog's own tool and key changes nothing.
    _row(fresh_activity_db, key=hook.match_key("Bash", {"command": "rm -rf build"}))
    r = _permission(client, choice="allow", tool="Bash", match_key=_BASH_KEY, summary="touch probe.txt")
    assert r.status_code == 409
    pane.assert_not_called()


@pytest.mark.parametrize("screen", [_IDLE, _ASK, _SHELL], ids=["idle", "ask-user-question", "shell"])
def test_permission_409s_when_no_permission_dialog_is_on_screen(client, pane, fresh_activity_db, mocker, screen):
    _row(fresh_activity_db)
    mocker.patch.object(pane_actions, "capture", return_value=screen)
    assert _permission(client, choice="allow").status_code == 409
    pane.assert_not_called()


def test_permission_500s_and_sends_nothing_when_the_pane_cannot_be_read(client, pane, fresh_activity_db, mocker):
    _row(fresh_activity_db)
    mocker.patch.object(pane_actions, "capture", side_effect=RuntimeError("can't find pane"))
    assert _permission(client, choice="allow").status_code == 500
    pane.assert_not_called()


# --- /api/pane/reply -----------------------------------------------------------

_TEXT = "Postgres, please"
_EMPTY_ROW = "\x1b[39m❯\xa0"
_NOT_AT_PROMPT = "the pane's composer isn't at a plain prompt — shell mode or a draft; open it to see"
_CHANGED = (
    "the pane changed while the reply was typed — the text may be in the composer or the dialog;"
    " nothing was submitted; open it"
)


def _composer(prompt_row: str) -> str:
    """An idle Claude 2.1.280 screen as capture -e returns it: a past prompt
    (drawn white on grey, so it reads as typed) and its answer in scrollback,
    then the composer row between its rules."""
    rule = "─" * 60
    past = "\x1b[38;2;80;80;80m\x1b[48;2;55;55;55m❯ \x1b[38;2;255;255;255mWhich database should we use?\x1b[39m\x1b[49m"
    return f"{past}\n\n⏺ Postgres or SQLite?\n\n{rule}\n{prompt_row}\n{rule}\n  ⏵⏵ auto mode on (shift+tab to cycle)\n"


def _typed(screen: str, text: str = _TEXT) -> str:
    """`screen` once the paste shows: its empty `❯ ` composer row holding `text`."""
    assert screen.count("\n❯ \n") == 1
    return screen.replace("\n❯ \n", f"\n❯ {text}\n")


def _idle_capture() -> str:
    return (_COMPOSER / "idle.txt").read_text()


def _idle_capture_typed(text: str = _TEXT) -> str:
    # The real capture's composer row, its suggestion swapped for the paste.
    row = '\x1b[39m❯\xa0\x1b[2mTry "fix lint errors"\x1b[0m'
    screen = _idle_capture()
    assert screen.count(row) == 1
    return screen.replace(row, f"\x1b[39m❯\xa0{text}")


def _screens(mocker, *seq: str):
    """capture returns `seq` in order, then its last screen for good."""
    it = iter(seq)
    last = [seq[-1]]

    def _next(_pane_id):
        last[0] = next(it, last[0])
        return last[0]

    return mocker.patch.object(pane_actions, "capture", side_effect=_next)


def _assert_replied(pane, text: str = _TEXT) -> None:
    # The paste alone, then the Enter, both at the checked %id.
    assert pane.call_args_list == [call("s:4", text, [], address="%9"), call("s:4", None, ["Enter"], address="%9")]


def _assert_left_unsent(pane, text: str = _TEXT) -> None:
    assert pane.call_args_list == [call("s:4", text, [], address="%9")]


@pytest.fixture
def idle(mocker):
    """The pane is an idle Claude: a live session, an empty composer, and,
    once the paste lands, the reply in it."""
    mocker.patch.object(pane_actions, "session_state_for", return_value={"state": "idle", "waiting_for": None})
    _screens(mocker, _IDLE, _typed(_IDLE))


def test_reply_pastes_looks_again_then_enters(client, pane, mocker):
    mocker.patch.object(pane_actions, "session_state_for", return_value={"state": "idle", "waiting_for": None})
    events = []
    screens = iter([_IDLE, _typed(_IDLE)])
    mocker.patch.object(pane_actions, "capture", side_effect=lambda p: events.append(("capture", p)) or next(screens))
    pane.side_effect = lambda *a, **k: events.append(("send", a[1], a[2], k["address"])) or {"ok": True}
    r = _reply(client, f"  {_TEXT}  ")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    assert events == [
        ("capture", "%9"),
        ("send", _TEXT, [], "%9"),
        ("capture", "%9"),
        ("send", None, ["Enter"], "%9"),
    ]


def test_reply_goes_to_a_working_agent(client, pane, mocker):
    # Claude queues a message typed while it works; Ask agent to fix relies on it.
    mocker.patch.object(pane_actions, "session_state_for", return_value={"state": "working", "waiting_for": None})
    _screens(mocker, _IDLE, _typed(_IDLE))
    assert _reply(client).status_code == 200
    _assert_replied(pane)


@pytest.mark.parametrize("screen", [_IDLE, _CLAUDE_ON_SCREEN], ids=["composer", "status-line"])
def test_reply_goes_to_an_unmapped_pane_showing_claude(client, pane, mocker, screen):
    assert parse_pane(screen)["agent"] == "claude"
    mocker.patch.object(pane_actions, "live_session_id_for_pane", return_value=None)
    mocker.patch.object(pane_actions, "session_state_for", return_value=None)
    _screens(mocker, screen, _typed(screen))
    assert _reply(client).status_code == 200
    _assert_replied(pane)


@pytest.mark.parametrize(
    "prompt_row",
    [
        _EMPTY_ROW,
        # Claude's suggestion, dim (SGR 2), as live 2.1.280 panes draw it.
        '\x1b[39m❯\xa0\x1b[2mtry "how does the poll loop work"\x1b[0m',
    ],
    ids=["empty", "suggestion"],
)
def test_reply_goes_to_an_empty_composer(client, pane, idle, mocker, prompt_row):
    _screens(mocker, _composer(prompt_row), _composer(f"\x1b[39m❯\xa0{_TEXT}"))
    assert _reply(client).status_code == 200
    _assert_replied(pane)


def test_reply_goes_to_a_real_idle_composer(client, pane, idle, mocker):
    _screens(mocker, _idle_capture(), _idle_capture_typed())
    assert _reply(client).status_code == 200
    _assert_replied(pane)


def test_reply_enters_on_a_long_paste_claude_collapsed(client, pane, idle, mocker):
    # The composer was empty a moment ago, so the placeholder is this paste.
    collapsed = "\x1b[39m❯\xa0\x1b[38;5;240m[Pasted text #1 +3 lines]\x1b[39m"
    _screens(mocker, _composer(_EMPTY_ROW), _composer(collapsed))
    assert _reply(client).status_code == 200
    _assert_replied(pane)


def test_reply_waits_a_few_looks_for_the_paste_to_show(client, pane, idle, mocker):
    capture = _screens(mocker, _IDLE, _IDLE, _IDLE, _typed(_IDLE))
    assert _reply(client).status_code == 200
    _assert_replied(pane)
    assert capture.call_count == 4


# --- /api/pane/reply: the look after the paste ---------------------------------


def test_reply_never_enters_when_a_dialog_renders_after_the_paste(client, pane, idle, mocker):
    # A working agent raises a permission dialog between the first look and
    # the Enter: it takes focus, and Enter on it would confirm `1. Yes`.
    _screens(mocker, _IDLE, _screen("bash.txt"))
    r = _reply(client)
    assert r.status_code == 409
    assert r.json()["detail"] == _CHANGED
    _assert_left_unsent(pane)


def test_reply_never_enters_on_a_dialog_even_with_the_paste_showing(client, pane, idle, mocker):
    # The second look's dialog check stands on its own: here the composer
    # does hold the reply.
    after = _idle_capture_typed().rstrip("\n") + "\n  Esc to cancel\n"
    _screens(mocker, _idle_capture(), after)
    r = _reply(client)
    assert r.status_code == 409
    assert r.json()["detail"] == _CHANGED
    _assert_left_unsent(pane)


def test_reply_never_enters_under_a_half_drawn_dialog(client, pane, idle, mocker):
    # Mid-redraw: a dialog's top half above the composer, which still shows
    # the paste. Enter would land on the dialog once it finishes drawing.
    top = "─" * 100 + "\n Bash command\n\n   rm -rf build\n\n Do you want to proceed?\n ❯ 1. Yes\n"
    after = _idle_capture_typed().replace("\n\n\n\x1b[38;2;136;136;136m", "\n" + top + "\x1b[38;2;136;136;136m", 1)
    assert after != _idle_capture_typed()
    _screens(mocker, _idle_capture(), after)
    r = _reply(client)
    assert r.status_code == 409
    assert r.json()["detail"] == _CHANGED
    _assert_left_unsent(pane)


def test_reply_logs_a_paste_it_did_not_submit_without_its_text(client, pane, idle, mocker, caplog):
    secret = "my token is hunter2"
    _screens(mocker, _IDLE, _screen("bash.txt"))
    with caplog.at_level("INFO", logger="periscope"):
        assert _reply(client, secret).status_code == 409
    lines = [r.getMessage() for r in caplog.records]
    assert f"pane-action: reply on ab12cd34 (%9), {len(secret)} chars, pasted but not submitted" in lines
    assert not any("hunter2" in line for line in lines)


def test_reply_never_enters_when_the_paste_does_not_show(client, pane, idle, mocker):
    capture = _screens(mocker, _IDLE)
    r = _reply(client)
    assert r.status_code == 409
    assert r.json()["detail"] == _CHANGED
    _assert_left_unsent(pane)
    assert capture.call_count == 1 + pane_actions._PASTE_LOOKS


@pytest.mark.parametrize(
    "after",
    [
        # Something else got typed in between: Enter would send that.
        _typed(_IDLE, "rm -rf build"),
        # The paste landed in shell mode, where Enter runs it.
        _IDLE.replace("\n❯ \n", f"\n! {_TEXT}\n"),
    ],
    ids=["other-text", "shell-mode"],
)
def test_reply_never_enters_unless_the_composer_holds_the_reply(client, pane, idle, mocker, after):
    _screens(mocker, _IDLE, after)
    assert _reply(client).status_code == 409
    _assert_left_unsent(pane)


def test_reply_500s_when_the_second_look_cannot_read_the_pane(client, pane, idle, mocker):
    screens = iter([_IDLE])
    mocker.patch.object(pane_actions, "capture", side_effect=lambda p: next(screens))
    assert _reply(client).status_code == 500
    _assert_left_unsent(pane)


# --- /api/pane/reply: refusals before anything is typed ------------------------


def test_reply_rejects_empty_text(client, pane, idle):
    assert _reply(client, "   ").status_code == 400
    pane.assert_not_called()


def test_reply_404s_for_a_pane_that_is_gone(client, pane, idle, mocker):
    mocker.patch.object(pane_actions, "window_by_pid", return_value=("", "", {}))
    assert _reply(client, "hi").status_code == 404
    pane.assert_not_called()


@pytest.mark.parametrize("headers", [{}, {"content-type": "text/plain"}], ids=["no-type", "text-plain"])
def test_reply_refuses_a_body_that_is_not_json(client, pane, idle, headers):
    r = client.post("/api/pane/reply", content='{"pid": "ab12cd34", "text": "hi"}', headers=headers)
    assert r.status_code == 415
    pane.assert_not_called()


@pytest.mark.parametrize("waiting_for", ["approve AskUserQuestion", "permission prompt", None])
def test_reply_409s_while_the_session_waits_on_something(client, pane, mocker, waiting_for):
    # waitingFor is best-effort: a wait without one is still a wait.
    mocker.patch.object(
        pane_actions, "session_state_for", return_value={"state": "needs-input", "waiting_for": waiting_for}
    )
    mocker.patch.object(pane_actions, "capture", return_value=_IDLE)
    assert _reply(client, "1 sounds good").status_code == 409
    pane.assert_not_called()


@pytest.mark.parametrize(
    "screen",
    [_ASK, _screen("bash.txt"), _screen("write.txt"), "Something new\n\n  Esc to cancel\n"],
    ids=["choice-footer", "bash-dialog", "write-dialog", "unknown-dialog"],
)
@pytest.mark.parametrize("sess", [None, {"state": "idle", "waiting_for": None}], ids=["unmapped", "lagging-session"])
def test_reply_409s_on_a_dialog_on_screen(client, pane, mocker, screen, sess):
    # The session file can lag or be missing (an unmapped pane); the screen can't.
    mocker.patch.object(pane_actions, "session_state_for", return_value=sess)
    mocker.patch.object(pane_actions, "capture", return_value=screen)
    r = _reply(client, "1 sounds good")
    assert r.status_code == 409
    assert "dialog open" in r.json()["detail"]
    pane.assert_not_called()


def test_reply_409s_on_a_dialog_even_over_a_ready_composer(client, pane, idle, mocker):
    # The dialog check stands on its own: here the composer reads as ready.
    screen = _idle_capture().rstrip("\n") + "\n  Esc to cancel\n"
    assert composer_ready(screen) and dialog_open(screen)
    mocker.patch.object(pane_actions, "capture", return_value=screen)
    r = _reply(client, "1 sounds good")
    assert r.status_code == 409
    assert "dialog open" in r.json()["detail"]
    pane.assert_not_called()


def test_reply_409s_on_a_pane_not_running_an_agent(client, pane, mocker):
    # A press can come long after the banner: the agent may have exited to its shell.
    mocker.patch.object(pane_actions, "live_session_id_for_pane", return_value=None)
    mocker.patch.object(pane_actions, "session_state_for", return_value=None)
    mocker.patch.object(pane_actions, "capture", return_value=_SHELL)
    r = _reply(client)
    assert r.status_code == 409
    assert "isn't running an agent" in r.json()["detail"]
    pane.assert_not_called()


def test_reply_takes_only_a_live_session_as_an_agent(client, pane, mocker):
    # The recorded pane_sessions row can name another pane's session after a
    # resume or a reused %N; it isn't evidence this pane runs Claude.
    mocker.patch.object(pane_actions, "live_session_id_for_pane", return_value=None)
    mocker.patch.object(pane_actions, "session_id_for_pane", return_value="sid-1")
    idle_state = {"state": "idle", "waiting_for": None}
    mocker.patch.object(
        pane_actions, "session_state_for", side_effect=lambda sid: idle_state if sid == "sid-1" else None
    )
    # No composer on screen: one is evidence on its own (THI-434). Taken as
    # an agent here, the press would 409 at the composer check instead.
    mocker.patch.object(pane_actions, "capture", return_value="⏺ Done.\n")
    r = _reply(client)
    assert r.status_code == 409
    assert "isn't running an agent" in r.json()["detail"]
    pane.assert_not_called()


@pytest.mark.parametrize("foreground", ["zsh", "-zsh", "bash"])
def test_reply_409s_when_a_shell_is_in_the_foreground(client, pane, idle, mocker, foreground):
    # A Claude suspended with ctrl+z is still a live claude with a session file;
    # the prompt taking input is the shell's.
    window = {"session": "s", "index": 4, "pane_id": "%9", "current_command": foreground}
    mocker.patch.object(pane_actions, "window_by_pid", return_value=("ab12cd34", "%9", window))
    assert _reply(client).status_code == 409
    pane.assert_not_called()


def test_reply_409s_on_a_codex_pane(client, pane, mocker):
    # No Claude session, no Claude screen: the route can't rule out a Codex
    # approval prompt, where a `y…` would approve.
    window = {"session": "s", "index": 4, "pane_id": "%9", "current_command": "codex"}
    mocker.patch.object(pane_actions, "window_by_pid", return_value=("ab12cd34", "%9", window))
    mocker.patch.object(pane_actions, "live_session_id_for_pane", return_value=None)
    mocker.patch.object(pane_actions, "session_state_for", return_value=None)
    codex = "› Explain this codebase\n\n  ⏎ send   ⇧⏎ newline   ⌃T transcript   ⌃C quit\n"
    mocker.patch.object(pane_actions, "capture", return_value=codex)
    assert _reply(client).status_code == 409
    pane.assert_not_called()


def test_reply_500s_and_sends_nothing_when_the_pane_cannot_be_read(client, pane, idle, mocker):
    mocker.patch.object(pane_actions, "capture", side_effect=RuntimeError("can't find pane"))
    assert _reply(client).status_code == 500
    pane.assert_not_called()


@pytest.mark.parametrize(
    "prompt_row",
    [
        # A draft is drawn in the default fg, like the ❯ (2.1.280).
        "\x1b[39m❯\xa0half a thought",
        # A paste placeholder is grey like a suggestion, but it is the draft.
        "\x1b[39m❯\xa0\x1b[38;5;240m[Pasted text #1 +40 lines]\x1b[39m",
    ],
    ids=["draft", "pasted-block"],
)
def test_reply_409s_on_a_half_typed_draft(client, pane, idle, mocker, prompt_row):
    # The paste would be appended to the draft, and Enter would submit both.
    mocker.patch.object(pane_actions, "capture", return_value=_composer(prompt_row))
    r = _reply(client)
    assert r.status_code == 409
    assert r.json()["detail"] == _NOT_AT_PROMPT
    pane.assert_not_called()


@pytest.mark.parametrize("name", ["bash_empty.txt", "bash_draft.txt"])
def test_reply_409s_in_shell_mode(client, pane, idle, mocker, name):
    # Real 2.1.280 captures of a fresh session: `!` where `❯` was, and no
    # scrollback prompt. Enter would run the reply as a shell command.
    mocker.patch.object(pane_actions, "capture", return_value=(_COMPOSER / name).read_text())
    r = _reply(client)
    assert r.status_code == 409
    assert r.json()["detail"] == _NOT_AT_PROMPT
    pane.assert_not_called()


# --- what reaches tmux, and what's logged ---------------------------------------


@pytest.fixture
def tmux_calls(mocker, pane):
    """The real _send_to_target over a mocked routes.send.tmux; captures and
    tmux calls land in one ordered list."""
    calls = []
    mocker.patch.object(pane_actions, "_send_to_target", send._send_to_target)
    mocker.patch.object(send, "tmux", side_effect=lambda *a: calls.append(a) or "")
    return calls


@pytest.mark.parametrize(("choice", "key"), [("allow", "1"), ("deny", "Escape")])
def test_permission_sends_one_key_to_the_pane_id(client, tmux_calls, fresh_activity_db, choice, key):
    # `1` alone: Yes is taken at once, and an Enter would land on whatever's next.
    _row(fresh_activity_db)
    assert _permission(client, choice=choice).status_code == 200
    assert tmux_calls == [("send-keys", "-t", "%9", key)]


def test_reply_pastes_then_enters_at_the_pane_id(client, tmux_calls, mocker):
    mocker.patch.object(pane_actions, "session_state_for", return_value={"state": "idle", "waiting_for": None})
    screens = iter([_IDLE, _typed(_IDLE)])
    mocker.patch.object(
        pane_actions, "capture", side_effect=lambda p: tmux_calls.append(("capture", p)) or next(screens)
    )
    assert _reply(client).status_code == 200
    assert tmux_calls[0] == ("capture", "%9")
    assert tmux_calls[1][:3] == ("set-buffer", "-b", tmux_calls[1][2]) and tmux_calls[1][3] == _TEXT
    buf = tmux_calls[1][2]
    assert tmux_calls[2:] == [
        ("paste-buffer", "-d", "-p", "-b", buf, "-t", "%9"),
        ("capture", "%9"),
        ("send-keys", "-t", "%9", "Enter"),
    ]


def test_reply_text_cannot_break_out_of_the_bracketed_paste(client, tmux_calls, mocker):
    # tmux passes an embedded ESC[201~ through paste-buffer -p: the paste
    # would end there and the rest be typed as keys, the CR submitting it.
    # A check name in "Ask agent to fix" can carry one.
    mocker.patch.object(pane_actions, "session_state_for", return_value={"state": "idle", "waiting_for": None})
    clean = "Fix it[201~!rm -rf x\tnow\nplease"
    _screens(mocker, _IDLE, _typed(_IDLE, "Fix it[201~!rm -rf x now please"))
    r = _reply(client, "Fix it\x1b[201~\r!rm -rf x\tnow\nplease\x7f\x9b")
    assert r.status_code == 200, r.text
    pasted = next(c[3] for c in tmux_calls if c[0] == "set-buffer")
    assert pasted == clean
    assert "\x1b" not in pasted and "\r" not in pasted


def test_reply_of_nothing_but_control_characters_is_empty(client, pane, idle):
    assert _reply(client, "\x1b\r\x03\x07\x9b\x00").status_code == 400
    pane.assert_not_called()


def test_reply_logs_the_pane_and_length_never_the_text(client, pane, idle, mocker, caplog):
    secret = "my token is hunter2"
    _screens(mocker, _IDLE, _typed(_IDLE, secret))
    with caplog.at_level("INFO", logger="periscope"):
        assert _reply(client, secret).status_code == 200
    lines = [r.getMessage() for r in caplog.records]
    assert f"pane-action: reply on ab12cd34 (%9), {len(secret)} chars" in lines
    assert not any("hunter2" in line for line in lines)


def test_a_permission_send_that_fails_after_marking_is_logged(client, pane, fresh_activity_db, caplog):
    _row(fresh_activity_db)
    pane.side_effect = HTTPException(500, "tmux: no such pane")
    with caplog.at_level("WARNING", logger="periscope"):
        assert _permission(client, choice="allow").status_code == 500
    assert any(r.levelname == "WARNING" and _REQUEST in r.getMessage() for r in caplog.records)

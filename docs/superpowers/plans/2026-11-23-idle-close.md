# Idle close (server part) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** At 04:00, close the Claude panes that were idle for a set number of weekdays, and let Resume bring each one back with its pid, name, track, PR, ticket and wrapper profile.

**Architecture:** A new module `periscope/idle_close.py`, in the shape of `poke.py`, holds the pure rules, the sweep and the prod-only tick. A new `closed_panes` table in the activity DB holds one row for each closed pane. `_window_new_resume` reads that row and opens the window with the old pid in one tmux command.

**Tech Stack:** FastAPI, sqlite3, tmux 3.6a, pytest (`pytest-mock`), Preact, vitest.

**Spec:** `docs/superpowers/specs/2026-11-19-idle-close-design.md`.

**Worktree:** `/Users/thibaultdody/github/tomplex/periscope/.claude/worktrees/idle-close-server`, branch `thibaultdody/thi-466-idle-close-server`. Run every command from there. Never edit `/Users/thibaultdody/github/tomplex/periscope`: prod runs from it.

**What was run:** the code blocks of tasks 2 to 15 were applied to a throwaway clone of `58e1592a` and run there: 2,404 Python tests (87 new), 1,553 vitest tests (3 new), ruff and ty clean, the same 3 Biome warnings as `main`. The "break it on purpose" steps failed as written. Task 1 ran on 23 Nov 2026: both assumptions hold.

## Rules for every task

- Read `docs/invariants.md` (pid identity) before you touch `pids.py`, `panes.py`, `tmux.py` or `routes/sessions.py`. Read `docs/testing.md` before you touch `tests/conftest.py`.
- The worktree's `.venv` is on Python 3.14. If it is missing, run `uv sync --python 3.14`: uv's default here is 3.15, which cannot build `rpds-py`.
- Before each commit, run `uv run ruff format periscope tests` and `bin/check`.
- Commit after each task. One line, a blank line, then `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Never push, open a PR or edit a PR without the user's yes.**
- Never start a real Claude without the user's yes (Task 1). Dev and prod share tmux.
- A dev server runs only on port 17376 with a scratch `XDG_CONFIG_HOME`. Never use 17374 (prod) or 17375 (another worktree).
- Comments and docstrings are short and describe what the code does now. No ticket numbers, no history.

## Two PRs

| PR | Tasks | What ships |
|---|---|---|
| 1. Closed record and resume | 2 to 8 | The table, retention, resume and the `/history` label. Nothing writes a record yet, so nothing changes for the user. Carries the spec correction (`116f4040`, `2657228f`, already on the branch). |
| 2. Setting and sweep | 9 to 16 | The setting, the sweep, the preview and the settings row. Off by default. Start it from `main` after PR 1 merges. |

The order is a safety rule: the sweep must not exist before Resume can bring a pane back.

## Differences from the spec

- `sweep(views, weekdays)`: the tick reads the setting once and passes it. The spec has `sweep(views)`.
- `pids.stamped_pids(windows)` is new. It is the set that `resolve_pids` calls `carried`. Resume, the sweep and the boot prune need it too.
- The boot prune runs in `_pane_sessions_housekeeping` (`periscope/app.py`), which already lists the windows.
- `transcript_mtime` is the float from `stat()`, so the re-check sees a write in the same second.
- `/api/history/search` rows always have `closed_at`; it is `null` without a record, like `linked_pr`.

## File map

| File | PR | Change |
|---|---|---|
| `periscope/activity.py` | 1 | `closed_panes` table, its accessors, `prune_pane_tracks` keeps closed pids. |
| `periscope/pids.py` | 1 | `stamped_pids()`, used by `resolve_pids`. |
| `periscope/idle_close.py` | 1, 2 | New. PR 1: `came_back`, `prune_closed`. PR 2: the rules, the sweep, the tick, the preview. |
| `periscope/routes/sessions.py` | 1 | `_window_new_resume` uses the record. |
| `periscope/channels.py` | 1 | `resume_session` keeps the record's pid. |
| `periscope/routes/history.py`, `static/history.js`, `static/historyList.js` | 1 | `closed_at` and "closed Thu 04:00". |
| `periscope/app.py` | 1, 2 | PR 1: prune at boot. PR 2: the prod task and the preview router. |
| `periscope/tmux.py`, `periscope/panes.py` | 2 | `window_panes` in `PANE_FIELDS`, `panes` in the `list_windows` row. |
| `periscope/store.py`, `periscope/routes/settings.py` | 2 | `idle_close_weekdays`, `idle_close_log`. |
| `periscope/resurrect.py` | 2 | `save_now(force=True)`. |
| `periscope/routes/idle_close.py` | 2 | New. `GET /api/idle-close/preview`. |
| `static/src/overlays/SettingsModal.jsx`, `static/src/overlays/idleClose.js`, `static/styles.css`, `static/dist/` | 2 | The settings row. |
| `tests/conftest.py`, `tests/test_app.py` | 1, 2 | Neuter the sweep thread; stub the tick in the lifespan tests. |

---

## Before the code

### Task 1: Check the assumptions with the user

Section 10 of the spec has three assumptions. Two of them can change the sweep, so check them first.

**This task starts a real Claude on a private tmux server. Ask the user first. If the user says no, skip to Task 2 and write "not checked" in the PR body.**

- [x] **Step 1: Make a scratch directory and the private server helper**

```bash
CHECK=/private/tmp/claude-501/idle-close-check
mkdir -p "$CHECK"
T() { tmux -L idle-close-check -f /dev/null "$@"; }
# No TMUX variables: the pane-session hook would write this pane id over a prod pane's row.
CLAUDE='env -u TMUX -u TMUX_PANE claude --model haiku --strict-mcp-config --settings {"disableAllHooks":true}'
```

- [x] **Step 2: Check that a Claude with no prompt writes no transcript**

```bash
ls ~/.claude/projects/*/*.jsonl | sort > "$CHECK/before.txt"
T new-session -d -s t -x 200 -y 50 -c /Users/thibaultdody/github/tomplex/periscope "$CLAUDE"
sleep 20
T capture-pane -p -t t | tail -15
ls ~/.claude/projects/*/*.jsonl | sort | comm -13 "$CHECK/before.txt" -
```

Expected: the pane shows Claude's composer, and the last command prints nothing. If the pane shows a trust dialog, stop and ask the user. If a file appears, tell the user: a Claude that you never prompted then has a transcript.

- [x] **Step 3: Check that `kill-pane` on an idle Claude loses no turn**

```bash
T send-keys -t t 'Reply with the single word ok.' Enter
sleep 20
NEW=$(ls ~/.claude/projects/*/*.jsonl | sort | comm -13 "$CHECK/before.txt" -); echo "$NEW"
cp "$NEW" "$CHECK/before-kill.jsonl"; wc -l "$CHECK/before-kill.jsonl"
grep -c '"role":"assistant"' "$CHECK/before-kill.jsonl"
T kill-pane -t t
sleep 5
head -n "$(wc -l < "$CHECK/before-kill.jsonl")" "$NEW" | cmp - "$CHECK/before-kill.jsonl" && echo "prefix intact"
```

Expected: the file has at least one assistant line before the kill, and the last line is `prefix intact`. If not, stop and tell the user: the sweep design changes.

- [x] **Step 4: Remove the scratch files**

```bash
T kill-server 2>/dev/null
rm -r "$CHECK"
```

The transcript that the check made stays under `~/.claude/projects`. Tell the user its path, so that they can delete it.

- [x] **Step 5: Write the result in the spec and commit**

Edit section 10 of the spec. For each of the two assumptions, replace the "Check:" sentence with what you saw and the Claude version (`claude --version`).

```bash
git add docs/superpowers/specs/2026-11-19-idle-close-design.md
git commit -m "spec: idle-close assumptions checked against a live Claude

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## PR 1: closed record and resume

### Task 2: `pids.stamped_pids`

**Files:**
- Modify: `periscope/pids.py` (`resolve_pids`, the `carried` line, and a new function after `_is_pid`)
- Test: `tests/test_pids.py`

- [x] **Step 1: Write the failing test**

Add to `tests/test_pids.py`:

```python
def test_stamped_pids_are_the_ids_the_windows_wear():
    from periscope.pids import stamped_pids

    windows = [_mkwin(pid_raw="aaaa1111"), _mkwin(pid_raw=" bbbb2222\n"), _mkwin(pid_raw=""), _mkwin(pid_raw="%5")]
    assert stamped_pids(windows) == {"aaaa1111", "bbbb2222"}
```

- [x] **Step 2: Run it to make sure that it fails**

Run: `uv run pytest tests/test_pids.py -q -k stamped_pids`
Expected: FAIL with `ImportError: cannot import name 'stamped_pids'`.

- [x] **Step 3: Write the function and use it in `resolve_pids`**

In `periscope/pids.py`, after `_is_pid`:

```python
def stamped_pids(windows: list[dict]) -> set[str]:
    """The periscope ids that `windows` (list_windows rows) wear in tmux."""
    return {p for w in windows if _is_pid(p := (w.get("pid_raw") or "").strip())}
```

In `resolve_pids`, replace the `carried` line:

```python
        carried = stamped_pids(windows)
```

- [x] **Step 4: Run the pid tests**

Run: `uv run pytest tests/test_pids.py -q`
Expected: all pass.

- [x] **Step 5: Commit**

```bash
git add periscope/pids.py tests/test_pids.py
git commit -m "pids: stamped_pids names the ids that windows wear

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3: the `closed_panes` table

**Files:**
- Modify: `periscope/activity.py` (`_SCHEMA`, imports, accessors after `sweep_legacy_pane_track_rows`, `prune_pane_tracks`)
- Test: `tests/test_activity.py`

- [x] **Step 1: Write the failing tests**

Add to `tests/test_activity.py`, after `test_pane_tracks_set_get_map_prune`:

```python
def _closed(session_id="s1", pid="dddd4444", **over):
    row = {"session_id": session_id, "pid": pid, "mem_mb": 812, "profile": None, "idle_since": 1000, "closed_at": 2000}
    return {**row, **over}


def test_closed_panes_record_get_list_delete():
    activity.record_closed_pane(_closed(profile="full"))
    activity.record_closed_pane(_closed("s2", "eeee5555", mem_mb=None, closed_at=2001))
    assert activity.get_closed_pane("s1") == _closed(profile="full")
    assert activity.get_closed_pane("unknown") is None
    assert [r["session_id"] for r in activity.closed_panes()] == ["s1", "s2"]
    activity.delete_closed_pane("s1")
    assert activity.get_closed_pane("s1") is None


def test_a_second_close_of_a_session_replaces_its_row():
    activity.record_closed_pane(_closed(profile="full"))
    again = _closed("s1", "eeee5555", mem_mb=300, idle_since=3000, closed_at=4000)
    activity.record_closed_pane(again)
    assert activity.closed_panes() == [again]


def test_prune_pane_tracks_keeps_the_tag_of_a_closed_pane_until_its_row_goes():
    activity.set_pane_track("dddd4444", "tk_foo")
    activity.record_closed_pane(_closed())
    assert activity.prune_pane_tracks(set()) == 0
    assert activity.get_pane_track("dddd4444") == "tk_foo"
    activity.delete_closed_pane("s1")
    assert activity.prune_pane_tracks(set()) == 1
```

- [x] **Step 2: Run them to make sure that they fail**

Run: `uv run pytest tests/test_activity.py -q -k "closed_pane or closed_panes or second_close"`
Expected: 3 FAIL with `AttributeError: module 'periscope.activity' has no attribute 'record_closed_pane'`.

- [x] **Step 3: Add the table, the row type and the accessors**

In `_SCHEMA`, after the `pane_tracks` table:

```sql
-- One row for each pane that idle close closed (periscope.idle_close).
CREATE TABLE IF NOT EXISTS closed_panes (
  session_id   TEXT PRIMARY KEY NOT NULL,  -- Claude session id
  pid          TEXT NOT NULL,              -- @periscope_id of the closed pane
  mem_mb       INTEGER,                    -- footprint at close, NULL if unknown
  profile      TEXT,                       -- wrapper profile, NULL for the default
  idle_since   INTEGER NOT NULL,
  closed_at    INTEGER NOT NULL
);
```

In the imports, add:

```python
from typing import TypedDict, cast
```

After `sweep_legacy_pane_track_rows`:

```python
# --- closed_panes: the panes that idle close closed ---
class ClosedPane(TypedDict):
    session_id: str
    pid: str
    mem_mb: int | None
    profile: str | None
    idle_since: int
    closed_at: int


_CLOSED_COLS = tuple(ClosedPane.__annotations__)
_CLOSED_SELECT = f"SELECT {','.join(_CLOSED_COLS)} FROM closed_panes"


def _closed_row(row: tuple) -> ClosedPane:
    return cast(ClosedPane, dict(zip(_CLOSED_COLS, row, strict=True)))


def record_closed_pane(pane: ClosedPane) -> None:
    """Write the pane's row. A session can close twice, so the newer close wins."""
    with _LOCK:
        c = _conn()
        c.execute(
            f"INSERT INTO closed_panes ({','.join(_CLOSED_COLS)}) "
            f"VALUES ({','.join(':' + col for col in _CLOSED_COLS)}) ON CONFLICT(session_id) DO UPDATE SET "
            + ", ".join(f"{col}=excluded.{col}" for col in _CLOSED_COLS[1:]),
            dict(pane),
        )
        c.commit()


def get_closed_pane(session_id: str) -> ClosedPane | None:
    with _LOCK:
        row = _conn().execute(f"{_CLOSED_SELECT} WHERE session_id=?", (session_id,)).fetchone()
        return _closed_row(row) if row else None


def closed_panes() -> list[ClosedPane]:
    with _LOCK:
        return [_closed_row(r) for r in _conn().execute(f"{_CLOSED_SELECT} ORDER BY closed_at, session_id")]


def delete_closed_pane(session_id: str) -> None:
    with _LOCK:
        c = _conn()
        c.execute("DELETE FROM closed_panes WHERE session_id=?", (session_id,))
        c.commit()
```

In `prune_pane_tracks`, replace the body:

```python
    with _LOCK:
        c = _conn()
        existing = [r[0] for r in c.execute("SELECT pid FROM pane_tracks")]
        # A closed pane's pid is dead until Resume stamps it again.
        closed = {r[0] for r in c.execute("SELECT pid FROM closed_panes")}
        # %N rows predate the pid re-key and are the sweep's business — judged
        # against a pid set they would ALWAYS look dead and lose their tag
        # before resolve's lazy migration could carry it over.
        dead = [p for p in existing if not p.startswith("%") and p not in alive_pids and p not in closed]
        c.executemany("DELETE FROM pane_tracks WHERE pid=?", [(p,) for p in dead])
        c.commit()
        return len(dead)
```

- [x] **Step 4: Run the activity tests**

Run: `uv run pytest tests/test_activity.py -q`
Expected: all pass.

- [x] **Step 5: Commit**

```bash
git add periscope/activity.py tests/test_activity.py
git commit -m "activity: closed_panes keeps one row for each pane idle close closed

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 4: `came_back`, `prune_closed` and the boot prune

**Files:**
- Create: `periscope/idle_close.py`, `tests/test_idle_close.py`
- Modify: `periscope/app.py` (`_pane_sessions_housekeeping`)

- [x] **Step 1: Write the failing tests**

Create `tests/test_idle_close.py`:

```python
"""Idle close: the rules (pure), the sweep (I/O mocked) and retention.

Nothing here spawns a thread: conftest neuters idle_close._bg, and `sweep` is
called directly."""

import pytest

from periscope import activity, idle_close

RECORD: activity.ClosedPane = {
    "session_id": "s1",
    "pid": "dddd4444",
    "mem_mb": 812,
    "profile": None,
    "idle_since": 1000,
    "closed_at": 2000,
}


@pytest.fixture
def no_live_claude(monkeypatch):
    monkeypatch.setattr(idle_close.session_status, "claude_proc_for", lambda sid: None)


def test_came_back_when_a_live_window_wears_the_records_pid(no_live_claude):
    assert idle_close.came_back(RECORD, {"dddd4444"})
    assert not idle_close.came_back(RECORD, {"aaaa1111"})


def test_came_back_when_a_live_claude_runs_the_session(monkeypatch):
    monkeypatch.setattr(idle_close.session_status, "claude_proc_for", lambda sid: {"pid": 1} if sid == "s1" else None)
    assert idle_close.came_back(RECORD, set())


def test_prune_closed_drops_a_row_with_no_transcript_or_whose_session_came_back(monkeypatch, no_live_claude, tmp_path):
    for sid, pid in [("kept", "aaaa1111"), ("no-file", "bbbb2222"), ("worn", "cccc3333")]:
        activity.record_closed_pane({**RECORD, "session_id": sid, "pid": pid})
    transcripts = {"kept": tmp_path / "kept.jsonl", "worn": tmp_path / "worn.jsonl"}
    monkeypatch.setattr(idle_close, "jsonl_for_session", transcripts.get)

    assert idle_close.prune_closed({"cccc3333"}) == 2
    assert [r["session_id"] for r in activity.closed_panes()] == ["kept"]


def test_prune_closed_drops_a_row_whose_session_a_live_claude_runs(monkeypatch, tmp_path):
    activity.record_closed_pane(RECORD)
    monkeypatch.setattr(idle_close, "jsonl_for_session", lambda sid: tmp_path / "s1.jsonl")
    monkeypatch.setattr(idle_close.session_status, "claude_proc_for", lambda sid: {"pid": 1})
    assert idle_close.prune_closed(set()) == 1
    assert activity.closed_panes() == []
```

- [x] **Step 2: Run them to make sure that they fail**

Run: `uv run pytest tests/test_idle_close.py -q`
Expected: ERROR with `ImportError: cannot import name 'idle_close' from 'periscope'`.

- [x] **Step 3: Write the module**

Create `periscope/idle_close.py`:

```python
"""Idle close: each night, close the Claude panes idle for a set number of
weekdays, to free their memory. A closed pane keeps a `closed_panes` row, from
which Resume brings it back with its pid, name, track and wrapper profile.

Spec: docs/superpowers/specs/2026-11-19-idle-close-design.md.
"""

from periscope import activity, session_status
from periscope.turns import jsonl_for_session


def came_back(record: activity.ClosedPane, live_pids: set[str]) -> bool:
    """A live window wears the record's pid, or a live Claude runs its session.
    Both, because a typed `claude --resume` can keep or change the session id."""
    return record["pid"] in live_pids or session_status.claude_proc_for(record["session_id"]) is not None


def prune_closed(live_pids: set[str]) -> int:
    """Delete the records that Resume can no longer use: Claude deleted the
    transcript, or the session came back another way. Returns how many."""
    dropped = 0
    for record in activity.closed_panes():
        if jsonl_for_session(record["session_id"]) is None or came_back(record, live_pids):
            activity.delete_closed_pane(record["session_id"])
            dropped += 1
    return dropped
```

- [x] **Step 4: Run the tests**

Run: `uv run pytest tests/test_idle_close.py -q`
Expected: 4 passed.

- [x] **Step 5: Prune at boot**

In `periscope/app.py`, in `_pane_sessions_housekeeping`, replace the `alive` line and add the prune at the end of the function:

```python
        windows = list_windows()
        alive = {w["pane_id"] for w in windows if w.get("pane_id")}
```

```python
        dropped_closed = idle_close.prune_closed(stamped_pids(windows))
        if dropped_closed:
            log.info("pruned %d closed pane record(s)", dropped_closed)
```

In `lifespan`, extend the local imports next to `from periscope import activity`:

```python
    from periscope import activity, idle_close
    from periscope.panes import list_windows
    from periscope.pids import stamped_pids
```

- [x] **Step 6: Run the app tests**

Run: `uv run pytest tests/test_app.py tests/test_idle_close.py -q`
Expected: all pass.

- [x] **Step 7: Commit**

```bash
git add periscope/idle_close.py periscope/app.py tests/test_idle_close.py
git commit -m "idle close: came_back and retention for closed pane records

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 5: Resume brings a closed pane back

**Files:**
- Modify: `periscope/routes/sessions.py` (imports, `_window_new_resume`)
- Test: `tests/routes/test_sessions.py`

- [x] **Step 1: Write the failing tests**

In `tests/routes/test_sessions.py`, add to the top of the file, after the docstring:

```python
import subprocess
import time

import pytest

from tests.conftest import needs_tmux
```

After `test_window_new_resume_refuses_an_indexed_session_whose_transcript_claude_deleted`, add:

```python
# --- resume of a pane that idle close closed ----------------------------------


@pytest.fixture
def closed_pane(clean_state):
    """Session "abc" ran in pane dddd4444, which idle close closed. Returns a
    function that writes the record, with the fields you pass in."""
    from periscope import activity

    clean_state["windows"]["dddd4444"] = {
        "linked_pr": 12,
        "name_pinned": True,
        "last_seen": {"name": "notify-buttons", "session": "main", "sid": "abc", "ts": int(time.time())},
    }
    activity.set_pane_track("dddd4444", "t1")

    def record(**over):
        row = {"session_id": "abc", "pid": "dddd4444", "mem_mb": 812, "profile": None, "idle_since": 1, "closed_at": 2}
        activity.record_closed_pane(row | over)

    record()
    return record


def _resume_closed(mocker, tmp_path, has_session):
    """Resume session "abc", with no live window or Claude on it. Returns the
    route's result and the tmux calls."""
    from periscope.routes import sessions

    calls: list[tuple] = []
    _patch_resume_path(mocker, calls, tmp_path, has_session=has_session)
    _patch(mocker, "list_windows", return_value=[])
    mocker.patch("periscope.session_status.claude_proc_for", return_value=None)
    return sessions._window_new_resume("resumes", "claude --resume abc", "abc", "resume"), calls


def _spawn(calls):
    return next(list(c) for c in calls if c[0] in ("new-window", "new-session"))


# `_run` returns tmux has-session's code: 0 is the new-window branch, 1 the new-session one.
BOTH_BRANCHES = pytest.mark.parametrize("has_session", [0, 1], ids=["new-window", "new-session"])


@BOTH_BRANCHES
def test_resume_of_a_closed_pane_takes_back_its_pid_name_track_and_links(mocker, tmp_path, closed_pane, has_session):
    from periscope import activity, store
    from periscope.pids import resolve_pids

    stamp = mocker.patch("periscope.pids._stamp_pid")
    result, calls = _resume_closed(mocker, tmp_path, has_session)

    spawn = _spawn(calls)
    assert spawn[spawn.index("-n") + 1] == "notify-buttons"
    assert spawn[-5:] == [";", "set-option", "-w", "@periscope_id", "dddd4444"]
    assert result["pid"] == "dddd4444"
    assert activity.get_closed_pane("abc") is None
    # The next poll keeps the stamp, so the window gets its old entry and tag.
    window = {"session": "resumes", "index": 3, "name": "notify-buttons", "pid_raw": "dddd4444"}
    window |= {"pane_id": "%9", "window_id": "@9", "cwd": str(tmp_path), "branch": None}
    resolve_pids([window])
    assert window["pid"] == "dddd4444"
    entry = store.get_window("dddd4444")
    assert (entry["linked_pr"], entry["name_pinned"]) == (12, True)
    assert activity.get_pane_track("dddd4444") == "t1"
    # A second stamp after the spawn is the race that the chained one closes.
    stamp.assert_not_called()


@BOTH_BRANCHES
def test_resume_of_a_closed_pane_keeps_its_wrapper_profile(mocker, tmp_path, closed_pane, has_session):
    closed_pane(profile="full")
    _, calls = _resume_closed(mocker, tmp_path, has_session)

    spawn = _spawn(calls)
    assert "CLAUDE_WRAPPER_PROFILE=full" in spawn
    assert not any(a.startswith("ANTHROPIC_MODEL=") for a in spawn)
    scrubbed = any(c[0] == "set-environment" and "CLAUDE_WRAPPER_PROFILE" in c for c in calls)
    assert scrubbed == (has_session == 1), "only new-session -e sets the session env"


@BOTH_BRANCHES
def test_resume_of_a_closed_pane_on_the_default_profile_sends_no_profile(mocker, tmp_path, closed_pane, has_session):
    _, calls = _resume_closed(mocker, tmp_path, has_session)
    assert "-e" not in _spawn(calls)


def test_resume_does_not_restore_a_placeholder_name(mocker, tmp_path, closed_pane, clean_state):
    clean_state["windows"]["dddd4444"]["last_seen"]["name"] = "claude.exe"
    _, calls = _resume_closed(mocker, tmp_path, has_session=0)
    assert "-n" not in _spawn(calls)
    assert _spawn(calls)[-1] == "dddd4444"


@BOTH_BRANCHES
def test_resume_of_a_session_with_no_closed_record_adds_nothing(mocker, tmp_path, has_session):
    result, calls = _resume_closed(mocker, tmp_path, has_session)
    assert _spawn(calls)[-1] == "#{window_index}"
    assert "-n" not in _spawn(calls)
    assert "pid" not in result


@pytest.mark.parametrize("how", ["a window wears the pid", "a live Claude runs the session"])
def test_resume_of_a_session_that_came_back_takes_a_fresh_pid(mocker, tmp_path, closed_pane, how):
    from periscope import activity
    from periscope.routes import sessions

    calls: list[tuple] = []
    _patch_resume_path(mocker, calls, tmp_path)
    by_window = how == "a window wears the pid"
    _patch(mocker, "list_windows", return_value=[{"pid_raw": "dddd4444"}] if by_window else [])
    mocker.patch("periscope.session_status.claude_proc_for", return_value=None if by_window else {"pid": 1})

    result = sessions._window_new_resume("resumes", "claude --resume abc", "abc", "resume")

    assert _spawn(calls)[-1] == "#{window_index}"
    assert "pid" not in result
    assert activity.get_closed_pane("abc") is None


def test_a_failed_spawn_keeps_the_closed_record(mocker, tmp_path, closed_pane):
    from fastapi import HTTPException

    from periscope import activity
    from periscope.routes import sessions

    _patch_resume_path(mocker, [], tmp_path)
    _patch(mocker, "list_windows", return_value=[])
    mocker.patch("periscope.session_status.claude_proc_for", return_value=None)
    _patch(mocker, "_tmux_mutate", return_value=(False, "no space for new pane"))
    with pytest.raises(HTTPException):
        sessions._window_new_resume("resumes", "claude --resume abc", "abc", "resume")
    assert activity.get_closed_pane("abc") is not None


@needs_tmux
@pytest.mark.parametrize("session_exists", [True, False], ids=["new-window", "new-session"])
def test_the_resumed_window_wears_the_records_pid_in_tmux(
    mocker, tmp_path, closed_pane, tmux_test_server, session_exists
):
    from periscope.routes import sessions

    def on_test_server(cmd, **_):
        r = subprocess.run(["tmux", "-L", tmux_test_server, *cmd[1:]], capture_output=True, text=True)
        return r.returncode, r.stdout

    on_test_server(["tmux", "new-session", "-d", "-s", "resumes" if session_exists else "other"])
    _patch(mocker, "jsonl_for_session", return_value=_transcript(tmp_path, age_s=300))
    _patch(mocker, "_run", side_effect=on_test_server)
    _patch(mocker, "_send_and_stamp")
    _patch(mocker, "list_windows", return_value=[])
    mocker.patch("periscope.session_status.claude_proc_for", return_value=None)
    mocker.patch.dict(sessions._resuming, clear=True)

    sessions._window_new_resume("resumes", "claude --resume abc", "abc", "resume")

    _, listed = on_test_server(["tmux", "list-windows", "-a", "-F", "#{window_name} #{@periscope_id}"])
    assert "notify-buttons dddd4444" in listed.splitlines()
```

- [x] **Step 2: Run them to make sure that they fail**

Run: `uv run pytest tests/routes/test_sessions.py -q -k "closed or came_back or placeholder or records_pid"`
Expected: the tests of a closed pane FAIL (`ValueError: '-n' is not in list`, or the assert on `spawn[-5:]`). The two tests of a session with no record pass.

- [x] **Step 3: Write the implementation**

In `periscope/routes/sessions.py`, add `idle_close` to the `from periscope import (…)` block, in alphabetical order. Add two imports:

```python
from periscope.narrator import is_placeholder_name
```

and extend the pids import:

```python
from periscope.pids import stamp_new_window, stamped_pids
```

Add to the docstring of `_window_new_resume`, before the `force` paragraph:

```
    A session whose pane idle close closed has a record: the new window takes
    back its pid, name and wrapper profile, and the result has that `pid`.
```

After the `cwd` lines in `_window_new_resume`, add:

```python
    record = activity.get_closed_pane(resume_id)
    if record and idle_close.came_back(record, stamped_pids(list_windows())):
        # A second window with the record's pid would take its PR, ticket and name pin.
        activity.delete_closed_pane(resume_id)
        record = None
    name_args: list[str] = []
    stamp_args: list[str] = []
    profile = None
    if record:
        # Read before the spawn: the next poll writes the new window's name over it.
        name = (store.get_window(record["pid"]).get("last_seen") or {}).get("name")
        if name and not is_placeholder_name(name):
            name_args = ["-n", name]
        # In the spawn's own tmux command: a poll that lists the window unstamped
        # mints an id for it, and the PR, ticket and track stay on the old one.
        stamp_args = [";", "set-option", "-w", "@periscope_id", record["pid"]]
        profile = record["profile"]
```

In the `new-session` branch, replace the `*tmux_mod.env_args(config_dir),` line and the `"-P", "-F", "#{window_index}"` lines:

```python
            *tmux_mod.env_args(config_dir, profile),
            *name_args,
            "-P",
            "-F",
            "#{window_index}",
            *stamp_args,
```

Replace the scrub condition, and its comment's first sentence stays:

```python
        if config_dir or profile:
            tmux_mod.scrub_session_env(session)
```

In the same branch, replace the final `return {…}` with:

```python
        return _finish_resume(
            record,
            {
                "ok": True,
                "session": session,
                "index": index,
                "target": target,
                "mode": mode,
                "resumed_session_id": resume_id,
            },
        )
```

In the `new-window` branch, make the same argument change:

```python
        *tmux_mod.env_args(config_dir, profile),
        *name_args,
        "-P",
        "-F",
        "#{window_index}",
        *stamp_args,
```

Replace its final `return {…}` with:

```python
    return _finish_resume(
        record,
        {
            "ok": True,
            "session": session,
            "index": index,
            "target": target,
            "mode": mode,
            "exec": cmd,
            "resumed_session_id": resume_id,
        },
    )
```

Add the helper above `_window_new_resume`:

```python
def _finish_resume(record: activity.ClosedPane | None, result: dict) -> dict:
    """The spawn result; with a closed record, its pid, and the record is gone."""
    if record is None:
        return result
    activity.delete_closed_pane(record["session_id"])
    return {**result, "pid": record["pid"]}
```

The `new-window` call must stay without `-d`: with `-d`, the chained `set-option -w` lands on the old current window.

- [x] **Step 4: Run the sessions tests**

Run: `uv run pytest tests/routes/test_sessions.py -q`
Expected: all pass, old tests included.

- [x] **Step 5: Break it on purpose**

Remove `*stamp_args,` from the `new-window` branch and run `uv run pytest tests/routes/test_sessions.py -q -k "closed or records_pid"`. Expected: FAIL. Put it back. Then change `if config_dir or profile:` to `if config_dir:` and run the same tests. Expected: the `new-session` profile test FAILS. Put it back, and run the file once more: all pass.

- [x] **Step 6: Commit**

```bash
git add periscope/routes/sessions.py tests/routes/test_sessions.py
git commit -m "resume: a session that idle close closed takes back its pid, name and profile

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 6: `resume_session` keeps the record's pid

**Files:**
- Modify: `periscope/channels.py` (`_do_resume_session_tool`)
- Test: `tests/test_channels.py`

- [x] **Step 1: Write the failing test**

Add to `tests/test_channels.py`, after `test_resume_session_tool_wraps_window_new_resume`:

```python
def test_resume_session_tool_keeps_the_pid_of_a_closed_pane(mocker):
    from periscope.channels import _do_resume_session_tool

    mocker.patch(
        "periscope.routes.sessions._window_new_resume",
        return_value={"ok": True, "target": "resumes:3", "session": "resumes", "index": 3, "pid": "dddd4444"},
    )
    stamp = mocker.patch("periscope.channels.stamp_new_window")
    move = mocker.patch("periscope.channels.tracks.move_pane")
    mocker.patch("periscope.activity.get_track", return_value={"id": "t2", "archived_at": None})

    body = _body(_do_resume_session_tool("%5", {"session_id": "abc123", "workspace_id": "t2"}))

    assert body["pid"] == "dddd4444"
    stamp.assert_not_called()
    move.assert_called_once_with("dddd4444", "t2")
```

- [x] **Step 2: Run it to make sure that it fails**

Run: `uv run pytest tests/test_channels.py -q -k keeps_the_pid_of_a_closed_pane`
Expected: FAIL, because `stamp_new_window` was called.

- [x] **Step 3: Write the implementation**

In `_do_resume_session_tool`, replace the comment and the `new_pid` line:

```python
    # A closed pane comes back with its own pid, stamped in the spawn. Any other
    # window is new: mint one now, so the caller gets the durable handle and its
    # track without a follow-up list_claudes.
    target = result.get("target") or f"{result.get('session')}:{result.get('index')}"
    new_pid = result.get("pid") or stamp_new_window(target)
```

- [x] **Step 4: Run the channel tests**

Run: `uv run pytest tests/test_channels.py -q`
Expected: all pass.

- [x] **Step 5: Commit**

```bash
git add periscope/channels.py tests/test_channels.py
git commit -m "resume_session: keep the pid of a closed pane

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 7: `/history` says when idle close closed a session

**Files:**
- Modify: `periscope/routes/history.py` (`_annotate`), `static/historyList.js`, `static/history.js` (import, `marksOf`)
- Test: `tests/routes/test_history.py`, `static/src/__tests__/historyList.test.js`

- [x] **Step 1: Write the failing route test**

Add to `tests/routes/test_history.py`, after `test_history_search_drops_a_placeholder_pane_name`:

```python
def test_history_search_rows_say_when_idle_close_closed_the_session(client, mocker, no_transcripts):
    from periscope import activity

    activity.record_closed_pane(
        {
            "session_id": "abc",
            "pid": "dddd4444",
            "mem_mb": 812,
            "profile": None,
            "idle_since": 1,
            "closed_at": 1795078800,
        }
    )
    mocker.patch("history.recent", return_value=[{"session_id": "abc"}, {"session_id": "def"}])
    rows = client.get("/api/history/search?q=").json()["results"]
    assert [r["closed_at"] for r in rows] == [1795078800, None]
```

- [x] **Step 2: Run it to make sure that it fails**

Run: `uv run pytest tests/routes/test_history.py -q -k idle_close_closed`
Expected: FAIL with `KeyError: 'closed_at'`.

- [x] **Step 3: Write the route change**

In `periscope/routes/history.py`, import `activity`:

```python
from periscope import activity, store
```

In `_annotate`, extend the docstring's first sentence and add the lookup and the field:

```python
    """Add what periscope knows and the history index does not: why resume
    would refuse the session (null when it would resume), when idle close
    closed it (null when it did not), and the name, PR and ticket of the pane
    that last ran it. A placeholder name is null, so the page titles the row
    with the summary."""
    panes = _panes_by_session()
    closed_at = {r["session_id"]: r["closed_at"] for r in activity.closed_panes()}
```

```python
        r["closed_at"] = closed_at.get(sid)
```

- [x] **Step 4: Run the history route tests**

Run: `uv run pytest tests/routes/test_history.py -q`
Expected: all pass.

- [x] **Step 5: Write the failing page test**

Add to `static/src/__tests__/historyList.test.js`, and add `closedLabel` to its import:

```js
describe("closedLabel", () => {
  it("says the weekday and the local time, with two digits each", () => {
    const at = new Date(2026, 10, 19, 4, 0).getTime() / 1000;
    expect(closedLabel(at)).toBe("closed Thu 04:00");
  });
});
```

Run: `npx vitest run static/src/__tests__/historyList.test.js`
Expected: FAIL with `closedLabel is not a function`.

- [x] **Step 6: Write the label**

In `static/historyList.js`, after `baseName`:

```js
// When idle close closed the session, in local time: "closed Thu 04:00".
export function closedLabel(epochSec) {
  const d = new Date(epochSec * 1000);
  const two = (n) => String(n).padStart(2, "0");
  return `closed ${d.toLocaleDateString("en-US", { weekday: "short" })} ${two(d.getHours())}:${two(d.getMinutes())}`;
}
```

In `static/history.js`, add `closedLabel` to the import from `./historyList.js`, and add to `marksOf`, after the "no transcript" line:

```js
  if (r.closed_at) out.push(closedLabel(r.closed_at));
```

- [x] **Step 7: Run the page tests**

Run: `npm test`
Expected: all pass.

- [x] **Step 8: Commit**

```bash
git add periscope/routes/history.py static/historyList.js static/history.js tests/routes/test_history.py static/src/__tests__/historyList.test.js
git commit -m "history: a row says when idle close closed the session

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 8: Finish PR 1

- [x] **Step 1: Run every check**

```bash
uv run pytest -q
npm test
bin/check
git status --short
```

Expected: all tests pass, `bin/check` has no violation in a file that this PR changes, and the tree is clean.

- [x] **Step 2: Look at the `/history` label in a browser**

Start a private server with a scratch config, then seed one record for a session that its history page lists:

```bash
export XDG_CONFIG_HOME=/private/tmp/claude-501/idle-close-dev
export PERISCOPE_TMUX_SOCKET=idle-close-dev    # an empty tmux server, not the one that prod uses
mkdir -p "$XDG_CONFIG_HOME/periscope"
PERISCOPE_PORT=17376 PERISCOPE_DEV=1 uv run server.py    # run in the background
curl -s 'http://127.0.0.1:17376/api/history/search?q=&limit=1' | python3 -m json.tool | grep session_id
sqlite3 "$XDG_CONFIG_HOME/periscope/periscope-dev.db" "INSERT INTO closed_panes VALUES ('<that session id>','dddd4444',812,NULL,1,strftime('%s','now'))"
```

Open `http://127.0.0.1:17376/history`. Expected: the row shows "closed <weekday> <time>". Click no button. Stop the server, and delete the scratch folder, the screenshots and `.playwright-mcp` files.

- [x] **Step 3: Update `docs/channels.md` and the spec**

In `docs/channels.md`, replace the end of the `resume_session` entry:

```markdown
  refuses a session that looks live, one already resumed in another window,
  or one whose transcript Claude no longer keeps. A pane that idle close closed
  comes back with its own pid, name, track and wrapper profile.
```

In the spec, change the `Status:` line to `Status: PR 1 of 2 (closed record and resume) implemented.` Commit both:

```bash
git add docs/channels.md docs/superpowers/specs/2026-11-19-idle-close-design.md
git commit -m "docs: resume_session keeps a closed pane's pid

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 4: Ask before you push**

Ask the user for a yes to push the branch and open the PR `THI-466: closed pane records, and resume that brings a closed pane back`. List the assumptions that remain in the body (`docs/superpowers/plans/2026-11-23-idle-close.md`, "Assumptions for the PR bodies").

---

## PR 2: setting and sweep

Start from `main` after PR 1 merges: `git switch -c thibaultdody/thi-466-idle-close-sweep origin/main`.

### Task 9: `panes` in the window row

**Files:**
- Modify: `periscope/tmux.py` (`PANE_FIELDS`), `periscope/panes.py` (`list_windows`)
- Test: `tests/test_tmux.py` (`_FIELDS`), `tests/test_panes.py` (`_PANES` and its expected rows)

- [ ] **Step 1: Write the failing test changes**

In `tests/test_tmux.py`, extend `_FIELDS` with the pane count:

```python
_FIELDS = ["main", "0", "shell", "1", "/home/tom", "", "%5", "1717250000", "@1", "4242", "zsh", "", "1", "1"]
```

In `tests/test_panes.py`, append the pane count to each row of `_PANES`: `"2"` to both `main:0` rows, and `"1"` to the `main:1` row and the `INPUT_CTL_SESSION` row. In `test_list_windows_reads_each_windows_active_pane`, add `"panes": 2` to the first expected dict and `"panes": 1` to the second.

`test_list_windows_matches_list_windows_on_a_live_server` compares with a real tmux. Make its oracle read the count too: add a last field to `_LIST_WINDOWS_FMT`, and a last key to the dict in `_list_windows_rows`:

```python
    "#{pane_current_command}\t#{@periscope_opened}\t#{window_panes}"
```

```python
                "opened": p[11],
                "panes": int(p[12]),
```

- [ ] **Step 2: Run them to make sure that they fail**

Run: `uv run pytest tests/test_tmux.py tests/test_panes.py -q -k "list_panes or list_windows or embedded_newline"`
Expected: FAIL. The rows have one field too many, so `list_panes` skips them, and the live rows have no `panes`.

- [ ] **Step 3: Write the implementation**

In `periscope/tmux.py`, add to `PANE_FIELDS` after `"pane_active"`:

```python
    "window_panes",
```

In `periscope/panes.py`, in the `list_windows` row after `"opened"`:

```python
                "panes": int(p["window_panes"] or 0),
```

`build_window_view` starts from the window row, so the view gets `panes` too.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q`
Expected: all pass. A failure here is another test that builds a raw row: add the field there.

- [ ] **Step 5: Commit**

```bash
git add periscope/tmux.py periscope/panes.py tests/test_tmux.py tests/test_panes.py
git commit -m "panes: list_windows says how many panes a window has

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 10: the setting

**Files:**
- Modify: `periscope/store.py` (`Settings`), `periscope/routes/settings.py`
- Test: `tests/routes/test_settings.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/routes/test_settings.py`:

```python
@pytest.mark.parametrize("days", [1, 3, 30])
def test_patch_idle_close_weekdays_takes_1_to_30(client, clean_state, days):
    assert client.patch("/api/settings", json={"idle_close_weekdays": days}).status_code == 200
    assert clean_state["settings"]["idle_close_weekdays"] == days


@pytest.mark.parametrize("days", [0, 31])
def test_patch_idle_close_weekdays_refuses_a_number_out_of_range(client, clean_state, days):
    assert client.patch("/api/settings", json={"idle_close_weekdays": days}).status_code == 400
    assert "idle_close_weekdays" not in clean_state["settings"]


# A lax int reads `true` as 1, the most aggressive value.
@pytest.mark.parametrize("days", [True, "3", 3.5])
def test_patch_idle_close_weekdays_refuses_anything_but_an_integer(client, clean_state, days):
    assert client.patch("/api/settings", json={"idle_close_weekdays": days}).status_code == 422
    assert "idle_close_weekdays" not in clean_state["settings"]


def test_patch_idle_close_weekdays_null_turns_it_off(client, clean_state):
    clean_state["settings"]["idle_close_weekdays"] = 3
    assert client.patch("/api/settings", json={"idle_close_weekdays": None}).status_code == 200
    assert "idle_close_weekdays" not in clean_state["settings"]
```

Add `import pytest` at the top of the file if it is not there.

- [ ] **Step 2: Run them to make sure that they fail**

Run: `uv run pytest tests/routes/test_settings.py -q -k idle_close`
Expected: FAIL. The model ignores the unknown field, so every PATCH gives 200 and stores nothing.

- [ ] **Step 3: Write the implementation**

In `periscope/store.py`, in `Settings` after `cleanup_idle_days`:

```python
    idle_close_weekdays: int  # 1-30; unset => idle close is off (periscope.idle_close)
```

In `periscope/routes/settings.py`, import `StrictInt` from pydantic and add to `SettingsPatch` after `cleanup_idle_days`:

```python
    idle_close_weekdays: StrictInt | None = None  # StrictInt: a lax int reads `true` as 1
```

After the `cleanup_idle_days` block in `settings_patch`:

```python
    if "idle_close_weekdays" in sent:
        v = body.idle_close_weekdays
        if v is None or 1 <= v <= 30:
            patch["idle_close_weekdays"] = v
        else:
            raise HTTPException(400, f"idle_close_weekdays must be 1..30: {v!r}")
```

- [ ] **Step 4: Run the settings tests**

Run: `uv run pytest tests/routes/test_settings.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add periscope/store.py periscope/routes/settings.py tests/routes/test_settings.py
git commit -m "settings: idle_close_weekdays, off when unset

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 11: the rules

**Files:**
- Modify: `periscope/idle_close.py`
- Test: `tests/test_idle_close.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_idle_close.py`, add to the imports:

```python
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
```

and at the end of the file:

```python
# --- the rules ----------------------------------------------------------------

MON, FRI = datetime(2026, 11, 16, 17), datetime(2026, 11, 20, 4)
DAY = 24 * 3600


def ts(at: datetime) -> int:
    return int(at.timestamp())


@pytest.mark.parametrize(
    "start,end,hours",
    [
        (datetime(2026, 11, 18, 10), datetime(2026, 11, 18, 13), 3),  # same day
        (MON, datetime(2026, 11, 19, 4), 59),  # the spec's example: Thursday 04:00
        (MON, FRI, 83),  # Friday 04:00
        (datetime(2026, 11, 20, 17), datetime(2026, 11, 23, 4), 11),  # Friday to Monday
        (datetime(2026, 11, 21, 9), datetime(2026, 11, 23, 4), 4),  # starts on Saturday
        (datetime(2026, 11, 19, 4), datetime(2026, 11, 22, 12), 44),  # ends on Sunday
    ],
)
def test_weekday_hours_skip_saturday_and_sunday(start, end, hours):
    assert idle_close.weekday_hours(start, end) == hours


def test_idle_since_is_the_latest_time_the_pane_was_used():
    assert idle_close.idle_since(300.9, 200, 100) == 300
    assert idle_close.idle_since(100.0, 0, 250) == 250
    assert idle_close.idle_since(None, 0, 0) is None


def pane(**over):
    base = {
        "pid": "aaaa1111",
        "pane_id": "%1",
        "name": "notify-buttons",
        "session_id": "s1",
        "state": "idle",
        "panes": 1,
        "idle_since": ts(MON),
        "transcript_mtime": float(ts(MON)),
        "mem_mb": 812,
        "profile": None,
    }
    return {**base, **over}


def verdict(at=FRI, weekdays=3, pinned=frozenset(), **over):
    return idle_close.verdict(pane(**over), at, weekdays, pinned)


def test_verdict_closes_a_pane_at_exactly_the_limit():
    # 72 weekday hours: Monday 17:00 to Thursday 17:00.
    assert verdict(at=datetime(2026, 11, 19, 17)) == "closes"
    assert verdict(at=datetime(2026, 11, 19, 16, 59)) is None


def test_verdict_leaves_out_a_pane_under_the_limit_or_with_no_stamp():
    assert verdict(at=datetime(2026, 11, 19, 4)) is None
    assert verdict(idle_since=None) is None


@pytest.mark.parametrize(
    "over,code",
    [
        ({"session_id": None}, "no-transcript"),
        ({"transcript_mtime": None}, "no-transcript"),
        ({"panes": 2}, "split-window"),
        ({"state": "needs-input"}, "needs-input"),
        ({"state": "done"}, "done"),
        ({"state": "working"}, "working"),
    ],
)
def test_verdict_names_why_a_pane_stays(over, code):
    assert verdict(**over) == code


def test_verdict_keeps_a_pinned_pane():
    assert verdict(pinned={"aaaa1111"}) == "pinned"


def test_verdict_says_weekend_for_a_pane_over_the_limit_only_in_wall_clock_hours():
    # Friday 17:00 to Monday 18:00 is 73 hours, but only 25 weekday hours.
    assert verdict(at=datetime(2026, 11, 23, 18), idle_since=ts(datetime(2026, 11, 20, 17))) == "weekend"


def due(now, settings=None, log=None):
    return idle_close.due(now, {"idle_close_weekdays": 3} if settings is None else settings, log or {})


def test_due_from_04_00_for_90_minutes_once_a_night():
    assert not due(datetime(2026, 11, 20, 3, 59))
    assert due(datetime(2026, 11, 20, 4, 0))
    assert due(datetime(2026, 11, 20, 5, 29))
    assert not due(datetime(2026, 11, 20, 5, 30))
    assert not due(FRI, log={"date": "2026-11-20", "at": 1})
    assert due(FRI, log={"date": "2026-11-19", "at": 1})


def test_due_never_with_the_setting_off():
    assert not due(FRI, settings={})


def test_next_sweep_is_the_next_04_00():
    assert idle_close.next_sweep(datetime(2026, 11, 20, 3, 59)) == FRI
    assert idle_close.next_sweep(FRI) == datetime(2026, 11, 21, 4)
    assert idle_close.next_sweep(datetime(2026, 11, 20, 22)) == datetime(2026, 11, 21, 4)


# --- pane_from_view -----------------------------------------------------------


def view(**over):
    return {
        "agent": "claude",
        "pid": "aaaa1111",
        "pane_id": "%1",
        "name": "notify-buttons",
        "state": "idle",
        "panes": 1,
        "session": "main",
        "index": 3,
        "completed_at": 0,
        "acted_at": 0,
        "focused_at": 0,
        "res": {"mem_mb": 812, "cpu_pct": 0.0},
        "profile": "default",
        **over,
    }


@pytest.fixture
def transcript(monkeypatch, tmp_path):
    """The transcript of session s1, which runs in pane %1: last written on
    Monday at 17:00."""
    import os

    path = tmp_path / "s1.jsonl"
    path.write_text("{}\n")
    os.utime(path, (ts(MON), ts(MON)))
    monkeypatch.setattr(idle_close, "session_id_for_pane", {"%1": "s1"}.get)
    monkeypatch.setattr(idle_close, "jsonl_for_session", {"s1": path}.get)
    return path


def test_pane_from_view_reads_the_view_and_the_transcript(transcript):
    assert idle_close.pane_from_view(view()) == pane()


def test_pane_from_view_does_not_count_terminal_focus(transcript):
    assert idle_close.pane_from_view(view(focused_at=ts(FRI))) == pane()


@pytest.mark.parametrize("agent", ["codex", None])
def test_pane_from_view_skips_a_pane_that_is_not_claude(transcript, agent):
    assert idle_close.pane_from_view(view(agent=agent)) is None


def test_pane_from_view_with_no_process_snapshot_can_still_close(transcript):
    got = idle_close.pane_from_view(view(res=None))
    assert got is not None and got["mem_mb"] is None
    assert idle_close.verdict(got, FRI, 3, set()) == "closes"


def test_pane_from_view_reads_the_wrapper_profile(transcript):
    assert idle_close.pane_from_view(view(profile="full"))["profile"] == "full"
    assert idle_close.pane_from_view(view(profile="default"))["profile"] is None


def test_pane_from_view_finds_no_transcript_outside_the_default_projects_folder(monkeypatch):
    # jsonl_for_session looks only in ~/.claude/projects, so an account-B pane stays.
    monkeypatch.setattr(idle_close, "session_id_for_pane", lambda pane_id: "s9")
    monkeypatch.setattr(idle_close, "jsonl_for_session", lambda sid: None)
    got = idle_close.pane_from_view(view(completed_at=ts(MON)))
    assert got["transcript_mtime"] is None
    assert idle_close.verdict(got, FRI, 3, set()) == "no-transcript"
```

- [ ] **Step 2: Run them to make sure that they fail**

Run: `uv run pytest tests/test_idle_close.py -q`
Expected: ERROR with `AttributeError: module 'periscope.idle_close' has no attribute 'Pane'`.

- [ ] **Step 3: Write the rules**

In `periscope/idle_close.py`, replace the imports with:

```python
from collections.abc import Collection, Mapping
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import TypedDict

from periscope import activity, config, session_status
from periscope.store import Settings
from periscope.turns import jsonl_for_session, session_id_for_pane

SWEEP_AT = time(4, 0)
_GRACE_MIN = 90
```

Add after `prune_closed`:

```python
class Pane(TypedDict):
    """What the sweep reads about one Claude pane."""

    pid: str
    pane_id: str
    name: str
    session_id: str | None
    state: str
    panes: int  # in its window
    idle_since: int | None
    transcript_mtime: float | None
    mem_mb: int | None
    profile: str | None  # wrapper profile, None for the default


def weekday_hours(start: datetime, end: datetime) -> float:
    """The hours from `start` to `end` on Monday to Friday, in local time."""
    hours = 0.0
    while start < end:
        midnight = datetime.combine(start.date() + timedelta(days=1), time())
        upto = min(midnight, end)
        if start.weekday() < 5:
            hours += (upto - start).total_seconds() / 3600
        start = upto
    return hours


def idle_since(transcript_mtime: float | None, completed_at: int, acted_at: int) -> int | None:
    """When the pane was last used, or None when nothing says. 0 is no stamp."""
    return int(max(transcript_mtime or 0, completed_at, acted_at)) or None


def verdict(pane: Pane, at: datetime, weekdays: int, pinned: Collection[str]) -> str | None:
    """What the sweep does with `pane` at `at`: "closes", or why it stays. None
    for a pane that is not near the limit."""
    limit = weekdays * 24
    if pane["idle_since"] is None:
        return None
    since = datetime.fromtimestamp(pane["idle_since"])
    if (at - since).total_seconds() / 3600 < limit:
        return None
    if not pane["session_id"] or pane["transcript_mtime"] is None:
        return "no-transcript"  # Resume refuses it, so a record could never resume
    if pane["pid"] in pinned:
        return "pinned"
    if pane["panes"] > 1:
        return "split-window"  # the window keeps the pid after kill-pane
    if pane["state"] != "idle":
        return pane["state"]
    return "closes" if weekday_hours(since, at) >= limit else "weekend"


def due(now: datetime, settings: Settings, log: Mapping[str, object]) -> bool:
    """True for 90 minutes from SWEEP_AT, when the setting is on and the sweep
    did not run today. A Mac that sleeps past it skips the night."""
    if not settings.get("idle_close_weekdays"):
        return False
    start = SWEEP_AT.hour * 60 + SWEEP_AT.minute
    return start <= now.hour * 60 + now.minute < start + _GRACE_MIN and log.get("date") != now.strftime("%Y-%m-%d")


def next_sweep(now: datetime) -> datetime:
    at = datetime.combine(now.date(), SWEEP_AT)
    return at if now < at else at + timedelta(days=1)


def _mtime(transcript: Path | None) -> float | None:
    """None when Claude has no transcript, or deleted it."""
    try:
        return transcript.stat().st_mtime if transcript else None
    except OSError:
        return None


def pane_from_view(view: dict) -> Pane | None:
    """The sweep's view of a dashboard pane; None for a pane that is not Claude."""
    if view.get("agent") != "claude":
        return None
    session_id = session_id_for_pane(view["pane_id"])
    mtime = _mtime(jsonl_for_session(session_id) if session_id else None)
    return {
        "pid": view["pid"],
        "pane_id": view["pane_id"],
        "name": view["name"],
        "session_id": session_id,
        "state": view["state"],
        "panes": view["panes"],
        "idle_since": idle_since(mtime, view["completed_at"], view["acted_at"]),
        "transcript_mtime": mtime,
        "mem_mb": (view.get("res") or {}).get("mem_mb"),
        "profile": config.profile_env(view["profile"]) or None,
    }
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_idle_close.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add periscope/idle_close.py tests/test_idle_close.py
git commit -m "idle close: weekday hours, idle since, and one verdict for each pane

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 12: `save_now(force=True)`

**Files:**
- Modify: `periscope/resurrect.py` (`save_now`)
- Test: `tests/test_resurrect_save.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_resurrect_save.py`:

```python
def test_save_now_force_clears_the_last_save_time_before_it_runs_the_script(mocker):
    mocker.patch.object(config, "is_prod", return_value=True)
    mocker.patch.object(Path, "exists", return_value=True)
    run = mocker.patch("periscope.resurrect.subprocess.run")
    resurrect.save_now(force=True)
    argvs = [c.args[0] for c in run.call_args_list]
    assert argvs[0] == ["tmux", "set-option", "-gu", "@continuum-save-last-timestamp"]
    assert "continuum_save.sh" in argvs[1][0]
    assert len(argvs) == 2


def test_a_plain_save_now_keeps_the_last_save_time(mocker):
    mocker.patch.object(config, "is_prod", return_value=True)
    mocker.patch.object(Path, "exists", return_value=True)
    run = mocker.patch("periscope.resurrect.subprocess.run")
    resurrect.save_now()
    assert not any("set-option" in c.args[0] for c in run.call_args_list)
```

- [ ] **Step 2: Run them to make sure that they fail**

Run: `uv run pytest tests/test_resurrect_save.py -q`
Expected: the first new test FAILS with `TypeError: save_now() got an unexpected keyword argument 'force'`.

- [ ] **Step 3: Write the implementation**

In `periscope/resurrect.py`, above `save_now`:

```python
# continuum_save.sh does nothing until its interval has passed since this time.
_LAST_SAVE_OPTION = "@continuum-save-last-timestamp"
```

Change the signature to `def save_now(force: bool = False) -> None:`, add to the end of the docstring:

```
    `force` saves now, whatever the interval: the script reads a cleared
    timestamp as 0, saves, and writes a new one.
```

and before the last `subprocess.run`:

```python
    if force:
        subprocess.run(["tmux", "set-option", "-gu", _LAST_SAVE_OPTION], capture_output=True, check=False)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_resurrect_save.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add periscope/resurrect.py tests/test_resurrect_save.py
git commit -m "resurrect: save_now(force=True) saves inside continuum's interval

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 13: the sweep

**Files:**
- Modify: `periscope/idle_close.py`, `tests/conftest.py` (`_no_plan_usage_refresh`)
- Test: `tests/test_idle_close.py`

- [ ] **Step 1: Neuter the thread in conftest**

In `tests/conftest.py`, at the end of `_no_plan_usage_refresh`:

```python
    # The sweep's thread writes the activity DB, kills panes, and indexes into
    # the real ~/.claude/history.db (and can call Haiku).
    from periscope import idle_close

    monkeypatch.setattr(idle_close, "_bg", lambda *a, **kw: None, raising=False)
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_idle_close.py`, and add `import os`, `import subprocess` and `from tests.conftest import needs_tmux` at the top:

```python
# --- the sweep ----------------------------------------------------------------


@pytest.fixture
def night(monkeypatch, tmp_path):
    """Friday 04:00, with two closable panes: %1 (session s1) and %2 (s2). Each
    ran last on Monday at 17:00, in a window with one pane. `night` holds what
    the sweep read and did; a test changes the parts that it is about."""
    n = SimpleNamespace(
        sids={"%1": "s1", "%2": "s2"}, states={}, paths={}, kills=[], kill_ok=True, saves=[], indexed=[]
    )
    for sid in ("s1", "s2"):
        path = tmp_path / f"{sid}.jsonl"
        path.write_text("{}\n")
        os.utime(path, (ts(MON), ts(MON)))
        n.paths[sid] = path
    n.windows = [
        {"pane_id": f"%{i}", "session": "main", "index": i, "panes": 1, "pid_raw": f"aaaa111{i}"} for i in (1, 2)
    ]
    n.views = [view(pane_id=f"%{i}", pid=f"aaaa111{i}", index=i) for i in (1, 2)]

    def kill(*args):
        n.kills.append((args, activity.get_closed_pane(n.sids[args[-1]])))
        return n.kill_ok, "" if n.kill_ok else "no such pane"

    monkeypatch.setattr(idle_close, "_now", lambda: FRI)
    monkeypatch.setattr(idle_close, "list_windows", lambda: n.windows)
    monkeypatch.setattr(idle_close, "session_id_for_pane", lambda pane_id: n.sids.get(pane_id))
    monkeypatch.setattr(idle_close, "jsonl_for_session", lambda sid: n.paths.get(sid))
    monkeypatch.setattr(
        idle_close.session_status, "session_state_for", lambda sid: {"state": n.states.get(sid, "idle")}
    )
    monkeypatch.setattr(idle_close.session_status, "claude_proc_for", lambda sid: None)
    monkeypatch.setattr(idle_close, "_tmux_mutate", kill)
    monkeypatch.setattr(idle_close, "drop_target_focus", lambda target: None)
    monkeypatch.setattr(idle_close.resurrect, "save_now", lambda force=False: n.saves.append(force))
    monkeypatch.setattr("history.indexer.index_one", lambda path, force=False: n.indexed.append((path, force)))
    return n


def closed():
    return [r["session_id"] for r in activity.closed_panes()]


def test_sweep_records_each_pane_then_kills_it_by_pane_id(night):
    idle_close.sweep(night.views, 3)
    assert [args for args, _ in night.kills] == [("kill-pane", "-t", "%1"), ("kill-pane", "-t", "%2")]
    assert all(record for _, record in night.kills), "the record comes before the kill"
    assert closed() == ["s1", "s2"]
    assert activity.get_closed_pane("s1") == {
        "session_id": "s1",
        "pid": "aaaa1111",
        "mem_mb": 812,
        "profile": None,
        "idle_since": ts(MON),
        "closed_at": ts(FRI),
    }


def test_sweep_keeps_the_panes_wrapper_profile(night):
    night.views[0]["profile"] = "full"
    idle_close.sweep(night.views, 3)
    assert activity.get_closed_pane("s1")["profile"] == "full"


def test_sweep_saves_tmux_once_and_indexes_each_closed_session(night):
    idle_close.sweep(night.views, 3)
    assert night.saves == [True]
    assert night.indexed == [(str(night.paths["s1"]), True), (str(night.paths["s2"]), True)]


def test_sweep_with_no_pane_to_close_saves_and_indexes_nothing(night):
    idle_close.sweep([view(state="working")], 3)
    assert (night.kills, night.saves, night.indexed) == ([], [], [])


def test_a_failed_kill_deletes_the_record(night, caplog):
    night.kill_ok = False
    idle_close.sweep(night.views, 3)
    assert closed() == []
    assert (night.saves, night.indexed) == ([], [])
    assert "no such pane" in caplog.text


def test_one_failing_pane_does_not_stop_the_others(night, monkeypatch):
    def session_id(pane_id):
        if pane_id == "%1":
            raise RuntimeError("boom")
        return night.sids[pane_id]

    monkeypatch.setattr(idle_close, "session_id_for_pane", session_id)
    idle_close.sweep(night.views, 3)
    assert closed() == ["s2"]


def test_an_index_failure_does_not_stop_the_others(night, monkeypatch):
    def index(path, force=False):
        if path.endswith("s1.jsonl"):
            raise RuntimeError("boom")
        night.indexed.append(path)

    monkeypatch.setattr("history.indexer.index_one", index)
    idle_close.sweep(night.views, 3)
    assert night.indexed == [str(night.paths["s2"])]


def test_a_pane_with_no_stamp_stays_beside_one_that_closes(night):
    night.sids["%2"] = None
    idle_close.sweep(night.views, 3)
    assert closed() == ["s1"]


def test_a_pinned_pane_stays(night, clean_state):
    clean_state["ui"]["pinned_pids"] = ["aaaa1111"]
    idle_close.sweep(night.views, 3)
    assert closed() == ["s2"]


def _turn_after_the_snapshot(night, monkeypatch):
    real = idle_close.list_windows
    calls = []

    def list_windows():
        calls.append(1)
        if len(calls) == 2:  # the first call is retention's, before the panes are read
            os.utime(night.paths["s1"], (ts(FRI), ts(FRI)))
        return real()

    monkeypatch.setattr(idle_close, "list_windows", list_windows)


def _session_rotated(night, monkeypatch):
    seen = []

    def session_id(pane_id):
        seen.append(pane_id)
        return "s1-new" if seen.count("%1") > 1 else night.sids[pane_id]

    monkeypatch.setattr(idle_close, "session_id_for_pane", session_id)


@pytest.mark.parametrize(
    "change",
    [
        lambda n, mp: n.windows.pop(0),  # the pane is gone
        lambda n, mp: n.windows[0].update(panes=2),  # the window was split
        lambda n, mp: n.states.update(s1="working"),
        _session_rotated,
        _turn_after_the_snapshot,
    ],
    ids=["gone", "split", "working", "new session", "new turn"],
)
def test_the_re_check_skips_a_pane_that_changed(night, monkeypatch, change):
    change(night, monkeypatch)
    idle_close.sweep(night.views[:1], 3)
    assert (closed(), night.kills) == ([], [])


def test_a_second_close_of_the_same_session_replaces_its_row(night):
    activity.record_closed_pane({**RECORD, "session_id": "s1", "pid": "bbbb2222", "closed_at": 1})
    idle_close.sweep(night.views, 3)
    assert activity.get_closed_pane("s1")["pid"] == "aaaa1111"


def test_sweep_prunes_first(night):
    activity.record_closed_pane({**RECORD, "session_id": "gone", "pid": "cccc3333"})
    idle_close.sweep([], 3)
    assert closed() == []


@needs_tmux
def test_sweep_closes_the_real_window(night, monkeypatch, tmux_test_server):
    from periscope import panes, tmux

    def on_test_server(*args):
        return subprocess.run(["tmux", "-L", tmux_test_server, *args], capture_output=True, text=True).stdout.strip()

    on_test_server("new-session", "-d", "-s", "main", "cat")
    pane_id = on_test_server("new-window", "-t", "main:", "-P", "-F", "#{pane_id}", "cat")
    night.sids[pane_id] = "s1"
    monkeypatch.setattr(idle_close, "list_windows", panes.list_windows)
    monkeypatch.setattr(idle_close, "_tmux_mutate", tmux._tmux_mutate)
    idle_close.sweep([view(pane_id=pane_id)], 3)
    assert closed() == ["s1"]
    assert pane_id not in on_test_server("list-panes", "-a", "-F", "#{pane_id}").splitlines()
    assert "main" in on_test_server("list-sessions", "-F", "#{session_name}")
```

- [ ] **Step 3: Run them to make sure that they fail**

Run: `uv run pytest tests/test_idle_close.py -q -k "sweep or re_check or kill or pinned or index or second_close or failing"`
Expected: ERROR with `AttributeError: … has no attribute '_now'`.

- [ ] **Step 4: Write the sweep**

In `periscope/idle_close.py`, extend the imports:

```python
from periscope import activity, config, resurrect, session_status, store
from periscope.log import log
from periscope.panes import drop_target_focus, list_windows
from periscope.pids import stamped_pids
from periscope.store import Settings
from periscope.tmux import _tmux_mutate
from periscope.turns import jsonl_for_session, session_id_for_pane
```

Add at the end of the file:

```python
def _now() -> datetime:
    return datetime.now()


def _pinned() -> set[str]:
    return set(store.get_ui().get("pinned_pids") or [])


def _close(pane: Pane, at: datetime) -> Path | None:
    """Close `pane`, and return its transcript. None when it changed since the
    sweep read it (the views can be 13 seconds old), or when the kill failed."""
    session_id = pane["session_id"]
    window = next((w for w in list_windows() if w["pane_id"] == pane["pane_id"]), None)
    if not session_id or window is None or window["panes"] != 1:
        return None
    if session_id_for_pane(pane["pane_id"]) != session_id:
        return None
    if (session_status.session_state_for(session_id) or {}).get("state") != "idle":
        return None
    transcript = jsonl_for_session(session_id)
    # The only check that sees a short turn that started and ended in between.
    if transcript is None or _mtime(transcript) != pane["transcript_mtime"]:
        return None
    assert pane["idle_since"] is not None  # verdict() closes no pane without it
    activity.record_closed_pane(
        {
            "session_id": session_id,
            "pid": pane["pid"],
            "mem_mb": pane["mem_mb"],
            "profile": pane["profile"],
            "idle_since": pane["idle_since"],
            "closed_at": int(at.timestamp()),
        }
    )
    # By pane id: `renumber-windows on` moves the indexes during the loop.
    ok, msg = _tmux_mutate("kill-pane", "-t", pane["pane_id"])
    if not ok:
        activity.delete_closed_pane(session_id)
        log.warning("idle close: could not kill %s (%s): %s", pane["pane_id"], pane["name"], msg)
        return None
    drop_target_focus(f"{window['session']}:{window['index']}")
    log.info("idle close: closed %s (%s), session %s", pane["pane_id"], pane["name"], session_id)
    return transcript


def sweep(views: list[dict], weekdays: int) -> None:
    """Worker-thread body: close every pane whose verdict is "closes"."""
    at = _now()
    prune_closed(stamped_pids(list_windows()))
    pinned = _pinned()
    transcripts: list[Path] = []
    for view in views:
        try:
            pane = pane_from_view(view)
            if pane and verdict(pane, at, weekdays, pinned) == "closes" and (transcript := _close(pane, at)):
                transcripts.append(transcript)
        except Exception:
            log.exception("idle close: pane %s failed", view.get("pane_id"))
    if not transcripts:
        return
    # A reboot before the next continuum save would bring the closed panes back.
    resurrect.save_now(force=True)
    from history.indexer import index_one

    # Claude's SessionEnd hook may not fire when tmux kills the pane.
    for transcript in transcripts:
        try:
            index_one(str(transcript), force=True)
        except Exception:
            log.exception("idle close: index_one failed for %s", transcript)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_idle_close.py -q`
Expected: all pass. If `needs_tmux` tests are skipped, install tmux: they are the only check of the real kill.

- [ ] **Step 6: Break it on purpose**

Delete the `_mtime(transcript) != pane["transcript_mtime"]` check and run the file. Expected: the "new turn" case FAILS. Put it back. Move `activity.record_closed_pane(…)` after the kill and run the file. Expected: `test_sweep_records_each_pane_then_kills_it_by_pane_id` FAILS. Put it back, and run the file once more: all pass.

- [ ] **Step 7: Commit**

```bash
git add periscope/idle_close.py tests/test_idle_close.py tests/conftest.py
git commit -m "idle close: the sweep records a pane, checks it again and kills it

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 14: the tick and the prod task

**Files:**
- Modify: `periscope/store.py`, `periscope/idle_close.py`, `periscope/app.py`
- Test: `tests/test_idle_close.py`, `tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_idle_close.py`, and add `import asyncio` and `from periscope import store` at the top:

```python
# --- the tick -----------------------------------------------------------------


@pytest.fixture
def ticking(monkeypatch, clean_state):
    """Friday 04:00 with the setting at 3 weekdays. Collects what the tick
    handed to a thread."""
    clean_state["settings"]["idle_close_weekdays"] = 3
    spawned = []

    async def current_windows():
        return [view()]

    monkeypatch.setattr(idle_close, "_now", lambda: FRI)
    monkeypatch.setattr(idle_close, "_bg", lambda name, fn, *a: spawned.append((name, fn, a)))
    monkeypatch.setattr(idle_close.state_hub, "current_windows", current_windows)
    return spawned


def test_tick_logs_the_night_and_hands_one_sweep_to_a_thread(ticking):
    asyncio.run(idle_close._tick())
    assert ticking == [("idle-close", idle_close.sweep, ([view()], 3))]
    assert store.get_idle_close_log() == {"date": "2026-11-20", "at": ts(FRI)}
    asyncio.run(idle_close._tick())
    assert len(ticking) == 1


def test_tick_does_nothing_outside_the_window(ticking, monkeypatch):
    monkeypatch.setattr(idle_close, "_now", lambda: datetime(2026, 11, 20, 5, 30))
    asyncio.run(idle_close._tick())
    assert ticking == []
    assert store.get_idle_close_log() == {}


def test_a_tick_that_fails_does_not_start_a_second_sweep_that_night(ticking, monkeypatch):
    async def boom():
        raise RuntimeError("no tmux")

    monkeypatch.setattr(idle_close.state_hub, "current_windows", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(idle_close._tick())
    asyncio.run(idle_close._tick())
    assert ticking == []
    assert store.get_idle_close_log()["date"] == "2026-11-20"
```

In `tests/test_app.py`, next to each of the two `mocker.patch("periscope.poke.run", side_effect=_noop)` lines, add:

```python
    # Prod-gated too, and its tick can kill panes: between 04:00 and 05:30 a
    # real one would sweep the developer's tmux server.
    mocker.patch("periscope.idle_close.run", side_effect=_noop)
```

- [ ] **Step 2: Run them to make sure that they fail**

Run: `uv run pytest tests/test_idle_close.py tests/test_app.py -q`
Expected: FAIL. The module has no `state_hub` or `run`.

- [ ] **Step 3: Write the log accessors**

In `periscope/store.py`, after `PokeEntry`:

```python
class IdleCloseLog(TypedDict, total=False):
    """The last night that idle close ran."""

    date: str  # local YYYY-MM-DD
    at: int
```

Add `"idle_close_log": {},` to `_STATE_DEFAULTS`, and to `_V2_EMPTY_DEFAULTS` in `tests/test_store.py`. After `record_poke`:

```python
def get_idle_close_log() -> IdleCloseLog:
    """`.get` because a state file written before the key existed loads without it."""
    with _STATE_LOCK:
        return cast(IdleCloseLog, dict(_STATE.get("idle_close_log") or {}))


def record_idle_close(entry: IdleCloseLog) -> None:
    with _STATE_LOCK:
        _STATE["idle_close_log"] = dict(entry)
        _write_state(_STATE)
```

- [ ] **Step 4: Write the tick and the loop**

In `periscope/idle_close.py`, add `import asyncio` as the first import, add `state_hub` to the `from periscope import` line, change the log import to `from periscope.log import _bg, log`, and add `_TICK_S = 60.0` after `_GRACE_MIN`. At the end of the file:

```python
async def _tick() -> None:
    now, settings = _now(), store.get_settings()
    if not due(now, settings, store.get_idle_close_log()):
        return
    # Before the work: a failing pane, or a sweep longer than a tick, must not
    # start a second sweep.
    store.record_idle_close({"date": now.strftime("%Y-%m-%d"), "at": int(now.timestamp())})
    views = await state_hub.current_windows()
    _bg("idle-close", sweep, views, settings["idle_close_weekdays"])


async def run() -> None:
    """Lifespan task, prod only (registered in app.lifespan)."""
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("idle close tick failed")
        await asyncio.sleep(_TICK_S)
```

- [ ] **Step 5: Register the task**

In `periscope/app.py`, after `poke_task = _task("poke", poke.run())` in the prod branch:

```python
        # Idle close (periscope.idle_close): kills idle Claude panes at 04:00.
        # Prod only: dev shares prod's tmux.
        idle_close_task = _task("idle-close", idle_close.run())
```

In the `else` branch, after `poke_task = None`:

```python
        idle_close_task = None
```

In the `finally` block, after the poke cancel:

```python
        if idle_close_task is not None:
            idle_close_task.cancel()
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_idle_close.py tests/test_app.py tests/test_store.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add periscope/store.py periscope/idle_close.py periscope/app.py tests/test_idle_close.py tests/test_app.py tests/test_store.py
git commit -m "idle close: one sweep a night at 04:00, prod only

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 15: the preview and the settings row

**Files:**
- Create: `periscope/routes/idle_close.py`, `tests/routes/test_idle_close.py`, `static/src/overlays/idleClose.js`, `static/src/overlays/__tests__/idleClose.test.js`
- Modify: `periscope/idle_close.py` (`preview`), `periscope/app.py` (router), `static/src/overlays/SettingsModal.jsx`, `static/styles.css`, `static/dist/`

- [ ] **Step 1: Write the failing route tests**

Create `tests/routes/test_idle_close.py`:

```python
"""GET /api/idle-close/preview: the panes that close at the next 04:00."""

import os
from datetime import datetime

import pytest

from periscope import idle_close

# Monday 19:00. The next sweep is Tuesday 04:00, and 3 weekdays is 72 hours.
NOW = datetime(2026, 11, 23, 19)
OLD = datetime(2026, 11, 18, 1)  # Wednesday 01:00: 99 weekday hours at the sweep


def ts(at):
    return int(at.timestamp())


@pytest.fixture
def board(monkeypatch, tmp_path, clean_state):
    """Adds a Claude pane: `add("closes")`. Each one ran last at `idle`."""
    clean_state["settings"]["idle_close_weekdays"] = 3
    clean_state["ui"]["pinned_pids"] = ["pinned00"]
    views, sids, paths = [], {}, {}

    async def current_windows():
        return views

    monkeypatch.setattr("periscope.state_hub.current_windows", current_windows)
    monkeypatch.setattr(idle_close, "_now", lambda: NOW)
    monkeypatch.setattr(idle_close, "session_id_for_pane", sids.get)
    monkeypatch.setattr(idle_close, "jsonl_for_session", paths.get)

    def add(name, idle=OLD, transcript=True, pid="aaaa1111", **over):
        pane_id = f"%{len(views)}"
        sids[pane_id] = f"s-{name}"
        if transcript:
            path = tmp_path / f"{name}.jsonl"
            path.write_text("{}\n")
            os.utime(path, (ts(idle), ts(idle)))
            paths[f"s-{name}"] = path
        base = {"agent": "claude", "pid": pid, "pane_id": pane_id, "name": name, "state": "idle", "panes": 1}
        stamps = {"completed_at": 0 if transcript else ts(idle), "acted_at": 0}
        views.append({**base, **stamps, "res": {"mem_mb": 812}, "profile": "default", **over})

    return add


def preview(client):
    r = client.get("/api/idle-close/preview")
    assert r.status_code == 200
    return {row["name"]: row["verdict"] for row in r.json()}


def test_preview_has_one_row_for_each_verdict(client, board):
    board("closes")
    board("weekend", idle=datetime(2026, 11, 20, 23))  # Friday 23:00: 77 hours, 29 of them on weekdays
    board("pinned", pid="pinned00")
    board("split", panes=2)
    board("waits", state="needs-input")
    board("unread", state="done")
    board("runs", state="working")
    board("archived", transcript=False)
    assert preview(client) == {
        "closes": "closes",
        "weekend": "weekend",
        "pinned": "pinned",
        "split": "split-window",
        "waits": "needs-input",
        "unread": "done",
        "runs": "working",
        "archived": "no-transcript",
    }


def test_preview_leaves_out_a_pane_with_no_stamp_or_not_near_the_limit(client, board):
    board("no-stamp", transcript=False, completed_at=0)
    board("fresh", idle=datetime(2026, 11, 23, 12))
    board("shell", agent=None)
    assert preview(client) == {}


def test_preview_counts_to_the_next_sweep_not_to_now(client, board):
    # Thursday 02:00: 65 weekday hours now, 74 at 04:00.
    board("crosses", idle=datetime(2026, 11, 19, 2))
    assert preview(client) == {"crosses": "closes"}


def test_preview_row_fields(client, board):
    board("closes")
    (row,) = client.get("/api/idle-close/preview").json()
    assert row == {"name": "closes", "idle_since": ts(OLD), "weekday_hours": 99.0, "mem_mb": 812, "verdict": "closes"}


def test_preview_is_empty_with_the_setting_off(client, board, clean_state):
    board("closes")
    del clean_state["settings"]["idle_close_weekdays"]
    assert preview(client) == {}
```

- [ ] **Step 2: Run them to make sure that they fail**

Run: `uv run pytest tests/routes/test_idle_close.py -q`
Expected: FAIL with 404 from the static mount.

- [ ] **Step 3: Write the preview and the route**

In `periscope/idle_close.py`, after `sweep`:

```python
def preview(views: list[dict], weekdays: int) -> list[dict]:
    """One row for each Claude pane that the next sweep decides about."""
    at = next_sweep(_now())
    pinned = _pinned()
    rows = []
    for view in views:
        pane = pane_from_view(view)
        if pane is None or pane["idle_since"] is None:
            continue
        code = verdict(pane, at, weekdays, pinned)
        if code is None:
            continue
        since = datetime.fromtimestamp(pane["idle_since"])
        rows.append(
            {
                "name": pane["name"],
                "idle_since": pane["idle_since"],
                "weekday_hours": weekday_hours(since, at),
                "mem_mb": pane["mem_mb"],
                "verdict": code,
            }
        )
    return rows
```

Create `periscope/routes/idle_close.py`:

```python
"""GET /api/idle-close/preview — the panes that idle close closes at the next 04:00."""

import asyncio

from fastapi import APIRouter

from periscope import idle_close, state_hub, store

router = APIRouter()


@router.get("/api/idle-close/preview")
async def idle_close_preview():
    """[] when the setting is off. The sweep decides again at 04:00."""
    weekdays = store.get_settings().get("idle_close_weekdays")
    if not weekdays:
        return []
    views = await state_hub.current_windows()
    return await asyncio.to_thread(idle_close.preview, views, weekdays)
```

In `periscope/app.py`, add next to `from periscope.routes import cleanup as cleanup_routes`:

```python
from periscope.routes import idle_close as idle_close_routes
```

and add `idle_close_routes,` to the `for r in (…)` tuple, after `history,`.

- [ ] **Step 4: Run the route tests**

Run: `uv run pytest tests/routes/test_idle_close.py -q`
Expected: all pass.

- [ ] **Step 5: Write the failing settings line test**

Create `static/src/overlays/__tests__/idleClose.test.js`:

```js
import { describe, expect, it } from "vitest";
import { idleCloseLine } from "../idleClose.js";

const row = (name, verdict = "closes") => ({ name, verdict });

describe("idleCloseLine", () => {
  it("counts and names the panes that close", () => {
    expect(idleCloseLine([row("notify-buttons"), row("pinned-one", "pinned"), row("bq-cost-guard")])).toBe(
      "2 panes close at 04:00 tonight: notify-buttons, bq-cost-guard",
    );
    expect(idleCloseLine([row("notify-buttons")])).toBe("1 pane closes at 04:00 tonight: notify-buttons");
  });

  it("says so when no pane closes", () => {
    expect(idleCloseLine([row("a", "weekend")])).toBe("No pane closes at 04:00 tonight");
  });
});
```

Run: `npx vitest run static/src/overlays/__tests__/idleClose.test.js`
Expected: FAIL with "Failed to resolve import".

- [ ] **Step 6: Write the line and the settings row**

Create `static/src/overlays/idleClose.js`:

```js
// The settings row's second line, from the rows of GET /api/idle-close/preview.
export function idleCloseLine(rows) {
  const names = rows.filter((r) => r.verdict === "closes").map((r) => r.name);
  if (!names.length) return "No pane closes at 04:00 tonight";
  return `${names.length} ${names.length === 1 ? "pane closes" : "panes close"} at 04:00 tonight: ${names.join(", ")}`;
}
```

In `static/src/overlays/SettingsModal.jsx`:

Add the import:

```jsx
import { idleCloseLine } from "./idleClose.js";
```

Add the state, after `idleDays`:

```jsx
  const [idleClose, setIdleClose] = useState("");  // weekdays; "" is off
  const [idleCloseRows, setIdleCloseRows] = useState(null);  // null: the setting is off
```

In the load effect, after `setIdleDays(…)`:

```jsx
      setIdleClose(s.idle_close_weekdays ?? "");
      setIdleCloseRows(null);
      if (s.idle_close_weekdays) {
        const { data: rows } = await modalRequest("load idle close preview", "/api/idle-close/preview");
        setIdleCloseRows(rows ?? null);
      }
```

Put these three lines at the end of the `async` body, after `setRoots(…)`, so that a slow preview does not hold up the other fields.

In `handleSubmit`, after the `idle` check:

```jsx
    const weekdays = idleClose === "" ? null : Number(idleClose);
    if (weekdays !== null && !(Number.isInteger(weekdays) && weekdays >= 1 && weekdays <= 30)) {
      setError("idle close weekdays must be 1 to 30, or empty for off");
      return;
    }
```

and add to `patch`:

```jsx
      idle_close_weekdays: weekdays,  // null turns it off
```

Add the row, after the "Cleanup idle threshold" label:

```jsx
          <div class="settings-idle-close">
            <label for="settings-idle-close">Close idle Claude panes after</label>
            <input
              id="settings-idle-close"
              type="number"
              min="1"
              max="30"
              placeholder="off"
              value={idleClose}
              onInput={(e) => setIdleClose(e.currentTarget.value)}
            />
            <span>weekdays</span>
            <button type="button" disabled={idleClose === ""} onClick={() => setIdleClose("")}>off</button>
          </div>
          {idleCloseRows && <p class="settings-overrides-hint">{idleCloseLine(idleCloseRows)}</p>}
```

In `static/styles.css`, after the `#settings-form textarea` rule:

```css
.settings-idle-close {
  display: flex; align-items: baseline; gap: 0.5em; margin-bottom: 0.25em; font-size: 0.9em;
}
/* ID-qualified to outrank #settings-form label / input, which are full-width blocks. */
#settings-form .settings-idle-close label { display: inline; margin: 0; }
#settings-form .settings-idle-close input { display: inline-block; width: 5em; margin: 0; }
```

- [ ] **Step 7: Run the frontend checks and build the bundle**

```bash
npm test
npm run build
bin/check
git status --short
```

Expected: all pass. `static/dist/app.js` changed.

- [ ] **Step 8: Look at the row in a browser**

Start the private server as in Task 8, Step 2. Open the settings modal. Check: the row shows "off" with an empty input and no second line. Type 3 and save. Open the modal again: the second line shows "No pane closes at 04:00 tonight", because the private tmux server has no Claude pane. Press off and save. Open it again: the line is gone. Type 31 and save: the modal shows the error. Check the row at about 900 px wide. Stop the server, and delete the scratch folder, the screenshots and `.playwright-mcp` files.

- [ ] **Step 9: Commit**

```bash
git add periscope/idle_close.py periscope/routes/idle_close.py periscope/app.py tests/routes/test_idle_close.py static/src/overlays static/styles.css static/dist
git commit -m "idle close: a preview of tonight's sweep, and the settings row

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 16: Finish PR 2

- [ ] **Step 1: Update the docs**

In `CLAUDE.md`, add a row to the server module table, after the `poke.py` row:

```markdown
| `idle_close.py` | Nightly close of idle Claude panes: the rules, the 04:00 sweep and its tick, the closed records' retention, the preview; prod-only, off by default |
```

and a row to the reference docs table, after the `poke.py` row:

```markdown
| `idle_close.py`, `closed_panes`, resume of a closed pane | `docs/superpowers/specs/2026-11-19-idle-close-design.md` — what counts as idle, the verdict order, the re-check before each kill, the chained pid stamp |
```

In the spec, change the `Status:` line to `Status: implemented (server part). The history view is phase 2.`

- [ ] **Step 2: Run every check**

```bash
uv run pytest -q
npm test
npm run build && git status --short static/dist
bin/check
```

Expected: all tests pass, and `static/dist` has no new change.

- [ ] **Step 3: Check the build time**

The re-check covers a build that takes 3 to 13 seconds. Ask the user for the poll time on the last line of the resource pill's popover in the prod dashboard. If it is longer than 13 seconds, tell the user what that means: a pane can change for longer before the re-check, and the re-check still sees it.

- [ ] **Step 4: Commit, then ask before you push**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-11-19-idle-close-design.md
git commit -m "docs: idle close in the module map

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Ask the user for a yes to push the branch and open the PR `THI-466: close idle Claude panes at 04:00`.

## Assumptions for the PR bodies

PR 1:

- tmux runs the chained commands in one queue. Checked on tmux 3.6a for `new-window` without `-d` and for `new-session -d`. Other versions are not checked; a tmux test covers the version that CI runs.
- `claude_proc_for` finds a live Claude by the session file of its process. It reads nothing for a Claude whose footprint it cannot read.
- A closed pane's `state.json` entry outlives its record (30 days, or longer with a link).

PR 2:

- Kill on an idle Claude loses no turn (Task 1).
- `session_state_for` says `idle`. A pane whose status Claude does not report never closes.
- An idle Claude can own a background shell. The sweep kills it with the pane.
- `jsonl_for_session` looks only in `~/.claude/projects`.

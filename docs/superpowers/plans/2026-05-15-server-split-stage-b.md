# Server split — Stage B (Peels 5–9) implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the structural split. Stage A extracted infra/tmux/channels/store; Stage B extracts panes/pids, LGTM, git_pr/usage/rename_ai, all 11 route handlers, and finally moves `app = FastAPI(...)` + lifespan into `periscope/app.py` (flipping the uvicorn import string from `"server:app"` to `"periscope.app:app"`). Each peel is preceded by a pytest test for what's moving; each peel ends with a green test suite.

**Architecture:** Same conventions as Stage A. Routes go to `periscope/routes/` using `APIRouter` per file; server.py (through Peel 8) does `app.include_router(...)` per file. Peel 9 is the only peel that changes the uvicorn target — done atomically with the `app` move. After Peel 9, `server.py` is a ~30-line entry-point shim and nothing in `periscope/` imports from `server`.

**Tech Stack:** Same as Stage A (Python 3.11+, FastAPI, uvicorn, mcp 1.27.*, pytest 8+, pytest-mock 3+).

**Reference docs:**
- Design spec: `docs/superpowers/specs/2026-05-15-server-split-design.md` (rev 3)
- Stage A plan: `docs/superpowers/plans/2026-05-15-server-split-stage-a.md`

**State at start of Stage B (post-Peel-4 commit `d3ad760`):**
- `server.py`: 2,694 lines
- `periscope/`: config, log, pidfile, tmux, channels, store (~934 lines total)
- 116 pytest tests passing
- Two surviving bridges in `periscope/channels.py` (lines ~143 and ~210): function-local `from server import list_windows, _attach_git_then_resolve_pids[, note_focus, note_action]` — resolved in Peel 5.

---

## Verification gate (every peel)

```sh
uv run pytest -q
uv run server.py > /tmp/peel-boot.log 2>&1 &
SERVER_PID=$!
for i in 1 2 3 4 5 6 7 8; do sleep 1; curl -fsS http://127.0.0.1:8765/api/state > /dev/null 2>&1 && { echo "API OK"; break; }; done
kill $SERVER_PID 2>/dev/null
wait $SERVER_PID 2>/dev/null
rm -f ~/.config/periscope/periscope.pid /tmp/periscope-mcp.sock
```

Plus a per-peel manual smoke (see each peel).

If either step fails: revert, diagnose, retry. Do NOT commit on red.

---

## File structure (end of Stage B)

```
periscope/
├── __init__.py
├── __main__.py
├── app.py                          # NEW (Peel 9) — FastAPI() + lifespan + mounts
├── config.py
├── log.py
├── pidfile.py
├── tmux.py
├── channels.py                     # bridges removed in Peel 5
├── store.py
├── panes.py                        # NEW (Peel 5)
├── pids.py                         # NEW (Peel 5)
├── lgtm.py                         # NEW (Peel 6)
├── git_pr.py                       # NEW (Peel 7)
├── usage.py                        # NEW (Peel 7)
├── rename_ai.py                    # NEW (Peel 7)
└── routes/
    ├── __init__.py
    ├── state.py                    # NEW (Peel 8)
    ├── prefs.py                    # NEW (Peel 8)
    ├── pane.py                     # NEW (Peel 8)
    ├── send.py                     # NEW (Peel 8)
    ├── sessions.py                 # NEW (Peel 8)
    ├── paste_image.py              # NEW (Peel 8)
    ├── channel.py                  # NEW (Peel 8)
    ├── history.py                  # NEW (Peel 8)
    ├── auto_rename.py              # NEW (Peel 8)
    ├── lgtm.py                     # NEW (Peel 8)
    └── ws.py                       # NEW (Peel 8)

tests/
├── conftest.py                     # extended in Peel 5 (re-bind list per module)
├── test_smoke.py
├── test_config.py
├── test_log.py
├── test_pidfile.py
├── test_tmux.py
├── test_channels.py
├── test_store.py
├── test_panes.py                   # NEW (Peel 5) — folds test_parse_pane.py
├── test_pids.py                    # NEW (Peel 5)
├── test_lgtm.py                    # NEW (Peel 6)
├── test_git_pr.py                  # NEW (Peel 7)
├── test_usage.py                   # NEW (Peel 7)
├── test_rename_ai.py               # NEW (Peel 7)
├── test_app.py                     # NEW (Peel 9) — lifespan startup/shutdown
└── routes/                         # NEW (Peel 8)
    ├── __init__.py
    ├── test_state.py
    ├── test_prefs.py
    ├── test_pane.py
    ├── test_send.py
    ├── test_sessions.py
    ├── test_paste_image.py
    ├── test_channel.py
    ├── test_history.py
    ├── test_auto_rename.py
    ├── test_lgtm.py
    └── test_ws.py

server.py                           # Peel 9: ~30-line entry-point shim
test_parse_pane.py                  # DELETED in Peel 5 (folded into tests/test_panes.py)
tests/test_channel_smoke.py         # untouched (collect_ignore'd already)
```

---

## Peel 5: `panes.py` + `pids.py` + fold `test_parse_pane.py`

**Goal:** Move pane parsing, focus/smoothing, window listing, and pid minting/resolution into two modules. Resolve the two surviving channels bridges. Fold `test_parse_pane.py` into `tests/test_panes.py`.

**Files:**
- Create: `periscope/panes.py`, `periscope/pids.py`
- Create: `tests/test_panes.py`, `tests/test_pids.py`
- Modify: `server.py`, `periscope/channels.py`, `tests/conftest.py`
- Delete: `test_parse_pane.py` (repo root)

### Task 5.1: Write `tests/test_panes.py` (folds in `test_parse_pane.py`)

- [ ] **Step 1: Create `tests/test_panes.py`**

Copy the body of `test_parse_pane.py` and convert from its `# /// script` shape into pytest. The source has **four** datasets exercised by four runner functions (`run_regex_cases`, `run_parse_cases`, `run_ghost_cases`, `run_last_line_cases`); fold all four — do NOT lose any one.

- Drop the PEP-723 header.
- Drop the `sys.path.insert(0, str(Path(__file__).parent))` and `import server` lines at the top.
- Change `server.SPINNER_RE`, `server.ACTIVE_OP_RE`, `server.parse_pane` → bare names after adding `from server import SPINNER_RE, ACTIVE_OP_RE, parse_pane` at module top (the test-pre-move import; flipped in Task 5.4).
- Wrap each of the four runner functions into a `def test_*():` (e.g. `test_regex_cases`, `test_parse_cases`, `test_ghost_cases`, `test_last_line_cases`) that loops internally and asserts. Each existing runner already collects failures and prints summary — preserve that structure but convert print+exit into `assert not failures, ...`. PARSE_CASES, GHOST_CASES, and LAST_LINE_CASES are the datasets that catch real parse_pane regressions; REGEX_CASES alone is not adequate coverage.

Add these new tests (smoothing + focus tracking + list_windows, which `test_parse_pane.py` does not cover):

```python
import time
import pytest

from server import (
    smooth_spinner, smooth_is_claude,
    note_focus, note_action, update_focus_from_windows,
    _focused_at, _acted_at, _spinner_last_seen, _claude_last_seen,
    SPINNER_GRACE_S, CLAUDE_STICKY_S,
)


@pytest.fixture(autouse=True)
def reset_panes_state():
    """Clear in-memory pane state between tests."""
    _focused_at.clear()
    _acted_at.clear()
    _spinner_last_seen.clear()
    _claude_last_seen.clear()
    yield
    _focused_at.clear()
    _acted_at.clear()
    _spinner_last_seen.clear()
    _claude_last_seen.clear()


def test_smooth_spinner_returns_current_when_present():
    out = smooth_spinner("foo:0", "Envisioning")
    assert out == "Envisioning"
    assert "foo:0" in _spinner_last_seen


def test_smooth_spinner_returns_last_seen_within_grace_when_current_none():
    smooth_spinner("foo:0", "Envisioning")
    # Within grace window — should return the cached value.
    out = smooth_spinner("foo:0", None)
    assert out == "Envisioning"


def test_smooth_spinner_returns_none_after_grace_expires(mocker):
    mocker.patch(
        "time.time",
        side_effect=[100.0, 100.0 + SPINNER_GRACE_S + 1.0],
    )
    smooth_spinner("foo:0", "Envisioning")
    out = smooth_spinner("foo:0", None)
    assert out is None


def test_smooth_is_claude_true_passes_through():
    assert smooth_is_claude("foo:0", True) is True
    assert "foo:0" in _claude_last_seen


def test_smooth_is_claude_false_after_stickiness_expires(mocker):
    mocker.patch(
        "time.time",
        side_effect=[100.0, 100.0 + CLAUDE_STICKY_S + 1.0],
    )
    smooth_is_claude("foo:0", True)
    assert smooth_is_claude("foo:0", False) is False


def test_smooth_is_claude_sticky_within_window(mocker):
    """If we just saw is_claude=True, a momentary False should still
    return True until the stickiness window expires."""
    mocker.patch("time.time", side_effect=[100.0, 100.5])
    smooth_is_claude("foo:0", True)
    assert smooth_is_claude("foo:0", False) is True


def test_note_focus_stamps_now():
    note_focus("foo:0")
    assert _focused_at["foo:0"] > 0
    assert "foo:0" not in _acted_at  # focus alone doesn't bump acted_at


def test_note_action_stamps_both():
    note_action("foo:0")
    assert _acted_at["foo:0"] > 0
```

Add list_windows tests by mocking `tmux()`:

```python
def test_list_windows_parses_tmux_list_output(mocker):
    from server import list_windows
    sample = (
        "main\t0\tshell\t1\t/home/tom/dev/foo\t1234\t%5\t1\n"
        "main\t1\tclaude\t0\t/home/tom/dev/bar\t1235\t%6\t1\n"
    )
    mocker.patch("periscope.tmux.tmux", return_value=sample)
    out = list_windows()
    assert len(out) == 2
    assert out[0]["session"] == "main"
    assert out[0]["index"] == 0
    assert out[0]["pane_id"] == "%5"
```

Adjust the field assertions to match what `list_windows` actually returns — read its body in `server.py:983` first to get the keys (session, index, name, active, cwd, pid_raw, pane_id, ...).

- [ ] **Step 2: Run against current `server.py`**

```sh
uv run pytest tests/test_panes.py -v
```

Must be all green. If `list_windows` assertions fail, adjust the test to match what the function returns — don't change the function.

### Task 5.2: Write `tests/test_pids.py`

- [ ] **Step 1: Create `tests/test_pids.py`**

```python
"""Periscope window-ids (@periscope_id): mint, stamp, rebind, resolve."""

import re

from server import (
    _mint_pid, _stamp_pid, _rebind_pid, resolve_pids,
    _attach_git_then_resolve_pids, _PID_TTL_S,
)


def test_mint_pid_format():
    """_mint_pid returns short ID matching the documented format."""
    pid = _mint_pid()
    assert isinstance(pid, str)
    assert len(pid) > 0
    # Read server.py:1034 to confirm the exact format (likely 8-char hex)
    # and assert against that. Adjust this test once you see the impl.


def test_mint_pid_uniqueness():
    seen = {_mint_pid() for _ in range(100)}
    assert len(seen) == 100


def test_stamp_pid_calls_tmux_set_option(mocker):
    mock_tmux = mocker.patch("periscope.tmux.tmux", return_value="")
    _stamp_pid("foo:0", "abc12345")
    # Should call tmux set-option -t foo:0 -w @periscope_id abc12345
    mock_tmux.assert_called()
    args = mock_tmux.call_args.args
    assert "set-option" in args or "setw" in args
    assert "@periscope_id" in args or any("@periscope_id" in a for a in args)
    assert "abc12345" in args


def test_resolve_pids_assigns_pid_to_each_window(mocker):
    """Stub out _mint_pid + _stamp_pid; verify resolve_pids walks windows
    and ensures each has a pid (either pre-existing or freshly minted)."""
    mocker.patch("periscope.tmux.tmux", return_value="")
    mocker.patch("server._mint_pid", side_effect=[f"pid{i:08x}" for i in range(10)])
    mocker.patch("server._stamp_pid")  # no-op for the test
    windows = [
        {"session": "main", "index": 0, "pane_id": "%5", "cwd": "/x", "pid_raw": ""},
        {"session": "main", "index": 1, "pane_id": "%6", "cwd": "/y", "pid_raw": ""},
    ]
    resolve_pids(windows)
    for w in windows:
        assert w.get("pid")
```

Inspect `server.py:1034-1180` first to refine these tests against the actual signatures. The signatures of `_mint_pid`, `_stamp_pid`, `_rebind_pid`, `resolve_pids`, `_attach_git_then_resolve_pids` may take different args than guessed.

- [ ] **Step 2: Run against current `server.py`**

```sh
uv run pytest tests/test_pids.py -v
```

### Task 5.3: Create `periscope/panes.py` and `periscope/pids.py`

**Pre-step: extend `periscope/config.py` with `USAGE_SESSION_PREFIX`.**

`list_windows()` filters out periscope-spawned usage-scrape sessions by checking `s.startswith(USAGE_SESSION_PREFIX)`. That constant currently lives at `server.py:860` inside the usage block (which moves in Peel 7). If we leave it there, `panes.list_windows` will `NameError` immediately after the move. Fix: promote it to `config.py` now.

Add to `periscope/config.py`:

```python
# Tmux session prefix for periscope-spawned `claude /usage` scrape sessions.
# panes.list_windows filters these out; usage.py creates them.
USAGE_SESSION_PREFIX = "periscope-usage-"
```

Delete the original at `server.py:860` (when stripping the usage block in Peel 7, verify it's already gone).

In Peel 5: `panes.py` does `from periscope.config import USAGE_SESSION_PREFIX`. In Peel 7: `usage.py` does the same.

- [ ] **Step 1: Write `periscope/panes.py`**

Source ranges from current `server.py`:
- Lines ~115–209: focus/smoothing globals (`_focused_at`, `_acted_at`, `_completed_at`, `_prev_state`, `_active_per_session`, `_resuming`, `_spinner_last_seen`, `_claude_last_seen`, `RESUME_EXPIRY_S`, `SPINNER_GRACE_S`, `CLAUDE_STICKY_S`) + `smooth_spinner` + `smooth_is_claude` + `note_focus` + `note_action` + `update_focus_from_windows`
- Lines ~212–289: parse-pane regexes (`STATUS_RE`, `TITLE_RE`, `PR_RE`, `SPINNER_RE`, `ACTIVE_OP_RE`, `IDLE_INDICATOR_RE`, `SPINNER_VERB_RE`, `NEEDS_INPUT_FOOTER_RE`, `RECAP_RE`, `PROMPT_LINE_RE`)
- Lines ~983–1032: `list_windows()`
- Lines ~1181–~1333: `parse_pane()`

Module top:

```python
"""Pane introspection: tmux window listing + Claude TUI parsing + focus
tracking + spinner/is_claude smoothing.

The smoothing dicts (`_spinner_last_seen`, `_claude_last_seen`) absorb
single-frame capture-pane glitches so the dashboard's "thinking"
indicator and is_claude classification don't flicker. The focus dicts
(`_focused_at`, `_acted_at`, etc.) drive the stream view's recency
ordering.

`_resuming` lives here too (pane-shaped state — used by /api/state for
the resume-in-flight check and by routes/sessions / routes/history for
the resume orchestration).
"""

import re
import time

from periscope.config import USAGE_SESSION_PREFIX
from periscope.tmux import tmux
```

Add the symbols verbatim from server.py. Verify with `grep -n "^def parse_pane\|^def list_windows\|^def smooth_spinner\|^STATUS_RE\|^_focused_at" server.py` before extracting.

- [ ] **Step 2: Write `periscope/pids.py`**

Source: `server.py:1031–1180` (`_PID_TTL_S` constant + `_mint_pid`, `_stamp_pid`, `_rebind_pid`, `resolve_pids`, `_attach_git_then_resolve_pids`).

Module top:

```python
"""Periscope window-ids (@periscope_id).

Each tmux window gets a periscope-managed id stored as a tmux user
option `@periscope_id`. The id survives renames + moves within a tmux
server lifetime; the rebind heuristic recovers ids across tmux server
restarts. Time-to-live is 30 days — older state.json entries get GC'd.
"""

import time

from periscope.log import log
from periscope.panes import list_windows
from periscope.store import _STATE, _STATE_LOCK, _write_state
from periscope.tmux import tmux

# _attach_git_then_resolve_pids depends on cached_git_state. Peel 7 moves
# git_pr to periscope.git_pr; in Peel 5 we use a function-local bridge
# to server.cached_git_state. Documented with a `# BRIDGE: removed in
# Peel 7` marker.
```

In `_attach_git_then_resolve_pids`, add a function-local bridge:

```python
def _attach_git_then_resolve_pids(windows: list[dict]) -> None:
    # BRIDGE: removed in Peel 7.
    from server import cached_git_state
    ...
```

### Task 5.4: Strip from `server.py`, resolve channels bridges, re-point tests

- [ ] **Step 1: Delete the moved blocks from `server.py`**

Remove the four ranges (recheck line numbers via grep after each delete):
- Focus/smoothing globals + functions (~115–209)
- Parse-pane regexes (~212–289)
- `list_windows` (~983–1032)
- pids block (~1031–1180)
- `parse_pane` (~1181–~1333)

Replace each block with a one-line marker (`# Panes code now lives in periscope/panes.py.`).

- [ ] **Step 2: Add imports to `server.py`**

```python
from periscope.panes import (
    _focused_at, _acted_at, _completed_at, _prev_state, _active_per_session,
    _resuming, _spinner_last_seen, _claude_last_seen,
    RESUME_EXPIRY_S, SPINNER_GRACE_S, CLAUDE_STICKY_S,
    smooth_spinner, smooth_is_claude,
    note_focus, note_action, update_focus_from_windows,
    list_windows, parse_pane,
    STATUS_RE, TITLE_RE, PR_RE,
    SPINNER_RE, ACTIVE_OP_RE, IDLE_INDICATOR_RE, SPINNER_VERB_RE,
    NEEDS_INPUT_FOOTER_RE, RECAP_RE, PROMPT_LINE_RE,
)
from periscope.pids import (
    _PID_TTL_S, _mint_pid, _stamp_pid, _rebind_pid,
    resolve_pids, _attach_git_then_resolve_pids,
)
```

Trim symbols not referenced from `server.py` by grepping after the strip.

- [ ] **Step 3: Resolve the two surviving channels bridges**

In `periscope/channels.py`:

`_resolve_pid_for_pane`:
```python
def _resolve_pid_for_pane(pane_id: str) -> str:
    # OLD: from server import list_windows, _attach_git_then_resolve_pids
    # ...
```

Replace with module-top:
```python
from periscope.panes import list_windows
from periscope.pids import _attach_git_then_resolve_pids
```

`_do_spawn_claude_tool`:
```python
# OLD function-local:
# from server import list_windows, note_focus, note_action, _attach_git_then_resolve_pids
```

Replace with module-top (extend the existing imports added above):
```python
from periscope.panes import list_windows, note_focus, note_action
from periscope.pids import _attach_git_then_resolve_pids
```

Verify what `from server import` remains in `periscope/`:
```sh
grep -rn "from server import" /Users/tom/dev/periscope/periscope/
```

**Expected output for Peel 5: exactly ONE match** — the function-local bridge in `periscope/pids.py::_attach_git_then_resolve_pids` to `cached_git_state` (Peel 7 resolves it when `periscope/git_pr.py` ships). The two channels bridges (`list_windows`, `note_focus`, `note_action`, `_attach_git_then_resolve_pids`) MUST be gone after this peel.

After Peel 7, the grep returns zero matches.

- [ ] **Step 4: Re-point `tests/test_panes.py` and `tests/test_pids.py`**

Change every `from server import ...` to the appropriate `periscope.panes` / `periscope.pids` import. Update `mocker.patch("server._mint_pid", ...)` → `mocker.patch("periscope.pids._mint_pid", ...)`.

- [ ] **Step 5: Update `tests/conftest.py` `clean_state` fixture**

Add `periscope.pids` to the re-bind list (because Peel 5 introduces `from periscope.store import _STATE` in pids.py):

```python
for mod_name in ("periscope.channels", "periscope.pids"):
```

- [ ] **Step 6: Delete `test_parse_pane.py` at repo root**

```sh
git rm /Users/tom/dev/periscope/test_parse_pane.py
```

- [ ] **Step 7: Verification gate**

```sh
uv run pytest -q
```

Plus the boot smoke. Plus manual smoke: open the dashboard in a browser, confirm a Claude pane shows the right state badge + spinner, click a session to focus, confirm `focused_at` updates.

- [ ] **Step 8: Commit**

```sh
git add periscope/panes.py periscope/pids.py periscope/channels.py \
        tests/test_panes.py tests/test_pids.py tests/conftest.py \
        server.py
git rm test_parse_pane.py
git commit -m "split: extract panes + pids, resolve channels bridges, fold test_parse_pane.py (Peel 5)"
```

---

## Peel 6: `lgtm.py`

**Goal:** Move LGTM helpers + state (`server.py:292–466`) into `periscope/lgtm.py`. The `/api/lgtm/start` route stays in `server.py` until Peel 8.

**Files:**
- Create: `periscope/lgtm.py`, `tests/test_lgtm.py`
- Modify: `server.py`

### Task 6.1: Write `tests/test_lgtm.py`

- [ ] **Step 1: Create the test file**

```python
"""LGTM mirror: cached_lgtm_state + slug helpers.

The SSE loop (_lgtm_periodic_refresh, _lgtm_sse_loop) is integration-only;
not covered here. Manual smoke is "boot periscope while LGTM runs on
:9900 and confirm the dashboard surfaces review pane indicators."
"""

from server import (
    _normalize_repo_path,
    cached_lgtm_state,
    _LGTM_LOCK, _LGTM_BY_REPO,
)


def test_normalize_repo_path_strips_trailing_slash():
    assert _normalize_repo_path("/Users/tom/dev/periscope/") == "/Users/tom/dev/periscope"
    assert _normalize_repo_path("/Users/tom/dev/periscope") == "/Users/tom/dev/periscope"


def test_normalize_repo_path_handles_none():
    assert _normalize_repo_path(None) == ""
    assert _normalize_repo_path("") == ""


def test_cached_lgtm_state_returns_none_for_unknown_repo():
    """When no LGTM session is mirrored for the repo, return None."""
    assert cached_lgtm_state("/no/such/repo") is None


def test_cached_lgtm_state_returns_stored_entry(monkeypatch):
    """Seed _LGTM_BY_REPO; cached_lgtm_state should return it."""
    fake = {"slug": "fake-slug", "review_id": "rev-123"}
    with _LGTM_LOCK:
        _LGTM_BY_REPO["/Users/tom/dev/periscope"] = fake
    try:
        out = cached_lgtm_state("/Users/tom/dev/periscope")
        assert out == fake
    finally:
        with _LGTM_LOCK:
            _LGTM_BY_REPO.pop("/Users/tom/dev/periscope", None)
```

Verify field names against `server.py:327` (`cached_lgtm_state`) and `_LGTM_BY_REPO` actual schema before finalizing the test.

- [ ] **Step 2: Run against current `server.py`** → must pass.

### Task 6.2: Create `periscope/lgtm.py`

- [ ] **Step 1: Write the module**

Source: `server.py:292–466` (entire LGTM block).

Module top:

```python
"""LGTM (Looks Good To Me) mirror.

Periscope polls localhost:9900 for active code-review sessions and
subscribes per-session SSE streams to keep its in-memory mirror fresh.
The dashboard reads `cached_lgtm_state(cwd)` to surface a review-pane
badge for any pane whose cwd matches a repo under review.

LGTM running is optional; if /api/lgtm/sessions is unreachable, all
helpers return None / empty silently.
"""

import asyncio
import os
import threading
from typing import Any

import httpx

from periscope.log import log, _task
```

- [ ] **Step 2: Strip from `server.py`, add imports**

Delete lines 292–466 (the entire LGTM block). Replace with a marker comment.

Add to `server.py`:

```python
from periscope.lgtm import (
    LGTM_BASE_URL, _LGTM_LOCK, _LGTM_BY_REPO, _LGTM_SSE_TASKS,
    cached_lgtm_state, _lgtm_submitted, _lgtm_periodic_refresh,
)
```

Trim symbols not actually referenced from `server.py` after stripping.

- [ ] **Step 3: Re-point `tests/test_lgtm.py`** to `from periscope.lgtm import ...`.

- [ ] **Step 4: Verification gate + commit**

```sh
uv run pytest -q
# boot smoke
git add periscope/lgtm.py tests/test_lgtm.py server.py
git commit -m "split: extract LGTM mirror + tests (Peel 6)"
```

---

## Peel 7: `git_pr.py` + `usage.py` + `rename_ai.py`

**Goal:** Move git/PR/activity helpers, the two usage-tracking paths, and the Anthropic-SDK rename helpers. Resolve the `_attach_git_then_resolve_pids` bridge in `periscope/pids.py`.

**Files:**
- Create: `periscope/git_pr.py`, `periscope/usage.py`, `periscope/rename_ai.py`
- Create: `tests/test_git_pr.py`, `tests/test_usage.py`, `tests/test_rename_ai.py`
- Modify: `server.py`, `periscope/pids.py`

### Task 7.1: Write the three test files

- [ ] **Step 1: `tests/test_git_pr.py`**

Cover: `_normalize_repo_path` (already in test_lgtm — skip duplicates if it's not in git_pr), `git_state_for` (mock subprocess for git rev-parse), `cached_git_state` TTL caching behavior, `pr_state_for` (mock `gh pr view` JSON), `_gh_run_state` glyph mapping.

```python
"""Git + PR state caching."""

import subprocess

from server import (
    git_state_for, cached_git_state, _gh_run_state,
    _git_cache, _pr_cache, _GIT_TTL, _PR_TTL,
)


def setup_function():
    _git_cache.clear()
    _pr_cache.clear()


def test_git_state_for_returns_none_for_non_git_path(tmp_path):
    assert git_state_for(str(tmp_path)) is None


def test_git_state_for_returns_branch_for_git_repo(tmp_path, mocker):
    # Mock _run to simulate `git rev-parse` returning a git dir + branch.
    def fake_run(cmd, cwd=None, timeout=3.0):
        if "--git-dir" in cmd:
            return (0, ".git")
        if "rev-parse" in cmd and "--abbrev-ref" in cmd:
            return (0, "main")
        if "status" in cmd:
            return (0, "")
        return (0, "")
    mocker.patch("periscope.tmux._run", side_effect=fake_run)
    # Also patch os.path.isdir to claim the path exists.
    mocker.patch("os.path.isdir", return_value=True)
    out = git_state_for(str(tmp_path))
    assert out is not None
    assert out["branch"] == "main"


def test_cached_git_state_uses_ttl(mocker):
    """First call hits git_state_for; second call within TTL hits cache."""
    mock_inner = mocker.patch("server.git_state_for", return_value={"branch": "main", "git": "clean"})
    cached_git_state("/foo")
    cached_git_state("/foo")
    assert mock_inner.call_count == 1


def test_gh_run_state_maps_status_to_glyph():
    assert _gh_run_state({"status": "completed", "conclusion": "success"}) == "✓"
    assert _gh_run_state({"status": "completed", "conclusion": "failure"}) == "✗"
    assert _gh_run_state({"status": "in_progress"}) == "⟳"
    # Adjust glyphs per actual implementation in server.py:601
```

- [ ] **Step 2: `tests/test_usage.py`**

Cover: `parse_usage_screen` (pure function), `compute_claude_usage` with a fake JSONL directory, `kill_orphan_usage_sessions` (mock tmux list-sessions).

```python
"""Claude usage tracking: JSONL parsing + /usage screen scraping."""

from server import parse_usage_screen, compute_claude_usage, _USAGE_LABELS


def test_parse_usage_screen_extracts_known_labels():
    sample = (
        "Claude Code Usage (5-hour window)\n"
        "\n"
        "Sonnet 4.5  ████████░░  82%  $8.43\n"
        "Opus 4.7    ██░░░░░░░░  18%  $1.21\n"
    )
    out = parse_usage_screen(sample)
    assert out  # adjust to actual return shape after reading server.py:831
    # E.g. assert "sonnet" in out


def test_parse_usage_screen_handles_empty_input():
    assert parse_usage_screen("") in ({}, None) or parse_usage_screen("") == {}


def test_compute_claude_usage_returns_zero_for_no_sessions(monkeypatch, tmp_path):
    """When ~/.claude/projects/ has no sessions, usage is zero/empty."""
    monkeypatch.setattr("server._CLAUDE_PROJECTS", tmp_path)
    out = compute_claude_usage()
    # Assert the documented "empty" shape from server.py:732
    assert isinstance(out, dict)
```

- [ ] **Step 3: `tests/test_rename_ai.py`**

Cover: `build_rename_prompt` (pure prompt assembly), `claude_complete` (mock Anthropic client).

```python
"""Auto-rename via the Anthropic SDK."""

from server import build_rename_prompt, claude_complete


def test_build_rename_prompt_includes_window_names():
    windows = [
        {"session": "main", "index": 0, "name": "claude", "is_claude": True},
        {"session": "main", "index": 1, "name": "shell", "is_claude": False},
    ]
    prompt = build_rename_prompt(windows)
    assert "main" in prompt
    assert "claude" in prompt or "Claude" in prompt


def test_claude_complete_calls_anthropic(mocker):
    """claude_complete invokes get_anthropic().messages.create with the prompt."""
    fake_msg = mocker.MagicMock()
    fake_msg.content = [mocker.MagicMock(text="response text")]
    fake_client = mocker.MagicMock()
    fake_client.messages.create.return_value = fake_msg
    mocker.patch("server.get_anthropic", return_value=fake_client)
    out = claude_complete("hello prompt")
    assert out == "response text"
    fake_client.messages.create.assert_called_once()
```

- [ ] **Step 4: Run all three test files against current `server.py`**

Must be green before any move.

### Task 7.2: Create the three modules

- [ ] **Step 1: `periscope/git_pr.py`**

Source: `server.py:468–715` (Git + PR + Activity) plus `prewarm_pr_cache()` (~2621). Module top:

```python
"""Git + GitHub PR state, plus the activity-timeline cache."""

import json
import os
import shutil
import subprocess
import threading
from typing import Any

from periscope.log import _bg, log
from periscope.panes import list_windows  # for prewarm_pr_cache
from periscope.tmux import _run
```

- [ ] **Step 2: `periscope/usage.py`**

Source: `server.py:717–1020`. Module top:

```python
"""Claude usage tracking: two parallel paths.

(1) JSONL parsing: walks ~/.claude/projects/*/sessions/*.jsonl and sums
    token usage in the current 5-hour window. Cheap, no IO with Claude.

(2) TUI scrape: spawns `claude` in a tmux session, navigates to /usage,
    captures + parses the screen. Authoritative because it's what
    Anthropic shows the user; expensive (tmux session + 5–12s startup).

The dashboard prefers (2) when available, falls back to (1).
"""

import asyncio
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from periscope.log import _bg, _task, log
from periscope.tmux import tmux
```

- [ ] **Step 3: `periscope/rename_ai.py`**

Source: `server.py:2029–2095` (`get_anthropic`, `claude_complete`, `build_rename_prompt`). Module top:

```python
"""Auto-rename via the Anthropic SDK.

Used by the /api/auto-rename-session and /api/auto-rename-window route
handlers (which still live in server.py through Peel 7 — Peel 8 moves
them to periscope/routes/auto_rename.py).
"""

import os
from typing import Any

import anthropic

from periscope.log import log
```

### Task 7.3: Strip from `server.py`, resolve `_attach_git_then_resolve_pids` bridge

- [ ] **Step 1: Delete the three blocks**

Remove from `server.py`:
- Lines 468–715 (Git + PR + Activity)
- Lines 717–1020 (both usage paths)
- Lines 2029–~2095 (rename_ai helpers)
- `prewarm_pr_cache()` near the bottom (~line 2621)

- [ ] **Step 2: Add imports to `server.py`**

```python
from periscope.git_pr import (
    cached_git_state, cached_pr_state, cached_pane_activity,
    prewarm_pr_cache, _GH_AVAILABLE,
)
from periscope.usage import (
    cached_claude_usage, cached_scraped_usage,
    kill_orphan_usage_sessions,
)
from periscope.rename_ai import (
    get_anthropic, claude_complete, build_rename_prompt,
)
```

Trim per grep.

- [ ] **Step 3: Resolve the `_attach_git_then_resolve_pids` bridge in `periscope/pids.py`**

Change function-local `from server import cached_git_state` to module-top:

```python
from periscope.git_pr import cached_git_state
```

Verify no `from server import` left in `periscope/`:

```sh
grep -rn "from server import" /Users/tom/dev/periscope/periscope/
```

Expected: empty.

- [ ] **Step 4: Re-point the three test files** to import from `periscope.git_pr` / `periscope.usage` / `periscope.rename_ai`. Update any `mocker.patch("server.X", ...)` references.

- [ ] **Step 5: Verification gate + commit**

```sh
uv run pytest -q
# boot smoke
git add periscope/git_pr.py periscope/usage.py periscope/rename_ai.py \
        periscope/pids.py \
        tests/test_git_pr.py tests/test_usage.py tests/test_rename_ai.py \
        server.py
git commit -m "split: extract git_pr/usage/rename_ai + tests, resolve pids bridge (Peel 7)"
```

---

## Peel 8: routes split

**Goal:** Move every route handler from `server.py` to a per-file `periscope/routes/*.py` using `APIRouter`. server.py keeps `app = FastAPI(...)` and does `app.include_router(...)` per file (Peel 9 moves the app construction).

**Files:**
- Create: 11 route modules under `periscope/routes/`
- Create: 11 test modules under `tests/routes/`
- Create: `tests/routes/__init__.py`
- Modify: `server.py`

### Route → file mapping

| Route file | Endpoints | Source lines (in current server.py — verify before move) |
|---|---|---|
| `routes/state.py` | `GET /api/state` | ~1335–1485 |
| `routes/prefs.py` | `/api/prefs/*` (8 endpoints) | ~1487–1640 |
| `routes/pane.py` | `GET /api/pane`, `POST /api/rename` | ~1640–1750 |
| `routes/sessions.py` | `/api/session/*`, `/api/window/*` | ~1751–1946 |
| `routes/history.py` | `/api/history/*`, `/history` page | ~1947–2028 |
| `routes/auto_rename.py` | `/api/auto-rename-{session,window}` | ~2097–2263 |
| `routes/send.py` | `_send_to_target` + `/api/send` + `/api/send-bulk` | ~2265–2309 |
| `routes/channel.py` | `/api/channel/clear-unread` | ~2310–2318 |
| `routes/lgtm.py` | `/api/lgtm/start` | ~2319–2358 |
| `routes/paste_image.py` | `_sweep_old_paste_images` + `/api/paste-image` | ~2359–2415 |
| `routes/ws.py` | `WS /ws/pane` | ~2416–~2690 |

### General per-route pattern

Each route module looks like:

```python
"""<endpoint name>: <one-line summary>."""

from fastapi import APIRouter
# domain-specific imports from periscope.*

router = APIRouter()


@router.get("/api/X")
def x_endpoint(...):
    ...
```

In `server.py`, replace the route handlers with:

```python
from periscope.routes import state as _state_route
# ... one import per route module
app.include_router(_state_route.router)
# ... one include_router call per module
```

### Per-route tests

For each route, write `tests/routes/test_<name>.py` using FastAPI's `TestClient`. Pattern:

```python
"""Tests for /api/X."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from server import app  # In Peel 8 server.py still owns `app`.
    return TestClient(app)


def test_X_returns_200(client, mocker):
    # Mock any tmux/subprocess calls so the test doesn't depend on a
    # live tmux server.
    mocker.patch("periscope.tmux.tmux", return_value="...")
    r = client.get("/api/X")
    assert r.status_code == 200
```

Per-route specifics — write at least one happy-path test and one error/edge case per endpoint. Use `fake_tmux` for tmux mocking and `clean_state` for `_STATE` mutation tests.

### Peel order within Peel 8 (least to most coupled)

Execute as 11 sub-peels, each its own commit (so `git bisect` works on any route regression):

1. **`routes/ws.py`** — depends on `tmux`, `panes.note_action`, `capture`, `deliver_input`. Move the WS handler + its helpers.

   **Test mocking surface for `tests/routes/test_ws.py`:** the handler creates a FIFO at `/tmp/periscope.{uuid}.fifo`, calls `tmux pipe-pane`, then `os.open(fifo_path, ...)` + `loop.add_reader(fd, ...)`. None of that works in a test process without a live tmux + writable /tmp. Mock aggressively:
   - `mocker.patch("periscope.tmux.tmux", return_value="...")` — display-message + capture-pane
   - `mocker.patch("os.mkfifo")` + `mocker.patch("os.open", return_value=42)`
   - `mocker.patch("os.read", side_effect=BlockingIOError)`
   - `mocker.patch("asyncio.AbstractEventLoop.add_reader")` (or `mocker.patch.object(loop_instance, ...)` inside the test)

   With those, `TestClient.websocket_connect("/ws/pane?session=main&index=0")` should reach the initial size frame and the initial-paint blob. Asserting on either is enough; full bidirectional terminal forwarding is out of scope for pytest.
2. **`routes/history.py`** — depends on `history.search.get_session` + `panes._resuming` + `config.STATIC`. Three GET endpoints + a static HTML response.
3. **`routes/paste_image.py`** — depends on `tmux`, `panes.note_action`. Two helpers + one POST.
4. **`routes/channel.py`** — depends on `channels._CHANNELS_LOCK`, `_CHANNEL_UNREAD`. One POST.
5. **`routes/lgtm.py`** — depends on `lgtm._lgtm_refresh_all`. One POST.
6. **`routes/auto_rename.py`** — depends on `rename_ai`, `panes`, `tmux`. Two POSTs.
7. **`routes/send.py`** — depends on `tmux`, `panes`, `store`. `_send_to_target` + two POSTs.
8. **`routes/sessions.py`** — depends on `tmux`, `store`, `pids`, `panes._resuming`, `panes.note_focus`, `panes.note_action`, `git_pr._run`. ~6 endpoints, biggest route module.
9. **`routes/pane.py`** — depends on `tmux`, `panes`, `lgtm`. Two endpoints.
10. **`routes/prefs.py`** — depends on `store`. Eight endpoints (CRUD on prefs/windows/commands).
11. **`routes/state.py`** — depends on essentially everything (`panes`, `pids`, `channels`, `lgtm`, `git_pr`, `usage`, `store`). One endpoint, ~150 lines. **Imports note:** the tail of `/api/state` does `for sid in list(_resuming): ...` plus uses `RESUME_EXPIRY_S` for the resume-GC pass. Include both in the `from periscope.panes import …` line or the route will `NameError` on the first poll.

After each sub-peel, run the verification gate. Commit individually so a regression in (say) sub-peel 7 doesn't require reverting sub-peels 8–11.

### Sub-peel skeleton (template — apply to all 11)

For each route module N:

- [ ] **Step 1: Write `tests/routes/test_<n>.py`** importing from `server` (the routes still live in server.py at this point).
- [ ] **Step 2: Run the test against current server.py** → green.
- [ ] **Step 3: Create `periscope/routes/<n>.py`** with `router = APIRouter()` and the handler bodies moved verbatim. Adjust `@app.X` → `@router.X` decorators. Resolve imports (the handler may reference helpers that already moved to `periscope.*`).
- [ ] **Step 4: Strip the route from server.py.**
- [ ] **Step 5: Add to server.py** near the `app = FastAPI(...)` line:
   ```python
   from periscope.routes import <n>
   app.include_router(<n>.router)
   ```
- [ ] **Step 6: Re-point the test file** to `from periscope.routes.<n> import router` (or to `from server import app` for TestClient usage — server.py still owns `app`).
- [ ] **Step 7: Verification gate + commit.**

### Commit messages for the 11 sub-peels

```
split: extract /ws/pane to periscope.routes.ws + tests (Peel 8a)
split: extract /api/history to periscope.routes.history + tests (Peel 8b)
split: extract /api/paste-image to periscope.routes.paste_image + tests (Peel 8c)
split: extract /api/channel/clear-unread to periscope.routes.channel + tests (Peel 8d)
split: extract /api/lgtm/start to periscope.routes.lgtm + tests (Peel 8e)
split: extract /api/auto-rename to periscope.routes.auto_rename + tests (Peel 8f)
split: extract /api/send to periscope.routes.send + tests (Peel 8g)
split: extract /api/session+window to periscope.routes.sessions + tests (Peel 8h)
split: extract /api/pane+rename to periscope.routes.pane + tests (Peel 8i)
split: extract /api/prefs to periscope.routes.prefs + tests (Peel 8j)
split: extract /api/state to periscope.routes.state + tests (Peel 8k)
```

---

## Peel 9: final `app` move + uvicorn target flip

**Goal:** Move `app = FastAPI(lifespan=lifespan)`, the `lifespan` function, the `app.include_router(...)` loop, and the `app.mount(StaticFiles)` call from `server.py` to `periscope/app.py`. Strip `server.py` to the entry-point shim. Change uvicorn target from `"server:app"` to `"periscope.app:app"`. **One atomic commit.**

**Files:**
- Create: `periscope/app.py`, `tests/test_app.py`
- Modify: `server.py` (drastic shrinkage)

### Task 9.1: Write `tests/test_app.py`

- [ ] **Step 1: Create the test**

```python
"""Lifespan startup + shutdown wiring."""

import pytest
from fastapi.testclient import TestClient


def test_app_construction_imports_cleanly():
    """Smoke: importing periscope.app gives us a FastAPI instance."""
    from periscope.app import app
    from fastapi import FastAPI
    assert isinstance(app, FastAPI)


def test_app_includes_state_router():
    """The /api/state route is registered."""
    from periscope.app import app
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/api/state" in paths


def test_lifespan_starts_and_shuts_down_cleanly(mocker):
    """TestClient triggers lifespan startup on enter, shutdown on exit."""
    # Mock the heavyweight prewarms so the test is fast.
    mocker.patch("periscope.git_pr.prewarm_pr_cache")
    mocker.patch("periscope.usage.cached_scraped_usage")
    mocker.patch("periscope.usage.kill_orphan_usage_sessions")

    # _lgtm_periodic_refresh and _mcp_listener are async generators wrapped
    # via _task(coro, name). Patching with `return_value=None` would make
    # the call return None instead of a coroutine, and `_task(None, ...)`
    # raises. Replace with coroutine factories.
    async def _noop():
        return None
    mocker.patch("periscope.lgtm._lgtm_periodic_refresh", side_effect=_noop)
    mocker.patch("periscope.channels._mcp_listener", side_effect=_noop)

    from periscope.app import app
    with TestClient(app) as client:
        r = client.get("/api/state")
        assert r.status_code == 200
```

### Task 9.2: Create `periscope/app.py`

- [ ] **Step 1: Write the module**

```python
"""FastAPI app construction + lifespan + static mount.

Imported by server.py (the entry-point shim) and by uvicorn via the
`periscope.app:app` import string. Stage A's `"server:app"` target was
the migration-only shape; Peel 9 flips it to this module.
"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from periscope.channels import _mcp_listener
from periscope.config import MCP_SOCKET_PATH, STATIC
from periscope.git_pr import prewarm_pr_cache
from periscope.lgtm import _LGTM_SSE_TASKS, _lgtm_periodic_refresh
from periscope.log import log, _bg, _task
from periscope.usage import cached_scraped_usage, kill_orphan_usage_sessions

# Routes
from periscope.routes import (
    auto_rename, channel, history, lgtm as lgtm_route,
    pane, paste_image, prefs, send, sessions, state, ws,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("periscope starting (pid=%d)", os.getpid())
    kill_orphan_usage_sessions()
    _bg("prewarm-pr", prewarm_pr_cache)
    _bg("prewarm-usage", cached_scraped_usage)
    mcp_task = _task(_mcp_listener(), "mcp-listener")
    lgtm_task = _task(_lgtm_periodic_refresh(), "lgtm-refresh")
    try:
        yield
    finally:
        log.info("periscope shutting down (pid=%d)", os.getpid())
        mcp_task.cancel()
        lgtm_task.cancel()
        for t in list(_LGTM_SSE_TASKS.values()):
            t.cancel()
        try:
            await mcp_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            os.unlink(MCP_SOCKET_PATH)
        except FileNotFoundError:
            pass


app = FastAPI(lifespan=lifespan)

for r in (state, prefs, pane, sessions, history, send, paste_image,
          channel, auto_rename, lgtm_route, ws):
    app.include_router(r.router)

# Mounted last so the API/WS routes above take precedence. `html=True`
# serves index.html for `/` (and any directory request) without needing
# a separate route.
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
```

### Task 9.3: Strip `server.py` to the entry-point shim

- [ ] **Step 1: Replace `server.py` with the shim**

The shim is approximately:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi", "uvicorn[standard]", "anthropic", "httpx",
#     "python-dotenv", "mcp==1.27.*", "websockets>=15,<17",
# ]
# ///
"""Periscope — live tmux dashboard. Run with: uv run server.py"""

if __name__ == "__main__":
    import atexit
    import os
    import signal
    import sys
    from pathlib import Path

    import uvicorn

    from periscope.log import log
    from periscope.pidfile import (
        _reclaim_existing_instance,
        _write_pidfile,
        _remove_pidfile,
    )

    _reclaim_existing_instance()
    _write_pidfile()
    atexit.register(_remove_pidfile)

    def _on_sigterm(signum, _frame):
        log.info("received signal %d; exiting", signum)
        sys.exit(0)
    signal.signal(signal.SIGTERM, _on_sigterm)

    dev_mode = os.environ.get("PERISCOPE_DEV") == "1"
    uvicorn.run(
        "periscope.app:app",
        host="127.0.0.1",
        port=8765,
        log_level="info",
        loop="asyncio",
        reload=dev_mode,
        reload_dirs=[str(Path(__file__).parent)] if dev_mode else None,
    )
```

Verify the resulting file:

```sh
wc -l /Users/tom/dev/periscope/server.py
```

Expected: ~50 lines. Anything significantly larger means something didn't move — diagnose.

- [ ] **Step 2: Update `periscope/__main__.py`**

Now that `periscope.app:app` is a real entry, replace the placeholder:

```python
"""Entry point: `python -m periscope` → uvicorn.run(periscope.app:app, ...).

Equivalent to `uv run server.py` but doesn't go through the PEP-723
header. Most users should use `uv run server.py`."""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("periscope.app:app", host="127.0.0.1", port=8765)
```

- [ ] **Step 3: Verify no remaining `from server import` ANYWHERE**

```sh
grep -rn "from server import\|import server\b" /Users/tom/dev/periscope/periscope/ /Users/tom/dev/periscope/tests/
```

Expected: empty (zero matches). If anything turns up, fix it before committing.

- [ ] **Step 4: Verification gate**

```sh
uv run pytest -q
```

Boot smoke (now exercises the new uvicorn target string):

```sh
uv run server.py > /tmp/peel9-boot.log 2>&1 &
SERVER_PID=$!
for i in 1 2 3 4 5 6 7 8; do sleep 1; curl -fsS http://127.0.0.1:8765/api/state > /dev/null 2>&1 && { echo "API OK"; break; }; done
kill $SERVER_PID 2>/dev/null
wait $SERVER_PID 2>/dev/null
rm -f ~/.config/periscope/periscope.pid
```

Confirm exactly one Python process under reload-off:

```sh
PERISCOPE_DEV=0 uv run server.py > /dev/null 2>&1 &
sleep 3
ps -ef | grep "[s]erver.py\|[p]eriscope.app" | wc -l
kill %1
wait 2>/dev/null
rm -f ~/.config/periscope/periscope.pid
```

Expected: `1` (one process).

- [ ] **Step 5: Manual smoke — full round-trip**

Boot periscope; open the dashboard in a browser; confirm:
- The grid renders with pane cards.
- Clicking a card opens the modal with a live terminal.
- Typing into the modal echoes in the pane.
- The Channels: spawn `claude --dangerously-load-development-channels server:periscope` in a new tmux window; invoke `reply`, `link_pr`, `link_linear`, `spawn_claude` from inside Claude; confirm each surfaces in the dashboard.
- Reload the dashboard; state persists.

- [ ] **Step 6: Commit (one atomic commit)**

```sh
git add periscope/app.py periscope/__main__.py tests/test_app.py server.py
git commit -m "split: final app move + uvicorn target flip to periscope.app:app (Peel 9)"
```

---

## End of Stage B — final verification

After Peel 9:

```sh
wc -l /Users/tom/dev/periscope/server.py
# Expected: ~50

wc -l /Users/tom/dev/periscope/periscope/*.py
# Expected: ~3000 total across all periscope/*.py + periscope/routes/*.py

ls /Users/tom/dev/periscope/periscope/routes/*.py
# Expected: __init__.py + 11 route modules

uv run pytest -q
# Expected: all green; 150+ tests

grep -rn "from server import\|import server\b" /Users/tom/dev/periscope/periscope/ /Users/tom/dev/periscope/tests/
# Expected: empty
```

Update `CLAUDE.md` to reflect the new structure (remove the "outgrowing one file — split on the table" note from §"Server (`server.py`)"; replace with a brief description of the periscope/ package layout).

### Update CLAUDE.md

- [ ] **Step 1: Replace the §"Server (`server.py`)" section**

Old:

```markdown
### Server (`server.py`)

Currently one file, but it's outgrowing that — a split is on the table.
...
```

New:

```markdown
### Server (`periscope/` package + `server.py` shim)

`server.py` is a ~50-line entry-point: PEP-723 header + `__main__` block
that does pidfile reclaim + signal install + `uvicorn.run("periscope.app:app", ...)`.

The package is organized by subsystem; one file per concern:

| Module | Role |
|---|---|
| `periscope/app.py` | FastAPI() + lifespan + include_router + StaticFiles mount |
| `periscope/config.py` | Cross-cutting paths + constants |
| `periscope/log.py` | Logging setup + _bg / _task crash wrappers |
| `periscope/pidfile.py` | Single-instance reclaim |
| `periscope/tmux.py` | tmux + subprocess wrappers |
| `periscope/store.py` | state.json layer (_STATE, load/write, migrations) |
| `periscope/channels.py` | In-process MCP server + tool implementations |
| `periscope/panes.py` | parse_pane + smoothing + focus tracking + list_windows |
| `periscope/pids.py` | @periscope_id mint/stamp/rebind/resolve |
| `periscope/git_pr.py` | Git state + GitHub PR cache + activity timeline |
| `periscope/lgtm.py` | LGTM mirror (poll + SSE) |
| `periscope/usage.py` | Claude plan usage (JSONL parse + TUI scrape) |
| `periscope/rename_ai.py` | Anthropic SDK plumbing for auto-rename |
| `periscope/routes/*.py` | One APIRouter per file (11 modules) |
| `tests/*.py` + `tests/routes/*.py` | pytest suite, mirrors package layout |

Tests run via `uv run pytest -q`. The existing `tests/test_channel_smoke.py`
is a separate PEP-723 script (excluded from pytest collection via
`tests/conftest.py:collect_ignore`).
```

- [ ] **Step 2: Commit**

```sh
git add CLAUDE.md
git commit -m "claude.md: replace single-file-server section with periscope/ package layout"
```

---

## Open items for future work (NOT in Stage B)

- **Typed accessors over `_STATE`.** Hide the raw dict behind `WindowAnnotation.get(pid)` / `.set(pid, **kwargs)`.
- **Extract `build_window_view(w)` from `routes/state.py`.** ~150 lines of per-window assembly — pure function, easy to unit-test.
- **Real pytest test for `_mcp_listener`.** Currently manual-smoke only.
- **`/ws/pane` integration test.** TestClient.websocket_connect smoke covers the happy path; real bidirectional terminal forwarding is still untested.
- **Replace `tests/test_channel_smoke.py`'s broken `channel_server.py` reference** (file was deleted in commit 7abb061 well before the split started).

# Inter-Claude Management Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add five MCP tools (`send_to`, `report`, `list_claudes`, `peek`, `terminate`) plus a `spawned_by` provenance breadcrumb so a Claude pane can manage other Claude panes through periscope instead of shelling out to tmux.

**Architecture:** Thin routing over existing primitives. All handlers are module-level `_do_*_tool(pane, arguments) -> _tool_result(body)` functions in `periscope/channels.py`, registered as `_CHANNEL_TOOLS` records — the unanimous convention for the eight existing tools. Messaging rides the existing `emit_channel_event` (channel notification) rail, which wakes idle recipients (empirically verified). One new shared resolver (`_resolve_window_by_pid`) and one shared delivery helper (`_deliver`). No new modules, no new store symbols.

**Tech Stack:** Python 3, FastAPI, MCP SDK (`mcp==1.27.*`), tmux, pytest + pytest-mock (no pytest-asyncio — async tests wrap calls in `asyncio.run(...)`).

**Reference docs:**
- Spec: `docs/superpowers/specs/2026-06-16-inter-claude-management-design.md`
- Structure: `docs/superpowers/specs/2026-06-16-inter-claude-management-structure.md`

---

## Background the engineer needs

**The tool convention.** Every tool is a function `_do_<name>_tool(pane, arguments)` where `pane` is the caller's `%N` tmux pane id and `arguments` is the JSON arg dict. It returns `_tool_result(body)` where `body` is a dict. Failures return `{"ok": False, "error": "<message>"}` — **never raise HTTPException** (that's the route convention; these are MCP tools). Success returns `{"ok": True, ...}`. The handler is wired by appending a record to the `_CHANNEL_TOOLS` list (near the bottom of `channels.py`): `{"name", "description", "inputSchema", "handler"}`. Dispatch (`_call_tool`) already awaits coroutine handlers via `asyncio.iscoroutinefunction`, so a handler may be `async def` or plain `def`.

**Handles.** A handle is an `@periscope_id` string (`pid`), the stable id `spawn_claude` returns. `list_windows()` rows carry the raw stamped id as `pid_raw`; the resolved `pid` is attached later by `_attach_git_then_resolve_pids`. Matching a handle therefore matches `pid_raw` (equal to `pid` for any stamped window).

**Existing helpers you will call (already in `channels.py` unless noted):**
- `emit_channel_event(pane_id, content) -> bool` (async) — push a channel block to a pane's Claude; `False` if no MCP session attached.
- `_resolve_pid_for_pane(pane_id) -> str` — pane → pid, `""` on miss.
- `channel_state_for(pane_id) -> dict` — has `["attached"]`.
- `_tool_result(body) -> list` — wraps body in MCP TextContent.
- `list_windows()` (imported) — raw window rows.
- `_attach_git_then_resolve_pids(windows)` (imported) — attaches `pid`, strips `pid_raw`, in place.
- `set_window_fields(pid, **fields)` (imported) — write window state; `None` value deletes the key.
- `_tmux_mutate(*args) -> (ok, msg)` (imported) — tmux command that surfaces failure.
- `get_window(pid) -> dict` — **add to the store import** (`from periscope.store import set_window_fields, get_window`); returns a copy of `windows[pid]` or `{}`.
- `capture(target, lines=100) -> str`, `parse_pane(content) -> dict` — lazy-import (`capture` from `periscope.tmux`, `parse_pane` from `periscope.panes`) for `list_claudes`'s stateless is_claude probe; `parsed["is_claude"]` is the Claude signal. **Do NOT use `build_window_view` for this** — it mutates poll state-transition tracking (`panes._completed_at`/`_prev_state`) as a side effect; a discovery tool must not perturb that.
- `pane_status_lines() -> dict[pane_id, (line, ts, rail)]` — lazy-import from `periscope.activity`.
- `session_id_for_pane(pane_id) -> str | None`, `jsonl_for_session(sid) -> Path | None`, `messages_from_jsonl(path_str) -> list` — lazy-import from `periscope.turns` (all three are module-level names there) for `peek`'s direct transcript read. **Do NOT use `get_turns_for_pane`** — it re-derives pane_id and re-runs the session lookup with a cwd fallback, defeating peek's no-cwd-guess guarantee.

**Testing conventions.** `tests/test_channels.py` has an autouse `reset_channel_state` fixture and uses `clean_state` + `mocker`. Async handlers are tested by wrapping the call in `asyncio.run(...)`. Mock `emit_channel_event` with `AsyncMock` (`from unittest.mock import AsyncMock`). To assert a tool body, decode the result: the handler returns `[TextContent(text=json.dumps(body))]`, so `json.loads(result[0].text)` is the body.

Add this helper near the top of the new test cases (once):

```python
import asyncio
import json
from unittest.mock import AsyncMock


def _body(result):
    """Decode a tool result's JSON body."""
    return json.loads(result[0].text)
```

---

## Task 1: `_resolve_window_by_pid` resolver helper

**Files:**
- Modify: `periscope/channels.py` (add helper next to `_resolve_pid_for_pane`, ~line 222)
- Test: `tests/test_channels.py`

- [ ] **Step 1: Write the failing test**

```python
def test_resolve_window_by_pid_matches_stamped_handle(mocker):
    from periscope import channels
    rows = [
        {"session": "s", "index": 2, "name": "w", "cwd": "/x",
         "pane_id": "%7", "pid_raw": "abcd1234"},
    ]
    mocker.patch("periscope.channels.list_windows", return_value=rows)

    def _attach(ws):
        for w in ws:
            w["pid"] = w.pop("pid_raw")
    mocker.patch("periscope.channels._attach_git_then_resolve_pids", side_effect=_attach)

    pid, pane_id, window = channels._resolve_window_by_pid("abcd1234")
    assert pid == "abcd1234"
    assert pane_id == "%7"
    assert window["session"] == "s" and window["index"] == 2


def test_resolve_window_by_pid_miss_returns_empty(mocker):
    from periscope import channels
    mocker.patch("periscope.channels.list_windows", return_value=[
        {"session": "s", "index": 2, "pane_id": "%7", "pid_raw": "other"},
    ])
    mocker.patch("periscope.channels._attach_git_then_resolve_pids")
    assert channels._resolve_window_by_pid("abcd1234") == ("", "", {})


def test_resolve_window_by_pid_empty_handle_returns_empty(mocker):
    from periscope import channels
    mocker.patch("periscope.channels.list_windows")
    assert channels._resolve_window_by_pid("") == ("", "", {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_channels.py::test_resolve_window_by_pid_matches_stamped_handle -v`
Expected: FAIL with `AttributeError: module 'periscope.channels' has no attribute '_resolve_window_by_pid'`

- [ ] **Step 3: Write minimal implementation**

Add after `_resolve_pid_for_pane` (~line 222 in `channels.py`):

```python
def _resolve_window_by_pid(handle: str) -> tuple[str, str, dict]:
    """Resolve an @periscope_id handle to (pid, pane_id, window).

    Matches on `pid_raw` — the stamped @periscope_id on the raw list_windows
    row — BEFORE resolution, because resolution attaches `pid` only after a
    match (raw rows carry pid_raw, not pid). peek/terminate read
    session/index off the returned window dict. Returns ("", "", {}) when no
    live window matches."""
    if not handle:
        return "", "", {}
    for w in list_windows():
        if w.get("pid_raw") == handle:
            _attach_git_then_resolve_pids([w])
            return w.get("pid") or "", w.get("pane_id") or "", w
    return "", "", {}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_channels.py -k resolve_window_by_pid -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add periscope/channels.py tests/test_channels.py
git commit -m "feat(channels): _resolve_window_by_pid handle resolver"
```

---

## Task 2: `spawned_by` provenance write in `spawn_claude`

**Files:**
- Modify: `periscope/channels.py` (`_do_spawn_claude_tool`, after the `pid, pane_id = _resolve_window(...)` block at ~line 485-487)
- Modify: `periscope/channels.py` import line (`from periscope.store import set_window_fields, get_window`)
- Test: `tests/test_channels.py`

- [ ] **Step 1: Write the failing test**

`_do_spawn_claude_tool` is async and runs the full tmux spawn path. The test mocks every tmux/resolution seam and asserts the provenance write. (There is **no existing spawn test** — this is new.)

```python
def test_spawn_claude_writes_spawned_by(mocker):
    from periscope import channels
    # tmux session lookup for the caller pane → session|cwd
    mocker.patch("periscope.channels.tmux", return_value="sess|/home/tom")
    mocker.patch("periscope.channels._run", return_value=(0, ""))  # has-session ok
    mocker.patch("periscope.channels._tmux_mutate", return_value=(True, "3"))
    mocker.patch("periscope.channels.os.path.isdir", return_value=True)
    mocker.patch("periscope.channels.asyncio.sleep", new=AsyncMock())
    mocker.patch("periscope.channels._plain_pane_snapshot", return_value="auto mode on")
    mocker.patch("periscope.channels.note_focus")
    mocker.patch("periscope.channels.note_action")
    mocker.patch("periscope.channels._resolve_window", return_value=("child99", "%9"))
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="parent11")
    set_fields = mocker.patch("periscope.channels.set_window_fields")

    asyncio.run(channels._do_spawn_claude_tool("%1", {"prompt": "go"}))

    set_fields.assert_any_call("child99", spawned_by="parent11")


def test_spawn_claude_no_parent_tolerated(mocker):
    from periscope import channels
    mocker.patch("periscope.channels.tmux", return_value="sess|/home/tom")
    mocker.patch("periscope.channels._run", return_value=(0, ""))
    mocker.patch("periscope.channels._tmux_mutate", return_value=(True, "3"))
    mocker.patch("periscope.channels.os.path.isdir", return_value=True)
    mocker.patch("periscope.channels.asyncio.sleep", new=AsyncMock())
    mocker.patch("periscope.channels._plain_pane_snapshot", return_value="auto mode on")
    mocker.patch("periscope.channels.note_focus")
    mocker.patch("periscope.channels.note_action")
    mocker.patch("periscope.channels._resolve_window", return_value=("child99", "%9"))
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="")  # vanished caller
    set_fields = mocker.patch("periscope.channels.set_window_fields")

    result = asyncio.run(channels._do_spawn_claude_tool("%1", {"prompt": "go"}))

    # no spawned_by write when parent pid can't be resolved, and no crash
    for call in set_fields.call_args_list:
        assert "spawned_by" not in call.kwargs
    assert json.loads(result[0].text)["ok"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_channels.py::test_spawn_claude_writes_spawned_by -v`
Expected: FAIL — `set_window_fields("child99", spawned_by="parent11")` never called (no provenance write yet).

- [ ] **Step 3: Write minimal implementation**

First extend the store import at the top of `channels.py`:

```python
from periscope.store import set_window_fields, get_window
```

Then, in `_do_spawn_claude_tool`, immediately after the existing block:

```python
    pid, pane_id = _resolve_window(
        lambda w: w.get("session") == session and w.get("index") == index
    )
```

add:

```python
    # Provenance breadcrumb: record who spawned this child so report() knows
    # where "back" is. Pure metadata, no ownership — a severed child simply
    # never calls report(). Guard on both ids so a vanished caller or
    # unresolved child doesn't write a junk link.
    parent_pid = _resolve_pid_for_pane(pane)
    if parent_pid and pid:
        set_window_fields(pid, spawned_by=parent_pid)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_channels.py -k spawn_claude -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add periscope/channels.py tests/test_channels.py
git commit -m "feat(channels): spawn_claude records spawned_by provenance"
```

---

## Task 3: `_deliver` helper + `send_to` tool

**Files:**
- Modify: `periscope/channels.py` (add `_deliver` helper + `_do_send_to_tool` near the other handlers; add `_CHANNEL_TOOLS` record)
- Test: `tests/test_channels.py`

- [ ] **Step 1: Write the failing test**

```python
def test_send_to_happy(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("ab12", "%9", {"session": "s", "index": 2}))
    emit = mocker.patch("periscope.channels.emit_channel_event",
                        new=AsyncMock(return_value=True))

    body = _body(asyncio.run(channels._do_send_to_tool("%1", {"handle": "ab12", "message": "hi"})))

    emit.assert_awaited_once_with("%9", "hi")
    assert body == {"ok": True, "handle": "ab12", "pane_id": "%9"}


def test_send_to_no_window(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid", return_value=("", "", {}))
    body = _body(asyncio.run(channels._do_send_to_tool("%1", {"handle": "ab12", "message": "hi"})))
    assert body["ok"] is False and "no live window" in body["error"]


def test_send_to_not_attached(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("ab12", "%9", {}))
    mocker.patch("periscope.channels.emit_channel_event", new=AsyncMock(return_value=False))
    body = _body(asyncio.run(channels._do_send_to_tool("%1", {"handle": "ab12", "message": "hi"})))
    assert body["ok"] is False and "not attached" in body["error"]


def test_send_to_self_refused(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("ab12", "%1", {}))
    emit = mocker.patch("periscope.channels.emit_channel_event", new=AsyncMock(return_value=True))
    body = _body(asyncio.run(channels._do_send_to_tool("%1", {"handle": "ab12", "message": "hi"})))
    assert body["ok"] is False and "own pane" in body["error"]
    emit.assert_not_awaited()


def test_send_to_missing_args(mocker):
    from periscope import channels
    body = _body(asyncio.run(channels._do_send_to_tool("%1", {"handle": "", "message": "hi"})))
    assert body["ok"] is False and "handle" in body["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_channels.py::test_send_to_happy -v`
Expected: FAIL — `module 'periscope.channels' has no attribute '_do_send_to_tool'`

- [ ] **Step 3: Write minimal implementation**

Add the shared delivery helper + handler (near the other `_do_*_tool` functions):

```python
async def _deliver(pane_id: str, message: str, caller_pane: str) -> dict:
    """Shared send_to/report delivery: self-send guard, channel push, and the
    not-attached error mapping. Returns the tool body dict (callers augment
    success with their own fields)."""
    if pane_id == caller_pane:
        return {"ok": False, "error": "refusing to send to your own pane"}
    sent = await emit_channel_event(pane_id, message)
    if not sent:
        return {"ok": False, "error": "target not attached to periscope channel"}
    return {"ok": True}


async def _do_send_to_tool(pane: str, arguments: dict):
    """Deliver a message to another live Claude by handle (pid). Wakes the
    recipient via the channel rail."""
    handle = str(arguments.get("handle", "")).strip()
    message = str(arguments.get("message", "")).strip()
    if not handle:
        return _tool_result({"ok": False, "error": "handle is required"})
    if not message:
        return _tool_result({"ok": False, "error": "message is required"})
    _pid, pane_id, _w = _resolve_window_by_pid(handle)
    if not pane_id:
        return _tool_result({"ok": False, "error": f"no live window for handle {handle}"})
    body = await _deliver(pane_id, message, pane)
    if body.get("ok"):
        body = {"ok": True, "handle": handle, "pane_id": pane_id}
    return _tool_result(body)
```

Append to `_CHANNEL_TOOLS`:

```python
    {
        "name": "send_to",
        "description": (
            "Send a message to another live Claude pane by its handle "
            "(the pid returned by spawn_claude / list_claudes). The message "
            "wakes the recipient and arrives as a channel block it acts on. "
            "Use to delegate a task to, or nudge, another Claude. Errors if "
            "the handle resolves to no live window or the target isn't "
            "attached to periscope's channel."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string", "description": "Target pid (from spawn_claude/list_claudes)."},
                "message": {"type": "string", "description": "Message to deliver."},
            },
            "required": ["handle", "message"],
        },
        "handler": _do_send_to_tool,
    },
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_channels.py -k send_to -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add periscope/channels.py tests/test_channels.py
git commit -m "feat(channels): send_to tool + shared _deliver helper"
```

---

## Task 4: `report` tool

**Files:**
- Modify: `periscope/channels.py` (`_do_report_tool` + `_CHANNEL_TOOLS` record)
- Test: `tests/test_channels.py`

- [ ] **Step 1: Write the failing test**

```python
def test_report_routes_to_spawner(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="child99")
    mocker.patch("periscope.channels.get_window", return_value={"spawned_by": "parent11"})
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("parent11", "%2", {}))
    emit = mocker.patch("periscope.channels.emit_channel_event", new=AsyncMock(return_value=True))

    body = _body(asyncio.run(channels._do_report_tool("%9", {"message": "done"})))

    emit.assert_awaited_once_with("%2", "done")
    assert body == {"ok": True, "to": "parent11"}


def test_report_no_spawner_errors(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="child99")
    mocker.patch("periscope.channels.get_window", return_value={})  # no spawned_by
    body = _body(asyncio.run(channels._do_report_tool("%9", {"message": "done"})))
    assert body["ok"] is False and "no spawner" in body["error"]


def test_report_spawner_gone(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="child99")
    mocker.patch("periscope.channels.get_window", return_value={"spawned_by": "parent11"})
    mocker.patch("periscope.channels._resolve_window_by_pid", return_value=("", "", {}))
    body = _body(asyncio.run(channels._do_report_tool("%9", {"message": "done"})))
    assert body["ok"] is False and "no longer live" in body["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_channels.py::test_report_routes_to_spawner -v`
Expected: FAIL — `_do_report_tool` missing.

- [ ] **Step 3: Write minimal implementation**

```python
async def _do_report_tool(pane: str, arguments: dict):
    """Report back to the pane that spawned this one. Sugar over send_to,
    routed via the spawned_by provenance breadcrumb — the worker doesn't carry
    the parent's handle, the server knows it."""
    message = str(arguments.get("message", "")).strip()
    if not message:
        return _tool_result({"ok": False, "error": "message is required"})
    caller_pid = _resolve_pid_for_pane(pane)
    if not caller_pid:
        return _tool_result({"ok": False, "error": f"could not resolve pid for pane {pane}"})
    spawned_by = get_window(caller_pid).get("spawned_by")
    if not spawned_by:
        return _tool_result({"ok": False, "error": "this pane has no spawner to report to"})
    _pid, pane_id, _w = _resolve_window_by_pid(spawned_by)
    if not pane_id:
        return _tool_result({"ok": False, "error": "spawner is no longer live"})
    body = await _deliver(pane_id, message, pane)
    if body.get("ok"):
        body = {"ok": True, "to": spawned_by}
    return _tool_result(body)
```

Append to `_CHANNEL_TOOLS`:

```python
    {
        "name": "report",
        "description": (
            "Report a message back to the Claude that spawned this pane. Use "
            "when you were delegated a task and want to return your result to "
            "your lead — it wakes them with your message. Errors if this pane "
            "has no recorded spawner (it was hand-created or its spawner has "
            "exited)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message to send to your spawner."},
            },
            "required": ["message"],
        },
        "handler": _do_report_tool,
    },
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_channels.py -k report -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add periscope/channels.py tests/test_channels.py
git commit -m "feat(channels): report tool routes to spawner via spawned_by"
```

---

## Task 5: `list_claudes` tool

**Files:**
- Modify: `periscope/channels.py` (`_do_list_claudes_tool` + `_CHANNEL_TOOLS` record)
- Test: `tests/test_channels.py`

- [ ] **Step 1: Write the failing test**

```python
def test_list_claudes_filters_and_trims(mocker):
    from periscope import channels
    rows = [
        {"session": "s", "index": 1, "name": "lead", "cwd": "/a", "pane_id": "%2", "pid_raw": "p1"},
        {"session": "s", "index": 2, "name": "shell", "cwd": "/b", "pane_id": "%3", "pid_raw": "p2"},
    ]
    mocker.patch("periscope.channels.list_windows", return_value=rows)

    def _attach(ws):
        for w in ws:
            if "pid_raw" in w:
                w["pid"] = w.pop("pid_raw")
    mocker.patch("periscope.channels._attach_git_then_resolve_pids", side_effect=_attach)

    # capture returns the target so parse_pane can tell windows apart; is_claude
    # true only for the "s:1" pane. This exercises the real per-pane probe path.
    mocker.patch("periscope.tmux.capture", side_effect=lambda target, *a, **k: target)
    mocker.patch("periscope.panes.parse_pane",
                 side_effect=lambda content: {"is_claude": content == "s:1"})
    mocker.patch("periscope.activity.pane_status_lines",
                 return_value={"%2": ("reviewing PR", 123, None)})
    mocker.patch("periscope.channels.channel_state_for", return_value={"attached": True})
    mocker.patch("periscope.channels.get_window", return_value={"spawned_by": "boss0"})

    body = _body(asyncio.run(channels._do_list_claudes_tool("%1", {})))

    assert body["ok"] is True
    assert len(body["claudes"]) == 1
    c = body["claudes"][0]
    assert c == {
        "handle": "p1", "name": "lead", "session": "s", "cwd": "/a",
        "status_line": "reviewing PR", "attached": True, "spawned_by": "boss0",
    }
    assert "pane_id" not in c
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_channels.py::test_list_claudes_filters_and_trims -v`
Expected: FAIL — `_do_list_claudes_tool` missing.

- [ ] **Step 3: Write minimal implementation**

```python
async def _do_list_claudes_tool(pane: str, arguments: dict):
    """List all live Claude panes with their handles, so the caller can
    discover, message (send_to), peek, or terminate them. Flat — supports peer
    discovery and handoff, not just a spawn subtree.

    is_claude is probed per pane with a stateless capture+parse_pane — NOT
    build_window_view, which mutates poll state-transition tracking. The
    capture fan-out is offloaded to a thread so it doesn't block the event
    loop; pid resolution (which writes state.json and is not thread-safe) runs
    in the loop first, mirroring the /api/state route's ordering."""
    from periscope.tmux import capture
    from periscope.panes import parse_pane
    from periscope.activity import pane_status_lines

    windows = list_windows()
    _attach_git_then_resolve_pids(windows)  # attaches pid, strips pid_raw (not thread-safe)
    statuses = pane_status_lines()

    def _collect():
        out = []
        for w in windows:
            target = f"{w['session']}:{w['index']}"
            try:
                parsed = parse_pane(capture(target))
            except Exception:
                continue
            if not parsed.get("is_claude"):
                continue
            pane_id = w.get("pane_id") or ""
            status = statuses.get(pane_id)
            pid = w.get("pid") or ""
            out.append({
                "handle": pid,
                "name": w.get("name"),
                "session": w.get("session"),
                "cwd": w.get("cwd"),
                "status_line": status[0] if status else None,
                "attached": channel_state_for(pane_id)["attached"],
                "spawned_by": get_window(pid).get("spawned_by"),
            })
        return out

    claudes = await asyncio.to_thread(_collect)
    return _tool_result({"ok": True, "claudes": claudes})
```

Append to `_CHANNEL_TOOLS`:

```python
    {
        "name": "list_claudes",
        "description": (
            "List every live Claude pane periscope can see, with each one's "
            "handle (pid), name, session, cwd, latest status line, whether "
            "it's attached to periscope's channel (messageable via send_to), "
            "and its spawner handle. Use to discover other Claudes before "
            "messaging, peeking, or terminating them."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _do_list_claudes_tool,
    },
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_channels.py -k list_claudes -v`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add periscope/channels.py tests/test_channels.py
git commit -m "feat(channels): list_claudes discovery tool"
```

---

## Task 6: `peek` tool

**Files:**
- Modify: `periscope/channels.py` (`_do_peek_tool` + `_CHANNEL_TOOLS` record)
- Test: `tests/test_channels.py`

- [ ] **Step 1: Write the failing test**

The critical assertion: when there's no recorded session, `peek` refuses **and never reaches `jsonl_for_session`/`messages_from_jsonl`** (the transcript read). peek reads directly off the resolved session id — it does NOT call `get_turns_for_pane` (which re-derives pane_id and has a cwd fallback). `_do_peek_tool` is a plain `def` — call it directly (no `asyncio.run`).

```python
def test_peek_happy(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("ab12", "%9", {}))
    mocker.patch("periscope.turns.session_id_for_pane", return_value="sess-abc")
    mocker.patch("periscope.turns.jsonl_for_session", return_value="/x/sess-abc.jsonl")
    msgs = [{"role": "user", "text": str(i)} for i in range(30)]
    mocker.patch("periscope.turns.messages_from_jsonl", return_value=msgs)

    body = _body(channels._do_peek_tool("%1", {"handle": "ab12"}))

    assert body["ok"] is True and body["handle"] == "ab12"
    assert len(body["turns"]) == 20
    assert body["turns"][-1]["text"] == "29"


def test_peek_no_session_refuses_without_transcript_read(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("ab12", "%9", {}))
    mocker.patch("periscope.turns.session_id_for_pane", return_value=None)
    jsonl = mocker.patch("periscope.turns.jsonl_for_session")
    msgs = mocker.patch("periscope.turns.messages_from_jsonl")

    body = _body(channels._do_peek_tool("%1", {"handle": "ab12"}))

    assert body["ok"] is False and "no recorded session" in body["error"]
    jsonl.assert_not_called()   # the cwd-collision footgun is unreachable
    msgs.assert_not_called()


def test_peek_no_window(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid", return_value=("", "", {}))
    body = _body(channels._do_peek_tool("%1", {"handle": "ab12"}))
    assert body["ok"] is False and "no live window" in body["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_channels.py::test_peek_happy -v`
Expected: FAIL — `_do_peek_tool` missing.

- [ ] **Step 3: Write minimal implementation**

```python
def _do_peek_tool(pane: str, arguments: dict):
    """Read another Claude's recent transcript by handle, without messaging it.
    Reads directly off the pane's recorded session id — refuses when there is
    none rather than guessing by cwd (which on a shared cwd would return a
    sibling pane's transcript). Bypasses get_turns_for_pane precisely because
    that helper re-derives pane_id and has the cwd fallback."""
    from periscope.turns import (
        session_id_for_pane, jsonl_for_session, messages_from_jsonl,
    )

    handle = str(arguments.get("handle", "")).strip()
    if not handle:
        return _tool_result({"ok": False, "error": "handle is required"})
    _pid, pane_id, _w = _resolve_window_by_pid(handle)
    if not pane_id:
        return _tool_result({"ok": False, "error": f"no live window for handle {handle}"})
    sid = session_id_for_pane(pane_id)
    if sid is None:
        return _tool_result({"ok": False, "error": f"no recorded session for handle {handle}"})
    jsonl = jsonl_for_session(sid)
    if jsonl is None:
        return _tool_result({"ok": False, "error": "session transcript not found"})
    messages = messages_from_jsonl(str(jsonl))
    return _tool_result({"ok": True, "handle": handle, "turns": messages[-20:]})
```

Append to `_CHANNEL_TOOLS`:

```python
    {
        "name": "peek",
        "description": (
            "Read the recent transcript (last ~20 messages) of another Claude "
            "pane by its handle, without sending it anything — use to check on "
            "a delegated worker's progress instead of waiting for a report. "
            "Refuses if the pane has no recorded session yet."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string", "description": "Target pid (from spawn_claude/list_claudes)."},
            },
            "required": ["handle"],
        },
        "handler": _do_peek_tool,
    },
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_channels.py -k peek -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add periscope/channels.py tests/test_channels.py
git commit -m "feat(channels): peek tool (refuses cwd-fallback transcript)"
```

---

## Task 7: `terminate` tool

**Files:**
- Modify: `periscope/channels.py` (`_do_terminate_tool` + `_CHANNEL_TOOLS` record)
- Test: `tests/test_channels.py`

- [ ] **Step 1: Write the failing test**

`_do_terminate_tool` is a plain `def` — call it directly (no `asyncio.run`).

```python
def test_terminate_happy(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("ab12", "%9", {"session": "s", "index": 4}))
    mut = mocker.patch("periscope.channels._tmux_mutate", return_value=(True, ""))
    body = _body(channels._do_terminate_tool("%1", {"handle": "ab12"}))
    mut.assert_called_once_with("kill-window", "-t", "s:4")
    assert body == {"ok": True, "terminated": "ab12"}


def test_terminate_self_refused(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("ab12", "%1", {"session": "s", "index": 4}))
    mut = mocker.patch("periscope.channels._tmux_mutate")
    body = _body(channels._do_terminate_tool("%1", {"handle": "ab12"}))
    assert body["ok"] is False and "own pane" in body["error"]
    mut.assert_not_called()


def test_terminate_no_window(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid", return_value=("", "", {}))
    body = _body(channels._do_terminate_tool("%1", {"handle": "ab12"}))
    assert body["ok"] is False and "no live window" in body["error"]


def test_terminate_mutate_failure(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("ab12", "%9", {"session": "s", "index": 4}))
    mocker.patch("periscope.channels._tmux_mutate", return_value=(False, "no such window"))
    body = _body(channels._do_terminate_tool("%1", {"handle": "ab12"}))
    assert body["ok"] is False and body["error"] == "no such window"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_channels.py::test_terminate_happy -v`
Expected: FAIL — `_do_terminate_tool` missing.

- [ ] **Step 3: Write minimal implementation**

```python
def _do_terminate_tool(pane: str, arguments: dict):
    """Kill another Claude's tmux window by handle — cleanup after delegation,
    or tear down a stuck worker. Refuses to kill the caller's own pane."""
    handle = str(arguments.get("handle", "")).strip()
    if not handle:
        return _tool_result({"ok": False, "error": "handle is required"})
    _pid, pane_id, window = _resolve_window_by_pid(handle)
    if not pane_id:
        return _tool_result({"ok": False, "error": f"no live window for handle {handle}"})
    if pane_id == pane:
        return _tool_result({"ok": False, "error": "refusing to terminate your own pane"})
    target = f"{window['session']}:{window['index']}"
    ok, msg = _tmux_mutate("kill-window", "-t", target)
    if not ok:
        return _tool_result({"ok": False, "error": msg})
    return _tool_result({"ok": True, "terminated": handle})
```

Append to `_CHANNEL_TOOLS`:

```python
    {
        "name": "terminate",
        "description": (
            "Kill another Claude's tmux window by its handle. Use to clean up "
            "a worker after it's delegated its result, or to tear down a stuck "
            "one. Refuses to terminate your own pane. This is destructive — the "
            "window and its Claude session are gone."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string", "description": "Target pid (from spawn_claude/list_claudes)."},
            },
            "required": ["handle"],
        },
        "handler": _do_terminate_tool,
    },
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_channels.py -k terminate -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add periscope/channels.py tests/test_channels.py
git commit -m "feat(channels): terminate tool"
```

---

## Task 8: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the channels + store suites**

Run: `uv run pytest tests/test_channels.py tests/test_store.py -q`
Expected: all green (existing cases + ~21 new).

- [ ] **Step 2: Run the full suite to confirm no regression**

Run: `uv run pytest -q`
Expected: full suite green (~630 + new cases). Investigate any failure that touches `channels`, `pids`, `store`, `panes`, `turns`, or `window_view` — those are this change's blast radius.

- [ ] **Step 3: Smoke the MCP wire format (tool registry didn't break)**

Run: `uv run tests/test_channel_smoke.py`
Expected: passes — confirms the five new `_CHANNEL_TOOLS` records didn't break the listing/dispatch wire shape.

---

## Self-review notes (author)

**Spec coverage:** send_to (Task 3), report (Task 4), list_claudes (Task 5), peek (Task 6), terminate (Task 7), provenance (Task 2), resolver (Task 1). All six tools + the breadcrumb + the resolve-before-match helper are covered. Error semantics (no-window, not-attached, no-spawner, self-target, mutate-failure) each have a test. The peek cwd-fallback refusal — the spec's sharpest edge — is asserted via `get_turns_for_pane.assert_not_called()`.

**Deliberately not in this plan (per spec):** the concurrent-reports contention check (3 workers → 1 idle lead) — it validates Claude Code's wake coalescing, needs a live Claude, and is a manual/integration verification, not a unit test. The spec marks it "verify during implementation"; do it manually after merge if desired. Raw keystroke control is a documented non-goal.

**Type/name consistency:** `_resolve_window_by_pid -> (pid, pane_id, window)` used identically in Tasks 3/4/6/7. `_deliver(pane_id, message, caller_pane) -> dict` introduced in Task 3, reused in Task 4. `get_window(pid).get("spawned_by")` in Tasks 4 and 5. `_tmux_mutate -> (ok, msg)` in Tasks 2 and 7.

**Lazy imports:** `capture`/`parse_pane` (list_claudes probe), `pane_status_lines`, and the `turns` read functions (`session_id_for_pane`/`jsonl_for_session`/`messages_from_jsonl`) are imported inside their handlers (matching the module's existing lazy-import pattern for `history`/`routes.sessions`) to avoid import cycles. `get_window` is a top-level import (store has no cycle back to channels).

**Plan-review fixes folded in (2026-06-16):** (Must-fix #1) `peek` reads the transcript directly via `session_id_for_pane → jsonl_for_session → messages_from_jsonl` instead of `get_turns_for_pane`, so the gate and the read share one pane_id/session id and the cwd-fallback path is genuinely unreachable (the test now asserts `jsonl_for_session`/`messages_from_jsonl` are not called on a no-session miss). (Should-fix #3) `list_claudes` probes `is_claude` with a stateless `capture`+`parse_pane` offloaded to a thread, not `build_window_view` (which mutates poll state-transition tracking). (Consider #5) provenance insertion line corrected to ~485-487.

# Linear start and select Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let another local app start Claude on a Linear issue's branch with a prompt (`POST /api/linear/start`), and ask the UI to select a pane (`POST /api/select`).

**Architecture:** The start route copies `/api/reviews/prepare`'s spawn path (worktree → session → `spawn_claude_with_prompt` → rail placement) and adds the Linear link. A Claude already on the branch is reported, not prompted. The select route keeps one request in memory; `build_state()` puts it in the state blob, and `poll.js` applies it once per tab.

**Tech Stack:** FastAPI, pytest (`pytest-mock`, `httpx.ASGITransport`), Preact signals, vitest.

**Spec:** `docs/superpowers/specs/2026-11-15-linear-start-design.md`.

**Worktree:** `/Users/thibaultdody/dev/worktrees/periscope/start-in-periscope`, branch `thibaultdody/start-in-periscope`. Run every command from there. Never edit `/Users/thibaultdody/github/tomplex/periscope`: prod runs from it.

---

## File map

| File | Change |
|---|---|
| `periscope/state_hub.py` | Add `last_windows()`: the windows of the last state blob. |
| `periscope/routes/reviews.py` | Use `state_hub.last_windows()`; drop its private `_windows()`. |
| `periscope/routes/linear.py` | New: `POST /api/linear/start`. |
| `periscope/app.py` | Register the `linear` router. |
| `periscope/routes/state.py` | `POST /api/select`; `select` in `build_state()`. |
| `static/src/select.js` | New: `selectToApply()`, a pure helper. |
| `static/src/poll.js` | Apply a select request in `applyState`. |
| `static/dist/` | Rebuilt bundle. |
| `tests/test_state_hub.py`, `tests/routes/test_linear.py` (new), `tests/routes/test_state.py`, `static/src/__tests__/select.test.js` (new) | Tests. |
| `README.md`, `CLAUDE.md`, the spec | Docs. |

---

### Task 0: Set up the worktree

- [ ] **Step 1: Install dependencies**

```bash
cd /Users/thibaultdody/dev/worktrees/periscope/start-in-periscope
uv sync
npm install
```

- [ ] **Step 2: Check that the suites pass before any change**

Run: `uv run pytest -q -x && npm test`
Expected: both pass. If a test fails before any change, stop and report it.

---

### Task 1: One way to read the last state's windows

`routes/reviews.py` has a private `_windows()` that decodes `state_hub._last_blob`. The new route needs the same answer, so the helper moves to `state_hub`.

**Files:**
- Modify: `periscope/state_hub.py` (after `unsubscribe`)
- Modify: `periscope/routes/reviews.py:29-31` and its 5 callers of `_windows()`
- Test: `tests/test_state_hub.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_state_hub.py`)

```python
def test_last_windows_reads_the_last_blob():
    assert state_hub.last_windows() == []
    state_hub._last_blob = json.dumps({"windows": [{"pid": "a"}], "ts": 1})
    assert state_hub.last_windows() == [{"pid": "a"}]
```

Add `import json` to the imports at the top of the file.

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_state_hub.py::test_last_windows_reads_the_last_blob -v`
Expected: FAIL with `AttributeError: module 'periscope.state_hub' has no attribute 'last_windows'`.

- [ ] **Step 3: Add `last_windows` to `periscope/state_hub.py`** (after `def unsubscribe`)

```python
def last_windows() -> list[dict]:
    """The windows of the last computed blob ([] before the first tick). Routes read this instead of
    computing the blob, which costs a full tmux and git poll."""
    blob = _last_blob
    return json.loads(blob).get("windows", []) if blob else []
```

- [ ] **Step 4: Use it in `periscope/routes/reviews.py`**

Delete these lines:

```python
def _windows() -> list[dict]:
    blob = state_hub._last_blob
    return json.loads(blob).get("windows", []) if blob else []
```

Then replace each call `_windows()` with `state_hub.last_windows()` (5 calls: in `reviews_list`'s `load`, the walkthrough route, the comment route, `_prepare`, `reviews_merge`). If `json` is no longer used in the file, remove `import json`; `bin/check` reports it.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_state_hub.py tests/routes/test_reviews.py -q`
Expected: all pass. The review tests patch `state_hub._last_blob`, which `last_windows()` reads.

- [ ] **Step 6: Commit**

```bash
git add periscope/state_hub.py periscope/routes/reviews.py tests/test_state_hub.py
git commit -m "state_hub: last_windows() reads the last blob's windows; reviews uses it

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `POST /api/linear/start`

**Files:**
- Create: `periscope/routes/linear.py`
- Modify: `periscope/app.py` (import list near line 22, router tuple near line 207)
- Test: `tests/routes/test_linear.py`

- [ ] **Step 1: Write the failing tests** (create `tests/routes/test_linear.py`)

```python
"""POST /api/linear/start — worktree, Claude with a prompt, the Linear link, rail placement.
Windows come from a patched state_hub._last_blob and the spawn is an AsyncMock (docs/testing.md)."""

import json
from unittest.mock import AsyncMock

import pytest

from periscope import channels, state_hub, store

BODY = {
    "repo": "/repos/api",
    "branch": "me/eng-1-fix-it",
    "ticket": "ENG-1",
    "title": "Fix it",
    "prompt": "Read Linear ticket ENG-1.",
}
SPAWNED = {"target": "api-wt:2", "session": "api-wt", "index": 2, "pid": "k", "pane_id": "%5"}


def _win(**over):
    base = {
        "pid": "p1",
        "pane_id": "%1",
        "session": "s",
        "index": 1,
        "repo_label": "api",
        "branch": "me/eng-1-fix-it",
        "agent": "claude",
        "channel_attached": True,
        "activity": 1,
    }
    return {**base, **over}


@pytest.fixture(autouse=True)
def _env(monkeypatch, mocker):
    from periscope.routes import linear as route

    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": []}))
    # A spawn in one test must not make the next test's start refuse to spawn.
    monkeypatch.setattr(route, "_recent_spawn", {})
    # Never a live tmux listing.
    mocker.patch("periscope.routes.linear.list_windows", return_value=[])
    mocker.patch("periscope.tracks.repo_default_track", return_value="/repos/api")
    mocker.patch("periscope.open_ops.resolve_worktree_session", return_value=("api-wt", {"repo": "/repos/api"}))


@pytest.fixture
def worktree(mocker):
    return mocker.patch(
        "periscope.worktree_spawn.spawn_worktree",
        return_value={"path": "/repos/api-wt", "base_branch": "main", "branch": BODY["branch"], "reused": False},
    )


@pytest.fixture
def spawn(mocker):
    return mocker.patch("periscope.channels.spawn_claude_with_prompt", new=AsyncMock(return_value=dict(SPAWNED)))


@pytest.fixture
def place(mocker):
    return mocker.patch("periscope.open_ops.place_in_rail")


def test_start_spawns_claude_in_the_worktree(client, worktree, spawn, place):
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 200
    assert r.json() == {"mode": "spawned", "pid": "k", "path": "/repos/api-wt", "reused": False}
    worktree.assert_called_once_with("/repos/api", "me/eng-1-fix-it")
    args, kwargs = spawn.call_args
    assert args == ("/repos/api-wt", "/repos/api", "Read Linear ticket ENG-1.") and kwargs == {"session": "api-wt"}
    assert store.get_window("k") == {"linked_linear": "ENG-1", "linked_linear_title": "Fix it"}
    place.assert_called_once_with("api-wt", {"repo": "/repos/api"}, ["k"])


def test_a_claude_on_the_branch_is_reported_not_prompted(client, monkeypatch, worktree, spawn, place):
    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": [_win()]}))
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 200 and r.json() == {"mode": "focused", "pid": "p1"}
    worktree.assert_not_called()
    spawn.assert_not_called()
    place.assert_not_called()


def test_a_shell_or_an_unattached_pane_on_the_branch_does_not_count(client, monkeypatch, worktree, spawn, place):
    wins = [_win(agent=None), _win(pid="p2", channel_attached=False)]
    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": wins}))
    assert client.post("/api/linear/start", json=BODY).json()["mode"] == "spawned"


def test_a_start_before_the_spawned_claude_attaches_is_refused(client, mocker, worktree, spawn, place):
    from periscope.routes import linear as route

    clock = mocker.patch("periscope.routes.linear.time.monotonic", return_value=1000.0)
    assert client.post("/api/linear/start", json=BODY).status_code == 200
    clock.return_value = 1000.0 + route.RECENT_SPAWN_S - 1
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 409 and "attached" in r.json()["detail"] and spawn.call_count == 1
    clock.return_value = 1000.0 + route.RECENT_SPAWN_S
    assert client.post("/api/linear/start", json=BODY).status_code == 200 and spawn.call_count == 2


def test_concurrent_starts_spawn_once(mocker, worktree, place):
    import asyncio

    import httpx

    from periscope import app as app_mod
    from periscope.routes import linear as route

    async def run():
        gate = asyncio.Event()

        async def slow_spawn(*a, **k):
            await gate.wait()
            return dict(SPAWNED)

        spawn = mocker.patch("periscope.channels.spawn_claude_with_prompt", new=AsyncMock(side_effect=slow_spawn))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_mod.app), base_url="http://t") as c:
            first = asyncio.create_task(c.post("/api/linear/start", json=BODY))
            while not spawn.call_count:
                await asyncio.sleep(0.01)
            second = await c.post("/api/linear/start", json=BODY)
            gate.set()
            return (await first).status_code, second.status_code, spawn.call_count

    assert asyncio.run(run()) == (200, 409, 1)
    assert route._starting == set()


def test_a_bad_branch_is_400(client, worktree, spawn):
    worktree.side_effect = ValueError("branch name must not start with '-'")
    r = client.post("/api/linear/start", json={**BODY, "branch": "-x"})
    assert r.status_code == 400 and "must not start" in r.json()["detail"]
    spawn.assert_not_called()


def test_a_worktree_outside_git_is_400(client, mocker, worktree, spawn):
    mocker.patch("periscope.open_ops.resolve_worktree_session", return_value=None)
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 400 and "not inside a git repo" in r.json()["detail"]
    spawn.assert_not_called()


def test_a_failed_spawn_is_502(client, worktree, spawn, place):
    spawn.side_effect = channels.SpawnFailed("no server")
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 502 and r.json()["detail"] == "spawn failed: no server"
    place.assert_not_called()


def test_a_rail_placement_failure_still_answers_spawned(client, worktree, spawn, place):
    place.side_effect = RuntimeError("store busy")
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 200 and r.json()["pid"] == "k"


def test_a_spawn_without_a_pid_still_answers_spawned(client, worktree, spawn, place):
    spawn.return_value = {**SPAWNED, "pid": ""}
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 200 and r.json()["pid"] is None
    place.assert_not_called()


def test_no_title_stamps_only_the_ticket(client, worktree, spawn, place):
    assert client.post("/api/linear/start", json={**BODY, "title": ""}).status_code == 200
    assert store.get_window("k") == {"linked_linear": "ENG-1"}
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/routes/test_linear.py -q`
Expected: FAIL at collection or setup: `ModuleNotFoundError: No module named 'periscope.routes.linear'`.

- [ ] **Step 3: Write `periscope/routes/linear.py`**

```python
"""POST /api/linear/start — another app (todo-hub) starts Claude on a Linear issue's branch.

The steps are /api/reviews/prepare's spawn path: worktree, Claude with a prompt, rail placement,
plus the Linear link on the new pane. A Claude already on the branch is only reported (mode
"focused"): its prompt is never pasted into a session that may be busy. Selecting the pane is a
separate request (POST /api/select), sent when the user asks for it.
"""

import asyncio
import os
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from periscope import channels, open_ops, reviews, state_hub, store, tracks, worktree_spawn
from periscope.log import log
from periscope.panes import list_windows

router = APIRouter()

# A start for the same (repo, branch) while one runs: a double press in the caller.
_starting: set[tuple[str, str]] = set()
# A spawned Claude shows as the owner only once its channel attaches and the next state lands
# (~10-20 s after the request returns); a start in that window is refused, not spawned twice.
_recent_spawn: dict[tuple[str, str], float] = {}
RECENT_SPAWN_S = 60.0


class StartBody(BaseModel):
    repo: str = Field(min_length=1)  # a main checkout, as GET /api/open/catalog lists it
    branch: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    title: str = ""
    prompt: str = Field(min_length=1)


@router.post("/api/linear/start")
async def linear_start(body: StartBody):
    key = (os.path.realpath(body.repo), body.branch)
    if key in _starting:
        raise HTTPException(409, "start already running")
    _starting.add(key)
    try:
        return await _start(body, key)
    finally:
        _starting.discard(key)


async def _start(body: StartBody, key: tuple[str, str]) -> dict:
    repo, branch = key
    owner = reviews.owner_for(state_hub.last_windows(), os.path.basename(repo.rstrip("/")), branch)
    if owner:
        _recent_spawn.pop(key, None)
        return {"mode": "focused", "pid": owner["pid"]}
    age = time.monotonic() - _recent_spawn.get(key, -RECENT_SPAWN_S)
    if age < RECENT_SPAWN_S:
        raise HTTPException(409, f"a pane was spawned {int(age)} s ago and hasn't attached yet")
    try:
        wt = await asyncio.to_thread(worktree_spawn.spawn_worktree, repo, branch)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    anchored = await asyncio.to_thread(open_ops.resolve_worktree_session, wt["path"])
    if not anchored:
        raise HTTPException(400, f"{wt['path']} is not inside a git repo")
    session, project = anchored
    try:
        spawned = await channels.spawn_claude_with_prompt(
            wt["path"], tracks.repo_default_track(project["repo"]), body.prompt, session=session
        )
    except channels.SpawnFailed as e:
        raise HTTPException(502, f"spawn failed: {e}") from e
    _recent_spawn[key] = time.monotonic()
    result = {"mode": "spawned", "pid": spawned["pid"] or None, "path": wt["path"], "reused": wt["reused"]}
    if not spawned["pid"]:
        # Claude is running with the prompt pasted; an error would invite a retry that spawns a second one.
        log.warning("linear start: spawned but pane id unknown for %s %s", repo, branch)
        return result
    store.set_window_fields(spawned["pid"], linked_linear=body.ticket, linked_linear_title=body.title or None)

    # Same server-side rail placement as prepare's spawn.
    def place() -> None:
        pane_pids = [w["pid_raw"] for w in list_windows() if w["session"] == session and w.get("pid_raw")]
        open_ops.place_in_rail(session, project, pane_pids or [spawned["pid"]])

    try:
        await asyncio.to_thread(place)
    except Exception:
        # Placement only orders the rail; the Claude exists, and an error would invite a second spawn.
        log.warning("linear start: rail placement failed for %s %s", repo, branch, exc_info=True)
    return result
```

- [ ] **Step 4: Register the router in `periscope/app.py`**

In the `from periscope.routes import (...)` block, add `linear,` after `history,`:

```python
    history,
    linear,
    pane,
```

In the `for r in (...)` tuple, add `linear,` after `history,`:

```python
    history,
    linear,
    open_route,
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/routes/test_linear.py -q`
Expected: 11 passed.

- [ ] **Step 6: Run lint and the route suites**

Run: `bin/check && uv run pytest tests/routes -q`
Expected: `bin/check` reports zero violations; all route tests pass. Fix anything `bin/check --fix` cannot.

- [ ] **Step 7: Commit**

```bash
git add periscope/routes/linear.py periscope/app.py tests/routes/test_linear.py
git commit -m "linear: POST /api/linear/start makes the worktree, starts Claude with a prompt and links the issue

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `POST /api/select` and `select` in the state blob

**Files:**
- Modify: `periscope/routes/state.py` (imports; the `blob` dict in `build_state()` near line 182; a new route after `state()`)
- Test: `tests/routes/test_state.py`

- [ ] **Step 1: Write the failing test** (append to `tests/routes/test_state.py`)

```python
def test_a_select_request_rides_the_state_blob(client, mocker, clean_state):
    from periscope.routes import state as route

    mocker.patch.object(route, "_select", None)
    kick = mocker.patch("periscope.state_hub.kick")
    _patch(mocker, "list_windows", return_value=[])
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "cached_claude_usage", return_value={})
    _patch(mocker, "cached_plan_usage", return_value={})

    assert client.get("/api/state").json()["select"] is None
    assert client.post("/api/select", json={"pid": "a"}).json() == {"ok": True}
    kick.assert_called_once()
    first = client.get("/api/state").json()["select"]
    assert first["pid"] == "a" and isinstance(first["at"], int)
    client.post("/api/select", json={"pid": "b"})
    assert client.get("/api/state").json()["select"]["pid"] == "b"


def test_select_needs_a_pid(client):
    assert client.post("/api/select", json={"pid": ""}).status_code == 422
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/routes/test_state.py -q -k select`
Expected: FAIL with `AttributeError: <module 'periscope.routes.state'> does not have the attribute '_select'`.

- [ ] **Step 3: Add the route and the blob key in `periscope/routes/state.py`**

Imports: change

```python
from fastapi import APIRouter

from periscope import agent_processes, forks, resources, store, tmux, updater, usage
```

to

```python
from fastapi import APIRouter
from pydantic import BaseModel, Field

from periscope import agent_processes, forks, resources, state_hub, store, tmux, updater, usage
```

After `router = APIRouter()`, add:

```python
# The one pending "select this pane" request from another app (POST /api/select). It rides every
# blob; each tab applies a request once, while it is fresh (static/src/select.js).
_select: dict | None = None
```

In `build_state()`, add this entry to the `blob` dict, after `"resources": ...`:

```python
        # A pane another app asked the UI to select ({pid, at} in epoch ms), or None.
        "select": _select,
```

After the `state()` route at the end of the file, add:

```python
class SelectBody(BaseModel):
    pid: str = Field(min_length=1)


@router.post("/api/select")
def select(body: SelectBody):
    """Ask every open tab to select the pane `pid` (todo-hub's Show). A new request replaces the old one."""
    global _select
    _select = {"pid": body.pid, "at": int(time.time() * 1000)}
    state_hub.kick()  # push it now, not on the next tick
    return {"ok": True}
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/routes/test_state.py tests/test_state_hub.py -q`
Expected: all pass.

- [ ] **Step 5: Lint, then commit**

Run: `bin/check`
Expected: zero violations.

```bash
git add periscope/routes/state.py tests/routes/test_state.py
git commit -m "state: POST /api/select puts a one-time select request in the state blob

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The UI applies a select request

**Files:**
- Create: `static/src/select.js`
- Modify: `static/src/poll.js` (imports; `applyState`)
- Test: `static/src/__tests__/select.test.js`
- Rebuild: `static/dist/`

- [ ] **Step 1: Write the failing test** (create `static/src/__tests__/select.test.js`)

```js
import { describe, expect, it } from "vitest";
import { SELECT_FRESH_MS, selectToApply } from "../select.js";

describe("selectToApply", () => {
  const now = 1_000_000;

  it("applies a fresh request this tab has not applied", () => {
    expect(selectToApply({ pid: "a", at: now - 500 }, null, now)).toBe("a");
  });

  it("ignores the request it already applied", () => {
    expect(selectToApply({ pid: "a", at: now - 500 }, now - 500, now)).toBeNull();
  });

  it("ignores a request older than the fresh window", () => {
    expect(selectToApply({ pid: "a", at: now - SELECT_FRESH_MS - 1 }, null, now)).toBeNull();
  });

  it("ignores no request", () => {
    expect(selectToApply(null, null, now)).toBeNull();
    expect(selectToApply(undefined, null, now)).toBeNull();
  });
});
```

- [ ] **Step 2: Run it to see it fail**

Run: `npx vitest run static/src/__tests__/select.test.js`
Expected: FAIL: `Failed to resolve import "../select.js"`.

- [ ] **Step 3: Create `static/src/select.js`**

```js
// A select request from another app (POST /api/select), riding the state blob as `select`:
// { pid, at } with `at` in epoch ms. Each tab applies a request once, and only while it is
// fresh, so a tab opened later does not jump to a pane someone asked for minutes ago.
export const SELECT_FRESH_MS = 30_000;

/** The pid to select now, or null. `lastAt` is the `at` of the request this tab applied last. */
export function selectToApply(select, lastAt, nowMs) {
  if (!select?.pid || select.at === lastAt) return null;
  if (nowMs - select.at > SELECT_FRESH_MS) return null;
  return select.pid;
}
```

- [ ] **Step 4: Run the test**

Run: `npx vitest run static/src/__tests__/select.test.js`
Expected: 4 passed.

- [ ] **Step 5: Apply it in `static/src/poll.js`**

In the `import { ... } from "./store.js";` list, add `railSelection,` after `projects,`:

```js
  projects,
  railSelection,
  resources,
```

After that import statement, add:

```js
import { setLastSelected } from "./prefs.js";
import { selectToApply } from "./select.js";
```

After `let consecutivePollFails = 0;`, add:

```js
// The `at` of the last select request this tab applied (select.js).
let lastSelectAt = null;
```

In `applyState`, after `editorSignal.value = data.editor || "";` (inside the `try`), add:

```js
    // Another app asked for a pane (todo-hub's Show): select it as ⌘K does after an open.
    const pid = selectToApply(data.select, lastSelectAt, Date.now());
    if (pid) {
      lastSelectAt = data.select.at;
      railSelection.value = `pane:${pid}`;
      setLastSelected({ kind: "pane", pid });
    }
```

It sits after the rename and drag guards, so a request that lands mid-drag applies on the next state, while it is still fresh.

- [ ] **Step 6: Run the frontend tests and build**

Run: `npm test && npm run build && bin/check`
Expected: vitest passes; vite writes `static/dist/app.js`; `bin/check` reports zero violations.

- [ ] **Step 7: Commit the source, then the build**

```bash
git add static/src/select.js static/src/poll.js static/src/__tests__/select.test.js
git commit -m "poll: a select request from another app selects its pane once, while fresh

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git add static/dist
git commit -m "build: static/dist

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Docs

**Files:**
- Modify: `README.md` ("Opening work", after the ⌘K paragraph)
- Modify: `CLAUDE.md` (the reference table's `open_ops.py` row)
- Modify: `docs/superpowers/specs/2026-11-15-linear-start-design.md`

- [ ] **Step 1: README.** After the paragraph that ends "…(Claude, Codex or your own commands), then the account, model and effort.", add:

```markdown
Another app on this Mac can start work too. `POST /api/linear/start` takes a
repo, a branch, a Linear issue and a prompt: periscope makes the worktree,
starts Claude, sends it the prompt and links the issue to the card. If a
Claude is already on that branch, it reports that pane and starts nothing.
`POST /api/select` with a pane's `pid` makes the dashboard select it.
TodoHub's **Start in periscope** uses both.
```

- [ ] **Step 2: CLAUDE.md.** In the "Reference docs" table, change the first cell of the `docs/unified-open.md` row from

```
| `open_ops.py`, `routes/open.py`, `OpenOmnibox`, `LauncherModal`, `worktree_spawn.py` |
```

to

```
| `open_ops.py`, `routes/open.py`, `routes/linear.py`, `OpenOmnibox`, `LauncherModal`, `worktree_spawn.py` |
```

- [ ] **Step 3: Spec.** Make the spec match the code:
  - In step 5 of the route, change "`None` becomes 409" to "`None` becomes 400", because it is bad input, and 409 means "already starting" to the caller.
  - In step 2, change "read from `state_hub._last_blob`" to "read with `state_hub.last_windows()`".
  - In "Tests", change "New `static/src/__tests__/poll.test.js`" to "New `static/src/__tests__/select.test.js`", and "`selectToApply` … in `poll.js`" to "in `static/src/select.js`".
  - In "Changes", change "`static/src/poll.js`: `selectToApply` and its use in `applyState`." to "`static/src/select.js`: `selectToApply`. `static/src/poll.js`: its use in `applyState`."

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md docs/superpowers/specs/2026-11-15-linear-start-design.md
git commit -m "docs: the Linear start and select routes in the README and CLAUDE.md; spec matches the code

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Full check, then a real start against a dev server

- [ ] **Step 1: Run everything CI runs**

Run: `bin/check && npm test && uv run pytest -q && git status --short`
Expected: zero lint violations; all tests pass; `git status` is clean (so `static/dist` is fresh).

- [ ] **Step 2: Real start.** Ask the user first: this makes a real tmux window and a worktree. Start a dev server (backend on port 17375; it never binds the MCP socket):

```bash
npm run dev
```

In another shell, with a repo that the catalog lists (`curl -s http://127.0.0.1:17375/api/open/catalog | python3 -m json.tool | head`), start a throwaway branch:

```bash
curl -s -X POST http://127.0.0.1:17375/api/linear/start -H 'Content-Type: application/json' \
  -d '{"repo":"<repo path from the catalog>","branch":"me/plan-check","ticket":"ENG-1","title":"Plan check","prompt":"Say hello, then stop."}'
```

Expected: `{"mode":"spawned","pid":"…","path":"…/me-plan-check"…}`, and a new tmux window where Claude receives "Say hello, then stop." Then:

```bash
curl -s -X POST http://127.0.0.1:17375/api/select -H 'Content-Type: application/json' -d '{"pid":"<pid from above>"}'
```

Expected: the dev UI at `http://127.0.0.1:5174/` selects that pane within about 1 s.

Notes: the spawned Claude's channel attaches to the PROD periscope (dev never binds the MCP socket), so a second start on the same branch from the dev server answers 409 for 60 s, not `focused`. The `focused` path is covered by the tests.

- [ ] **Step 3: Clean up the check**

```bash
tmux kill-window -t "<session:index from the spawn answer's target>"
git -C "<repo path>" worktree remove "<path from the spawn answer>"
git -C "<repo path>" branch -D me/plan-check
```

Stop `npm run dev` with ctrl+c.

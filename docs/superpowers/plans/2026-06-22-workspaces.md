# Workspaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a goal-scoped, persistent "workspace" — a top-level rail group that sits alongside repo groups, holding explicitly-tagged Claude tabs, surviving with nothing live, with a workspace-aware narrator.

**Architecture:** A new `workspaces` dict in `state.json` (entity), a `pane_workspaces` table in `periscope.db` (the per-tab tag, keyed on tmux `pane_id`, pruned with the existing dead-pane reaper). `window_view` emits `workspace_id` per window; the rail merge gets a workspace pre-pass that diverts tagged windows into `ws:<id>` groups modeled on the dev-flat (`MAIN_KEY`) shape. Spawn-into pre-tags new tabs; the narrator factors the goal into names.

**Tech stack:** FastAPI + SQLite (`periscope.db`) backend; Preact + `@preact/signals` frontend built by Vite to `static/dist/app.js`; pytest (`uv run pytest -q`) + vitest (`npm test`).

**Spec:** `docs/superpowers/specs/2026-06-22-workspace-scoping-design.md`

**Execution context:** Run in a dedicated worktree on port 8766 (periscope dev convention):
```sh
git worktree add ../periscope-workspaces -b feature/workspaces
cd ../periscope-workspaces
PERISCOPE_PORT=8766 PERISCOPE_DEV=1 PERISCOPE_NO_RECLAIM=1 uv run server.py
```

**Structural note (deviation from spec module table):** the spec lists the tag map under `workspaces.py`. Physically the SQLite connection + schema live in `activity.py` (which owns `periscope.db`). To keep db code cohesive, the `pane_workspaces` table + its `set/get/map/prune` accessors live in **`activity.py`** alongside `pane_sessions`/`pane_status`; `workspaces.py` owns the **entity** (CRUD + `resolve`). This is the same split `pane_sessions` already uses (db in `activity.py`, consumed elsewhere).

**v1 scope trims (noted, deferred):**
- In-rail tagging is via a **row action** ("move to workspace"), not multi-select drag — the rail has no multi-select today. Drag-to-tag is a follow-on.
- Persistence is the "named shell" (no roster) per the spec.

---

## Task ordering

- **Tasks 1–8** — backend entity, tag map, window emit, routes, payload (foundation).
- **Tasks 9–10** — spawn-into-workspace.
- **Tasks 11–16** — frontend rail (merge, render, chip-move, tagging UI), build.
- **Task 17** — workspace-aware narrator (**sequenced last**, isolated commits).
- **Task 18** — real-tmux integration + browser verification.

---

## Task 1: `workspaces` state key + fixture

**Files:**
- Modify: `periscope/store.py` (`_STATE_DEFAULTS`, `_load_state` setdefault)
- Modify: `tests/conftest.py` (`clean_state` fresh dict)

- [ ] **Step 1: Add `workspaces` to the state defaults**

In `periscope/store.py`, `_STATE_DEFAULTS` (currently lines 99–106):

```python
_STATE_DEFAULTS: dict = {
    "version": 2,
    "ui": {},
    "windows": {},
    "commands": [],
    "projects": {},
    "workspaces": {},
    "settings": {},
}
```

- [ ] **Step 2: Ensure load tolerates older state without the key**

In `_load_state()`, after the existing per-key `setdefault` block (mirror whatever pattern sets `projects`), add:

```python
data.setdefault("workspaces", {})
```

If `_load_state` has no explicit setdefault list (it merges against `_STATE_DEFAULTS`), confirm the merge covers new keys; otherwise add the line above. No version bump — an absent dict defaults to empty, which is correct (no workspaces yet).

- [ ] **Step 3: Add `workspaces` to the `clean_state` fixture**

In `tests/conftest.py`, the `clean_state` `fresh` dict (lines ~63–70) must include the key so tests see it:

```python
    fresh = {
        "version": 2,
        "ui": {},
        "windows": {},
        "commands": [],
        "projects": {},
        "workspaces": {},
        "settings": {},
    }
```

- [ ] **Step 4: Run the suite to confirm nothing broke**

Run: `uv run pytest -q`
Expected: PASS (634 baseline; no new tests yet).

- [ ] **Step 5: Commit**

```bash
git add periscope/store.py tests/conftest.py
git commit -m "feat(workspaces): add workspaces state key + fixture"
```

---

## Task 2: Workspace entity CRUD

**Files:**
- Create: `periscope/workspaces.py`
- Create: `tests/test_workspaces.py`

The entity mirrors `projects.py` (functions + a `TypedDict`, module-qualified `_STATE` access through `store`). Workspace ids are `ws_<slug>` minted from the name.

- [ ] **Step 1: Write the failing tests**

`tests/test_workspaces.py`:

```python
import periscope.store as store
from periscope.workspaces import (
    create_workspace, get_workspace, all_workspaces,
    update_workspace, archive_workspace, resolve_workspace_for_window,
)


def test_create_and_get(clean_state):
    ws = create_workspace(name="Auth refactor", base_repo="/dev/fdy")
    assert ws["id"].startswith("ws_")
    assert ws["name"] == "Auth refactor"
    assert ws["base_repo"] == "/dev/fdy"
    assert ws["archived_at"] is None
    assert get_workspace(ws["id"])["name"] == "Auth refactor"


def test_id_is_slugged_and_unique(clean_state):
    a = create_workspace(name="Auth refactor")
    b = create_workspace(name="Auth refactor")
    assert a["id"] != b["id"]
    assert a["id"].startswith("ws_auth-refactor")


def test_all_excludes_nothing_returns_snapshot(clean_state):
    create_workspace(name="One")
    create_workspace(name="Two")
    assert len(all_workspaces()) == 2


def test_update(clean_state):
    ws = create_workspace(name="X")
    assert update_workspace(ws["id"], name="Y", base_worktree="/dev/fdy-x") is True
    assert get_workspace(ws["id"])["name"] == "Y"
    assert get_workspace(ws["id"])["base_worktree"] == "/dev/fdy-x"
    assert update_workspace("ws_nope", name="Z") is False


def test_archive(clean_state):
    ws = create_workspace(name="X")
    assert archive_workspace(ws["id"]) is True
    assert get_workspace(ws["id"])["archived_at"] is not None
    assert archive_workspace("ws_nope") is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_workspaces.py -q`
Expected: FAIL with `ModuleNotFoundError: periscope.workspaces`.

- [ ] **Step 3: Implement `periscope/workspaces.py`**

```python
"""Workspaces: goal-scoped, persistent top-level rail groups.

A workspace is a named entity in state['workspaces'] (parallel to projects).
Membership is NOT stored here — it is a per-tab tag in the `pane_workspaces`
table (periscope.db, owned by activity.py), keyed on tmux pane_id. This module
owns only the entity (CRUD) and the per-window resolve.
"""
from __future__ import annotations

import re
import time
from typing import Optional, TypedDict

from periscope import store
from periscope import activity


class Workspace(TypedDict, total=False):
    id: str
    name: str
    base_repo: Optional[str]
    base_worktree: Optional[str]
    created_at: int
    archived_at: Optional[int]


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "workspace"


def create_workspace(*, name: str, base_repo: Optional[str] = None,
                     base_worktree: Optional[str] = None) -> Workspace:
    base = f"ws_{_slug(name)}"
    with store._STATE_LOCK:
        existing = store._STATE["workspaces"]
        wid = base
        n = 2
        while wid in existing:
            wid = f"{base}-{n}"
            n += 1
        row: Workspace = {
            "id": wid,
            "name": name,
            "base_repo": base_repo,
            "base_worktree": base_worktree,
            "created_at": int(time.time()),
            "archived_at": None,
        }
        existing[wid] = row
        store._write_state(store._STATE)
        return dict(row)


def get_workspace(wid: str) -> Workspace:
    return dict(store._STATE["workspaces"].get(wid, {}))


def all_workspaces() -> dict[str, Workspace]:
    return {k: dict(v) for k, v in store._STATE["workspaces"].items()}


def update_workspace(wid: str, **fields) -> bool:
    with store._STATE_LOCK:
        row = store._STATE["workspaces"].get(wid)
        if row is None:
            return False
        for k, v in fields.items():
            if k in ("name", "base_repo", "base_worktree"):
                row[k] = v
        store._write_state(store._STATE)
        return True


def archive_workspace(wid: str) -> bool:
    with store._STATE_LOCK:
        row = store._STATE["workspaces"].get(wid)
        if row is None:
            return False
        row["archived_at"] = int(time.time())
        store._write_state(store._STATE)
        return True


def resolve_workspace_for_window(w: dict) -> Optional[str]:
    """The workspace id a window is tagged into, or None.

    Looks up the per-tab tag by tmux pane_id, then validates the workspace
    still exists and is not archived (a stale tag for a deleted/archived
    workspace folds back to normal repo sorting)."""
    pane_id = w.get("pane_id")
    if not pane_id:
        return None
    wid = activity.get_pane_workspace(pane_id)
    if not wid:
        return None
    row = store._STATE["workspaces"].get(wid)
    if row is None or row.get("archived_at"):
        return None
    return wid
```

Note: `activity.get_pane_workspace` lands in Task 3. Import is fine — `activity` is imported lazily-safe at module level (no circular: `activity` does not import `workspaces`).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_workspaces.py -q`
Expected: FAIL on `resolve_workspace_for_window` tests only if any — but the written tests don't call `resolve` yet, so all 5 PASS once `activity.get_pane_workspace` exists. If import of `activity` fails for lack of the symbol, add a placeholder in Task 3 first. To keep this task green standalone, the 5 tests above avoid `resolve`; they PASS.

Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add periscope/workspaces.py tests/test_workspaces.py
git commit -m "feat(workspaces): entity CRUD"
```

---

## Task 3: Per-tab tag map (`pane_workspaces` table)

**Files:**
- Modify: `periscope/activity.py` (`_SCHEMA`, new accessors)
- Modify: `tests/test_activity.py` (or create if absent — check first)

The tag keys on `pane_id` exactly like `pane_sessions`/`pane_status`, so it reuses the dead-pane prune verbatim.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_activity.py` (use the `fresh_activity_db` fixture; check the file's existing import style):

```python
def test_pane_workspace_set_get(fresh_activity_db):
    activity = fresh_activity_db
    assert activity.get_pane_workspace("%1") is None
    activity.set_pane_workspace("%1", "ws_auth")
    assert activity.get_pane_workspace("%1") == "ws_auth"


def test_pane_workspace_retag_overwrites(fresh_activity_db):
    activity = fresh_activity_db
    activity.set_pane_workspace("%1", "ws_a")
    activity.set_pane_workspace("%1", "ws_b")
    assert activity.get_pane_workspace("%1") == "ws_b"


def test_pane_workspace_untag_clears(fresh_activity_db):
    activity = fresh_activity_db
    activity.set_pane_workspace("%1", "ws_a")
    activity.set_pane_workspace("%1", None)
    assert activity.get_pane_workspace("%1") is None


def test_pane_workspace_map(fresh_activity_db):
    activity = fresh_activity_db
    activity.set_pane_workspace("%1", "ws_a")
    activity.set_pane_workspace("%2", "ws_a")
    activity.set_pane_workspace("%3", "ws_b")
    assert activity.pane_workspace_map() == {"%1": "ws_a", "%2": "ws_a", "%3": "ws_b"}


def test_prune_pane_workspaces(fresh_activity_db):
    activity = fresh_activity_db
    activity.set_pane_workspace("%1", "ws_a")
    activity.set_pane_workspace("%2", "ws_a")
    dropped = activity.prune_pane_workspaces({"%1"})
    assert dropped == 1
    assert activity.get_pane_workspace("%2") is None
    assert activity.get_pane_workspace("%1") == "ws_a"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_activity.py -k pane_workspace -q`
Expected: FAIL with `AttributeError: module 'periscope.activity' has no attribute 'set_pane_workspace'`.

- [ ] **Step 3: Add the table to `_SCHEMA`**

In `periscope/activity.py`, inside the `_SCHEMA` string (next to the `pane_sessions` CREATE TABLE), add:

```sql
CREATE TABLE IF NOT EXISTS pane_workspaces (
  pane_id      TEXT PRIMARY KEY,   -- tmux pane id, e.g. '%56'
  workspace_id TEXT NOT NULL,      -- workspaces[].id (state.json)
  updated_at   INTEGER NOT NULL
);
```

- [ ] **Step 4: Add the accessors**

In `periscope/activity.py`, near `get_pane_session` / `prune_pane_sessions` (use the same `_conn()` + `_LOCK` pattern those use):

```python
def set_pane_workspace(pane_id: str, workspace_id: str | None) -> None:
    """Tag a tab into a workspace, or clear the tag when workspace_id is None."""
    conn = _conn()
    with _LOCK:
        if workspace_id is None:
            conn.execute("DELETE FROM pane_workspaces WHERE pane_id = ?", (pane_id,))
        else:
            conn.execute(
                "INSERT INTO pane_workspaces (pane_id, workspace_id, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(pane_id) DO UPDATE SET "
                "workspace_id = excluded.workspace_id, updated_at = excluded.updated_at",
                (pane_id, workspace_id, int(time.time())),
            )
        conn.commit()


def get_pane_workspace(pane_id: str) -> str | None:
    conn = _conn()
    with _LOCK:
        cur = conn.execute(
            "SELECT workspace_id FROM pane_workspaces WHERE pane_id = ?", (pane_id,)
        )
        row = cur.fetchone()
    return row[0] if row else None


def pane_workspace_map() -> dict[str, str]:
    """All live tags as {pane_id: workspace_id} — one bulk read for the worker
    tick and window_view fan-out."""
    conn = _conn()
    with _LOCK:
        cur = conn.execute("SELECT pane_id, workspace_id FROM pane_workspaces")
        return {pid: wid for pid, wid in cur.fetchall()}


def prune_pane_workspaces(alive_pane_ids: set[str]) -> int:
    """Drop tags for tmux pane ids that no longer exist (tab died)."""
    conn = _conn()
    with _LOCK:
        cur = conn.execute("SELECT pane_id FROM pane_workspaces")
        dead = [pid for (pid,) in cur.fetchall() if pid not in alive_pane_ids]
        for pid in dead:
            conn.execute("DELETE FROM pane_workspaces WHERE pane_id = ?", (pid,))
        conn.commit()
    return len(dead)
```

(Match the exact `_conn`/`_LOCK`/`time` usage of the neighbouring functions — if `prune_pane_sessions` uses a single `DELETE ... WHERE pane_id NOT IN (...)`, mirror that instead. The behaviour, not the SQL shape, is what the tests pin.)

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_activity.py -k pane_workspace -q`
Expected: PASS (5 passed).

Also re-run Task 2 now that `get_pane_workspace` exists:
Run: `uv run pytest tests/test_workspaces.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add periscope/activity.py tests/test_activity.py
git commit -m "feat(workspaces): pane_workspaces tag table + accessors"
```

---

## Task 4: Prune tags with dead panes (lifespan)

**Files:**
- Modify: `periscope/app.py` (`_pane_sessions_housekeeping`)

- [ ] **Step 1: Add the prune call**

In `periscope/app.py`, `_pane_sessions_housekeeping()` (lines ~48–60), after the `prune_pane_status` block:

```python
    dropped_ws = activity.prune_pane_workspaces(alive)
    if dropped_ws:
        log.info("pruned %d dead pane_workspaces row(s)", dropped_ws)
```

`alive` is already `{w["pane_id"] for w in list_windows() if w.get("pane_id")}` — reuse it.

- [ ] **Step 2: Verify import + boot**

Run: `uv run python -c "import periscope.app"`
Expected: no error.

- [ ] **Step 3: Commit**

```bash
git add periscope/app.py
git commit -m "feat(workspaces): prune dead tab tags in lifespan housekeeping"
```

---

## Task 5: Archived-workspace GC

**Files:**
- Modify: `periscope/pids.py` (add `_gc_workspaces`, call it where `_gc_projects` is called)
- Modify: `tests/test_pids.py` (or `tests/test_workspaces.py` — wherever `_gc_projects` is tested)

- [ ] **Step 1: Write the failing test**

Locate the `_gc_projects` test (grep `grep -rn "_gc_projects" tests/`). Mirror it. In `tests/test_workspaces.py`:

```python
import time
from periscope.pids import _gc_workspaces
from periscope.workspaces import create_workspace, archive_workspace, all_workspaces


def test_gc_drops_old_archived(clean_state):
    ws = create_workspace(name="Old")
    archive_workspace(ws["id"])
    # Backdate the archive stamp 31 days.
    clean_state["workspaces"][ws["id"]]["archived_at"] = int(time.time()) - 31 * 86400
    _gc_workspaces()
    assert ws["id"] not in all_workspaces()


def test_gc_keeps_recent_archived_and_live(clean_state):
    live = create_workspace(name="Live")
    recent = create_workspace(name="Recent")
    archive_workspace(recent["id"])
    _gc_workspaces()
    assert live["id"] in all_workspaces()
    assert recent["id"] in all_workspaces()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_workspaces.py -k gc -q`
Expected: FAIL (`ImportError: cannot import name '_gc_workspaces'`).

- [ ] **Step 3: Implement `_gc_workspaces`**

In `periscope/pids.py`, mirror `_gc_projects` (lines ~199–220). Read it first for the exact `_STATE`/lock/threshold idiom; then:

```python
def _gc_workspaces() -> None:
    """Drop workspaces archived more than 30 days ago (mirrors _gc_projects)."""
    cutoff = int(time.time()) - 30 * 86400
    with store._STATE_LOCK:
        wss = store._STATE.get("workspaces", {})
        dead = [k for k, v in wss.items()
                if v.get("archived_at") and v["archived_at"] < cutoff]
        for k in dead:
            del wss[k]
        if dead:
            store._write_state(store._STATE)
```

Call it adjacent to the existing `_gc_projects()` call site (same scheduled pass).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_workspaces.py -k gc -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add periscope/pids.py tests/test_workspaces.py
git commit -m "feat(workspaces): 30-day GC of archived workspaces"
```

---

## Task 6: Emit `workspace_id` per window

**Files:**
- Modify: `periscope/window_view.py` (`build_window_view`)
- Modify: `tests/test_window_view.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_window_view.py` (use the file's existing window-builder helper; the key assertion is the new field):

```python
def test_window_view_emits_workspace_id(clean_state, fresh_activity_db):
    from periscope import activity
    from periscope.workspaces import create_workspace
    ws = create_workspace(name="WS")
    activity.set_pane_workspace("%9", ws["id"])
    w = {"session": "s", "index": 1, "name": "claude", "cwd": "/dev/x",
         "pane_id": "%9", "pid": "@1", "pid_raw": "@1", "active": True, "activity": 0}
    view, _ = build_window_view(w, int(time.time()))
    assert view["workspace_id"] == ws["id"]


def test_window_view_workspace_id_none_when_untagged(clean_state, fresh_activity_db):
    w = {"session": "s", "index": 1, "name": "claude", "cwd": "/dev/x",
         "pane_id": "%8", "pid": "@2", "pid_raw": "@2", "active": True, "activity": 0}
    view, _ = build_window_view(w, int(time.time()))
    assert view.get("workspace_id") is None
```

(Match the existing test module's imports for `build_window_view` and `time`.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_window_view.py -k workspace -q`
Expected: FAIL (`KeyError`/`assert None == ws_...`).

- [ ] **Step 3: Implement the emit**

In `periscope/window_view.py`, near the `project_key = resolve_project_for_window(w)` block (lines ~172–179), add:

```python
    from periscope.workspaces import resolve_workspace_for_window
    workspace_id = resolve_workspace_for_window(w)
```

Then in the `view = {...}` dict (lines ~181–203), add the key:

```python
        "workspace_id": workspace_id,
```

(Import at function scope avoids any import-order surprise with `workspaces` → `activity`.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_window_view.py -k workspace -q`
Expected: PASS (2 passed). Then full module: `uv run pytest tests/test_window_view.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add periscope/window_view.py tests/test_window_view.py
git commit -m "feat(workspaces): emit workspace_id per window"
```

---

## Task 7: Workspaces REST routes

**Files:**
- Create: `periscope/routes/workspaces.py`
- Modify: `periscope/app.py` (register the router)
- Create: `tests/routes/test_workspaces.py`

Endpoints: create (with optional initial pane tags = "promote"), patch, archive, tag, untag.

- [ ] **Step 1: Write the failing route tests**

`tests/routes/test_workspaces.py` (mirror `tests/routes/test_projects.py`'s TestClient + `clean_state` autouse pattern — read it first for the exact fixture wiring):

```python
import pytest
from fastapi.testclient import TestClient
from periscope.app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _state(clean_state, fresh_activity_db):
    yield


def test_create_workspace():
    r = client.post("/api/workspaces", json={"name": "Auth", "base_repo": "/dev/fdy"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["id"].startswith("ws_auth")
    assert body["name"] == "Auth"


def test_create_promote_tags_panes():
    r = client.post("/api/workspaces", json={"name": "Auth", "tag_panes": ["%1", "%2"]})
    wid = r.json()["id"]
    from periscope import activity
    assert activity.get_pane_workspace("%1") == wid
    assert activity.get_pane_workspace("%2") == wid


def test_tag_and_untag():
    wid = client.post("/api/workspaces", json={"name": "W"}).json()["id"]
    assert client.post("/api/workspaces/tag",
                       json={"workspace_id": wid, "pane_id": "%5"}).status_code == 200
    from periscope import activity
    assert activity.get_pane_workspace("%5") == wid
    assert client.post("/api/workspaces/untag", json={"pane_id": "%5"}).status_code == 200
    assert activity.get_pane_workspace("%5") is None


def test_tag_unknown_workspace_404():
    r = client.post("/api/workspaces/tag", json={"workspace_id": "ws_nope", "pane_id": "%1"})
    assert r.status_code == 404


def test_patch_and_archive():
    wid = client.post("/api/workspaces", json={"name": "W"}).json()["id"]
    assert client.post("/api/workspaces/patch",
                       json={"workspace_id": wid, "name": "W2"}).status_code == 200
    assert client.post("/api/workspaces/archive",
                       json={"workspace_id": wid}).status_code == 200
    r = client.post("/api/workspaces/archive", json={"workspace_id": "ws_nope"})
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/routes/test_workspaces.py -q`
Expected: FAIL (404s — router not registered).

- [ ] **Step 3: Implement `periscope/routes/workspaces.py`**

```python
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope import activity
from periscope.workspaces import (
    create_workspace, get_workspace, update_workspace, archive_workspace,
)

router = APIRouter()


class CreateBody(BaseModel):
    name: str
    base_repo: str | None = None
    base_worktree: str | None = None
    tag_panes: list[str] | None = None  # promote: tag these pane_ids on create


@router.post("/api/workspaces")
def workspaces_create(body: CreateBody):
    ws = create_workspace(
        name=body.name, base_repo=body.base_repo, base_worktree=body.base_worktree,
    )
    for pane_id in body.tag_panes or []:
        activity.set_pane_workspace(pane_id, ws["id"])
    return {"ok": True, **ws}


class PatchBody(BaseModel):
    workspace_id: str
    name: str | None = None
    base_repo: str | None = None
    base_worktree: str | None = None


@router.post("/api/workspaces/patch")
def workspaces_patch(body: PatchBody):
    fields = {k: v for k, v in body.model_dump(exclude={"workspace_id"}).items()
              if k in body.model_fields_set}
    if not update_workspace(body.workspace_id, **fields):
        raise HTTPException(404, f"no workspace {body.workspace_id!r}")
    return {"ok": True, **get_workspace(body.workspace_id)}


class ArchiveBody(BaseModel):
    workspace_id: str


@router.post("/api/workspaces/archive")
def workspaces_archive(body: ArchiveBody):
    if not archive_workspace(body.workspace_id):
        raise HTTPException(404, f"no workspace {body.workspace_id!r}")
    return {"ok": True, **get_workspace(body.workspace_id)}


class TagBody(BaseModel):
    workspace_id: str
    pane_id: str


@router.post("/api/workspaces/tag")
def workspaces_tag(body: TagBody):
    if not get_workspace(body.workspace_id):
        raise HTTPException(404, f"no workspace {body.workspace_id!r}")
    activity.set_pane_workspace(body.pane_id, body.workspace_id)
    return {"ok": True}


class UntagBody(BaseModel):
    pane_id: str


@router.post("/api/workspaces/untag")
def workspaces_untag(body: UntagBody):
    activity.set_pane_workspace(body.pane_id, None)
    return {"ok": True}
```

- [ ] **Step 4: Register the router**

In `periscope/app.py`, find the `include_router` loop / list and add `workspaces` to it (mirror how `projects` route is registered — likely an import + `app.include_router(workspaces.router)` or an entry in a routers tuple).

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/routes/test_workspaces.py -q`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add periscope/routes/workspaces.py periscope/app.py tests/routes/test_workspaces.py
git commit -m "feat(workspaces): REST routes (create/promote/patch/archive/tag/untag)"
```

---

## Task 8: `workspaces` payload in `/api/state`

**Files:**
- Modify: `periscope/routes/state.py`
- Modify: `tests/routes/test_state.py`

- [ ] **Step 1: Write the failing test**

In `tests/routes/test_state.py` (mirror the existing `projects` payload assertion):

```python
def test_state_includes_workspaces(clean_state, fresh_activity_db):
    from periscope.workspaces import create_workspace
    ws = create_workspace(name="WS", base_repo="/dev/fdy")
    from periscope.routes.state import state  # or call via TestClient
    payload = state()
    ids = [w["id"] for w in payload["workspaces"]]
    assert ws["id"] in ids
```

(If the suite calls `/api/state` via TestClient elsewhere, follow that style instead of importing `state` directly.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/routes/test_state.py -k workspaces -q`
Expected: FAIL (`KeyError: 'workspaces'`).

- [ ] **Step 3: Implement the payload**

In `periscope/routes/state.py`, near the `projects_view` block (lines ~113–118):

```python
    from periscope.workspaces import all_workspaces
    workspaces_view = [
        v for v in all_workspaces().values() if not v.get("archived_at")
    ]
```

Add to the returned dict:

```python
        "workspaces": workspaces_view,
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/routes/test_state.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/state.py tests/routes/test_state.py
git commit -m "feat(workspaces): workspaces payload in /api/state"
```

---

## Task 9: Spawn-into-workspace (backend)

**Files:**
- Modify: `periscope/routes/workspaces.py` (spawn endpoint)
- Modify: `tests/test_workspaces_spawn.py` (create; real-tmux, `@needs_tmux`)

Spawn a new worktree off the workspace's `base_worktree` (resolved to a branch), open it, tag the new claude pane.

- [ ] **Step 1: Write the failing integration test**

`tests/test_workspaces_spawn.py` (mirror `tests/test_worktree_spawn.py`'s `@needs_tmux` + `PERISCOPE_TMUX_SOCKET`/`PERISCOPE_CLAUDE_EXEC` harness — read it for the exact fixtures):

```python
import pytest
from tests.conftest import needs_tmux  # or however the marker is exposed


@needs_tmux
def test_spawn_into_workspace_tags_pane(clean_state, fresh_activity_db,
                                        tmux_test_server, tmp_git_repo):
    from periscope.workspaces import create_workspace
    from periscope import activity
    ws = create_workspace(name="WS", base_repo=str(tmp_git_repo))
    from periscope.routes.workspaces import workspaces_spawn, SpawnBody
    result = workspaces_spawn(SpawnBody(workspace_id=ws["id"], branch="ws-feature"))
    pane_id = result["pane_id"]
    assert activity.get_pane_workspace(pane_id) == ws["id"]
```

(Names `tmux_test_server`, `tmp_git_repo`, and the marker must match the existing harness in `tests/test_worktree_spawn.py`. Read it and copy the decorator/fixtures exactly.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_workspaces_spawn.py -q`
Expected: FAIL (`ImportError: workspaces_spawn`).

- [ ] **Step 3: Implement the spawn endpoint**

Add to `periscope/routes/workspaces.py`:

```python
import subprocess
from periscope import open_ops, worktree_spawn
from periscope.workspaces import get_workspace as _get_ws


class SpawnBody(BaseModel):
    workspace_id: str
    branch: str


@router.post("/api/workspaces/spawn")
def workspaces_spawn(body: SpawnBody):
    ws = _get_ws(body.workspace_id)
    if not ws:
        raise HTTPException(404, f"no workspace {body.workspace_id!r}")
    if not ws.get("base_repo"):
        raise HTTPException(400, "workspace has no base_repo to spawn from")
    base_branch = None
    base_wt = ws.get("base_worktree")
    if base_wt:
        # base_worktree is a PATH; spawn_worktree wants a branch NAME.
        out = subprocess.run(
            ["git", "-C", base_wt, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        )
        base_branch = out.stdout.strip() or None
    spawn = worktree_spawn.spawn_worktree(
        ws["base_repo"], body.branch, base_branch=base_branch, fetch=False,
    )
    result = open_ops.open_target(open_ops.PathTarget(path=spawn["path"]))
    activity.set_pane_workspace_for_pid(result.claude_pid, body.workspace_id)
    return {"ok": True, "workspace_id": body.workspace_id,
            "pane_id": result.claude_pid, "path": spawn["path"], "ui": result.ui}
```

**Wrinkle to resolve in this task:** `open_target`/`OpenResult.claude_pid` is the `@periscope_id` (`pid_raw`), but the tag keys on **`pane_id`** (`%N`). You need the tmux `pane_id` of the spawned claude window. Two options — pick one and implement it:
- (a) Have `open_ops` return the claude window's `pane_id` alongside `claude_pid` (extend `OpenResult`), then `activity.set_pane_workspace(pane_id, ws_id)` directly. **Preferred.**
- (b) Resolve `pid_raw → pane_id` via `list_windows()` (find the window whose `pid_raw == result.claude_pid`, read its `pane_id`). Add `set_pane_workspace_for_pid(pid_raw, ws_id)` as a thin helper doing that lookup.

The test asserts on `get_pane_workspace(pane_id)`, so whichever path, the tag must end up keyed on the real `%N`. Implement (a): add `claude_pane_id` to `OpenResult` (populated from the same `list_windows()` scan `_open_path` already does — it has the window dict), and tag with that. Update the endpoint to `activity.set_pane_workspace(result.claude_pane_id, body.workspace_id)`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_workspaces_spawn.py -q`
Expected: PASS (1 passed) — or SKIP if tmux unavailable on the runner (acceptable; the `@needs_tmux` marker gates it).

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/workspaces.py periscope/open_ops.py tests/test_workspaces_spawn.py
git commit -m "feat(workspaces): spawn-into-workspace (worktree off base + pre-tag)"
```

---

## Task 10: `workspaces` signal (frontend store)

**Files:**
- Modify: `static/src/store.js`
- Modify: `static/src/poll.js`

- [ ] **Step 1: Declare the signal**

In `static/src/store.js`, next to `export const projects = signal([]);`:

```javascript
export const workspaces = signal([]);  // /api/state workspaces (poll-fed)
```

- [ ] **Step 2: Feed it from the poll**

In `static/src/poll.js`, next to `projects.value = data.projects || [];`:

```javascript
workspaces.value = data.workspaces || [];
```

Add `workspaces` to the import from `./store` at the top of `poll.js`.

- [ ] **Step 3: Commit**

```bash
git add static/src/store.js static/src/poll.js
git commit -m "feat(workspaces): workspaces signal fed from /api/state"
```

---

## Task 11: Rail merge — workspace grouping (`railTree.js`)

**Files:**
- Modify: `static/src/split/railTree.js`
- Modify: `static/src/split/__tests__/railTree.test.js`

Workspaces become top-level `ws:<id>` groups modeled on the dev-flat (`MAIN_KEY`) shape: `worktreesByRepo["ws:id"] = []`, `panesByWorktree["ws:id"]` = flat tagged pid list, kept-and-interleaved in `repoOrder` (NOT bottom-pinned), and always rendered (even parked).

- [ ] **Step 1: Write the failing vitest cases**

In `static/src/split/__tests__/railTree.test.js`, add a `wsproj`/`ws` factory and cases (mirror the existing `win`/`proj` style):

```javascript
const ws = (over = {}) => ({ id: "ws_a", name: "Auth", base_repo: "/dev/myproj", ...over });

describe("mergeLiveAndPrefs — workspaces", () => {
  const projects = [proj(), MAIN_PROJ];

  it("a tagged window groups under ws:<id> and leaves the repo group", () => {
    const wins = [
      win({ pid: "a", session: "myproj", workspace_id: "ws_a" }),
      win({ pid: "b", session: "myproj" }),
    ];
    const m = mergeLiveAndPrefs(wins, projects, [ws()], [], {}, {});
    expect(m.repoOrder).toContain("ws:ws_a");
    expect(m.repoOrder).toContain("/dev/myproj");
    expect(m.worktreesByRepo["ws:ws_a"]).toEqual([]);
    expect(m.panesByWorktree["ws:ws_a"]).toEqual(["a"]);
    // 'a' is NOT under the repo group (exactly one top-level group)
    expect(m.panesByWorktree["myproj"]).toEqual(["b", "review"]);
  });

  it("a workspace with no live tagged tabs still renders (parked)", () => {
    const m = mergeLiveAndPrefs([], projects, [ws()], [], {}, {});
    expect(m.repoOrder).toContain("ws:ws_a");
    expect(m.panesByWorktree["ws:ws_a"]).toEqual([]);
  });

  it("ws: keys are interleaved per pref, not bottom-pinned like dev", () => {
    const wins = [
      win({ pid: "a", session: "myproj", workspace_id: "ws_a" }),
      win({ pid: "b", session: "myproj" }),
    ];
    const m = mergeLiveAndPrefs(wins, projects, [ws()], ["ws:ws_a", "/dev/myproj"], {}, {});
    expect(m.repoOrder.indexOf("ws:ws_a")).toBeLessThan(m.repoOrder.indexOf("/dev/myproj"));
  });

  it("a stale tag for an unknown workspace folds back to repo sorting", () => {
    const wins = [win({ pid: "a", session: "myproj", workspace_id: "ws_gone" })];
    const m = mergeLiveAndPrefs(wins, projects, [], [], {}, {});
    expect(m.repoOrder).toEqual(["/dev/myproj"]);
    expect(m.panesByWorktree["myproj"]).toEqual(["a", "review"]);
  });
});
```

**Note the new signature:** `mergeLiveAndPrefs(windows, projects, workspaces, prefRepoOrder, prefWtByRepo, prefPanesByWt)` — `workspaces` is inserted as the 3rd arg. The existing tests must be updated to pass `[]` as the new 3rd arg (do that in Step 4).

- [ ] **Step 2: Run to verify failure**

Run: `npm test -- railTree`
Expected: FAIL (new cases + arity mismatch).

- [ ] **Step 3: Implement the merge changes**

In `static/src/split/railTree.js`:

(a) Add a workspace index helper after `indexProjects`:

```javascript
// { id: workspaceRow } from the /api/state workspaces payload.
export function indexWorkspaces(workspaces) {
  const out = {};
  for (const w of (workspaces || [])) out[w.id] = w;
  return out;
}
```

(b) Extend `groupKeyForWindow` to check the tag first:

```javascript
export function groupKeyForWindow(w, projectsByPin, workspacesById) {
  const wid = w.workspace_id;
  if (wid && workspacesById && workspacesById[wid]) return `ws:${wid}`;
  const pin = w.project_pinned_dir;
  if (!pin || pin === MAIN_KEY) return MAIN_KEY;
  const row = projectsByPin[pin];
  if (!row) return MAIN_KEY;
  return row.repo || pin;
}
```

(c) Rewrite `mergeLiveAndPrefs` with the new arg + workspace pre-pass. Full replacement:

```javascript
export function mergeLiveAndPrefs(windows, projects, workspaces, prefRepoOrder, prefWtByRepo, prefPanesByWt) {
  const projectsByPin = indexProjects(projects);
  const workspacesById = indexWorkspaces(workspaces);
  const allWsKeys = (workspaces || []).map(w => `ws:${w.id}`);
  const liveByRepo = {};       // group key → ordered session list (first-seen)
  const livePanesByWt = {};    // session → ordered pane pids (first-seen)
  const liveDevPids = [];      // flat dev membership (cross-session)
  const livePanesByWs = {};    // ws:<id> → flat tagged pid order (cross-session)
  for (const w of (windows || [])) {
    const g = groupKeyForWindow(w, projectsByPin, workspacesById);
    if (g.startsWith("ws:")) {
      if (!livePanesByWs[g]) livePanesByWs[g] = [];
      if (!livePanesByWs[g].includes(w.pid)) livePanesByWs[g].push(w.pid);
      continue;
    }
    if (g === MAIN_KEY) {
      if (!liveDevPids.includes(w.pid)) liveDevPids.push(w.pid);
      continue;
    }
    const s = w.session;
    if (!liveByRepo[g]) liveByRepo[g] = [];
    if (!liveByRepo[g].includes(s)) liveByRepo[g].push(s);
    if (!livePanesByWt[s]) livePanesByWt[s] = [];
    if (!livePanesByWt[s].includes(w.pid)) livePanesByWt[s].push(w.pid);
  }

  // Top-level order: pref-first (kept iff a live repo OR a current ws key),
  // then live-new repos, then ws keys not yet in pref (parked / new). ws keys
  // are interleaved (NOT bottom-pinned). Dev (MAIN_KEY) is always last.
  const liveRepoSet = new Set(Object.keys(liveByRepo));
  const wsKeySet = new Set(allWsKeys);
  const keep = (k) => k !== MAIN_KEY && (liveRepoSet.has(k) || wsKeySet.has(k));
  const fromPref = prefRepoOrder.filter(keep);
  const fromPrefSet = new Set(fromPref);
  const newRepos = Object.keys(liveByRepo).filter(r => !fromPrefSet.has(r) && r !== MAIN_KEY);
  const newWs = allWsKeys.filter(k => !fromPrefSet.has(k));
  const realTop = [...fromPref, ...newRepos, ...newWs];
  const repoOrder = liveDevPids.length ? [...realTop, MAIN_KEY] : realTop;

  // Worktree (session) lists: ws:/dev are flat → [].
  const worktreesByRepo = {};
  for (const r of repoOrder) {
    if (r === MAIN_KEY || r.startsWith("ws:")) { worktreesByRepo[r] = []; continue; }
    const live = liveByRepo[r] || [];
    const liveSet = new Set(live);
    const pref = (prefWtByRepo[r] || []).filter(w => liveSet.has(w));
    const prefSet = new Set(pref);
    worktreesByRepo[r] = [...pref, ...live.filter(w => !prefSet.has(w))];
  }

  // Pane children per repo session (unchanged); ws:/dev handled flat below.
  const panesByWorktree = {};
  for (const r of repoOrder) {
    if (r === MAIN_KEY || r.startsWith("ws:")) continue;
    const own = projectsByPin[r];
    const hasReview = !(own && !own.repo);
    for (const w of worktreesByRepo[r]) {
      const live = livePanesByWt[w] || [];
      const liveSet = new Set(live);
      const pref = prefPanesByWt[w] || [];
      const prefKept = pref.filter(c => (c === "review" && hasReview) || liveSet.has(c));
      const prefSet = new Set(prefKept);
      const merged = [...prefKept, ...live.filter(p => !prefSet.has(p))];
      if (hasReview && !merged.includes("review")) merged.push("review");
      panesByWorktree[w] = merged;
    }
  }
  // Dev flat (unchanged).
  if (liveDevPids.length) {
    const liveSet = new Set(liveDevPids);
    const pref = (prefPanesByWt[MAIN_KEY] || []).filter(p => liveSet.has(p));
    const prefSet = new Set(pref);
    panesByWorktree[MAIN_KEY] = [...pref, ...liveDevPids.filter(p => !prefSet.has(p))];
  }
  // Workspace flat: ALWAYS build (parked → []), so every non-archived
  // workspace renders. pref-first pid order, then new live tagged pids.
  for (const k of allWsKeys) {
    const live = livePanesByWs[k] || [];
    const liveSet = new Set(live);
    const pref = (prefPanesByWt[k] || []).filter(p => liveSet.has(p));
    const prefSet = new Set(pref);
    panesByWorktree[k] = [...pref, ...live.filter(p => !prefSet.has(p))];
  }

  return { repoOrder, worktreesByRepo, panesByWorktree };
}
```

- [ ] **Step 4: Update existing call sites + tests for the new arg**

Every existing `mergeLiveAndPrefs(...)` test call gains `[]` as the 3rd arg. Update all in `railTree.test.js` (the pre-existing describe blocks). The two app call sites are updated in Task 12.

- [ ] **Step 5: Run to verify pass**

Run: `npm test -- railTree`
Expected: PASS (existing + new cases).

- [ ] **Step 6: Commit**

```bash
git add static/src/split/railTree.js static/src/split/__tests__/railTree.test.js
git commit -m "feat(workspaces): rail merge groups tagged tabs into ws:<id> (dev-flat shape)"
```

---

## Task 12: Rail render — workspace groups + drag + syncRailPrefs (`Rail.jsx`)

**Files:**
- Modify: `static/src/split/Rail.jsx`

This is UI — verify in the browser (Task 18), no unit test. Mirror the existing **dev branch** (lines ~369–402) since ws groups share its flat shape.

- [ ] **Step 1: Pass `workspaces` into both merge calls**

Import `workspaces` from `../store` and build the index. Update `currentMergedOrder()` (lines ~109–113):

```javascript
function currentMergedOrder() {
  return mergeLiveAndPrefs(
    windows.value, projects.value, workspaces.value,
    prefs.getRepoOrder(), prefs.getWorktreesByRepo(), prefs.getPanesByWorktree()
  );
}
```

And the `syncRailPrefs` merge call (line ~63) gains `workspaces.value` as the 3rd arg likewise. And the render-path `mergeLiveAndPrefs(...)` call (wherever the component computes the tree for rendering).

- [ ] **Step 2: `syncRailPrefs` — keep ws: keys, persist their flat panes**

Update the MAIN_KEY-stripping block (lines ~73–84). ws: keys are KEPT in `repo_order` and their flat panes persisted:

```javascript
  const nextRepoOrder = merged.repoOrder.filter((r) => r !== MAIN_KEY); // ws: kept
  const nextWtByRepo = { ...merged.worktreesByRepo };
  delete nextWtByRepo[MAIN_KEY];
  for (const k of Object.keys(nextWtByRepo)) {
    if (k.startsWith("ws:")) delete nextWtByRepo[k]; // ws: have empty wt lists
  }
  const nextPanesByWt = {};
  for (const r of nextRepoOrder) {
    if (r.startsWith("ws:")) {
      nextPanesByWt[r] = merged.panesByWorktree[r] || [];
      continue;
    }
    for (const wt of (nextWtByRepo[r] || [])) {
      nextPanesByWt[wt] = merged.panesByWorktree[wt] || [];
    }
  }
  if (merged.panesByWorktree[MAIN_KEY]) {
    nextPanesByWt[MAIN_KEY] = merged.panesByWorktree[MAIN_KEY];
  }
```

- [ ] **Step 3: Render the workspace branch**

In the rail render loop over `repoOrder`, add a branch for `repoKey.startsWith("ws:")` BEFORE the dev branch. Mirror the dev branch but: draggable header (ws are reorderable), repo chip on the header, "spawn into workspace" newtab row, and a parked hint when no panes. Build `workspacesById` once:

```javascript
const workspacesById = indexWorkspaces(workspaces.value);
```

Branch (place alongside the `isDev` branch):

```javascript
{repoKey.startsWith("ws:") && (() => {
  const wid = repoKey.slice(3);
  const wsRow = workspacesById[wid] || {};
  const pids = merged.panesByWorktree[repoKey] || [];
  const wsWindows = pids.map(pid => windowsByPid[pid]).filter(Boolean);
  const rows = wsWindows.map((w) => (
    <PaneRow
      key={`pane:${w.pid}`}
      w={w}
      chip={paneChip(w, { isDev: true })}  // show branch chip like dev
      selectedKey={selectedKey}
      dim={passesFilter(w, filter)}
      onSelect={selectKey}
      onClose={() => closePane(w)}
      onRename={(next) => renamePane(w, next)}
      onUntag={() => untagPane(w)}          // Task 14
      dragProps={makeDragProps({ kind: "pane", key: `pane:${w.pid}`, childKey: w.pid, worktreeKey: repoKey })}
      dropPos={dropPosFor(`pane:${w.pid}`)}
      pinned={prefs.getPinnedPids().includes(w.pid)}
      onTogglePin={() => prefs.togglePin(w.pid)}
    />
  ));
  if (rows.length === 0) {
    rows.push(
      <div key={`parked:${repoKey}`} class="rail-row child-row rail-dim">
        <span class="rail-label">parked · spawn from base</span>
      </div>
    );
  }
  rows.push(
    <NewTabRow
      key={`newtab:${repoKey}`}
      worktreeKey={repoKey}
      onOpen={() => spawnIntoWorkspace(wid)}  // Task 14
    />
  );
  return rows;
})()}
```

`windowsByPid` is the across-all-windows map the dev branch builds (`indexWindowsByWorktree` is per-session; for the flat ws/dev lists build `const windowsByPid = {}; for (const w of windows.value) windowsByPid[w.pid] = w;` once per render — the dev branch already needs this).

- [ ] **Step 4: Workspace header row (RepoRow with chip + draggable)**

Where the top-level row for `repoKey` is rendered, for a `ws:` key use a `RepoRow` variant that shows the workspace name + base_repo chip and stays draggable (ws are reorderable, unlike dev). Pass `label={wsRow.name}` and a new `chip` prop = basename(wsRow.base_repo). `isDev={false}` so it stays draggable. (RepoRow chip slot is added in Task 13.)

- [ ] **Step 5: `isValidDropTarget` — ws: panes use synthetic worktreeKey**

No code change needed: ws pane rows already pass `worktreeKey: repoKey` (= `ws:<id>`), so the existing pane rule `drag.worktreeKey === target.worktreeKey` keeps cross-tab reorder inside a workspace working — exactly as dev does with `MAIN_KEY`. Confirm the repo-drag rule does NOT special-case `ws:` (ws headers are reorderable, so leave them allowed).

- [ ] **Step 6: Browser smoke (deferred to Task 18)**

No commit gate here beyond "renders without console errors". Commit at the end of Task 14 once tagging works end-to-end.

---

## Task 13: Move the chip to line 2 (`RailRows.jsx`)

**Files:**
- Modify: `static/src/split/RailRows.jsx`

- [ ] **Step 1: Remove the inline chip from line 1**

In `PaneRow` (lines ~123–131), delete the inline chip from `.pane-row-main`:

```javascript
        <RailLabel label={label} kind="pane" renameable onCommit={onRename} />
        {/* chip removed from line 1 — moved to line 2 below */}
        {w.burn_hot && (
```

- [ ] **Step 2: Render line 2 on `chip || status_line`**

Replace the status-only line-2 block (lines ~149–154) with one that renders when there's a chip OR a status:

```javascript
      {(chip || w.status_line) && (
        <div class={`rail-meta${statusStale ? " stale" : ""}`}>
          {chip && <span class="rail-chip" title={w.cwd}>⧉ {chip}</span>}
          {w.status_line && (
            <span class="rail-status" title={w.status_line}>
              {w.status_rail || w.status_line}
            </span>
          )}
        </div>
      )}
```

- [ ] **Step 3: CSS — line 2 layout, chip truncates first**

In the rail stylesheet (find `.rail-chip` / `.rail-status` rules; likely `static/src/**/*.css` or `static/styles.css`), ensure `.rail-meta` is a flex row where `.rail-chip` is `flex: 0 1 auto; min-width: 0; text-overflow: ellipsis; overflow: hidden; white-space: nowrap` and `.rail-status` is `flex: 1 1 auto` (the verb wins the space). Add a small gap. The chip shrinks/truncates before the status.

- [ ] **Step 4: Add a `chip` slot to `RepoRow`**

For the workspace header (Task 12 Step 4), `RepoRow` needs an optional chip. Add a `chip` prop and render it after the label:

```javascript
export function RepoRow({ repoKey, label, chip, collapsed, rolledUp, dim, isDev, onToggle, dragProps, dropPos }) {
  // ...
      <span class="rail-label"><b>{label}</b></span>
      {chip && <span class="rail-chip rail-chip-repo" title={chip}>⟨{chip}⟩</span>}
      <span class={statusDotClass(rolledUp)}></span>
  // ...
}
```

- [ ] **Step 5: Browser-verify the chip move**

`npm run dev`, open `http://localhost:5174/`. Confirm: a regular pane row shows the name at full width on line 1 and the chip on line 2 next to the status; a shell tab with a chip but no status still shows its chip; in a narrow rail the chip truncates before the status.

- [ ] **Step 6: Commit**

```bash
git add static/src/split/RailRows.jsx static/src/**/*.css
git commit -m "feat(rail): move pane chip to line 2 (renders on chip||status); RepoRow chip slot"
```

---

## Task 14: Tagging UI — promote, move-to-workspace, spawn, untag (`Rail.jsx` + omnibox)

**Files:**
- Modify: `static/src/split/Rail.jsx` (row actions + handlers)
- Modify: `static/src/split/RailRows.jsx` (PaneRow `onUntag` + a "move to workspace" affordance)
- Modify: `static/src/overlays/OpenOmnibox.jsx`, `static/src/open/classify.js` (new-workspace + spawn-into cards)

- [ ] **Step 1: Handlers in `Rail.jsx`**

```javascript
import { apiCall } from "../util.js";

async function tagPane(w, workspaceId) {
  await apiCall("tag", "/api/workspaces/tag", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace_id: workspaceId, pane_id: w.pane_id }),
  });
}

async function untagPane(w) {
  await apiCall("untag", "/api/workspaces/untag", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pane_id: w.pane_id }),
  });
}

async function promotePane(w, name) {
  const data = await apiCall("promote", "/api/workspaces", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, base_repo: w.repo_key || null, tag_panes: [w.pane_id] }),
  });
  return data;
}

async function spawnIntoWorkspace(wid) {
  const branch = window.prompt("New worktree branch for this workspace:");
  if (!branch) return;
  const data = await apiCall("spawn", "/api/workspaces/spawn", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace_id: wid, branch }),
  });
  if (data && data.ui) setUI(data.ui);
}
```

(`w.pane_id` is on every window view — the tmux `%N` the tag keys on.)

- [ ] **Step 2: PaneRow "move to workspace" menu**

Add a small action to `PaneRow` (an existing kebab/hover-action slot if there is one; otherwise a minimal `<select>` shown on hover). The menu lists non-archived `workspaces.value` + "New workspace…". Selecting an existing workspace → `tagPane(w, id)`; "New workspace…" → `window.prompt("Workspace name")` then `promotePane(w, name)`. When `w.workspace_id` is set, the action becomes "Remove from workspace" → `untagPane(w)`.

Keep it minimal — a `<select>` is acceptable for v1:

```javascript
{!w.workspace_id ? (
  <select class="rail-ws-pick" onClick={(e)=>e.stopPropagation()}
          onChange={(e) => onMoveToWorkspace(e.target.value)}>
    <option value="">＋ws</option>
    {workspaceOptions.map(o => <option value={o.id}>{o.name}</option>)}
    <option value="__new__">New workspace…</option>
  </select>
) : (
  <button class="rail-ws-clear" title="remove from workspace"
          onClick={(e)=>{e.stopPropagation(); onUntag();}}>⧉×</button>
)}
```

Thread `workspaceOptions` (from `workspaces.value`) and `onMoveToWorkspace`/`onUntag` props from `Rail.jsx` into `PaneRow`.

- [ ] **Step 3: Omnibox "new workspace" card**

In `static/src/open/classify.js`, add a card kind after the repos loop:

```javascript
  for (const r of catalog.repos || []) {
    if (match(r.label, q) || match(r.repo, q)) {
      cards.push({ kind: "workspace", label: `${r.label} · new workspace…`,
                   sub: "goal-scoped group", repo: r.repo, descriptor: null });
    }
  }
```

In `OpenOmnibox.jsx`, add to `KIND_META`: `workspace: { group: "Workspaces", icon: "▧" }`, and in `pick()`:

```javascript
  if (card.kind === "workspace" && card.repo) {
    const name = window.prompt("Workspace name");
    if (!name) return;
    return post_workspace({ name, base_repo: card.repo });
  }
```

with a small helper that POSTs `/api/workspaces` and closes (mirrors `post`).

- [ ] **Step 4: Browser-verify the full tagging loop**

`npm run dev`. Verify: promote a tab → it jumps into a new `ws:` group and leaves its repo group; tag a second tab via the menu → it joins; untag → it returns to repo sorting; "new workspace" from the omnibox creates a parked group; spawn-into creates a worktree tab already inside the workspace.

- [ ] **Step 5: Commit**

```bash
git add static/src/split/Rail.jsx static/src/split/RailRows.jsx static/src/overlays/OpenOmnibox.jsx static/src/open/classify.js
git commit -m "feat(workspaces): tagging UI — promote, move-to-workspace, untag, spawn-into, omnibox"
```

---

## Task 15: Build the bundle

**Files:**
- Modify: `static/dist/app.js` (committed build artifact)

- [ ] **Step 1: Build**

Run: `npm run build`
Expected: writes `static/dist/app.js` with no errors.

- [ ] **Step 2: Commit the artifact**

```bash
git add static/dist/app.js
git commit -m "build(workspaces): rebuild app.js bundle"
```

---

## Task 16: Full suite + lint gate

- [ ] **Step 1: Backend suite**

Run: `uv run pytest -q`
Expected: PASS. If only the two `test_channel_shim.py` reconnect tests fail, run `uv sync` (`.venv` drift — see CLAUDE.md) and re-run.

- [ ] **Step 2: Frontend tests**

Run: `npm test`
Expected: PASS.

- [ ] **Step 3: Commit (if any test fixups were needed)**

```bash
git add -A && git commit -m "test(workspaces): suite green"
```

---

## Task 17: Workspace-aware narrator (sequenced last, isolated commits)

**Files:**
- Modify: `periscope/activity.py` (`_worker_tick` builds a workspace context, passes to `narrator.tick`)
- Modify: `periscope/narrator.py` (`tick`, `build_narrator_prompt`)
- Modify: `periscope/rename_ai.py` (`RENAME_RULES` clause)
- Modify: `tests/test_narrator.py`

The tick already holds each window's `pane_id`. Resolve `pane_id → workspace_id` via `activity.pane_workspace_map()` (one bulk read), build a per-workspace sibling-name index from the tick's panes, and feed both into the prompt.

- [ ] **Step 1: Write the failing test**

In `tests/test_narrator.py` (mirror the module's existing `build_narrator_prompt` test style):

```python
def test_prompt_includes_workspace_and_siblings():
    from periscope.narrator import build_narrator_prompt
    p = build_narrator_prompt(
        window_name="token-store", branch="auth-core", pr=None, cwd="/dev/fdy",
        signals={}, workspace_name="Auth refactor",
        sibling_names=["rename-flow", "token-store"],
    )
    assert "Auth refactor" in p
    assert "rename-flow" in p
    # the don't-repeat-the-goal rule is present
    assert "don't repeat" in p.lower() or "do not repeat" in p.lower()


def test_prompt_without_workspace_unchanged():
    from periscope.narrator import build_narrator_prompt
    p = build_narrator_prompt(
        window_name="x", branch=None, pr=None, cwd="/dev/x", signals={},
        workspace_name=None, sibling_names=[],
    )
    assert "Auth refactor" not in p
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_narrator.py -k workspace -q`
Expected: FAIL (`TypeError: unexpected keyword argument 'workspace_name'`).

- [ ] **Step 3: Add the prompt params + clause**

In `periscope/narrator.py`, `build_narrator_prompt` signature gains `workspace_name: str | None = None, sibling_names: list[str] | None = None`. After the rename rules splice, add (only when a workspace is supplied):

```python
    if workspace_name:
        sibs = ", ".join(n for n in (sibling_names or []) if n) or "(none yet)"
        lines += [
            "",
            f"This pane is part of the workspace GOAL: \"{workspace_name}\".",
            f"Sibling tabs in this workspace: {sibs}.",
            "  - The goal is shared context — do NOT repeat it in the name.",
            "  - Name what distinguishes THIS tab from its siblings.",
        ]
```

In `periscope/rename_ai.py`, add to `RENAME_RULES`:

```python
    "- If a workspace goal is provided, don't repeat the goal; pick the name",
    "  that sets this tab apart from its siblings.",
```

- [ ] **Step 4: Wire the tick to resolve workspace + siblings**

In `periscope/narrator.py` `tick(panes)`: before the per-pane loop, build:

```python
    tag_map = activity.pane_workspace_map()
    from periscope.workspaces import all_workspaces
    ws_names = {k: v["name"] for k, v in all_workspaces().items()}
    # pane_id → workspace_id, and workspace_id → [window names]
    siblings: dict[str, list[str]] = {}
    for w, _parsed in panes:
        wid = tag_map.get(w.get("pane_id") or "")
        if wid:
            siblings.setdefault(wid, []).append(w.get("name") or "")
```

Then in `_generate` (or wherever `build_narrator_prompt` is called), pass:

```python
        wid = tag_map.get(pane_id)
        workspace_name = ws_names.get(wid) if wid else None
        sibling_names = siblings.get(wid) if wid else None
```

Thread `workspace_name` / `sibling_names` from `tick` into `_generate` (add params) and into the `build_narrator_prompt(...)` call. Keep all existing `should_regenerate` / cooldown / session-id-first logic untouched.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_narrator.py -q`
Expected: PASS (existing + 2 new).

- [ ] **Step 6: Commit**

```bash
git add periscope/narrator.py periscope/rename_ai.py periscope/activity.py tests/test_narrator.py
git commit -m "feat(workspaces): workspace-aware narrator (goal + siblings into the prompt)"
```

---

## Task 18: Integration verification (real tmux + browser)

- [ ] **Step 1: Backend integration on the dev port**

```sh
PERISCOPE_PORT=8766 PERISCOPE_DEV=1 PERISCOPE_NO_RECLAIM=1 uv run server.py
```

Then exercise the API against live tmux:
```sh
# create a workspace
curl -s localhost:8766/api/workspaces -d '{"name":"Scratch WS","base_repo":"'$HOME'/dev/periscope"}' -H 'content-type: application/json'
# confirm it shows in state
curl -s localhost:8766/api/state | python3 -c 'import sys,json; print([w["name"] for w in json.load(sys.stdin)["workspaces"]])'
```
Expected: the workspace appears in the `workspaces` payload.

- [ ] **Step 2: Browser checklist** (`npm run dev`, `http://localhost:5174/`)

Verify each, watching the browser console for errors:
- [ ] A new workspace (omnibox) renders as a parked top-level group with a base-repo chip.
- [ ] Promote a live Claude tab → it moves into a `ws:` group and disappears from its repo group (exactly one group).
- [ ] Tag a second tab via the row menu → joins; reorder the two tabs within the workspace (drag) → order persists across a poll.
- [ ] Untag → the tab returns to its repo group.
- [ ] Reorder the workspace header among repo groups → position persists (interleaved, not bottom-pinned).
- [ ] Spawn-into-workspace → a new worktree tab appears already inside the workspace.
- [ ] Chip is on line 2 for all rows; name has full width on line 1.
- [ ] (After Task 17, prod-only narrator) a workspace tab's auto-name doesn't repeat the goal — verify by reading `pane_status` or watching a rename land.

- [ ] **Step 3: Final full suite**

Run: `uv run pytest -q && npm test`
Expected: PASS.

- [ ] **Step 4: Merge to main + restart prod**

```sh
cd ~/dev/periscope
git merge feature/workspaces
uv run pytest -q          # re-run on the actual merge target (.venv may differ)
bin/periscope restart
git worktree remove ../periscope-workspaces
```

---

## Self-review notes

- **Spec coverage:** entity (T2), tag map keyed on `pane_id` + prune reuse (T3/T4), GC (T5), `workspace_id` emit (T6), routes incl. promote/tag/untag (T7), payload (T8), spawn-into with `base_worktree`→`base_branch` + `fetch=False` (T9), signal (T10), dev-flat merge + single-group invariant + interleaved-not-pinned (T11), render + syncRailPrefs ws handling + drag (T12), chip-to-line-2 on `chip||status_line` for all rows (T13), tagging UI (T14), build (T15), workspace-aware narrator sequenced last (T17). All spec sections map to a task.
- **Open wrinkle (T9):** `claude_pid` (`@periscope_id`) vs tag key (`pane_id`) — resolved by returning `claude_pane_id` from `open_ops` and tagging on that. The plan's spawn test asserts on `get_pane_workspace(pane_id)`, so this must be implemented as written (option (a)).
- **Type consistency:** merge signature `mergeLiveAndPrefs(windows, projects, workspaces, prefRepoOrder, prefWtByRepo, prefPanesByWt)` is used identically in T11 (def), T12 (both call sites), and all T11 tests. `groupKeyForWindow(w, projectsByPin, workspacesById)` consistent across def + merge. Tag accessors `set_pane_workspace`/`get_pane_workspace`/`pane_workspace_map`/`prune_pane_workspaces` consistent across T3/T4/T6/T7/T17.
- **Line-number caveat:** all `lines ~NNN` references are from a 2026-06-22 read; verify the anchor text before editing, since concurrent commits to `main` may shift them.

# Metadata-anchored rail (semantic fix) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop "close worktree" from killing a tab dragged into a workspace, by demoting the tmux session from semantic identity to runtime container — group membership becomes per-pane metadata.

**Architecture:** A new `pane_projects` SQLite tag (`pane_id → project pinned_dir`) makes a pane's project context explicit instead of session-derived. `resolve_project_for_window` reads the tag first, falling back to the existing session-match. The two `kill-session` callers (rail close, cleanup) are rewired to kill only the panes whose rail *placement* is the group being closed (`kill-pane` of the project's non-workspace-overridden panes), with per-target focus-dict cleanup. A synchronous startup backfill seeds tags for existing managed panes so the rail is byte-identical at cutover.

**Tech Stack:** Python 3 / FastAPI, SQLite (`periscope.db` via `periscope/activity.py`), pytest (`uv run pytest`), Preact frontend (Vite → `static/dist/app.js`).

**Scope:** Semantic fix only. The physical session collapse + the rail `session→project` rekey are DEFERRED to a follow-on (see spec). Do not implement them here.

**References:**
- Spec: `docs/superpowers/specs/2026-06-24-metadata-anchored-rail-design.md`
- Structure: `docs/superpowers/specs/2026-06-24-metadata-anchored-rail-structure.md`

**Two plan-time decisions (justified):**
- `projects.py` imports `activity` **function-level** inside the new functions (cycle-sensitivity; matches the `narrator` precedent).
- `placement_kill_set` reuses `resolve_project_for_window` (tag-first + session fallback), NOT a tags-only predicate — `window_new_worktree` creates untagged managed panes and backfill only tags managed panes, so a tags-only rule would miss them. Reusing the resolver makes close exactly mirror the rail grouping.

**Run tests from the worktree:** `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest ...`

---

## File Structure

| File | Responsibility / change |
|---|---|
| `periscope/activity.py` | New `pane_projects` table + `set/get/pane_project_map/prune` accessors (sibling of `pane_workspaces`). |
| `periscope/projects.py` | `resolve_project_for_window` tag-first; new `placement_kill_set` (pure-ish rule) + `backfill_pane_projects`. |
| `periscope/panes.py` | New `drop_target_focus(target)` shared per-target focus cleanup. |
| `periscope/routes/sessions.py` | `session_delete` placement-aware; `window_move`/`window_delete` adopt `drop_target_focus`. |
| `periscope/routes/cleanup.py` | `cleanup_archive` teardown placement-aware. |
| `periscope/open_ops.py` | Tag created panes in `_open_path`. |
| `periscope/channels.py` | Tag anchored spawned pane in `_do_spawn_claude_tool`. |
| `periscope/app.py` | Synchronous backfill before `yield`; `prune_pane_projects` in housekeeping. |
| `static/src/split/Rail.jsx` | `closeWorktree` confirm-copy reword; rebuild `static/dist/app.js`. |
| tests | `test_activity.py`, `test_projects.py`, `routes/test_sessions.py`, `routes/test_cleanup.py`. |

---

## Task 1: `pane_projects` table + accessors (`activity.py`)

**Files:**
- Modify: `periscope/activity.py` (schema block after line 52; accessors after line 275)
- Test: `tests/test_activity.py` (after line 524)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_activity.py`:

```python
def test_pane_project_set_get(fresh_activity_db):
    activity = fresh_activity_db
    assert activity.get_pane_project("%1") is None
    activity.set_pane_project("%1", "/repo/wt")
    assert activity.get_pane_project("%1") == "/repo/wt"


def test_pane_project_retag_overwrites(fresh_activity_db):
    activity = fresh_activity_db
    activity.set_pane_project("%1", "/a")
    activity.set_pane_project("%1", "/b")
    assert activity.get_pane_project("%1") == "/b"


def test_pane_project_untag_clears(fresh_activity_db):
    activity = fresh_activity_db
    activity.set_pane_project("%1", "/a")
    activity.set_pane_project("%1", None)
    assert activity.get_pane_project("%1") is None


def test_pane_project_map(fresh_activity_db):
    activity = fresh_activity_db
    activity.set_pane_project("%1", "/a")
    activity.set_pane_project("%2", "/a")
    activity.set_pane_project("%3", "/b")
    assert activity.pane_project_map() == {"%1": "/a", "%2": "/a", "%3": "/b"}


def test_prune_pane_projects(fresh_activity_db):
    activity = fresh_activity_db
    activity.set_pane_project("%1", "/a")
    activity.set_pane_project("%2", "/a")
    dropped = activity.prune_pane_projects({"%1"})
    assert dropped == 1
    assert activity.get_pane_project("%2") is None
    assert activity.get_pane_project("%1") == "/a"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest tests/test_activity.py -k pane_project -q`
Expected: FAIL with `AttributeError: module 'periscope.activity' has no attribute 'set_pane_project'`.

- [ ] **Step 3: Add the table to `_SCHEMA`**

In `periscope/activity.py`, immediately after the `pane_workspaces` table block (ends line 52, `);`), add:

```sql
CREATE TABLE IF NOT EXISTS pane_projects (
  pane_id    TEXT PRIMARY KEY,   -- tmux pane id, e.g. '%56'
  project    TEXT NOT NULL,      -- project pinned_dir (projects[] key)
  updated_at INTEGER NOT NULL
);
```

- [ ] **Step 4: Add the accessors**

In `periscope/activity.py`, after the `prune_pane_workspaces` function (line 275), add:

```python
# --- pane_projects: tmux pane id -> project pinned_dir tag --------------
#
# The per-tab project-context tag. Demotes session-derived project grouping
# to explicit per-pane metadata. Sibling of pane_workspaces — keyed on tmux
# pane_id, reuses the dead-pane prune verbatim. A row means "this pane
# belongs to managed project X"; unmanaged/dev panes stay untagged.

def set_pane_project(pane_id: str, project: str | None) -> None:
    """Tag a pane's project context, or clear it when project is None."""
    with _LOCK:
        c = _conn()
        if project is None:
            c.execute("DELETE FROM pane_projects WHERE pane_id=?", (pane_id,))
        else:
            c.execute(
                "INSERT INTO pane_projects (pane_id, project, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(pane_id) DO UPDATE SET "
                "project=excluded.project, updated_at=excluded.updated_at",
                (pane_id, project, int(time.time())),
            )
        c.commit()


def get_pane_project(pane_id: str) -> str | None:
    """The project pinned_dir this pane is tagged with, or None."""
    if not pane_id:
        return None
    with _LOCK:
        c = _conn()
        row = c.execute(
            "SELECT project FROM pane_projects WHERE pane_id=?",
            (pane_id,),
        ).fetchone()
    return row[0] if row else None


def pane_project_map() -> dict[str, str]:
    """All project tags as {pane_id: project} — one bulk read."""
    with _LOCK:
        c = _conn()
        return {pid: proj for pid, proj
                in c.execute("SELECT pane_id, project FROM pane_projects")}


def prune_pane_projects(alive_pane_ids: set[str]) -> int:
    """Drop tags for tmux pane ids that no longer exist. Returns rows deleted."""
    with _LOCK:
        c = _conn()
        existing = {r[0] for r in c.execute("SELECT pane_id FROM pane_projects")}
        dead = existing - alive_pane_ids
        if not dead:
            return 0
        c.executemany("DELETE FROM pane_projects WHERE pane_id=?",
                      [(p,) for p in dead])
        c.commit()
        return len(dead)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest tests/test_activity.py -k pane_project -q`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git -C /Users/tom/dev/periscope-metadata-rail add periscope/activity.py tests/test_activity.py
git -C /Users/tom/dev/periscope-metadata-rail commit -m "feat(activity): pane_projects per-pane project-context tag (sibling of pane_workspaces)"
```

---

## Task 2: `resolve_project_for_window` tag-first (`projects.py`)

**Files:**
- Modify: `periscope/projects.py:153-169`
- Test: `tests/test_projects.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_projects.py` (uses `clean_state` for `_STATE` + `fresh_activity_db` for tags):

```python
def test_resolve_tag_wins_over_session_match(clean_state, fresh_activity_db):
    from periscope import projects
    clean_state["projects"]["/repo/a"] = {"tmux_session": "sess_a", "repo": "/repo"}
    clean_state["projects"]["/repo/b"] = {"tmux_session": "sess_b", "repo": "/repo"}
    fresh_activity_db.set_pane_project("%9", "/repo/b")
    # window lives in sess_a, but its tag says /repo/b — tag wins.
    assert projects.resolve_project_for_window(
        {"session": "sess_a", "pane_id": "%9"}) == "/repo/b"


def test_resolve_untagged_falls_back_to_session(clean_state, fresh_activity_db):
    from periscope import projects
    clean_state["projects"]["/repo/a"] = {"tmux_session": "sess_a", "repo": "/repo"}
    assert projects.resolve_project_for_window(
        {"session": "sess_a", "pane_id": "%1"}) == "/repo/a"


def test_resolve_external_session_is_main(clean_state, fresh_activity_db):
    from periscope import projects
    assert projects.resolve_project_for_window(
        {"session": "random", "pane_id": "%1"}) == projects.MAIN_KEY


def test_resolve_empty_session_is_none(clean_state, fresh_activity_db):
    from periscope import projects
    assert projects.resolve_project_for_window({"session": "", "pane_id": "%1"}) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest tests/test_projects.py -k resolve -q`
Expected: FAIL — `test_resolve_tag_wins_over_session_match` fails (returns `/repo/a`, the session match).

- [ ] **Step 3: Add the tag-first branch**

In `periscope/projects.py`, replace the body of `resolve_project_for_window` (lines 162-169, from `session = window.get(...)` to `return MAIN_KEY`) with:

```python
    pane_id = window.get("pane_id")
    if pane_id:
        from periscope import activity   # function-level: cycle-sensitivity (narrator precedent)
        tagged = activity.get_pane_project(pane_id)
        if tagged:
            return tagged
    session = window.get("session", "")
    if not session:
        return None
    with _store._STATE_LOCK:
        for key, row in _store._STATE.get("projects", {}).items():
            if row.get("tmux_session") == session:
                return key
    return MAIN_KEY
```

(Keep the existing docstring above; it still describes the fallback behavior. Add one line to it: "Reads the `pane_projects` tag first; falls back to session-match for untagged panes.")

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest tests/test_projects.py -k resolve -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git -C /Users/tom/dev/periscope-metadata-rail add periscope/projects.py tests/test_projects.py
git -C /Users/tom/dev/periscope-metadata-rail commit -m "feat(projects): resolve_project_for_window reads pane_projects tag first"
```

---

## Task 3: `placement_kill_set` pure rule (`projects.py`)

**Files:**
- Modify: `periscope/projects.py` (add after `resolve_project_for_window`, ~line 169)
- Test: `tests/test_projects.py`

The rule: a window is in project_key's worktree placement iff it has no workspace override AND `resolve_project_for_window(w) == project_key`. Returns `[(target, pane_id)]`. Refuses `MAIN_KEY`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_projects.py`:

```python
def test_placement_kill_set_excludes_ws_override(clean_state, fresh_activity_db):
    from periscope import projects
    clean_state["projects"]["/repo/a"] = {"tmux_session": "sess_a", "repo": "/repo"}
    windows = [
        {"session": "sess_a", "index": 0, "pane_id": "%claude"},
        {"session": "sess_a", "index": 1, "pane_id": "%shell"},
    ]
    ws_map = {"%claude": "ws_goal"}   # claude dragged into a workspace
    kill = projects.placement_kill_set("/repo/a", windows, ws_map)
    assert kill == [("sess_a:1", "%shell")]   # claude spared


def test_placement_kill_set_includes_untagged_managed_pane(clean_state, fresh_activity_db):
    from periscope import projects
    clean_state["projects"]["/repo/a"] = {"tmux_session": "sess_a", "repo": "/repo"}
    # untagged pane in the project's session resolves via session-match.
    windows = [{"session": "sess_a", "index": 2, "pane_id": "%new"}]
    kill = projects.placement_kill_set("/repo/a", windows, {})
    assert kill == [("sess_a:2", "%new")]


def test_placement_kill_set_refuses_main(clean_state, fresh_activity_db):
    from periscope import projects
    import pytest
    with pytest.raises(ValueError):
        projects.placement_kill_set(projects.MAIN_KEY, [], {})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest tests/test_projects.py -k placement -q`
Expected: FAIL with `AttributeError: ... has no attribute 'placement_kill_set'`.

- [ ] **Step 3: Implement**

In `periscope/projects.py`, after `resolve_project_for_window`, add:

```python
def placement_kill_set(project_key: str, windows: list[dict],
                       ws_map: dict[str, str]) -> list[tuple[str, str]]:
    """The panes whose rail placement is `project_key`'s worktree row, as
    `[(target, pane_id)]` for kill-pane. A pane is in the group iff it has no
    workspace override (`ws_map`) AND resolves to `project_key` — the SAME rule
    the rail uses to render the row, so close kills exactly what is shown.

    Reuses `resolve_project_for_window` (tag-first + session fallback) rather
    than a tags-only check: untagged-but-managed panes (e.g. a `window_new`
    tab) must still be killed. Refuses MAIN_KEY/dev — an unguarded dev group
    would mass-kill every unmanaged pane on the machine.
    """
    if project_key == MAIN_KEY:
        raise ValueError("refusing to kill the __main__/dev group")
    out: list[tuple[str, str]] = []
    for w in windows:
        pane_id = w.get("pane_id")
        if not pane_id or pane_id in ws_map:
            continue
        if resolve_project_for_window(w) == project_key:
            out.append((f"{w['session']}:{w['index']}", pane_id))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest tests/test_projects.py -k placement -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git -C /Users/tom/dev/periscope-metadata-rail add periscope/projects.py tests/test_projects.py
git -C /Users/tom/dev/periscope-metadata-rail commit -m "feat(projects): placement_kill_set — the rail-placement membership rule for close"
```

---

## Task 4: `backfill_pane_projects` (`projects.py`)

**Files:**
- Modify: `periscope/projects.py` (add after `placement_kill_set`)
- Test: `tests/test_projects.py`

Backfill tags every live managed pane (resolves to a real `pinned_dir`); unmanaged panes stay untagged. Idempotent (skips panes with an existing tag).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_projects.py`:

```python
def test_backfill_tags_managed_panes_only(clean_state, fresh_activity_db, monkeypatch):
    from periscope import projects
    clean_state["projects"]["/repo/a"] = {"tmux_session": "sess_a", "repo": "/repo"}
    monkeypatch.setattr(projects, "list_windows", lambda: [
        {"session": "sess_a", "index": 0, "pane_id": "%m"},   # managed
        {"session": "external", "index": 0, "pane_id": "%x"}, # unmanaged → dev
    ], raising=False)
    n = projects.backfill_pane_projects()
    assert n == 1
    assert fresh_activity_db.get_pane_project("%m") == "/repo/a"
    assert fresh_activity_db.get_pane_project("%x") is None   # unmanaged untagged


def test_backfill_is_idempotent(clean_state, fresh_activity_db, monkeypatch):
    from periscope import projects
    clean_state["projects"]["/repo/a"] = {"tmux_session": "sess_a", "repo": "/repo"}
    monkeypatch.setattr(projects, "list_windows", lambda: [
        {"session": "sess_a", "index": 0, "pane_id": "%m"},
    ], raising=False)
    assert projects.backfill_pane_projects() == 1
    assert projects.backfill_pane_projects() == 0   # already tagged
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest tests/test_projects.py -k backfill -q`
Expected: FAIL with `AttributeError: ... has no attribute 'backfill_pane_projects'`.

- [ ] **Step 3: Implement**

First ensure `list_windows` is importable in `projects.py`. At the top of `backfill_pane_projects`, import it function-level (avoids module-load import-order surprises, and lets the test monkeypatch `projects.list_windows`). Add after `placement_kill_set`:

```python
def backfill_pane_projects() -> int:
    """One-shot: tag every live managed pane with its project pinned_dir,
    seeding `pane_projects` from today's session-derived grouping so the rail
    is byte-identical at cutover. Unmanaged/dev panes (resolve to MAIN_KEY)
    stay untagged. Idempotent — skips panes that already have a tag. Returns
    the number of rows written. MUST run synchronously before serving (see
    app.py): the collapse follow-on deletes the session-match fallback.
    """
    from periscope import activity
    from periscope.panes import list_windows
    existing = activity.pane_project_map()
    written = 0
    for w in list_windows():
        pane_id = w.get("pane_id")
        if not pane_id or pane_id in existing:
            continue
        key = resolve_project_for_window(w)   # untagged → session-match
        if key and key != MAIN_KEY:
            activity.set_pane_project(pane_id, key)
            written += 1
    return written
```

Note: the test monkeypatches `projects.list_windows`; the function-level `from periscope.panes import list_windows` binds the name locally, so add a module-level `from periscope.panes import list_windows` is NOT used — instead the test patches the attribute the function reads. To make `monkeypatch.setattr(projects, "list_windows", ...)` effective, change the function to read a module-level alias. Implement it as: add near the top of `projects.py` imports `from periscope.panes import list_windows` ONLY IF no import cycle (panes does not import projects — verified). Then `backfill_pane_projects` uses the module-level `list_windows`, and the test's monkeypatch works.

Verification before choosing: run `grep -n "import projects\|from periscope import projects" periscope/panes.py` — expected: no output (no cycle). If output appears, keep the import function-level and change the tests to monkeypatch `periscope.panes.list_windows` instead.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest tests/test_projects.py -k backfill -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git -C /Users/tom/dev/periscope-metadata-rail add periscope/projects.py tests/test_projects.py
git -C /Users/tom/dev/periscope-metadata-rail commit -m "feat(projects): backfill_pane_projects — seed tags for managed panes at startup"
```

---

## Task 5: `drop_target_focus` shared cleanup (`panes.py`) + adopt in `sessions.py`

**Files:**
- Modify: `periscope/panes.py` (add after `note_action`, ~line 111)
- Modify: `periscope/routes/sessions.py:401-404` (window_move), `:414-415` (window_delete)
- Test: `tests/test_panes.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_panes.py`:

```python
def test_drop_target_focus_pops_both_dicts():
    from periscope import panes
    panes._focused_at["sess:1"] = 100
    panes._acted_at["sess:1"] = 100
    panes._focused_at["sess:2"] = 200
    panes.drop_target_focus("sess:1")
    assert "sess:1" not in panes._focused_at
    assert "sess:1" not in panes._acted_at
    assert panes._focused_at["sess:2"] == 200   # untouched
    panes._focused_at.pop("sess:2", None)        # cleanup
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest tests/test_panes.py -k drop_target_focus -q`
Expected: FAIL with `AttributeError: ... has no attribute 'drop_target_focus'`.

- [ ] **Step 3: Implement in `panes.py`**

After `note_action` (line 111), add:

```python
def drop_target_focus(target: str) -> None:
    """Drop a single window target's focus/acted bookkeeping. Shared by every
    per-target kill site (session_delete, cleanup_archive, window_move,
    window_delete) so the per-target cleanup rule lives in one place — a
    per-session prefix sweep would wipe a surviving workspace-tagged pane's
    state."""
    _focused_at.pop(target, None)
    _acted_at.pop(target, None)
```

- [ ] **Step 4: Adopt it in `window_move` and `window_delete`**

In `periscope/routes/sessions.py`, replace lines 401-404:

```python
    if src in _focused_at:
        _focused_at[new_target] = _focused_at.pop(src)
    if src in _acted_at:
        _acted_at[new_target] = _acted_at.pop(src)
```

with (note: move carries focus to the new target, so it pops src AFTER copying — keep the copy, then drop src via the helper):

```python
    if src in _focused_at:
        _focused_at[new_target] = _focused_at[src]
    if src in _acted_at:
        _acted_at[new_target] = _acted_at[src]
    panes.drop_target_focus(src)
```

And replace lines 414-415 in `window_delete`:

```python
    _focused_at.pop(target, None)
    _acted_at.pop(target, None)
```

with:

```python
    panes.drop_target_focus(target)
```

Ensure `from periscope import panes` is present in `sessions.py` imports (it imports `_focused_at` etc. from panes at line 23 — add `panes` to that area or `from periscope import panes`). Verify with `grep -n "import panes\|from periscope.panes import" periscope/routes/sessions.py` and add `from periscope import panes` if absent.

- [ ] **Step 5: Run tests to verify pass (helper + existing move/delete route tests)**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest tests/test_panes.py -k drop_target_focus tests/routes/test_sessions.py -q`
Expected: PASS (helper test + existing window_move/window_delete tests still green).

- [ ] **Step 6: Commit**

```bash
git -C /Users/tom/dev/periscope-metadata-rail add periscope/panes.py periscope/routes/sessions.py tests/test_panes.py
git -C /Users/tom/dev/periscope-metadata-rail commit -m "refactor(panes): drop_target_focus shared per-target focus cleanup; adopt in window_move/delete"
```

---

## Task 6: `session_delete` placement-aware (`routes/sessions.py`)

**Files:**
- Modify: `periscope/routes/sessions.py:60-71`
- Test: `tests/routes/test_sessions.py`

- [ ] **Step 1: Write the failing route test**

Append to `tests/routes/test_sessions.py` (follow the file's existing TestClient + `_tmux_mutate` mock pattern; adapt fixture names to those already used there):

```python
def test_session_delete_kills_placement_set_sparing_ws_pane(
        client, clean_state, fresh_activity_db, monkeypatch):
    from periscope import projects
    from periscope.routes import sessions as sroute
    clean_state["projects"]["/repo/a"] = {"tmux_session": "sess_a", "repo": "/repo"}
    monkeypatch.setattr(sroute, "list_windows", lambda: [
        {"session": "sess_a", "index": 0, "pane_id": "%claude"},
        {"session": "sess_a", "index": 1, "pane_id": "%shell"},
    ], raising=False)
    fresh_activity_db.set_pane_workspace("%claude", "ws_goal")   # dragged out
    calls = []
    monkeypatch.setattr(sroute, "_tmux_mutate", lambda *a: (calls.append(a), (True, ""))[1])

    r = client.delete("/api/session?session=sess_a")
    assert r.status_code == 200
    # only the shell pane killed; no kill-session
    assert ("kill-pane", "-t", "sess_a:1") in calls
    assert not any(a[0] == "kill-session" for a in calls)
    assert not any("sess_a:0" in a for a in calls)
```

(If the existing tests construct `client` differently, mirror that exact setup. The assertions on `calls` are the contract: `kill-pane` for the placement set, no `kill-session`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest tests/routes/test_sessions.py -k placement_set -q`
Expected: FAIL — current code issues `kill-session`.

- [ ] **Step 3: Rewrite `session_delete`**

In `periscope/routes/sessions.py`, replace the body (lines 62-71) of `session_delete` with:

```python
    from periscope import activity, projects, panes
    project_key = projects.resolve_project_for_window({"session": session})
    if not project_key or project_key == projects.MAIN_KEY:
        raise HTTPException(400, f"session {session!r} is not a closable worktree")
    windows = [w for w in list_windows() if w.get("session") == session]
    kill = projects.placement_kill_set(project_key, windows, activity.pane_workspace_map())
    for target, _pane_id in kill:
        ok, msg = _tmux_mutate("kill-pane", "-t", target)
        if not ok:
            raise HTTPException(500, msg)
        panes.drop_target_focus(target)
    # Only drop the session's active-tracking when the session is actually gone.
    survivors = [w for w in list_windows() if w.get("session") == session]
    if not survivors:
        _active_per_session.pop(session, None)
    return {"ok": True, "session": session, "killed": [t for t, _ in kill]}
```

Ensure imports: `list_windows` is already imported in this file; `from periscope import panes` added in Task 5. Confirm `HTTPException`, `_tmux_mutate`, `_active_per_session` are in scope (they are — used at lines 62/70).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest tests/routes/test_sessions.py -k placement_set -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C /Users/tom/dev/periscope-metadata-rail add periscope/routes/sessions.py tests/routes/test_sessions.py
git -C /Users/tom/dev/periscope-metadata-rail commit -m "fix(sessions): close-worktree kills the placement set, not the whole session"
```

---

## Task 7: `cleanup_archive` placement-aware (`routes/cleanup.py`)

**Files:**
- Modify: `periscope/routes/cleanup.py:66-68`
- Test: `tests/routes/test_cleanup.py`

- [ ] **Step 1: Write the failing route test**

Append to `tests/routes/test_cleanup.py` (mirror the file's existing fixture/mocking style). The contract: archiving a worktree kills its placement set via `kill-pane`, sparing a workspace-tagged pane, and never issues `kill-session`:

```python
def test_cleanup_archive_spares_ws_pane(
        client, clean_state, fresh_activity_db, monkeypatch):
    from periscope.routes import cleanup as croute
    clean_state["projects"]["/repo/a"] = {"tmux_session": "sess_a", "repo": "/repo"}
    monkeypatch.setattr(croute, "list_windows", lambda: [
        {"session": "sess_a", "index": 0, "pane_id": "%claude"},
        {"session": "sess_a", "index": 1, "pane_id": "%shell"},
    ], raising=False)
    fresh_activity_db.set_pane_workspace("%claude", "ws_goal")
    calls = []
    monkeypatch.setattr(croute, "_tmux_mutate", lambda *a: (calls.append(a), (True, ""))[1])
    # stub worktree removal so the test stays unit-scoped
    monkeypatch.setattr(croute, "_run", lambda *a, **k: (0, ""))

    r = client.post("/api/cleanup/archive", json={"candidates": [{"pinned_dir": "/repo/a"}]})
    assert r.status_code == 200
    assert ("kill-pane", "-t", "sess_a:1") in calls
    assert not any(a[0] == "kill-session" for a in calls)
```

(Adapt the request path/body and fixtures to the existing `test_cleanup.py` conventions.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest tests/routes/test_cleanup.py -k spares_ws_pane -q`
Expected: FAIL — current code issues `kill-session`.

- [ ] **Step 3: Rewrite the kill step**

In `periscope/routes/cleanup.py`, replace lines 66-68:

```python
            # 2. Kill tmux session.
            if tmux_session:
                _tmux_mutate("kill-session", "-t", tmux_session)
```

with:

```python
            # 2. Kill the worktree's placement set (panes whose rail placement
            # is this project), sparing any pane dragged into a workspace.
            if tmux_session:
                from periscope import activity, projects as _projects, panes
                from periscope.panes import list_windows
                windows = [w for w in list_windows()
                           if w.get("session") == tmux_session]
                for target, _pid in _projects.placement_kill_set(
                        pinned_dir, windows, activity.pane_workspace_map()):
                    _tmux_mutate("kill-pane", "-t", target)
                    panes.drop_target_focus(target)
```

(`pinned_dir` is the project key here, so `placement_kill_set` resolves against it directly. `placement_kill_set` raises `ValueError` on `MAIN_KEY`, but the loop above is guarded by the existing `pinned_dir == MAIN_KEY` raise at line 57-58, so it never reaches a dev group.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest tests/routes/test_cleanup.py -k spares_ws_pane -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C /Users/tom/dev/periscope-metadata-rail add periscope/routes/cleanup.py tests/routes/test_cleanup.py
git -C /Users/tom/dev/periscope-metadata-rail commit -m "fix(cleanup): teardown kills the placement set, not the whole session"
```

---

## Task 8: Tag panes on create (`open_ops.py`, `channels.py`)

**Files:**
- Modify: `periscope/open_ops.py` (in `_open_path`, after `place_in_rail` at line 203)
- Modify: `periscope/channels.py` (in `_do_spawn_claude_tool`, after `place_in_rail` at line 620)

Both tag using `resolve_project_for_window({"session": session})` (session-match → the canonical project key), so the tag equals exactly what the fallback would return.

- [ ] **Step 1: Tag in `open_ops._open_path`**

In `periscope/open_ops.py`, immediately after `ui = place_in_rail(...)` (line 203) and before `return OpenResult(...)`, add:

```python
    proj_key = projects.resolve_project_for_window({"session": session})
    if proj_key and proj_key != projects.MAIN_KEY:
        from periscope import activity
        for w in list_windows():
            if w.get("session") == session and w.get("pane_id"):
                activity.set_pane_project(w["pane_id"], proj_key)
```

(`projects` and `list_windows` are already imported in `open_ops.py`.)

- [ ] **Step 2: Tag in `channels._do_spawn_claude_tool`**

In `periscope/channels.py`, in the `if anchored:` block (lines 617-620), after `open_ops.place_in_rail(...)`, add:

```python
        from periscope import projects as _projects
        proj_key = _projects.resolve_project_for_window({"session": session})
        if pane_id and proj_key and proj_key != _projects.MAIN_KEY:
            activity.set_pane_project(pane_id, proj_key)
```

(`activity` is imported at line 504; `pane_id` is in scope from line 602.)

- [ ] **Step 3: Run the open/spawn integration tests**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest tests/test_open_ops.py tests/test_worktree_spawn.py -q`
Expected: PASS (these are `@needs_tmux` real-tmux tests; if tmux unavailable they skip — that's acceptable, note it). At minimum, no import errors: `uv run --directory /Users/tom/dev/periscope-metadata-rail python -c "import periscope.open_ops, periscope.channels"`.

- [ ] **Step 4: Commit**

```bash
git -C /Users/tom/dev/periscope-metadata-rail add periscope/open_ops.py periscope/channels.py
git -C /Users/tom/dev/periscope-metadata-rail commit -m "feat(open): tag created panes with their project (open_ops + channels spawn)"
```

---

## Task 9: Backfill invocation + prune wiring (`app.py`)

**Files:**
- Modify: `periscope/app.py:61-77` (housekeeping + before `yield`)

- [ ] **Step 1: Add `prune_pane_projects` to housekeeping**

In `periscope/app.py`, inside `_pane_sessions_housekeeping`, after the `prune_pane_workspaces` block (lines 73-75), add:

```python
        dropped_proj = activity.prune_pane_projects(alive)
        if dropped_proj:
            log.info("pruned %d dead pane_projects row(s)", dropped_proj)
```

- [ ] **Step 2: Add the synchronous backfill before `yield`**

In `periscope/app.py`, immediately before `try:` / `yield` (line 117-118), add a blocking call (NOT `_bg` — it must complete before serving):

```python
    # Synchronous (pre-serve) backfill: seed pane_projects from today's
    # session-derived grouping so the rail is byte-identical at cutover.
    # NOT _bg — the collapse follow-on deletes the session-match fallback,
    # so this must already be a blocking step.
    from periscope import projects as _projects
    try:
        seeded = _projects.backfill_pane_projects()
        if seeded:
            log.info("backfilled %d pane_projects row(s)", seeded)
    except Exception:
        log.warning("pane_projects backfill failed; falling back to session-match", exc_info=True)
```

- [ ] **Step 3: Verify the app imports and the lifespan tests still pass**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail python -c "import periscope.app"`
Expected: no error.
Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest tests/test_app.py -q` (if it exists; otherwise `tests/routes/` smoke).
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git -C /Users/tom/dev/periscope-metadata-rail add periscope/app.py
git -C /Users/tom/dev/periscope-metadata-rail commit -m "feat(app): synchronous pane_projects backfill before serving + prune wiring"
```

---

## Task 10: `closeWorktree` confirm copy + rebuild bundle (`Rail.jsx`)

**Files:**
- Modify: `static/src/split/Rail.jsx:276-279`
- Build: `static/dist/app.js`

- [ ] **Step 1: Reword the confirm dialog**

In `static/src/split/Rail.jsx`, replace the `confirmDialog` message in `closeWorktree` (lines 276-279):

```javascript
    const ok = await confirmDialog(
      `Close session "${session}"?\n\nThis kills every tmux window in this worktree.\nThe worktree directory on disk is not removed.`,
      { okLabel: "Close", danger: true }
    );
```

with:

```javascript
    const ok = await confirmDialog(
      `Close worktree "${session}"?\n\nTabs that live here are closed. Claude tabs you've moved into a workspace stay open.\nThe worktree directory on disk is not removed.`,
      { okLabel: "Close", danger: true }
    );
```

- [ ] **Step 2: Rebuild the bundle**

Run: `npm --prefix /Users/tom/dev/periscope-metadata-rail run build`
Expected: Vite build succeeds, writes `static/dist/app.js`.

- [ ] **Step 3: Commit**

```bash
git -C /Users/tom/dev/periscope-metadata-rail add static/src/split/Rail.jsx static/dist/app.js
git -C /Users/tom/dev/periscope-metadata-rail commit -m "fix(rail): close-worktree confirm copy reflects placement-aware close"
```

---

## Task 11: Full verification

- [ ] **Step 1: Run the whole suite**

Run: `uv run --directory /Users/tom/dev/periscope-metadata-rail pytest -q`
Expected: all green (the canonical env collects 634 + the new tests). If `test_channel_shim.py` shows the two known spurious reconnect failures, run `uv sync` (see CLAUDE.md `.venv` drift note) and re-run.

- [ ] **Step 2: Grep for residual `kill-session` in the two changed killers**

Run: `grep -n "kill-session" periscope/routes/sessions.py periscope/routes/cleanup.py`
Expected: no matches (both rewired). `periscope/routes/projects.py:267` (promote-rollback) is untouched and still has its `kill-session` — that is correct.

- [ ] **Step 3: Manual browser check (note for Tom — requires the dev server)**

Run dev: `PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run --directory /Users/tom/dev/periscope-metadata-rail server.py`, open `http://localhost:8766/`. Create a new worktree, drag its claude tab into a workspace, close the origin worktree row → the claude tab survives in the workspace; the shell tab is gone. (This is the regression the suite can't fully oracle.)

- [ ] **Step 4: Final state**

Leave the branch `feat/metadata-anchored-rail` ready. Do NOT merge to main / restart prod without Tom's go-ahead (restarting prod recreates sessions and would disrupt the live dashboard). Summarize the diff and test results for Tom.

---

## Self-review notes

- **Spec coverage:** pane_projects table + accessors (Task 1) ✓; tag-first resolution (Task 2) ✓; placement_kill_set + MAIN_KEY guard (Task 3) ✓; managed-only backfill (Task 4) ✓; drop_target_focus extracted, 4 sites (Task 5) ✓; close placement-aware (Task 6) ✓; cleanup placement-aware (Task 7) ✓; tag-on-create (Task 8) ✓; sync backfill before yield + prune (Task 9) ✓; Rail copy + rebuild (Task 10) ✓; full verify + leave-for-review (Task 11) ✓. The promote-rollback non-change is asserted in Task 11 Step 2.
- **Deviation from structure doc:** `placement_kill_set` reuses `resolve_project_for_window` instead of a tags-only `proj_map` predicate — forced by Tom's only-managed backfill (untagged managed panes exist). Keeps it tmux-free/testable; sacrifices strict map-purity for correctness + DRY (one resolution rule shared with the rail).
- **Type consistency:** `set_pane_project(pane_id, project)`, `get_pane_project`, `pane_project_map`, `prune_pane_projects`, `placement_kill_set(project_key, windows, ws_map) -> [(target, pane_id)]`, `backfill_pane_projects() -> int`, `drop_target_focus(target)` — names consistent across all tasks.

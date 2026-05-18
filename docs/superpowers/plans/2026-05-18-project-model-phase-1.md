# Project Model + Adoption (Phase 1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the project data model and adoption flow from `2026-05-15-project-model-design.md`. Specifically: state v2 with `projects[pinned_dir]`, the v1→v2 migration that auto-adopts existing tmux sessions, per-repo `threading.Lock` infrastructure, extended `windows` GC immunity (so PR/Linear links survive archive), per-tab worktree-affiliation in `/api/state`, and the rename/adopt/archive endpoints + frontend wiring.

**Out of scope (later phases):** new-project creation (phase 2), worktree-tab spawn changes (phase 3), PR-review verb (phase 4), conversation history (phase 5), cleanup view (phase 6), settings panel (phase 7), auto-archive logic (phase 6 — needs cleanup view to act on the candidates).

**Architecture:** A new `periscope/projects.py` module owns `state["projects"]` accessors, mirroring the existing `periscope/store.py` pattern (typed accessors over a single `_STATE_LOCK`). A `periscope/repo_locks.py` module exposes a `repo_lock(path)` context manager for serializing git operations per realpath. `periscope/worktrees.py` caches `git worktree list --porcelain` output per repo (60s TTL) and exposes an `affiliation(cwd, project_repo)` lookup used by `build_window_view`. The state v1→v2 migration runs at `periscope/store.py` import time, walks live tmux sessions via the existing `panes.list_windows`, and writes a `projects` row per discovered session. New REST endpoints live in `periscope/routes/projects.py`. The frontend updates rename strings + adds a worktree chip on the card; no architectural changes.

**Tech Stack:** Python 3.11+ / FastAPI / vanilla JS ES modules / tmux / git. No test suite — per `CLAUDE.md`, iteration is against the live dashboard. Each task ends with a **Verify** step exercising the change end-to-end via `curl` or browser observation.

**Spec:** `docs/superpowers/specs/2026-05-15-project-model-design.md` (project model). Companion: `docs/superpowers/specs/2026-05-15-workflow-management-design.md` (verbs — phase 1 implements Verb 6 + the per-project actions subset for archive/rename).

---

## File Structure

**Created:**
- `periscope/projects.py` — typed accessors over `_STATE["projects"]`; `resolve_project_for_window(window)` helper that maps a tmux window to its owning project. Handles the `__main__` sentinel.
- `periscope/repo_locks.py` — module-level `_LOCKS: dict[str, threading.Lock]` keyed on `os.path.realpath(repo)`; exposes `repo_lock(path)` as a context manager.
- `periscope/worktrees.py` — per-repo cache of `git worktree list --porcelain` output, 60s TTL, plus `affiliation(cwd, project_repo)` → `{"kind": "at-pin"|"sibling"|"off-repo", "label": str|None}`.
- `periscope/routes/projects.py` — `GET /api/projects`, `POST /api/projects/adopt`, `PATCH /api/projects/{pinned_dir}`, `POST /api/projects/{pinned_dir}/archive`.

**Modified:**
- `periscope/store.py` — adds `"projects": {}` and `"version": 2` to `_STATE_DEFAULTS`; introduces a `_migrate_v1_to_v2()` function and runs it at import; extends `_STATE_DEFAULTS` to include `"settings": {}` as a stub block for phase 7.
- `periscope/pids.py` — extends the windows-GC immunity check at lines 149-150 to cover `linked_pr`, `linked_linear`, `acked_at`, `completed_at`, `alias`.
- `periscope/views.py` — calls `worktrees.affiliation(...)` per window; emits `project_pinned_dir` and `worktree_affiliation` fields in the view dict.
- `periscope/routes/state.py` — populates a `projects` array in the `/api/state` response (list of project rows the frontend uses for header rendering + the adoption affordance).
- `periscope/app.py` — wires the new `routes/projects.py` router.
- `static/grid.js` — `renderSession` shows `name · pinned_dir-tilde` in the header; cards get a worktree chip from `w.worktree_affiliation` when non-`at-pin`; new "adopt" button on unmanaged tmux sessions (no matching `projects[pinned_dir]` row).
- `static/styles.css` — `.card-worktree-chip` + `.card-worktree-chip-off-repo` styles; `.session-pinned-dir` style for the header secondary line.

**Not modified (deliberately):**
- `CLAUDE.md` — the "single-file server" line is already inaccurate; updating it is orthogonal to this plan.
- `static/index.html` — no markup additions; chips render inside existing card slots.

---

## Task 1: state v2 schema + version bump in store.py

**Files:**
- Modify: `periscope/store.py` (around lines 78-83: `_STATE_DEFAULTS`)
- Modify: `periscope/store.py` (after `_load_state` at line 104)

- [ ] **Step 1: Bump `_STATE_DEFAULTS` to version 2 + add new blocks**

Replace the existing `_STATE_DEFAULTS` block in `periscope/store.py`:

```python
_STATE_LOCK = threading.Lock()
_STATE_DEFAULTS: dict = {
    "version": 2,
    "ui": {},
    "windows": {},
    "commands": [],
    "projects": {},
    "settings": {},
}
```

`projects` keys are `pinned_dir` strings (absolute paths starting with `/`)
plus the literal sentinel `"__main__"`. `settings` is a stub block reserved
for phase 7; this phase only writes a `version: 2` row into the file and
seeds `projects` on migration.

- [ ] **Step 2: Add a v1→v2 migration function**

Insert after `_load_state` (around line 104) and before the `_write_state` definition:

```python
def _migrate_v1_to_v2(data: dict) -> dict:
    """Bring a v1 state.json forward to v2.

    v2 introduces `projects[pinned_dir]` and `settings`. The migration
    walks live tmux sessions (via periscope.panes.list_windows) and
    auto-adopts each as a project pinned to its first window's git
    toplevel. Two import-time wrinkles:

    1. We can't `from periscope.panes import list_windows` at module top
       — panes.py imports from store.py for `get_window`. Lazy-import
       inside this function avoids the cycle.
    2. The migration runs ONCE per import. If state.json is already at
       v2, this is a no-op.

    Tmux sessions named literally `main` or `general` bind to the
    `__main__` sentinel rather than a regular `projects[<dir>]` row,
    preserving Tom's unpinned catch-all (see spec §"Main project").
    """
    if data.get("version", 1) >= 2:
        return data

    # Lazy imports — see docstring.
    from periscope.panes import list_windows
    from periscope.tmux import _run

    projects: dict = data.get("projects") or {}

    # Always ensure the main sentinel exists, even if no live `main`/`general`
    # session is currently running.
    projects.setdefault("__main__", {
        "name": "main",
        "tmux_session": "main",
        "repo": None,
        "pinned_repo": None,
        "created_at": 0,
        "archived_at": None,
        "base_branch": None,
    })

    try:
        windows = list_windows()
    except Exception as e:
        log.warning("v2 migration: list_windows failed: %s; main-only state written", e)
        windows = []

    # Group by session, sort each by tmux window index ascending so the
    # tiebreaker is deterministic (see spec §Migration step 1).
    by_session: dict[str, list[dict]] = {}
    for w in windows:
        by_session.setdefault(w["session"], []).append(w)
    for ws in by_session.values():
        ws.sort(key=lambda w: w["index"])

    for session_name in sorted(by_session.keys()):
        # `main`/`general` always map to __main__; never created as a regular
        # project even if their window 1 happens to be in a git repo.
        if session_name in ("main", "general"):
            projects["__main__"]["tmux_session"] = session_name
            continue

        pinned_dir = None
        for w in by_session[session_name]:
            cwd = w.get("cwd") or ""
            if not cwd:
                continue
            code, toplevel = _run(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
            if code == 0 and toplevel:
                pinned_dir = os.path.realpath(toplevel)
                break
        if pinned_dir is None:
            # Unmigratable — no window has a git toplevel. Frontend will
            # surface these as "unmanaged" and offer the adopt affordance.
            continue

        if pinned_dir in projects:
            existing = projects[pinned_dir]
            log.warning(
                "v2 migration: %r and %r both resolve to %r; keeping existing project %r",
                existing.get("tmux_session"), session_name, pinned_dir, existing.get("name"),
            )
            continue

        # Resolve the project's repo. For a normal checkout, --git-common-dir
        # returns <root>/.git, so the algorithm degenerates to "this cwd's
        # toplevel = repo." For a worktree, --git-common-dir returns the
        # shared .git dir of the main checkout, whose parent is the repo.
        code, common = _run(["git", "-C", pinned_dir, "rev-parse", "--git-common-dir"])
        if code == 0 and common:
            common_abs = common if os.path.isabs(common) else os.path.join(pinned_dir, common)
            repo = os.path.realpath(os.path.dirname(common_abs))
        else:
            repo = pinned_dir

        # base_branch: the worktree's current branch when first observed.
        # Empty if detached. Used by phase 3's worktree-tab spawn.
        _, branch = _run(["git", "-C", pinned_dir, "rev-parse", "--abbrev-ref", "HEAD"])
        if branch == "HEAD":
            branch = ""

        projects[pinned_dir] = {
            "name": session_name,
            "tmux_session": session_name,
            "repo": repo,
            "pinned_repo": None,
            "created_at": int(time.time()),
            "archived_at": None,
            "base_branch": branch or None,
        }

    data["projects"] = projects
    data["settings"] = data.get("settings") or {}
    data["version"] = 2
    return data
```

- [ ] **Step 3: Wire the migration into `_load_state`**

Modify `_load_state` to invoke the migration before returning. Replace:

```python
        for k, v in _STATE_DEFAULTS.items():
            data.setdefault(k, json.loads(json.dumps(v)))
        return data
```

with:

```python
        for k, v in _STATE_DEFAULTS.items():
            data.setdefault(k, json.loads(json.dumps(v)))
        data = _migrate_v1_to_v2(data)
        return data
```

- [ ] **Step 4: Return a (data, migrated) tuple from the migration**

The migration in Step 2 starts with `if data.get("version", 1) >= 2: return data`. That guard is wrong for the no-file path — `_STATE_DEFAULTS` already declares `version=2`, so a brand-new state.json would skip the migration and end up with an empty `projects` block instead of one populated from live tmux. We also need the caller to know whether the migration actually did work, so the post-load code can decide whether to persist.

Change the function signature to return `(data, migrated_bool)`:

```python
def _migrate_v1_to_v2(data: dict) -> tuple[dict, bool]:
    """[docstring as in Step 2; add: returns (data, True) iff the
    migration actually populated projects (i.e. the input did not
    already have a populated projects block from a prior run)]"""
    # Idempotency: if `projects` already has any non-sentinel rows,
    # someone has already migrated. The `__main__` sentinel alone
    # doesn't count — we always want to walk tmux on a fresh file
    # to populate the regular projects.
    existing = data.get("projects") or {}
    if any(k != MAIN_KEY_LITERAL for k in existing.keys()):
        return data, False

    # Lazy imports — see docstring.
    from periscope.panes import list_windows
    from periscope.tmux import _run

    # ... [rest of body from Step 2 unchanged through the projects population] ...

    data["projects"] = projects
    data["settings"] = data.get("settings") or {}
    data["version"] = 2
    return data, True
```

At the top of `store.py` next to the other constants (line ~77, before `_STATE_LOCK`), define the sentinel literal so the function can reference it without importing from projects.py (avoids a cycle, since projects.py imports store):

```python
MAIN_KEY_LITERAL = "__main__"
```

projects.py's `MAIN_KEY = "__main__"` (Task 2) is the public-facing constant; this one is the internal duplicate the migration uses.

Update both branches of `_load_state` to handle the tuple. The existing-file branch becomes:

```python
        for k, v in _STATE_DEFAULTS.items():
            data.setdefault(k, json.loads(json.dumps(v)))
        data, _migrated = _migrate_v1_to_v2(data)
        # _migrated bubbles up via a module-level flag — see post-load
        # write in Step 5.
        global _MIGRATION_RAN_THIS_LOAD
        _MIGRATION_RAN_THIS_LOAD = _MIGRATION_RAN_THIS_LOAD or _migrated
        return data
```

And the no-file branch:

```python
    if not path.exists():
        data = json.loads(json.dumps(_STATE_DEFAULTS))
        data, _migrated = _migrate_v1_to_v2(data)
        global _MIGRATION_RAN_THIS_LOAD
        _MIGRATION_RAN_THIS_LOAD = _MIGRATION_RAN_THIS_LOAD or _migrated
        return data
```

Declare the module-level flag near the top of store.py (alongside `MAIN_KEY_LITERAL`):

```python
_MIGRATION_RAN_THIS_LOAD = False
```

After this change, the migration is fully idempotent: it inspects `data["projects"]` for non-sentinel rows and bails if any exist. No `_projects_seeded` flag lives in the persisted file.

- [ ] **Step 5: Persist the migrated state once, if it actually ran**

After `_load_state` returns (back in module top-level, around line 116), the populated `projects` block hasn't been persisted yet. Persist exactly when the migration produced new data:

```python
# Persist the v2 migration result so subsequent imports skip the
# tmux-walk fast-path. `_MIGRATION_RAN_THIS_LOAD` is set by
# `_migrate_v1_to_v2` when it actually populated projects this
# import. Subsequent imports against a populated file don't hit this
# branch — the migration's "any non-sentinel project row?" check
# bails before doing work.
if _MIGRATION_RAN_THIS_LOAD:
    with _STATE_LOCK:
        _write_state(_STATE)
```

This block sits between `_STATE: dict = _load_state()` and the existing `_seed_commands_if_empty()` call. `_seed_commands_if_empty` and `_channels_migration_v1` both write again if they mutate, which is fine — those are independent legacy migrations on independent fields.

- [ ] **Step 6: Verify**

```bash
# Reset to a clean state.
mv ~/.config/periscope/state.json ~/.config/periscope/state.json.bak 2>/dev/null

# Start periscope. Migration runs at import.
cd /Users/tom/dev/periscope && uv run server.py &
sleep 3

# State file should exist with v2 + projects populated from live tmux.
python3 -c "import json; d = json.load(open('/Users/tom/.config/periscope/state.json')); print('version:', d['version']); print('projects:', list(d['projects'].keys())[:5]); print('__main__:', d['projects'].get('__main__', {}).get('name'))"

# Expected: version: 2, projects keys are absolute paths + __main__, main row exists.

# Stop server.
kill %1
wait 2>/dev/null
```

- [ ] **Step 7: Note on `sessions[]` legacy block**

The project-model spec §Migration step 4 mentions merging a legacy
`sessions[<name>].repo` block from a partial worktree-integration
implementation. In practice that spec never shipped, so no real
state.json has the block. This migration deliberately does not handle
it — adding 3 lines of merge code for a state that doesn't exist in
the wild is YAGNI. If the legacy block ever shows up (manual edits,
backups from a hypothetical branch), the migration ignores it; the
user can re-adopt anything that didn't carry over.

- [ ] **Step 8: Commit**

```bash
git add periscope/store.py
git commit -m "state v2: migrate to projects[pinned_dir] block with tmux-walk auto-adoption"
```

---

## Task 2: typed accessors for projects in periscope/projects.py

**Files:**
- Create: `periscope/projects.py`

- [ ] **Step 1: Write the accessor module**

Create `periscope/projects.py`:

```python
"""projects[pinned_dir]: project lifecycle metadata.

A project = pinned directory + repo + tmux session. Identity is
pinned_dir (absolute path, realpath'd). The `__main__` sentinel is
the unpinned catch-all (see spec §"Main project").

Accessors hold periscope.store._STATE_LOCK internally and persist
mutations via _write_state. Read accessors return copies.
"""

import os
import time
from typing import TypedDict, Optional

from periscope import store as _store
from periscope.log import log


MAIN_KEY = "__main__"


class Project(TypedDict, total=False):
    """A row in state['projects']."""
    name: str
    tmux_session: str
    repo: Optional[str]
    pinned_repo: Optional[str]
    created_at: int
    archived_at: Optional[int]
    base_branch: Optional[str]


def _canonical_key(pinned_dir: str) -> str:
    """Write-time canonicalization: absolute, realpath'd, no trailing slash.
    Called on insert paths only (create_project + the migration).

    For READS, prefer `_lookup_key` — it tries the literal first and
    only realpaths on miss. Realpath is multiple syscalls per call and
    we're hot-path in `build_window_view` per poll.
    """
    if pinned_dir == MAIN_KEY:
        return MAIN_KEY
    if not pinned_dir.startswith("/"):
        raise ValueError(f"pinned_dir must be absolute: {pinned_dir!r}")
    return os.path.realpath(pinned_dir).rstrip("/") or "/"


def _lookup_key(pinned_dir: str, projects: dict) -> str:
    """Read-time canonicalization: try the literal key first (the
    common case — keys in `projects` are already canonical after
    migration / create). Fall back to realpath only on miss.
    """
    if pinned_dir == MAIN_KEY:
        return MAIN_KEY
    if pinned_dir in projects:
        return pinned_dir
    rstripped = pinned_dir.rstrip("/") or "/"
    if rstripped in projects:
        return rstripped
    # Cold path: realpath. Symlinked input paths fall through here.
    if pinned_dir.startswith("/"):
        return os.path.realpath(pinned_dir).rstrip("/") or "/"
    return pinned_dir


def get_project(pinned_dir: str) -> Project:
    """Return a copy of projects[pinned_dir], or an empty dict if unknown."""
    with _store._STATE_LOCK:
        projects = _store._STATE.get("projects", {})
        key = _lookup_key(pinned_dir, projects)
        return dict(projects.get(key, {}))  # type: ignore[return-value]


def all_projects() -> dict[str, Project]:
    """Snapshot of all projects (copies)."""
    with _store._STATE_LOCK:
        return {
            k: dict(v)
            for k, v in _store._STATE.get("projects", {}).items()
        }


def create_project(pinned_dir: str, **fields) -> Project:
    """Insert a new project row. Raises ValueError on duplicate or
    non-absolute pinned_dir.

    `fields` should include at least `name` and `tmux_session`. Missing
    optional fields default to None / 0.
    """
    key = _canonical_key(pinned_dir)  # write-time realpath
    with _store._STATE_LOCK:
        projects = _store._STATE.setdefault("projects", {})
        if key in projects:
            raise ValueError(f"project already exists at {key!r}")
        row: Project = {
            "name": fields.get("name", ""),
            "tmux_session": fields.get("tmux_session", ""),
            "repo": fields.get("repo"),
            "pinned_repo": fields.get("pinned_repo"),
            "created_at": fields.get("created_at", int(time.time())),
            "archived_at": fields.get("archived_at"),
            "base_branch": fields.get("base_branch"),
        }
        projects[key] = dict(row)
        _store._write_state(_store._STATE)
        return dict(row)


def update_project(pinned_dir: str, **fields) -> bool:
    """Merge `fields` into projects[pinned_dir] and persist. Returns True
    if the project existed. Cannot modify identity (pinned_dir itself).
    None values overwrite — use them to clear archived_at, base_branch, etc.
    """
    with _store._STATE_LOCK:
        projects = _store._STATE.setdefault("projects", {})
        key = _lookup_key(pinned_dir, projects)
        if key not in projects:
            return False
        # __main__ is restricted: only tmux_session is mutable on it.
        if key == MAIN_KEY:
            for k in list(fields.keys()):
                if k != "tmux_session":
                    log.warning("ignoring update to __main__.%s", k)
                    fields.pop(k, None)
        projects[key].update(fields)
        _store._write_state(_store._STATE)
        return True


def archive_project(pinned_dir: str) -> bool:
    """Set archived_at to now. __main__ is never archivable."""
    with _store._STATE_LOCK:
        projects = _store._STATE.setdefault("projects", {})
        key = _lookup_key(pinned_dir, projects)
        if key == MAIN_KEY:
            raise ValueError("cannot archive __main__")
        if key not in projects:
            return False
        projects[key]["archived_at"] = int(time.time())
        _store._write_state(_store._STATE)
        return True


def resolve_project_for_window(window: dict) -> Optional[str]:
    """Map a tmux window (with `session` field) to its owning project key.

    Returns the pinned_dir key, MAIN_KEY for main, or None if no project
    matches. Lookup is by `tmux_session` match.
    """
    session = window.get("session", "")
    if not session:
        return None
    with _store._STATE_LOCK:
        for key, row in _store._STATE.get("projects", {}).items():
            if row.get("tmux_session") == session:
                return key
    return None
```

- [ ] **Step 2: Verify the module imports + exports cleanly**

```bash
cd /Users/tom/dev/periscope && python3 -c "
from periscope.projects import all_projects, get_project, resolve_project_for_window, MAIN_KEY
ps = all_projects()
print('project count:', len(ps))
print('main exists:', MAIN_KEY in ps)
print('main name:', ps[MAIN_KEY]['name'])
print('non-main keys:', [k for k in ps if k != MAIN_KEY][:3])
"
```

Expected: project count > 0, main exists: True, main name: main, list of absolute-path keys.

- [ ] **Step 3: Commit**

```bash
git add periscope/projects.py
git commit -m "projects: add typed accessors over state['projects']"
```

---

## Task 3: extend windows-GC immunity in pids.py

**Files:**
- Modify: `periscope/pids.py:147-155`

- [ ] **Step 1: Replace the immunity check**

In `periscope/pids.py`, locate the GC loop (around lines 142-156). Replace:

```python
        for pid in list(wblock.keys()):
            if pid in taken:
                continue
            entry = wblock[pid]
            if entry.get("notes") or entry.get("tags"):
                continue
            ts = (entry.get("last_seen") or {}).get("ts") or 0
            if ts < cutoff:
                del wblock[pid]
                dirty = True
```

with:

```python
        # Immunity fields: any of these set means the row carries state the
        # user expects to persist past archive (per the project-model spec
        # §"GC extension"). `notes`/`tags` were the v1 list; phase 1 adds
        # the channels-MCP fields so archiving a project doesn't silently
        # erase its PR/Linear linkage.
        _IMMUNITY_FIELDS = (
            "notes", "tags",
            "linked_pr", "linked_linear",
            "acked_at", "completed_at",
            "alias",
        )
        for pid in list(wblock.keys()):
            if pid in taken:
                continue
            entry = wblock[pid]
            if any(entry.get(k) for k in _IMMUNITY_FIELDS):
                continue
            ts = (entry.get("last_seen") or {}).get("ts") or 0
            if ts < cutoff:
                del wblock[pid]
                dirty = True
```

Note: `_IMMUNITY_FIELDS` lives inside the function scope to keep the change local; if a future phase grows the immunity list further, lift it to a module constant.

- [ ] **Step 2: Verify the immunity rule by inspection**

There's no easy way to test GC inside the 30-day window without time-travel. The verify here is: read the state.json after server start, confirm that any existing window rows carrying `linked_pr`/`linked_linear` still exist after a server restart (i.e. the GC didn't drop them on import):

```bash
# Pick a pid that has linked_pr or linked_linear set.
python3 -c "
import json
d = json.load(open('/Users/tom/.config/periscope/state.json'))
linked = {pid: e for pid, e in d['windows'].items() if e.get('linked_pr') or e.get('linked_linear')}
print('linked window count:', len(linked))
for pid, e in list(linked.items())[:3]:
    print('  ', pid, {k: v for k, v in e.items() if k in ('linked_pr', 'linked_linear', 'last_seen')})
"

# Run periscope to trigger a /api/state poll (which calls resolve_pids and runs GC).
cd /Users/tom/dev/periscope && uv run server.py &
sleep 5
curl -s http://127.0.0.1:8765/api/state > /dev/null
kill %1
wait 2>/dev/null

# Re-check — same linked rows must still be present.
python3 -c "
import json
d = json.load(open('/Users/tom/.config/periscope/state.json'))
linked = {pid: e for pid, e in d['windows'].items() if e.get('linked_pr') or e.get('linked_linear')}
print('linked window count after poll:', len(linked))
"
```

Expected: linked window count is unchanged before vs after the poll.

- [ ] **Step 3: Commit**

```bash
git add periscope/pids.py
git commit -m "pids: extend windows-GC immunity to cover linked_pr, linked_linear, acked_at, completed_at, alias"
```

---

## Task 4: per-repo lock infrastructure

**Files:**
- Create: `periscope/repo_locks.py`

- [ ] **Step 1: Write the module**

Create `periscope/repo_locks.py`:

```python
"""Per-repo threading.Lock registry.

`git worktree add` is not atomic with branch creation; concurrent
spawns against the same repo race. The coarse store._STATE_LOCK is
too coarse — it would serialize unrelated state writes during a slow
git operation. Instead, every git-mutating verb acquires a lock
keyed on the repo's realpath.

This module is used by phase 2+ verbs (new project, new worktree-tab,
PR review, cleanup). Phase 1 doesn't itself call any git-mutating
verb, but introduces the infrastructure so later phases don't
re-invent it.
"""

import contextlib
import os
import threading


_REGISTRY_LOCK = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def _key(repo_path: str) -> str:
    """Canonicalize a repo path to its registry key."""
    return os.path.realpath(repo_path)


@contextlib.contextmanager
def repo_lock(repo_path: str):
    """Hold the per-repo lock for the duration of the `with` block.

    Nested acquisition of the SAME repo by the SAME thread will
    deadlock — these are non-reentrant. Callers should hold the lock
    only across the git mutation itself, not surrounding state work.

    Multiple concurrent operations on DIFFERENT repos run in parallel.
    """
    key = _key(repo_path)
    with _REGISTRY_LOCK:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
    with lock:
        yield
```

- [ ] **Step 2: Verify**

```bash
cd /Users/tom/dev/periscope && python3 -c "
import threading
import time
from periscope.repo_locks import repo_lock

# Two threads on the same repo should serialize.
log: list[str] = []
def worker(name, repo):
    with repo_lock(repo):
        log.append(f'{name} acquired')
        time.sleep(0.1)
        log.append(f'{name} released')

t1 = threading.Thread(target=worker, args=('A', '/Users/tom/dev/periscope'))
t2 = threading.Thread(target=worker, args=('B', '/Users/tom/dev/periscope'))
t1.start(); t2.start()
t1.join(); t2.join()
# Expected: A acquired, A released, B acquired, B released  (or swapped)
print('\n'.join(log))
assert log[1].endswith('released') and log[2].endswith('acquired'), 'locks did not serialize'

# Two threads on DIFFERENT repos should overlap.
log2: list[str] = []
def worker2(name, repo):
    with repo_lock(repo):
        log2.append(f'{name} acquired')
        time.sleep(0.1)
        log2.append(f'{name} released')

t3 = threading.Thread(target=worker2, args=('A', '/Users/tom/dev/periscope'))
t4 = threading.Thread(target=worker2, args=('B', '/Users/tom/dev/trellis'))
t3.start(); t4.start()
t3.join(); t4.join()
print('---')
print('\n'.join(log2))
# Expected: both A and B acquire before either releases.
acquired = [i for i, line in enumerate(log2) if 'acquired' in line]
released = [i for i, line in enumerate(log2) if 'released' in line]
assert acquired[1] < released[0], 'locks did not overlap across repos'
print('OK')
"
```

Expected output ends with `OK`.

- [ ] **Step 3: Commit**

```bash
git add periscope/repo_locks.py
git commit -m "repo_locks: add per-realpath threading.Lock registry for git mutations"
```

---

## Task 5: worktree-affiliation cache + lookup

**Files:**
- Create: `periscope/worktrees.py`

- [ ] **Step 1: Write the module**

Create `periscope/worktrees.py`:

```python
"""Worktree affiliation per pane.

A project pins to one directory (worktree or repo root). A pane's
cwd is normally inside the pinned dir, but may be inside another
worktree of the same repo (sibling) or completely off-repo. We
classify and return a chip-rendering hint.

Caches `git worktree list --porcelain` per repo for 60s. Invalidation
on writes is the caller's responsibility (phase 2+: after worktree
add/remove, call invalidate(repo)).
"""

import os
import threading
import time
from typing import Optional, TypedDict

from periscope.tmux import _run


_TTL_S = 60.0
_lock = threading.Lock()
# repo_realpath → (fetched_at, [(worktree_realpath, branch_or_none), ...])
_cache: dict[str, tuple[float, list[tuple[str, Optional[str]]]]] = {}


class Affiliation(TypedDict):
    kind: str  # "at-pin" | "sibling" | "off-repo" | "no-repo"
    label: Optional[str]  # branch or worktree basename for chip text


def _list_worktrees(repo: str) -> list[tuple[str, Optional[str]]]:
    """Return [(worktree_path_realpath, branch_or_None), ...] for the repo.
    `repo` may be a worktree path; git resolves to the main checkout
    internally. We pass `-C repo` so this works either way.
    """
    code, out = _run(
        ["git", "-C", repo, "worktree", "list", "--porcelain"], timeout=3.0
    )
    if code != 0 or not out:
        return []
    rows: list[tuple[str, Optional[str]]] = []
    current_path: Optional[str] = None
    current_branch: Optional[str] = None
    for line in out.split("\n"):
        if line.startswith("worktree "):
            if current_path is not None:
                rows.append((current_path, current_branch))
            current_path = line[len("worktree "):]
            current_branch = None
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            current_branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        elif line == "detached":
            current_branch = None
    if current_path is not None:
        rows.append((current_path, current_branch))
    # Realpath each so symlinked entry points compare equal.
    return [(os.path.realpath(p), b) for p, b in rows]


def _cached_worktrees(repo: str) -> list[tuple[str, Optional[str]]]:
    key = os.path.realpath(repo)
    now = time.time()
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _TTL_S:
            return cached[1]
    fresh = _list_worktrees(repo)
    with _lock:
        _cache[key] = (now, fresh)
    return fresh


def invalidate(repo: str) -> None:
    """Drop the cache entry for this repo. Call after `git worktree add`
    or `git worktree remove`."""
    key = os.path.realpath(repo)
    with _lock:
        _cache.pop(key, None)


def affiliation(cwd: str, pinned_dir: Optional[str], repo: Optional[str]) -> Affiliation:
    """Classify a pane's cwd relative to its project's pinned_dir + repo.

    Returns:
      {"kind": "no-repo", "label": None}  — project has no repo (main, or
                                            non-git pinned dir)
      {"kind": "at-pin", "label": None}   — cwd is at pinned_dir or a subdir
      {"kind": "sibling", "label": branch}— cwd is in another worktree of
                                            the project's repo
      {"kind": "off-repo", "label": basename(cwd)}
                                          — cwd is not in any worktree of
                                            the project's repo
    """
    if not cwd:
        return {"kind": "no-repo", "label": None}
    cwd_real = os.path.realpath(cwd)
    if not pinned_dir or not repo:
        return {"kind": "no-repo", "label": None}
    pin_real = os.path.realpath(pinned_dir)
    # cwd inside pinned_dir (or pinned_dir == cwd_real exactly).
    if cwd_real == pin_real or cwd_real.startswith(pin_real.rstrip("/") + "/"):
        return {"kind": "at-pin", "label": None}
    # Cross-worktree of same repo?
    for wt_path, branch in _cached_worktrees(repo):
        if cwd_real == wt_path or cwd_real.startswith(wt_path.rstrip("/") + "/"):
            return {"kind": "sibling", "label": branch or os.path.basename(wt_path)}
    return {"kind": "off-repo", "label": os.path.basename(cwd_real) or "?"}
```

- [ ] **Step 2: Verify against your live worktrees**

```bash
cd /Users/tom/dev/periscope && python3 -c "
from periscope.worktrees import affiliation, _cached_worktrees

# Replace with one of your active worktree paths from \`git worktree list\`.
wts = _cached_worktrees('/Users/tom/dev/fdy')
print('fdy worktrees:')
for p, b in wts[:5]:
    print(' ', p, '→', b)

# at-pin
print('at-pin:', affiliation('/Users/tom/dev/periscope', '/Users/tom/dev/periscope', '/Users/tom/dev/periscope'))
# at-pin (subdir)
print('subdir:', affiliation('/Users/tom/dev/periscope/static', '/Users/tom/dev/periscope', '/Users/tom/dev/periscope'))
# off-repo
print('off-repo:', affiliation('/Users/tom/dev/trellis', '/Users/tom/dev/periscope', '/Users/tom/dev/periscope'))
# sibling (pick an active fdy worktree path)
print('sibling:', affiliation('/Users/tom/dev/worktrees/fdy/tc-canonical-attribute-selectors', '/Users/tom/dev/fdy', '/Users/tom/dev/fdy'))
"
```

Expected:
- `at-pin: {'kind': 'at-pin', 'label': None}`
- `subdir: {'kind': 'at-pin', 'label': None}`
- `off-repo: {'kind': 'off-repo', 'label': 'trellis'}`
- `sibling: {'kind': 'sibling', 'label': '<branch-name>'}` (the branch from `git worktree list`)

- [ ] **Step 3: Commit**

```bash
git add periscope/worktrees.py
git commit -m "worktrees: add per-repo worktree-list cache + affiliation classifier"
```

---

## Task 6: emit worktree_affiliation + project_pinned_dir in build_window_view

**Files:**
- Modify: `periscope/views.py` (around lines 30 + 122-136)

- [ ] **Step 1: Add imports + project lookup**

In `periscope/views.py`, add to the imports at the top:

```python
from periscope.projects import resolve_project_for_window, get_project
from periscope.worktrees import affiliation
```

- [ ] **Step 2: Compute project + affiliation per window**

In `build_window_view`, just before the `view = { ... }` assembly (around line 122), insert:

```python
    project_key = resolve_project_for_window(w)
    project = get_project(project_key) if project_key else {}
    # `project_key` is already canonical (post-migration / post-create); pass
    # it directly without re-realpath. `affiliation` realpaths the cwd as
    # part of its classification, which is the only realpath this code
    # path needs to pay per poll.
    pinned_for_aff = project_key if project_key and project_key != "__main__" else None
    aff = affiliation(w.get("cwd", ""), pinned_for_aff, project.get("repo"))
```

Then add the fields to the `view` dict (right before the closing `}` at line 136):

```python
        "project_pinned_dir": project_key,
        "project_name": project.get("name"),
        "project_archived": bool(project.get("archived_at")),
        "worktree_affiliation": aff,
```

The full assembled `view` dict now ends with these four new keys alongside the existing fields.

- [ ] **Step 3: Verify via /api/state**

```bash
cd /Users/tom/dev/periscope && uv run server.py &
sleep 3

curl -s http://127.0.0.1:8765/api/state | python3 -c "
import json, sys
data = json.load(sys.stdin)
seen = {}
for w in data['windows']:
    aff = w.get('worktree_affiliation') or {}
    kind = aff.get('kind', '?')
    seen.setdefault(kind, []).append((w['session'], w['index'], aff.get('label')))
for kind, rows in seen.items():
    print(kind, len(rows))
    for r in rows[:3]:
        print(' ', r)
"

kill %1
wait 2>/dev/null
```

Expected: `at-pin` rows dominate; `sibling` shows up for fdy tab 1 (`feature-store-geonorm` in `tc-geonorm-county-only`); `no-repo` for main session tabs without a project repo.

- [ ] **Step 4: Commit**

```bash
git add periscope/views.py
git commit -m "views: emit project_pinned_dir + worktree_affiliation per window"
```

---

## Task 7: expose projects array in /api/state response

**Files:**
- Modify: `periscope/routes/state.py:24-67`

- [ ] **Step 1: Import and emit projects**

In `periscope/routes/state.py`, add to the imports:

```python
from periscope.projects import all_projects
```

Replace the return statement at the bottom of `state()` (lines 61-66):

```python
    return {
        "windows": result,
        "ts": int(time.time()),
        "usage": cached_claude_usage(),
        "usage_scrape": cached_scraped_usage(),
    }
```

with:

```python
    # Map session→pinned_dir for the frontend's "unmanaged tmux session"
    # detection: any session that appears in `windows` but isn't owned by
    # any non-archived project gets the adopt affordance.
    projects = all_projects()
    projects_view = [
        {"pinned_dir": k, **v}
        for k, v in projects.items()
        if not v.get("archived_at")
    ]

    return {
        "windows": result,
        "projects": projects_view,
        "ts": int(time.time()),
        "usage": cached_claude_usage(),
        "usage_scrape": cached_scraped_usage(),
    }
```

- [ ] **Step 2: Verify**

```bash
cd /Users/tom/dev/periscope && uv run server.py &
sleep 3

curl -s http://127.0.0.1:8765/api/state | python3 -c "
import json, sys
data = json.load(sys.stdin)
print('projects count:', len(data['projects']))
for p in data['projects'][:5]:
    print(' ', p['pinned_dir'], '→', p['name'], '/', p['tmux_session'])
"

kill %1
wait 2>/dev/null
```

Expected: project rows including `__main__` and ~14 absolute-path keys matching tmux sessions.

- [ ] **Step 3: Commit**

```bash
git add periscope/routes/state.py
git commit -m "state route: emit projects[] for frontend header rendering + unmanaged-session detection"
```

---

## Task 8: REST endpoints for adopt / rename / archive

**Files:**
- Create: `periscope/routes/projects.py`
- Modify: `periscope/app.py`

- [ ] **Step 1: Write the routes module**

Create `periscope/routes/projects.py`:

```python
"""Project CRUD endpoints.

GET    /api/projects               — list all (incl. archived)
POST   /api/projects/adopt         — adopt unmanaged session OR existing worktree
POST   /api/projects/patch         — rename, edit base_branch, set/clear pinned_repo
POST   /api/projects/archive       — set archived_at

We deliberately do NOT use path params for `pinned_dir`. Starlette
rejects URL-encoded `/` in path-converter segments by default, and
pinned_dirs are absolute paths with many slashes. Body-carried
identifiers sidestep the issue entirely.

Phase 1 does NOT include POST /api/projects (create-new); that's phase 2.
"""

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope.panes import list_windows
from periscope.projects import (
    all_projects, archive_project, create_project, get_project,
    resolve_project_for_window, update_project, MAIN_KEY,
)
from periscope.tmux import _run


router = APIRouter()


@router.get("/api/projects")
def projects_list():
    return {"projects": [
        {"pinned_dir": k, **v} for k, v in all_projects().items()
    ]}


class AdoptBody(BaseModel):
    # Exactly one of these must be set.
    pinned_dir: str | None = None
    tmux_session: str | None = None
    # Optional: override the auto-derived name (defaults to pinned_dir basename
    # or tmux_session).
    name: str | None = None


@router.post("/api/projects/adopt")
def projects_adopt(body: AdoptBody):
    if bool(body.pinned_dir) == bool(body.tmux_session):
        raise HTTPException(400, "exactly one of pinned_dir or tmux_session required")

    pinned_dir: str
    tmux_session: str

    if body.pinned_dir:
        # Adopt a worktree on disk as a project.
        if not os.path.isdir(body.pinned_dir):
            raise HTTPException(400, f"pinned_dir does not exist: {body.pinned_dir}")
        pinned_dir = os.path.realpath(body.pinned_dir)
        # Find matching tmux session, if any (the user may have a session
        # already attached to this directory).
        windows = list_windows()
        matched_session: str | None = None
        for w in windows:
            if os.path.realpath(w.get("cwd") or "") == pinned_dir:
                matched_session = w["session"]
                break
        tmux_session = matched_session or (body.name or os.path.basename(pinned_dir))
    else:
        # Adopt an unmanaged tmux session.
        windows = [w for w in list_windows() if w["session"] == body.tmux_session]
        if not windows:
            raise HTTPException(404, f"no tmux session named {body.tmux_session!r}")
        windows.sort(key=lambda w: w["index"])
        for w in windows:
            cwd = w.get("cwd") or ""
            code, toplevel = _run(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
            if code == 0 and toplevel:
                pinned_dir = os.path.realpath(toplevel)
                break
        else:
            raise HTTPException(
                400,
                f"no window in session {body.tmux_session!r} has a git toplevel; cannot adopt as project",
            )
        tmux_session = body.tmux_session

    # 409 on duplicate per spec.
    if pinned_dir in all_projects():
        raise HTTPException(409, f"project already exists at {pinned_dir!r}")

    # Resolve repo via --git-common-dir (same algorithm as the v2 migration).
    code, common = _run(["git", "-C", pinned_dir, "rev-parse", "--git-common-dir"])
    if code == 0 and common:
        common_abs = common if os.path.isabs(common) else os.path.join(pinned_dir, common)
        repo = os.path.realpath(os.path.dirname(common_abs))
    else:
        repo = pinned_dir

    _, branch = _run(["git", "-C", pinned_dir, "rev-parse", "--abbrev-ref", "HEAD"])
    if branch == "HEAD":
        branch = ""

    try:
        row = create_project(
            pinned_dir,
            name=body.name or tmux_session,
            tmux_session=tmux_session,
            repo=repo,
            base_branch=branch or None,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "pinned_dir": pinned_dir, **row}


class PatchBody(BaseModel):
    # `pinned_dir` is REQUIRED — body-carried because URL-encoded slashes
    # in path params are broken in Starlette.
    pinned_dir: str
    # All other fields optional. Pydantic distinguishes "not sent" from
    # "sent as null" via model_fields_set — fields the client explicitly
    # omitted don't appear there. Sending `null` for base_branch or
    # pinned_repo CLEARS those fields; sending `null` for name or
    # tmux_session is rejected (those have no meaningful empty state).
    name: str | None = None
    base_branch: str | None = None
    pinned_repo: str | None = None
    tmux_session: str | None = None


@router.post("/api/projects/patch")
def projects_patch(body: PatchBody):
    key = body.pinned_dir
    sent = body.model_fields_set - {"pinned_dir"}  # exclude the identity field

    if key == MAIN_KEY:
        # __main__ is restricted; only tmux_session is mutable.
        if not sent.issubset({"tmux_session"}):
            raise HTTPException(400, "only tmux_session is mutable on main")
        if "tmux_session" not in sent or body.tmux_session is None:
            return {"ok": True, "pinned_dir": key, **get_project(key)}
        if not update_project(key, tmux_session=body.tmux_session):
            raise HTTPException(404, "main not found")
        return {"ok": True, "pinned_dir": key, **get_project(key)}

    existing = get_project(key)
    if not existing:
        raise HTTPException(404, f"no project at {key!r}")

    fields: dict = {}
    for k in sent:
        v = getattr(body, k)
        if k in ("name", "tmux_session") and v is None:
            raise HTTPException(400, f"{k} cannot be null")
        # base_branch and pinned_repo accept None as "clear."
        fields[k] = v

    # If tmux_session changes, run `tmux rename-session` FIRST. If tmux
    # fails, abort without touching state — drift between state and tmux
    # would break addressing on the next poll (resolve_project_for_window
    # matches on `tmux_session` literal). If tmux succeeds, state is then
    # updated; a state-write failure after that leaves us with a renamed
    # tmux session and stale state — recoverable via the user re-issuing
    # the rename, while the inverse drift is not.
    new_tmux = fields.get("tmux_session")
    if new_tmux and new_tmux != existing.get("tmux_session"):
        from periscope.log import log
        from periscope.tmux import _tmux_mutate
        ok, msg = _tmux_mutate(
            "rename-session", "-t", existing["tmux_session"], new_tmux
        )
        if not ok:
            raise HTTPException(
                500, f"tmux rename-session failed: {msg}"
            )
        log.info(
            "renamed tmux session %r → %r for project %r",
            existing["tmux_session"], new_tmux, key,
        )

    update_project(key, **fields)
    return {"ok": True, "pinned_dir": key, **get_project(key)}


class ArchiveBody(BaseModel):
    pinned_dir: str


@router.post("/api/projects/archive")
def projects_archive(body: ArchiveBody):
    key = body.pinned_dir
    if key == MAIN_KEY:
        raise HTTPException(400, "cannot archive __main__")
    if not archive_project(key):
        raise HTTPException(404, f"no project at {key!r}")
    return {"ok": True, "pinned_dir": key, **get_project(key)}
```

- [ ] **Step 2: Wire the router in app.py**

Open `periscope/app.py` and find the existing router-include block. Add the new import + include alongside the others. The exact line depends on how routers are wired today; use this as the pattern:

```python
from periscope.routes import projects as projects_routes
# ... elsewhere ...
app.include_router(projects_routes.router)
```

- [ ] **Step 3: Verify each endpoint**

```bash
cd /Users/tom/dev/periscope && uv run server.py &
sleep 3

# GET /api/projects
echo "--- GET /api/projects"
curl -s http://127.0.0.1:8765/api/projects | python3 -m json.tool | head -30

# Adopt an existing worktree (one that no project currently owns).
# Pick a directory that's a git repo but NOT yet in your projects.
ADOPT_DIR="/Users/tom/dev/trellis"  # adjust if trellis is already adopted
echo "--- POST /api/projects/adopt"
curl -s -X POST http://127.0.0.1:8765/api/projects/adopt \
  -H "Content-Type: application/json" \
  -d "{\"pinned_dir\": \"$ADOPT_DIR\"}" | python3 -m json.tool

# Rename it.
echo "--- POST /api/projects/patch (rename)"
curl -s -X POST "http://127.0.0.1:8765/api/projects/patch" \
  -H "Content-Type: application/json" \
  -d "{\"pinned_dir\": \"$ADOPT_DIR\", \"name\": \"renamed-test\"}" | python3 -m json.tool

# Archive it.
echo "--- POST /api/projects/archive"
curl -s -X POST "http://127.0.0.1:8765/api/projects/archive" \
  -H "Content-Type: application/json" \
  -d "{\"pinned_dir\": \"$ADOPT_DIR\"}" | python3 -m json.tool

# Verify it disappears from /api/state's projects[] (filtered to non-archived).
echo "--- /api/state projects[] after archive"
curl -s http://127.0.0.1:8765/api/state | python3 -c "
import json, sys
d = json.load(sys.stdin)
print([p['pinned_dir'] for p in d['projects']])
"

# 409 on duplicate adopt.
echo "--- duplicate adopt should 409"
curl -s -w "\nHTTP %{http_code}\n" -X POST http://127.0.0.1:8765/api/projects/adopt \
  -H "Content-Type: application/json" \
  -d "{\"pinned_dir\": \"$ADOPT_DIR\"}"

kill %1
wait 2>/dev/null
```

Expected:
- GET returns list including `__main__` + real projects
- Adopt returns `{"ok": true, ...}` with the resolved pinned_dir
- Rename returns the updated row
- Archive returns the row with `archived_at` set
- /api/state's projects[] no longer includes the archived one
- Duplicate adopt returns HTTP 409 (since it would also be archived but still present)

Note: archived projects still exist in `all_projects()` but are filtered out of `/api/state`'s view. The 409-on-duplicate check uses `all_projects()` (not the filtered view) so re-adoption of an archived project also 409s — by design, the user must un-archive instead (un-archive is a phase-6 verb).

- [ ] **Step 4: Reset state for cleanliness before frontend work**

```bash
# Un-archive the test project so the frontend tasks have a clean slate.
python3 -c "
import json
p = '/Users/tom/.config/periscope/state.json'
d = json.load(open(p))
adopted = '/Users/tom/dev/trellis'
if adopted in d['projects']:
    d['projects'][adopted]['archived_at'] = None
    d['projects'][adopted]['name'] = 'trellis'  # restore default
json.dump(d, open(p, 'w'), indent=2)
print('reset OK')
"
```

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/projects.py periscope/app.py
git commit -m "routes/projects: add GET, adopt, PATCH, archive endpoints"
```

---

## Task 9: frontend — project header rename + pinned_dir secondary line

**Files:**
- Modify: `static/grid.js` (around lines 285-313: `renderSession`)
- Modify: `static/grid.js` (around lines 261-282: `sessionPill`)
- Modify: `static/styles.css` (add `.session-pinned-dir` rule)

- [ ] **Step 1: Build a project lookup from /api/state**

Periscope's `/api/state` now returns a `projects` array (Task 7). The frontend needs to look up the project row by `tmux_session` per session. Add a helper near the top of `static/grid.js` (after the existing helpers around line 50):

```javascript
// Build a tmux_session → project row lookup from the most recent /api/state
// response. Lives on `state.projectsByTmux` so renderSession can consume it
// without re-walking the array per group.
function indexProjects(projects) {
  const idx = {};
  for (const p of projects || []) {
    if (p.tmux_session) idx[p.tmux_session] = p;
  }
  return idx;
}
```

Hook this into the render path. Locate the existing `render(allWindows)` function (the one called from the poll handler — search for `state.lastWindows = `). Inside it, before calling `renderSession`, add:

```javascript
  state.projectsByTmux = indexProjects(state.lastProjects || []);
```

Then update the poll callback to capture `data.projects` alongside `data.windows`. Search for where `state.lastWindows = data.windows` is set and add adjacent:

```javascript
  state.lastProjects = data.projects || [];
```

- [ ] **Step 2: Render project name + pinned_dir in the section header**

In `static/grid.js`, locate `renderSession` (around line 285). Replace the header section (lines 297-306):

```javascript
    <section class="session-group${collapsed}${alertClass}" data-session="${s}">
      <div class="session-header" draggable="true" data-session="${s}">
        <span class="session-name">${s}</span>
        <span class="session-meta">${meta}${recentLabel ? ` · ${recentLabel}` : ""}</span>
        ${sessionPill(ws)}
        <button class="adopt" data-session="${s}" hidden>+ adopt</button>
        <button class="auto-rename" data-session="${s}" title="ask Claude to auto-rename windows in this session">✨ rename</button>
        <button class="kill-session" data-session="${s}" title="kill this tmux session">✕</button>
      </div>
```

with:

```javascript
    <section class="session-group${collapsed}${alertClass}" data-session="${s}">
      <div class="session-header" draggable="true" data-session="${s}">
        <span class="session-name">${escapeHtml(project?.name || session)}</span>
        ${pinnedDirLabel ? `<span class="session-pinned-dir">${escapeHtml(pinnedDirLabel)}</span>` : ""}
        <span class="session-meta">${meta}${recentLabel ? ` · ${recentLabel}` : ""}</span>
        ${sessionPill(ws)}
        ${adoptBtn}
        <button class="auto-rename" data-session="${s}" title="ask Claude to auto-rename windows in this session">✨ rename</button>
        <button class="kill-session" data-session="${s}" title="kill this tmux session">✕</button>
      </div>
```

And add at the top of `renderSession`, just after `const s = escapeHtml(session);`:

```javascript
  const project = state.projectsByTmux?.[session] || null;
  const pinnedDirLabel = project && project.pinned_dir && project.pinned_dir !== "__main__"
    ? project.pinned_dir.replace(/^\/Users\/[^/]+/, "~")
    : null;
  const adoptBtn = project
    ? ""
    : `<button class="adopt" data-session="${s}" title="register this tmux session as a project">+ adopt</button>`;
```

- [ ] **Step 3: Wire the adopt button click handler**

`grid.addEventListener("click", (e) => { ... })` in static/grid.js (around line 649) is a **sync** arrow function. The existing async work is done by helpers (e.g. `handleAutoRename(autoBtn)` at line 651) which are themselves async functions called from inside the sync handler. Match that pattern:

First, add the async helper near the other handlers (e.g. just above `handleAutoRename` or `handleKillSession`):

```javascript
async function handleAdopt(btn) {
  const session = btn.dataset.session;
  if (!session) return;
  btn.disabled = true;
  try {
    const res = await fetch("/api/projects/adopt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tmux_session: session }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`adopt failed: ${err.detail || res.status}`);
    }
  } finally {
    btn.disabled = false;
  }
}
```

Then in the existing `grid.addEventListener("click", (e) => { ... })` cascade, add a branch matching the other `.closest()` style (lines 651-668 are the template):

```javascript
    const adoptBtn = e.target.closest(".adopt");
    if (adoptBtn) {
      e.stopPropagation();
      handleAdopt(adoptBtn);
      return;
    }
```

Place it alongside the existing `.auto-rename` / `.kill-session` / etc. matches. The fire-and-forget call returns immediately; the next `/api/state` poll picks up the new project row and re-renders without the adopt button.

- [ ] **Step 4: Style the pinned_dir line**

In `static/styles.css`, add:

```css
.session-pinned-dir {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.75em;
  opacity: 0.6;
  margin-left: 0.5em;
}
.session-header .adopt {
  font-size: 0.75em;
  opacity: 0.8;
  border: 1px dashed currentColor;
  background: transparent;
  padding: 0.1em 0.4em;
  margin-left: 0.4em;
}
```

- [ ] **Step 5: Verify**

```bash
cd /Users/tom/dev/periscope && PERISCOPE_DEV=1 ./dev.sh &
sleep 5
```

Open http://127.0.0.1:5174/ in the browser.

Visual checks:
1. Every existing session header shows the project name + (for non-main projects) `~/dev/...` pinned_dir.
2. If you killed all tmux sessions and recreated one (`tmux new-session -d -s test-adopt -c /Users/tom/dev/trellis`), it appears in periscope with an "+ adopt" button.
3. Clicking "+ adopt" makes it disappear and the session now renders with `trellis` as the project name and pinned_dir below.

```bash
# Cleanup test session.
tmux kill-session -t test-adopt 2>/dev/null
kill %1
wait 2>/dev/null
```

- [ ] **Step 6: Commit**

```bash
git add static/grid.js static/styles.css
git commit -m "grid: render project name + pinned_dir in session header; adopt button for unmanaged sessions"
```

---

## Task 10: frontend — worktree chip on cards

**Files:**
- Modify: `static/grid.js` (around lines 54-189: `renderCard`)
- Modify: `static/styles.css` (add `.card-worktree-chip` rules)

- [ ] **Step 1: Push the chip into `metaParts` in renderCard**

`renderCard` builds the meta row as an array `metaParts` that gets joined with spaces and wrapped in `.card-meta` (see static/grid.js:80-127). The existing entries follow this pattern:

```javascript
if (metaParts.length) metaParts.push(`<span class="card-dot">·</span>`);
metaParts.push(`<span class="card-something">…</span>`);
```

Match that pattern. Insert after the existing `linked_linear` push (around line 113-122, before the `w.lgtm` block):

```javascript
  const aff = w.worktree_affiliation || { kind: "no-repo" };
  if (aff.kind === "sibling") {
    if (metaParts.length) metaParts.push(`<span class="card-dot">·</span>`);
    metaParts.push(
      `<span class="card-worktree-chip card-worktree-chip-sibling" title="this tab is in a sibling worktree of the project's repo">↪ ${escapeHtml(aff.label || "")}</span>`
    );
  } else if (aff.kind === "off-repo") {
    if (metaParts.length) metaParts.push(`<span class="card-dot">·</span>`);
    metaParts.push(
      `<span class="card-worktree-chip card-worktree-chip-off-repo" title="this tab's cwd is outside the project's repo">⚠ ${escapeHtml(aff.label || "")}</span>`
    );
  }
```

The chip slots into the existing meta row without rewriting the row structure.

- [ ] **Step 2: Style the chips**

In `static/styles.css`:

```css
.card-worktree-chip {
  display: inline-block;
  font-size: 0.7em;
  padding: 0.1em 0.4em;
  border-radius: 0.4em;
  margin-left: 0.3em;
  background: var(--chip-bg, rgba(127, 127, 127, 0.15));
  white-space: nowrap;
}
.card-worktree-chip-sibling {
  color: var(--accent, #4a90e2);
}
.card-worktree-chip-off-repo {
  color: var(--warn, #d97706);
  background: var(--warn-bg, rgba(217, 119, 6, 0.12));
}
```

- [ ] **Step 3: Verify**

```bash
cd /Users/tom/dev/periscope && PERISCOPE_DEV=1 ./dev.sh &
sleep 5
```

Open http://127.0.0.1:5174/ and visually confirm:
1. The `fdy` session has tab 1 (`feature-store-geonorm`) showing a `↪ <branch>` chip — it's in a sibling worktree.
2. Tabs 2-4 of `fdy` (`figv2-feature-store-build`, `zsh`, `feature-store-qa`, all in `workers/model_train`) show NO chip — they're at-pin (subdir of pinned_dir).
3. The `splash` session's tab 8 (`drawings-dev` in `.worktrees/drawings-refactor`) shows a `↪ <branch>` chip.
4. Main session tabs show NO chip (project has no repo, so kind = `no-repo`).
5. The `tc/*` session tabs show NO chip — they're at-pin in their respective worktrees.

```bash
kill %1
wait 2>/dev/null
```

- [ ] **Step 4: Commit**

```bash
git add static/grid.js static/styles.css
git commit -m "grid: render worktree chip on tab cards (sibling/off-repo states)"
```

---

## Task 11: project archive UI (per-project ⋯ menu)

**Files:**
- Modify: `static/grid.js` (around `renderSession` header buttons)

This is a minimal version of the per-project actions menu from spec
§Verb 7. Phase 1 ships **archive** + **rename** only; other items are
deferred to later phases.

- [ ] **Step 1: Add a `⋯` button to the session header**

In `static/grid.js`, inside `renderSession`'s header markup (the same block edited in Task 9), add a button alongside the existing `auto-rename` and `kill-session` buttons:

```javascript
        ${project && project.pinned_dir !== "__main__" ? `<button class="project-menu" data-pinned-dir="${escapeHtml(project.pinned_dir)}" title="project actions">⋯</button>` : ""}
```

The button is hidden for the `__main__` project (which can't be archived or renamed).

- [ ] **Step 2: Implement the menu (browser prompt/confirm for v1)**

Phase 1 keeps the actions menu deliberately minimal — no popover, no overlay framework. Two actions. Match the sync-handler-calls-async-helper pattern from Task 9.

Add an async helper alongside `handleAdopt`:

```javascript
async function handleProjectMenu(btn) {
  const pinnedDir = btn.dataset.pinnedDir;
  if (!pinnedDir) return;
  const action = prompt(
    "Project action — type one of: rename, archive\n(blank = cancel)",
    ""
  );
  if (!action) return;
  if (action === "rename") {
    const name = prompt("New project name:");
    if (!name) return;
    const res = await fetch("/api/projects/patch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned_dir: pinnedDir, name, tmux_session: name }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`rename failed: ${err.detail || res.status}`);
    }
  } else if (action === "archive") {
    if (!confirm(`Archive project at ${pinnedDir}?`)) return;
    const res = await fetch("/api/projects/archive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned_dir: pinnedDir }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`archive failed: ${err.detail || res.status}`);
    }
  }
}
```

Then in the sync click cascade:

```javascript
    const projectMenuBtn = e.target.closest(".project-menu");
    if (projectMenuBtn) {
      e.stopPropagation();
      handleProjectMenu(projectMenuBtn);
      return;
    }
```

Yes, `prompt()` and `confirm()` are crude. Phase 7 introduces a proper popover; for phase 1 the goal is "the verbs exist and work end-to-end," not the UX polish.

- [ ] **Step 3: Verify**

```bash
cd /Users/tom/dev/periscope && PERISCOPE_DEV=1 ./dev.sh &
sleep 5
```

Browser:
1. Click `⋯` on any non-main project header.
2. Type `rename`, then a new name. The session header updates within ~3s (next poll). The tmux session is also renamed (run `tmux ls` to confirm).
3. Click `⋯` again, type `archive`, confirm. The session disappears from the grid (because /api/state filters archived projects out).
4. Re-create the project via adoption (kill its tmux session, recreate, click adopt) — or directly un-archive via curl for testing.

```bash
kill %1
wait 2>/dev/null
```

- [ ] **Step 4: Commit**

```bash
git add static/grid.js
git commit -m "grid: add minimal ⋯ menu for project rename + archive"
```

---

## Task 12: archived-project GC (30 days post-archive)

**Files:**
- Modify: `periscope/pids.py` (alongside the existing GC pass)

- [ ] **Step 1: Add a project GC pass**

The simplest place is to piggyback on `resolve_pids`, which already
acquires `_STATE_LOCK` and runs every 3s during a poll. We add a
second loop that walks `projects` and deletes any with
`archived_at` older than 30 days.

In `periscope/pids.py`, inside `resolve_pids` after the existing windows-GC
block (around line 156, just before `if dirty: _store._write_state(...)`):

```python
        # Project GC: drop archived projects whose archived_at is older
        # than 30 days (spec §"GC" rule 2). Auto-archive itself is a
        # phase-6 feature; this just collects what the user (or future
        # phases) explicitly archived.
        # __main__ is invariant — even if some future bug wrote
        # archived_at on it, the GC must not delete it.
        from periscope.projects import MAIN_KEY as _MAIN_KEY
        projects = _store._STATE.setdefault("projects", {})
        for key in list(projects.keys()):
            if key == _MAIN_KEY:
                continue
            row = projects[key]
            archived_at = row.get("archived_at")
            if archived_at and now_ts - archived_at > _PID_TTL_S:
                del projects[key]
                dirty = True
```

`_PID_TTL_S` is already defined at the top of the file (30 days). Reusing it keeps the windows and projects retention windows aligned.

- [ ] **Step 2: Verify (by time-travel via state.json edit)**

```bash
# Archive a test project.
ADOPT_DIR="/Users/tom/dev/trellis"

cd /Users/tom/dev/periscope && uv run server.py &
sleep 3
curl -s -X POST "http://127.0.0.1:8765/api/projects/archive" \
  -H "Content-Type: application/json" \
  -d "{\"pinned_dir\": \"$ADOPT_DIR\"}" > /dev/null

# Force archived_at to >30 days ago.
python3 -c "
import json, time
p = '/Users/tom/.config/periscope/state.json'
d = json.load(open(p))
d['projects']['$ADOPT_DIR']['archived_at'] = int(time.time()) - 31 * 86400
json.dump(d, open(p, 'w'), indent=2)
"

# Touch state.json from outside the server, then poll — server caches state in
# memory, so we need to restart it for the GC to see the edit.
kill %1
wait 2>/dev/null
uv run server.py &
sleep 3

# Trigger a poll → GC fires.
curl -s http://127.0.0.1:8765/api/state > /dev/null
sleep 1

# Confirm the project was GC'd.
python3 -c "
import json
d = json.load(open('/Users/tom/.config/periscope/state.json'))
print('$ADOPT_DIR in projects:', '$ADOPT_DIR' in d['projects'])
"

kill %1
wait 2>/dev/null
```

Expected: `$ADOPT_DIR in projects: False` after the poll fires.

- [ ] **Step 3: Commit**

```bash
git add periscope/pids.py
git commit -m "pids: GC archived projects older than 30 days"
```

---

## Task 13: smoke-test the full flow end-to-end

Not strictly an implementation task, but worth doing as a final
sanity pass before declaring phase 1 done.

- [ ] **Step 1: Reset to a known state**

```bash
# Back up your real state.json.
cp ~/.config/periscope/state.json ~/.config/periscope/state.json.before-phase-1
```

- [ ] **Step 2: Launch periscope and observe migration**

```bash
cd /Users/tom/dev/periscope && uv run server.py &
sleep 4

# Verify v2 was applied.
python3 -c "
import json
d = json.load(open('/Users/tom/.config/periscope/state.json'))
print('version:', d['version'])
print('project count:', len(d['projects']))
print('main exists:', '__main__' in d['projects'])
print()
print('non-main projects:')
for k, v in d['projects'].items():
    if k == '__main__': continue
    print(f'  {v[\"name\"]:30s} → {k}')
    print(f'    repo={v[\"repo\"]}, base_branch={v[\"base_branch\"]}, tmux_session={v[\"tmux_session\"]}')
"
```

Expected: every tmux session that has a window in a git repo has a corresponding project. Tmux sessions named `main`/`general` map to `__main__`.

- [ ] **Step 3: Open the dashboard and visually confirm**

Visit http://127.0.0.1:8765/ — verify:
- Project headers show the right names + pinned_dir paths.
- The `fdy` session (or whichever has cross-worktree tabs) shows the worktree chip on the right tab.
- No "+ adopt" button on existing sessions.
- Clicking `⋯` on a non-main project lets you rename/archive.

- [ ] **Step 4: Stress: create a session externally in a fresh repo**

Use a path that is NOT already a project (so the adopt isn't a 409). A throw-away git repo in /tmp is the cleanest:

```bash
SMOKE_DIR=$(mktemp -d)
( cd "$SMOKE_DIR" && git init -q && git commit --allow-empty -m init -q )

# Create the session manually (not via periscope).
tmux new-session -d -s smoke-test -c "$SMOKE_DIR"

# Wait for the next poll, then check the dashboard.
sleep 4
```

The `smoke-test` session should appear with an "+ adopt" button (no project row yet). Click adopt → header updates to show `smoke-test` as a project name with the `/tmp/...` pinned_dir secondary line. Then `⋯` → `archive` → it disappears.

```bash
tmux kill-session -t smoke-test 2>/dev/null
rm -rf "$SMOKE_DIR"
kill %1
wait 2>/dev/null
```

- [ ] **Step 5: Restore state**

```bash
# Restore your real state.json. The phase-1 schema (v2) is forward-compatible —
# the migration is a no-op on re-import.
mv ~/.config/periscope/state.json ~/.config/periscope/state.json.smoke-test
cp ~/.config/periscope/state.json.before-phase-1 ~/.config/periscope/state.json
```

- [ ] **Step 6: Final commit**

If anything turned up that needed a tweak in a prior task, commit it as a follow-up. Otherwise, no commit here — phase 1 is shipped.

---

## What's deliberately NOT in phase 1 (forward-references for clarity)

- **New project creation** (`POST /api/projects`, the "+ project" UI gesture). Phase 2.
- **Worktree-tab spawn** (`POST /api/window/new-worktree` with `base_branch` + `layout` params). Phase 3.
- **PR review verb** (`POST /api/pr-review`). Phase 4.
- **Conversation history view + resume verb**. Phase 5.
- **Cleanup view** (`GET /api/cleanup/candidates`, `POST /api/cleanup/archive`, the staleness signals). Phase 6. This is also when **auto-archive** logic lands.
- **Settings UI** (`GET/PATCH /api/settings`, worktree-layout per-repo, repos_dir, cleanup thresholds). Phase 7.
- **Promote-tab-to-project** for main project tabs. Folded into phase 2 (it's a thin wrapper over adopt + window move).

The phase-1 endpoints (adopt, PATCH, archive) are stable; later phases extend, not modify, this surface.

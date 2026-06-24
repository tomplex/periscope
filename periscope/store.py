"""state.json: persistent UI prefs, per-window annotations, command palette.

Single JSON file at $XDG_CONFIG_HOME/periscope/state.json (default
~/.config/periscope/state.json), mutated only by the server, under
threading.Lock, with atomic tempfile+rename writes.

Lock choice: threading.Lock (not asyncio.Lock). FastAPI runs sync `def`
endpoints on anyio's threadpool, so two concurrent /api/state polls
execute in parallel threads. asyncio.Lock only blocks coroutines.

Import-time side effect: `_STATE = _load_state()` runs on import, then
`_seed_commands_if_empty()` + `_channels_migration_v1()` run. Importing
periscope.store mutates ~/.config/periscope/state.json (creates it if
missing, runs migrations).

First-load is NOT cheap: on a fresh or v1 state file the v1→v2 migration
shells out to `tmux list-windows` plus per-session `git rev-parse`
subprocesses and blocks until they return. Import of this module is only
pure/fast once state.json is already at v2 (the migration's idempotency
check then bails before spawning anything).

## Accessor API

Callers should NOT touch `_STATE` directly. Instead use the typed
accessor functions below — each holds `_STATE_LOCK` internally and
schedules a `_write_state` on mutation. This avoids the dict-by-reference
landmine where `from periscope.store import _STATE` captures the dict
object, so monkeypatching `periscope.store._STATE` in a test doesn't
reach consumers that imported the name. The function-level API dodges
this by dispatching against the module each time.

Windows (per-pid annotations):
  - `get_window(pid) -> WindowAnnotation` — copy
  - `set_window_fields(pid, **fields)` — merge + persist
  - `set_window_fields_bulk(updates)` — batch under one lock + one write
  - `all_windows() -> dict[str, WindowAnnotation]` — full snapshot copy

UI prefs (free-form key/value):
  - `get_ui() -> dict` — copy
  - `update_ui(patch: dict)` — merge + persist; None values delete the key

Commands (palette):
  - `get_commands() -> list[Command]` — copy
  - `add_command(label, exec_cmd)` — append + persist
  - `update_command(label, *, exec_cmd=None, new_label=None) -> bool`
  - `delete_command(label) -> bool`
  - `reorder_commands(labels)` — reorder by label sequence

Full state read (rarely needed, mostly for /api/prefs):
  - `snapshot() -> dict` — deep copy of everything
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import TypedDict

from periscope.config import CLAUDE_EXEC, config_dir
from periscope.log import log


class WindowAnnotation(TypedDict, total=False):
    """Per-pid annotations persisted in state.json under windows[pid]."""
    linked_pr: int
    linked_linear: str
    linked_linear_title: str
    linked_linear_status: str
    completed_at: int
    acked_at: int
    alias: str
    is_fork: bool  # phase 4: set on PR-review projects' claude window
    open_tabs: list  # [{"path": str, "line": int|None}], see periscope.tabs
    active_tab: str  # "file:<path>"; absent means "pane"


class Settings(TypedDict, total=False):
    """Per-host preferences. Persisted under state['settings']."""
    worktree_layout_default: str  # "sibling" | "inline"
    worktree_layout_overrides: dict[str, str]  # realpath -> "sibling" | "inline"
    cleanup_idle_days: int


class Command(TypedDict):
    """A command-palette entry."""
    label: str
    exec: str


def _state_path() -> Path:
    return config_dir() / "state.json"


MAIN_KEY_LITERAL = "__main__"
_MIGRATION_RAN_THIS_LOAD = False

_STATE_LOCK = threading.Lock()
_STATE_DEFAULTS: dict = {
    "version": 2,
    "ui": {},
    "windows": {},
    "commands": [],
    "projects": {},
    "workspaces": {},
    "settings": {},
}


def _load_state() -> dict:
    """Read state.json. On parse failure rename to .corrupt-<ts> and return
    defaults — the next save writes a fresh valid file."""
    global _MIGRATION_RAN_THIS_LOAD
    path = _state_path()
    if not path.exists():
        data = json.loads(json.dumps(_STATE_DEFAULTS))
        data, _migrated = _migrate_v1_to_v2(data)
        _MIGRATION_RAN_THIS_LOAD = _MIGRATION_RAN_THIS_LOAD or _migrated
        return data
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for k, v in _STATE_DEFAULTS.items():
            data.setdefault(k, json.loads(json.dumps(v)))
        data, _migrated = _migrate_v1_to_v2(data)
        # _migrated bubbles up via a module-level flag — see post-load
        # write in Step 5.
        _MIGRATION_RAN_THIS_LOAD = _MIGRATION_RAN_THIS_LOAD or _migrated
        return data
    except (json.JSONDecodeError, OSError) as e:
        corrupt = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.rename(corrupt)
            log.warning("state.json unreadable (%s); renamed to %s", e, corrupt)
        except OSError:
            pass
        data = json.loads(json.dumps(_STATE_DEFAULTS))
        data, _migrated = _migrate_v1_to_v2(data)
        _MIGRATION_RAN_THIS_LOAD = _MIGRATION_RAN_THIS_LOAD or _migrated
        return data


def _migrate_v1_to_v2(data: dict) -> tuple[dict, bool]:
    """Bring a v1 state.json forward to v2.

    v2 introduces `projects[pinned_dir]` and `settings`. The migration
    walks live tmux sessions (via periscope.panes.list_windows) and
    auto-adopts each as a project pinned to its first window's git
    toplevel.

    Runs live subprocesses, one batch per session: a `tmux list-windows`
    up front, then `git rev-parse` invocations per session to resolve the
    pinned dir, repo, and base branch. On a fresh/v1 file this happens at
    module import time and blocks — see the module docstring.

    Two import-time wrinkles:

    1. `_load_state()` runs at module import time (see `_STATE = _load_state()`
       at module bottom). Importing `periscope.panes` at module top would
       force the panes module to fully load before this module's namespace
       is published — fragile if panes ever grows a transitive dependency
       on this module. Lazy import keeps the side-effect chain shallow.
    2. The migration runs ONCE per import. If state.json is already at
       v2, this is a no-op.

    Tmux sessions named literally `main` or `general` bind to the
    `__main__` sentinel rather than a regular `projects[<dir>]` row,
    preserving Tom's unpinned catch-all (see spec §"Main project").

    Returns (data, True) iff the migration actually populated projects
    (i.e. the input did not already have a populated projects block from
    a prior run).
    """
    # Idempotency: if `projects` already has any non-sentinel rows,
    # someone has already migrated. The `__main__` sentinel alone
    # doesn't count — we always want to walk tmux on a fresh file
    # to populate the regular projects.
    existing = data.get("projects") or {}
    if any(k != MAIN_KEY_LITERAL for k in existing):
        return data, False

    # Lazy imports — see docstring.
    from periscope.gitutil import resolve_repo_and_branch
    from periscope.panes import list_windows
    from periscope.tmux import _run

    projects: dict = data.get("projects") or {}

    # Always ensure the main sentinel exists, even if no live `main`/`general`
    # session is currently running.
    projects.setdefault(MAIN_KEY_LITERAL, {
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
            projects[MAIN_KEY_LITERAL]["tmux_session"] = session_name
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
            existing_row = projects[pinned_dir]
            log.warning(
                "v2 migration: %r and %r both resolve to %r; keeping existing project %r",
                existing_row.get("tmux_session"), session_name, pinned_dir, existing_row.get("name"),
            )
            continue

        # repo + base_branch from the pinned dir (gitutil documents the
        # --git-common-dir worktree-vs-checkout reasoning). base_branch is
        # the worktree's branch when first observed — empty if detached.
        repo, branch = resolve_repo_and_branch(pinned_dir)

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
    return data, True


def _write_state(data: dict) -> None:
    """Atomic write: tempfile + os.replace. Caller must hold _STATE_LOCK."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


_STATE: dict = _load_state()

# Persist the v2 migration result so subsequent imports skip the
# tmux-walk fast-path. `_MIGRATION_RAN_THIS_LOAD` is set by
# `_migrate_v1_to_v2` when it actually populated projects this
# import. Subsequent imports against a populated file don't hit this
# branch — the migration's "any non-sentinel project row?" check
# bails before doing work.
if _MIGRATION_RAN_THIS_LOAD:
    with _STATE_LOCK:
        _write_state(_STATE)

_DEFAULT_COMMANDS = [
    # Seeded entry. The `_channels_migration_v1` rewriter below upgrades
    # any pre-existing user state's `"exec": "claude"` to the channels-
    # enabled form; this default matches that post-migration value so a
    # fresh state.json doesn't need a no-op migration pass on first load.
    {"label": "claude", "exec": CLAUDE_EXEC},
    {"label": "shell", "exec": ""},
    {"label": "vim", "exec": "vim"},
]


def _seed_commands_if_empty() -> None:
    """If `commands` is empty, seed the three legacy defaults so the
    new-window tile keeps working."""
    with _STATE_LOCK:
        if not _STATE["commands"]:
            _STATE["commands"] = [dict(c) for c in _DEFAULT_COMMANDS]
            _write_state(_STATE)


_seed_commands_if_empty()


def _channels_migration_v1() -> None:
    """One-shot: rewrite seeded `claude` exec entries to include the
    dev-channels flag. Idempotent — gated by `channels_migration_v1_done`."""
    with _STATE_LOCK:
        if _STATE.get("channels_migration_v1_done"):
            return
        for cmd in _STATE.get("commands", []):
            if cmd.get("exec") == "claude":
                cmd["exec"] = CLAUDE_EXEC
        _STATE["channels_migration_v1_done"] = True
        _write_state(_STATE)


_channels_migration_v1()


# === Typed accessors ======================================================
# Every accessor holds _STATE_LOCK internally. Mutations call _write_state
# before releasing the lock. Read accessors return COPIES so callers can't
# mutate the cache by accident.


def get_window(pid: str) -> WindowAnnotation:
    """Return a copy of windows[pid], or an empty dict if unknown."""
    with _STATE_LOCK:
        return dict(_STATE.get("windows", {}).get(pid, {}))  # type: ignore[return-value]


def set_window_fields(pid: str, **fields) -> None:
    """Merge `fields` into windows[pid] and persist. Keys with `None`
    value are removed (symmetric with `update_ui`)."""
    with _STATE_LOCK:
        wblock = _STATE.setdefault("windows", {})
        entry = wblock.setdefault(pid, {})
        for k, v in fields.items():
            if v is None:
                entry.pop(k, None)
            else:
                entry[k] = v
        _write_state(_STATE)


def set_window_fields_bulk(updates: dict[str, dict]) -> int:
    """Apply many per-pid field updates under one lock + one write.

    `updates[pid]` is a dict of fields to merge into windows[pid].
    Returns the number of pids whose state changed (skipped writes when
    the new values match what's already persisted).

    Used by /api/state's batched stamp persistence — single lock
    acquisition per poll across every pane.
    """
    if not updates:
        return 0
    with _STATE_LOCK:
        wblock = _STATE.setdefault("windows", {})
        dirty = 0
        for pid, fields in updates.items():
            entry = wblock.setdefault(pid, {})
            changed = False
            for k, v in fields.items():
                if entry.get(k) != v:
                    entry[k] = v
                    changed = True
            if changed:
                dirty += 1
        if dirty:
            _write_state(_STATE)
        return dirty


def delete_window(pid: str) -> bool:
    """Remove windows[pid]. Returns True if it existed."""
    with _STATE_LOCK:
        wblock = _STATE.setdefault("windows", {})
        if pid not in wblock:
            return False
        del wblock[pid]
        _write_state(_STATE)
        return True


def all_windows() -> dict[str, WindowAnnotation]:
    """Snapshot of all window annotations (copies of each)."""
    with _STATE_LOCK:
        return {pid: dict(ann) for pid, ann in _STATE.get("windows", {}).items()}  # type: ignore[misc]


def get_ui() -> dict:
    """Return a copy of the ui prefs dict."""
    with _STATE_LOCK:
        return dict(_STATE.get("ui", {}))


def update_ui(patch: dict) -> None:
    """Merge `patch` into ui and persist. Keys with `None` value are removed."""
    with _STATE_LOCK:
        ui = _STATE.setdefault("ui", {})
        for k, v in patch.items():
            if v is None:
                ui.pop(k, None)
            else:
                ui[k] = v
        _write_state(_STATE)


def get_settings() -> Settings:
    """Snapshot of the settings block."""
    with _STATE_LOCK:
        # Deep-ish copy: shallow-copy the top-level + deep-copy the
        # worktree_layout_overrides dict so callers can't mutate state
        # by reference.
        raw = _STATE.get("settings", {})
        return {
            **raw,
            "worktree_layout_overrides": dict(
                raw.get("worktree_layout_overrides", {}) or {}
            ),
        }  # type: ignore[return-value]


def update_settings(patch: dict) -> None:
    """Merge `patch` into settings and persist. `worktree_layout_overrides`
    is replaced wholesale rather than merged (consumers always send the
    full override map). Keys with None value at the top level are removed.
    """
    with _STATE_LOCK:
        cur = _STATE.setdefault("settings", {})
        for k, v in patch.items():
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = v
        _write_state(_STATE)


def get_commands() -> list[Command]:
    """Snapshot of the command palette (copies of each entry)."""
    with _STATE_LOCK:
        return [dict(c) for c in _STATE.get("commands", [])]  # type: ignore[misc]


def add_command(label: str, exec_cmd: str) -> Command:
    """Append a new {label, exec} entry and persist. Returns a copy of it."""
    with _STATE_LOCK:
        cmds = _STATE.setdefault("commands", [])
        new: Command = {"label": label, "exec": exec_cmd}
        cmds.append(dict(new))
        _write_state(_STATE)
        return dict(new)


def update_command(
    label: str,
    *,
    exec_cmd: str | None = None,
    new_label: str | None = None,
) -> bool:
    """Update the command with matching `label`. Returns True if found."""
    with _STATE_LOCK:
        for cmd in _STATE.get("commands", []):
            if cmd.get("label") == label:
                if exec_cmd is not None:
                    cmd["exec"] = exec_cmd
                if new_label is not None:
                    cmd["label"] = new_label
                _write_state(_STATE)
                return True
        return False


def delete_command(label: str) -> bool:
    """Remove the command with matching `label`. Returns True if found."""
    with _STATE_LOCK:
        cmds = _STATE.get("commands", [])
        for i, cmd in enumerate(cmds):
            if cmd.get("label") == label:
                del cmds[i]
                _write_state(_STATE)
                return True
        return False


def reorder_commands(labels: list[str]) -> None:
    """Reorder commands by the given label sequence. Labels not in the
    given list keep their relative order at the end."""
    with _STATE_LOCK:
        cmds = _STATE.get("commands", [])
        index = {c["label"]: c for c in cmds}
        listed = [index[label] for label in labels if label in index]
        leftover = [c for c in cmds if c["label"] not in set(labels)]
        _STATE["commands"] = listed + leftover
        _write_state(_STATE)


def snapshot() -> dict:
    """Deep copy of the full state. Used by GET /api/prefs."""
    with _STATE_LOCK:
        return json.loads(json.dumps(_STATE))

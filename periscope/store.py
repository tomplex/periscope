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

from periscope.log import log


class WindowAnnotation(TypedDict, total=False):
    """Per-pid annotations persisted in state.json under windows[pid]."""
    linked_pr: int
    linked_linear: str
    completed_at: int
    acked_at: int
    alias: str


class Command(TypedDict):
    """A command-palette entry."""
    label: str
    exec: str


def _state_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "periscope" / "state.json"


_STATE_LOCK = threading.Lock()
_STATE_DEFAULTS: dict = {
    "version": 1,
    "ui": {},
    "windows": {},
    "commands": [],
}


def _load_state() -> dict:
    """Read state.json. On parse failure rename to .corrupt-<ts> and return
    defaults — the next save writes a fresh valid file."""
    path = _state_path()
    if not path.exists():
        return json.loads(json.dumps(_STATE_DEFAULTS))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for k, v in _STATE_DEFAULTS.items():
            data.setdefault(k, json.loads(json.dumps(v)))
        return data
    except (json.JSONDecodeError, OSError) as e:
        corrupt = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.rename(corrupt)
            log.warning("state.json unreadable (%s); renamed to %s", e, corrupt)
        except OSError:
            pass
        return json.loads(json.dumps(_STATE_DEFAULTS))


def _write_state(data: dict) -> None:
    """Atomic write: tempfile + os.replace. Caller must hold _STATE_LOCK."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


_STATE: dict = _load_state()

_DEFAULT_COMMANDS = [
    {"label": "claude", "exec": "claude"},
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
        new_exec = (
            "claude --dangerously-load-development-channels server:periscope"
        )
        for cmd in _STATE.get("commands", []):
            if cmd.get("exec") == "claude":
                cmd["exec"] = new_exec
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

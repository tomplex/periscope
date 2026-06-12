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

    Returns the pinned_dir key for a session owned by a project, MAIN_KEY
    for everything else (the fold-to-dev rule: unmanaged sessions belong
    to main). Only an empty/missing session returns None. Lookup is by
    `tmux_session` match; archived rows still match — the frontend folds
    them to dev via its no-row fallback.
    """
    session = window.get("session", "")
    if not session:
        return None
    with _store._STATE_LOCK:
        for key, row in _store._STATE.get("projects", {}).items():
            if row.get("tmux_session") == session:
                return key
    return MAIN_KEY

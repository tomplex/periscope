"""Safe merge/remove operations for Periscope's Codex hook definitions."""

import contextlib
import json
import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path

EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def command_for(repo: Path) -> str:
    return f"python3 {shlex.quote(str(repo / 'codex_pane_session_hook.py'))}"


def _entry(command: str) -> dict:
    return {"type": "command", "command": command, "timeout": 5}


def _group(command: str) -> dict:
    return {"matcher": "", "hooks": [_entry(command)]}


def _is_owned_group(value: object, command: str) -> bool:
    return value == _group(command)


def _write_atomic(path: Path, data: dict, mode: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode if mode is not None else 0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def update(repo: Path, *, install: bool) -> tuple[Path, list[str]]:
    path = codex_home() / "hooks.json"
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    if path.exists():
        # Invalid JSON is intentionally allowed to propagate. The caller
        # reports failure and, crucially, this function has not written.
        with path.open() as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("Codex hooks file must contain a JSON object")
    else:
        data = {}
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Codex hooks 'hooks' value must be an object")
    command = command_for(repo.resolve())
    changed: list[str] = []
    for event in EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"Codex hooks {event!r} value must be an array")
        if install:
            if not any(_is_owned_group(group, command) for group in groups):
                groups.append(_group(command))
                changed.append(event)
        else:
            filtered = [
                group for group in groups if not _is_owned_group(group, command)
            ]
            if len(filtered) != len(groups):
                changed.append(event)
            groups = filtered
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if changed:
        _write_atomic(path, data, mode)
    return path, changed


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("install", "uninstall"):
        return 2
    try:
        path, events = update(Path(sys.argv[2]), install=sys.argv[1] == "install")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Codex hook config unchanged: {exc}", file=sys.stderr)
        return 1
    action = "installed" if sys.argv[1] == "install" else "removed"
    detail = ", ".join(events) if events else "no changes"
    print(f"Codex hook {action}: {path} ({detail})")
    if sys.argv[1] == "install":
        print("Codex hook installed. Open /hooks in Codex and trust the Periscope hook.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

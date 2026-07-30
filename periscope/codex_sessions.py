"""Narrow, defensive catalog of Codex rollout session metadata."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CodexSessionMeta:
    session_id: str
    path: Path
    cwd: str
    started_at: datetime | None
    cli_version: str | None


_cache: dict[Path, tuple[int, int, CodexSessionMeta | None]] = {}


def codex_home() -> Path:
    # Empty CODEX_HOME is treated like unset, matching the hook installer.
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _metadata(path: Path) -> CodexSessionMeta | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _cache.get(path)
    if cached and cached[:2] == signature:
        return cached[2]
    result: CodexSessionMeta | None = None
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if row.get("type") != "session_meta":
                    continue
                payload = row.get("payload") or {}
                session_id = payload.get("id") or payload.get("session_id")
                if not isinstance(session_id, str) or not _UUID.fullmatch(session_id):
                    break
                cwd = payload.get("cwd")
                if not isinstance(cwd, str):
                    cwd = ""
                cli_version = payload.get("cli_version")
                if not isinstance(cli_version, str):
                    cli_version = None
                result = CodexSessionMeta(
                    session_id=session_id,
                    path=path,
                    cwd=cwd,
                    started_at=_parse_time(row.get("timestamp")),
                    cli_version=cli_version,
                )
                break
    except (OSError, UnicodeError):
        pass
    _cache[path] = (*signature, result)
    return result


def catalog() -> dict[str, CodexSessionMeta]:
    result: dict[str, CodexSessionMeta] = {}
    sessions = codex_home() / "sessions"
    try:
        paths = sessions.rglob("*.jsonl")
        seen: set[Path] = set()
        for path in paths:
            seen.add(path)
            meta = _metadata(path)
            if meta and (
                meta.session_id not in result
                or path.stat().st_mtime > result[meta.session_id].path.stat().st_mtime
            ):
                result[meta.session_id] = meta
        for stale in set(_cache) - seen:
            _cache.pop(stale, None)
    except OSError:
        return result
    return result

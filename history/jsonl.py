"""Stream-parse Claude Code JSONL transcripts into classified Event records."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)


@dataclass
class Event:
    """One JSONL event, normalized. Fields default to None when absent."""
    type: str
    raw: dict
    session_id: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    ts_ms: int | None = None
    uuid: str | None = None
    parent_uuid: str | None = None
    # Populated for user events
    user_text: str | None = None
    tool_results: list[dict] = field(default_factory=list)
    # Populated for assistant events
    assistant_text: str | None = None
    tool_uses: list[dict] = field(default_factory=list)


def _parse_ts(s: str | None) -> int | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _classify(raw: dict) -> Event:
    ev = Event(
        type=raw.get("type", "<missing>"),
        raw=raw,
        session_id=raw.get("sessionId"),
        cwd=raw.get("cwd"),
        git_branch=raw.get("gitBranch"),
        ts_ms=_parse_ts(raw.get("timestamp")),
        uuid=raw.get("uuid"),
        parent_uuid=raw.get("parentUuid"),
    )
    msg = raw.get("message")
    if not isinstance(msg, dict):
        return ev
    role = msg.get("role")
    content = msg.get("content")
    # Claude Code JSONLs put user prompts under message.content either as a
    # plain string OR as a list of content blocks (text/tool_use/tool_result).
    # Real data is overwhelmingly mixed — handle both shapes or we silently
    # drop the majority of human prompts.
    if isinstance(content, str):
        if role == "user":
            ev.user_text = content
        elif role == "assistant":
            ev.assistant_text = content
        return ev
    if not isinstance(content, list):
        return ev
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text")
            if isinstance(t, str):
                texts.append(t)
        elif btype == "tool_use" and role == "assistant":
            ev.tool_uses.append({
                "id": block.get("id"),
                "name": block.get("name"),
                "input": block.get("input") or {},
            })
        elif btype == "tool_result" and role == "user":
            content_val = block.get("content")
            if isinstance(content_val, list):
                # Filter empty/missing-text blocks (image blocks contribute "")
                # so we don't pad with blank lines.
                content_val = "\n".join(
                    t for c in content_val
                    if isinstance(c, dict) and (t := c.get("text"))
                )
            ev.tool_results.append({
                "tool_use_id": block.get("tool_use_id"),
                "content": content_val if isinstance(content_val, str) else "",
            })
    joined = "\n".join(t for t in texts if t.strip())
    if role == "user" and joined:
        ev.user_text = joined
    elif role == "assistant" and joined:
        ev.assistant_text = joined
    return ev


def parse_jsonl(path: str | Path) -> Iterator[Event]:
    """Stream events from a JSONL file. Skips malformed lines and logs them.
    The session_id falls back to the filename stem if no event carries it."""
    p = Path(path)
    fallback_sid = p.stem
    bad_lines = 0
    total_lines = 0
    with p.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            total_lines += 1
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            if not isinstance(raw, dict):
                bad_lines += 1
                continue
            ev = _classify(raw)
            if ev.session_id is None:
                ev.session_id = fallback_sid
            yield ev
    if total_lines and bad_lines / total_lines > 0.5:
        log.warning("history.jsonl: %s — %d/%d lines malformed, results may be incomplete",
                    p, bad_lines, total_lines)

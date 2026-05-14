"""Aggregate parsed JSONL events into a SessionRecord ready for DB upsert."""
from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .jsonl import Event

log = logging.getLogger(__name__)

# Notable-command filter: keep commands that are non-trivial.
_TRIVIAL_CMDS = {"ls", "pwd", "cat", "echo", "cd", "clear", "exit", "whoami", "date"}
_TRIVIAL_CMD_LENGTH = 20
_NON_TRIVIAL_CHARS = {"|", ">", "<", "/"}

# Path-bearing tool input keys that identify a single FILE touch. We deliberately
# exclude "path" (directory parameter on Glob/Grep) and rename-style keys not in
# Claude Code's current built-in tool set — they'd pollute the index with
# directory entries.
_FILE_KEYS = ("file_path", "notebook_path")

# Truncation lengths.
MAX_FIRST_LAST_USER = 500
MAX_FINAL_ASSISTANT = 1000


@dataclass
class SessionRecord:
    """One row's worth of mechanical fields. Haiku fields filled later."""
    session_id: str
    jsonl_path: str
    project_path: str
    branch: str | None
    started_at: int
    ended_at: int
    duration_s: int
    user_msg_count: int
    asst_msg_count: int
    tool_use_count: int
    was_interrupted: int
    ended_cleanly: int

    first_user_msg: str | None
    last_user_msg: str | None
    final_assistant_msg: str | None
    files_touched: str           # JSON array string
    notable_cmds: str            # JSON array string
    tool_use_counts: str         # JSON dict string
    user_messages_blob: str = ""     # joined for FTS + summary input
    assistant_text_blob: str = ""    # joined for FTS

    source_mtime: int = 0
    source_size: int = 0


def _decode_project_path(jsonl_path: str, events: list[Event]) -> str:
    """Project path = first cwd seen in events.

    Falls back to the encoded directory name unchanged when no event carries a
    cwd. Claude Code's encoding (`/Users/tom/dev/foo` → `-Users-tom-dev-foo`,
    plus `--` for dot-prefixed dirs like `~/.claude`) is ambiguous to invert
    (a dash-in-path-segment vs a dot-marker are indistinguishable), and real
    transcripts always carry cwd somewhere — this branch is a near-zero path."""
    for ev in events:
        if ev.cwd:
            return ev.cwd
    log.warning("extract: no cwd in any event for %s — using encoded dir name as-is", jsonl_path)
    return Path(jsonl_path).parent.name


def _is_notable_cmd(cmd: str) -> bool:
    if not cmd:
        return False
    stripped = cmd.strip()
    first_word = stripped.split(None, 1)[0] if stripped else ""
    if first_word in _TRIVIAL_CMDS:
        return False
    if " " not in stripped and len(stripped) < _TRIVIAL_CMD_LENGTH:
        return False
    if len(stripped) >= _TRIVIAL_CMD_LENGTH:
        return True
    return any(c in stripped for c in _NON_TRIVIAL_CHARS)


def extract_record(jsonl_path: str, events: list[Event], *,
                   source_mtime: int, source_size: int) -> SessionRecord:
    """Walk events once; emit a SessionRecord."""
    session_id: str | None = None
    branch: str | None = None
    timestamps: list[int] = []

    user_msg_count = 0
    asst_msg_count = 0
    tool_use_count = 0
    was_interrupted = False

    first_user_msg: str | None = None
    last_user_msg: str | None = None
    final_assistant_msg: str | None = None

    files_seen: list[str] = []
    files_set: set[str] = set()
    notable_cmds: list[str] = []
    notable_cmds_set: set[str] = set()
    tool_counts: Counter[str] = Counter()

    user_chunks: list[str] = []
    assistant_chunks: list[str] = []

    last_event_is_assistant_text = False

    for ev in events:
        if ev.session_id and not session_id:
            session_id = ev.session_id
        if ev.git_branch:
            branch = ev.git_branch
        if ev.ts_ms is not None:
            timestamps.append(ev.ts_ms)

        if ev.type == "user":
            if ev.user_text:
                user_msg_count += 1
                user_chunks.append(ev.user_text)
                if "Request interrupted by user" in ev.user_text:
                    was_interrupted = True
                if first_user_msg is None:
                    first_user_msg = ev.user_text[:MAX_FIRST_LAST_USER]
                last_user_msg = ev.user_text[:MAX_FIRST_LAST_USER]
                last_event_is_assistant_text = False
            # tool_result wrappers don't count as user messages

        elif ev.type == "assistant":
            asst_msg_count += 1
            if ev.assistant_text:
                assistant_chunks.append(ev.assistant_text)
                final_assistant_msg = ev.assistant_text[:MAX_FINAL_ASSISTANT]
                last_event_is_assistant_text = True
            else:
                last_event_is_assistant_text = False
            for tu in ev.tool_uses:
                tool_use_count += 1
                name = tu.get("name") or "?"
                tool_counts[name] += 1
                inp = tu.get("input") or {}
                # File touches
                for key in _FILE_KEYS:
                    fp = inp.get(key)
                    if isinstance(fp, str) and fp and fp not in files_set:
                        files_set.add(fp)
                        files_seen.append(fp)
                # Notable commands
                if name == "Bash":
                    cmd = inp.get("command")
                    if isinstance(cmd, str) and _is_notable_cmd(cmd):
                        if cmd not in notable_cmds_set:
                            notable_cmds_set.add(cmd)
                            notable_cmds.append(cmd)

    started_at = (min(timestamps) // 1000) if timestamps else 0
    ended_at = (max(timestamps) // 1000) if timestamps else 0
    duration_s = max(0, ended_at - started_at)

    project_path = _decode_project_path(jsonl_path, events)

    return SessionRecord(
        session_id=session_id or Path(jsonl_path).stem,
        jsonl_path=jsonl_path,
        project_path=project_path,
        branch=branch,
        started_at=started_at,
        ended_at=ended_at,
        duration_s=duration_s,
        user_msg_count=user_msg_count,
        asst_msg_count=asst_msg_count,
        tool_use_count=tool_use_count,
        was_interrupted=1 if was_interrupted else 0,
        ended_cleanly=1 if last_event_is_assistant_text else 0,
        first_user_msg=first_user_msg,
        last_user_msg=last_user_msg,
        final_assistant_msg=final_assistant_msg,
        files_touched=json.dumps(files_seen),
        notable_cmds=json.dumps(notable_cmds),
        tool_use_counts=json.dumps(dict(tool_counts)),
        user_messages_blob="\n".join(user_chunks),
        assistant_text_blob="\n".join(assistant_chunks),
        source_mtime=source_mtime,
        source_size=source_size,
    )


# Triviality thresholds.
TRIVIAL_USER_MSG_THRESHOLD = 2
TRIVIAL_DURATION_S = 60


def compute_summary_input_hash(rec: SessionRecord) -> str:
    """SHA256 over a canonical representation of the fields that drive the
    Haiku summary. If any of these change, the summary should be re-derived;
    if none change, we can reuse a stored summary."""
    notable_cmds_first_20 = json.loads(rec.notable_cmds)[:20]
    canonical = json.dumps([
        rec.first_user_msg or "",
        rec.user_messages_blob,
        rec.final_assistant_msg or "",
        rec.files_touched,
        rec.branch or "",
        notable_cmds_first_20,
    ], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_trivial(rec: SessionRecord) -> bool:
    """Trivial sessions skip the Haiku call and get a heuristic summary."""
    return (rec.user_msg_count < TRIVIAL_USER_MSG_THRESHOLD or
            rec.duration_s < TRIVIAL_DURATION_S)


def heuristic_summary(rec: SessionRecord) -> str:
    """Concrete placeholder for trivial sessions; surfaces in search results."""
    head = (rec.first_user_msg or "(no user message)")[:120]
    return (f"Short session ({rec.user_msg_count} messages, "
            f"{rec.duration_s}s) — first user message: {head}")

"""Pane -> Claude transcript resolver. Maps a tmux pane to the SPECIFIC Claude
session running in it, then parses that session's JSONL into turn messages.

cwd alone can't identify the session: many panes share one cwd (several Claude
sessions in the same repo), so newest-mtime-in-cwd returns the same transcript
for all of them. Resolution is live-first (session_status reads the running
claude's own ~/.claude/sessions/<pid>.json — see session_id_for_pane for why),
then the pane_sessions row published by pane_session_hook.py into periscope.db —
the hook reads session_id from Claude's hook payload (current + authoritative,
unlike inherited env which cross-contaminates from tool/subagent subprocesses).
Falls back to newest-mtime-in-cwd when neither knows the pane's session (hook
not yet installed, or the pane predates the first SessionStart firing).

Imports only periscope.* / history.* — never `from server import`."""

import json
import re
import threading
from pathlib import Path

import periscope.activity as activity
import periscope.session_status as session_status
from history.search import messages_from_jsonl
from periscope.tmux import tmux


def session_id_for_pane(pane_id: str) -> str | None:
    """The pane's Claude session id, or None when nothing knows it.

    The LIVE session file (~/.claude/sessions/<pid>.json, read via
    session_status) wins over the recorded pane_sessions row, because the row
    goes stale by design: Claude mints a NEW session id when a conversation is
    resumed or compacted, carrying the history into a fresh transcript, and the
    hook that records it does not always fire for the successor. A pane left
    pointing at its superseded id cost a real conversation — `move-account`
    resumed the pre-rotation transcript and landed ~18h back, 35 user turns
    behind the live one (2026-07-30/31). Don't "simplify" this back to a single
    DB read: the running process is the only source that can't lag a rotation.

    The row remains the fallback for panes with no live claude (exited, no
    session file, pid not a live claude) — that is the only reason it is still
    consulted."""
    return session_status.live_session_id_for_pane(pane_id) or activity.get_pane_session(pane_id)


def jsonl_for_session(session_id: str):
    """The <session_id>.jsonl anywhere under ~/.claude/projects, or None. Search
    by glob, NOT by encoding the pane's current cwd: Claude keys the JSONL dir on
    the cwd the session STARTED in, so a pane that has since `cd`'d (e.g. into a
    worktree) has its transcript under a different encoded dir. session ids are
    globally unique, so a glob finds the one true file regardless."""
    matches = list(activity._PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def jsonl_for_session_prefix(prefix: str):
    """Like jsonl_for_session but matches by id PREFIX. bg commander job ids are
    claude's SHORT session id (the 8-hex `backgrounded · <id>` stdout token),
    while the JSONL is named with the full uuid — the short id is its prefix."""
    matches = list(activity._PROJECTS_DIR.glob(f"*/{prefix}*.jsonl"))
    return matches[0] if matches else None


# ── last_reply: what Claude last said, for a done banner (THI-416) ───────
# The screen can't say it: the footer's statusline, the turn's `✻ Brewed for
# 42s` line and tool results all passed for the reply. The transcript holds it
# as the newest assistant text, a few KB from the end under Claude's
# bookkeeping rows (turn_duration, artifact-autoreact-ledger, mode, …).
_REPLY_CHUNK = 64 * 1024
# Bounds the read for a turn that ended in a long run of tool calls.
_REPLY_REACH = 1024 * 1024
# The banner shows the first sentence, clipped at 140; this rides every poll.
_REPLY_CHARS = 400
# sid -> (path, size, mtime_ns, reply). A transcript never moves, so the glob
# runs once per session; a done pane's file sits still between polls, so the
# read runs once per turn. Cleared past _REPLY_SESSIONS (one row per session
# the server has seen finish).
_reply_cache: dict[str, tuple[Path, int, int, str | None]] = {}
_reply_lock = threading.Lock()
_REPLY_SESSIONS = 256

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,}).*?^\s*\1[^\n]*$", re.MULTILINE | re.DOTALL)
_HEADING_RE = re.compile(r"^\s*#{1,6}\s.*$", re.MULTILINE)
_LINE_MARK_RE = re.compile(r"^\s*(?:>\s?|[-*+]\s+|\d+[.)]\s+)", re.MULTILINE)
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
# `**` and backticks only: `__`/`_`/`*` also sit inside identifiers.
_EMPHASIS_RE = re.compile(r"\*\*|`+")


def _message_text(msg) -> str:
    """An event's text, "" for a tool call or result, thinking, or no message."""
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and b.get("text"))


def _plain(md: str) -> str:
    """A reply's markdown as one line of prose: a banner shows it raw, where a
    link's URL would eat the 140 characters. Headings and code blocks go; they
    are never the sentence."""
    text = _FENCE_RE.sub("", md)
    text = _HEADING_RE.sub("", text)
    text = _LINE_MARK_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _EMPHASIS_RE.sub("", text)
    return " ".join(text.split())[:_REPLY_CHARS]


def last_reply(path) -> str | None:
    """The newest assistant text in transcript `path`, as plain prose; None when
    a prompt came after it (the new turn has said nothing yet), none sits in the
    last _REPLY_REACH bytes, or the file can't be read. Meta and sidechain
    events are skipped: a channel push, a subagent's words."""
    reach = _REPLY_CHUNK
    try:
        with open(path, "rb") as f:
            size = f.seek(0, 2)
            while True:
                start = max(0, size - reach)
                f.seek(start)
                rows = f.read(size - start).split(b"\n")
                for row in reversed(rows[1:] if start else rows):  # rows[0] starts mid-row
                    try:
                        ev = json.loads(row)
                    except ValueError:
                        continue
                    if not isinstance(ev, dict) or ev.get("isMeta") or ev.get("isSidechain"):
                        continue
                    text = _message_text(ev.get("message"))
                    if text.strip() and ev.get("type") == "assistant":
                        return _plain(text) or None
                    if text.strip() and ev.get("type") == "user":
                        return None
                if not start or reach >= _REPLY_REACH:
                    return None
                reach = min(reach * 4, _REPLY_REACH)
    except OSError:
        return None


def last_reply_for_session(session_id: str | None) -> str | None:
    """last_reply of the session's transcript, re-read only when it changes."""
    if not session_id:
        return None
    with _reply_lock:
        hit = _reply_cache.get(session_id)
    path = hit[0] if hit else jsonl_for_session(session_id)
    if path is None:
        return None
    try:
        st = path.stat()
    except OSError:
        with _reply_lock:
            _reply_cache.pop(session_id, None)
        return None
    if hit and hit[1:3] == (st.st_size, st.st_mtime_ns):
        return hit[3]
    reply = last_reply(path)
    with _reply_lock:
        if len(_reply_cache) >= _REPLY_SESSIONS:
            _reply_cache.clear()
        _reply_cache[session_id] = (path, st.st_size, st.st_mtime_ns, reply)
    return reply


# Delivered channel pushes render into the recipient's context as a meta user
# turn opening with this marker (see channels.emit_channel_event).
CHANNEL_MARKER = '<channel source="periscope"'


def channel_messages_from_jsonl(path: str) -> list[dict]:
    """The periscope channel pushes that ACTUALLY landed in this transcript.

    `messages_from_jsonl` drops every `isMeta` event, and a delivered channel
    push is exactly that — so the one thing a sender peeks to verify is
    invisible to peek by construction. A sender saw no channel block, concluded
    `send_to` was silently dropping, spent 40 minutes on it, filed a wrong bug,
    and re-sent the same directive four times. This surfaces them instead.

    Presence here is the delivery RECEIPT: the block is written by the
    recipient's own Claude when it processes the notification, so it cannot
    appear unless the message was received.
    """
    from history.jsonl import parse_jsonl

    out: list[dict] = []
    try:
        events = list(parse_jsonl(path))
    except OSError:
        # Rotated or removed between resolution and read. The transcript is the
        # caller's real payload — degrade to "no receipts" rather than failing
        # the whole peek over the receipt annotation.
        return out
    for ev in events:
        if ev.raw.get("isMeta") is not True or ev.type != "user":
            continue
        text = ev.user_text or ""
        if not text.startswith(CHANNEL_MARKER):
            continue
        out.append(
            {
                "role": "channel",
                "uuid": ev.uuid,
                "ts_ms": ev.ts_ms,
                "text": text,
            }
        )
    return out


def get_turns_for_pane(session: str, index: int) -> dict | None:
    """Resolve a pane (session:index) to its transcript messages.

    Uses the pane's recorded session id to read exactly that session's JSONL —
    correct even when several panes share a cwd. Falls back to the newest
    matching JSONL in the pane's cwd when no session id is recorded. Returns
    {session_id, jsonl_path, messages} or None."""
    target = f"{session}:{index}"
    try:
        meta = tmux("display-message", "-t", target, "-p", "#{pane_id}\t#{pane_current_path}").strip()
    except Exception:
        return None
    pane_id, _, cwd = meta.partition("\t")
    if not cwd:
        return None

    sid = session_id_for_pane(pane_id)
    jsonl = jsonl_for_session(sid) if sid else None
    if jsonl is None:
        jsonl = activity.live_transcript_for(cwd)
    if jsonl is None:
        return None
    return {
        "session_id": jsonl.stem,
        "jsonl_path": str(jsonl),
        "messages": messages_from_jsonl(str(jsonl)),
    }

"""Pane -> Claude transcript resolver. Maps a tmux pane to the SPECIFIC Claude
session running in it, then parses that session's JSONL into turn messages.

cwd alone can't identify the session: many panes share one cwd (several Claude
sessions in the same repo), so newest-mtime-in-cwd returns the same transcript
for all of them. The authoritative per-pane signal is CLAUDE_CODE_SESSION_ID
(the JSONL filename), which Claude Code exports — alongside TMUX_PANE — into its
child processes' environment; we recover it by scanning process env for the
pane's TMUX_PANE.

A tmux pane runs one Claude session for its life, so pane_id -> session_id is
resolved once and cached; subsequent polls are cache hits with no `ps` scan
(the cache entry is dropped only when its JSONL disappears — a rotated/cleared
session — so the next poll re-scans). Falls back to newest-mtime-in-cwd when no
session id is found. Imports only periscope.* / history.* — never
`from server import`."""
import re
import subprocess

import periscope.activity as activity
from history.search import messages_from_jsonl
from periscope.tmux import tmux

# pane_id ("%73") -> session_id (JSONL stem). See module docstring for lifetime.
_PANE_SESSION: dict[str, str] = {}

_SID_RE = re.compile(r"CLAUDE_CODE_SESSION_ID=([0-9a-fA-F-]{36})")


def _scan_session_ids(pane_id: str) -> list[str]:
    """Scan process env for the Claude session id(s) advertised by processes
    whose TMUX_PANE matches `pane_id`. One `ps eww` call (env is appended to each
    line). Usually one id; a pane spawned by another Claude (spawn_claude / a
    team worker) also carries its parent's inherited CLAUDE_CODE_SESSION_ID, so
    several can appear — the caller disambiguates by cwd + mtime."""
    pane_re = re.compile(rf"TMUX_PANE={re.escape(pane_id)}(?:\s|$)")
    try:
        out = subprocess.run(
            ["ps", "eww", "-A"], capture_output=True, text=True, timeout=4
        ).stdout
    except Exception:
        return []
    found: list[str] = []
    for line in out.splitlines():
        if pane_re.search(line):
            m = _SID_RE.search(line)
            if m and (sid := m.group(1).lower()) not in found:
                found.append(sid)
    return found


def _jsonl_in_cwd(cwd: str, session_id: str):
    """Path to <session_id>.jsonl under cwd's encoded projects dir, or None."""
    p = activity._PROJECTS_DIR / activity._encode_cwd(cwd) / f"{session_id}.jsonl"
    return p if p.is_file() else None


def session_id_for_pane(pane_id: str, cwd: str) -> str | None:
    """Cached pane_id -> session_id. Resolved once per pane. When a pane's env
    yields several session ids (inherited-parent case), pick the one whose JSONL
    lives in THIS pane's cwd and is most recently written — the pane's own live
    session; this also rejects an inherited session whose JSONL is in the
    parent's (different) cwd."""
    if not pane_id:
        return None
    sid = _PANE_SESSION.get(pane_id)
    if sid is not None:
        return sid
    cands = _scan_session_ids(pane_id)
    if not cands:
        return None
    if len(cands) == 1:
        sid = cands[0]
    else:
        def _mtime(s: str) -> float:
            p = _jsonl_in_cwd(cwd, s)
            try:
                return p.stat().st_mtime if p else -1.0
            except OSError:
                return -1.0
        sid = max(cands, key=_mtime)
    _PANE_SESSION[pane_id] = sid
    return sid


def get_turns_for_pane(session: str, index: int) -> dict | None:
    """Resolve a pane (session:index) to its transcript messages.

    Uses the pane's specific Claude session id (env signal, cached) to read
    exactly that session's JSONL — correct even when several panes share a cwd.
    Falls back to the newest matching JSONL in the pane's cwd when no session id
    is found. Returns {session_id, jsonl_path, messages} or None."""
    target = f"{session}:{index}"
    try:
        meta = tmux("display-message", "-t", target, "-p",
                    "#{pane_id}\t#{pane_current_path}").strip()
    except Exception:
        return None
    pane_id, _, cwd = meta.partition("\t")
    if not cwd:
        return None

    sid = session_id_for_pane(pane_id, cwd)
    jsonl = _jsonl_in_cwd(cwd, sid) if sid else None
    if sid and jsonl is None:
        # Cached session's JSONL is gone (cleared/rotated/resumed) — forget it so
        # the next poll re-scans for the pane's current session.
        _PANE_SESSION.pop(pane_id, None)
    if jsonl is None:
        jsonl = activity.live_transcript_for(cwd)
    if jsonl is None:
        return None
    return {
        "session_id": jsonl.stem,
        "jsonl_path": str(jsonl),
        "messages": messages_from_jsonl(str(jsonl)),
    }

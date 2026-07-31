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
    return (session_status.live_session_id_for_pane(pane_id)
            or activity.get_pane_session(pane_id))


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


def get_turns_for_pane(session: str, index: int) -> dict | None:
    """Resolve a pane (session:index) to its transcript messages.

    Uses the pane's recorded session id to read exactly that session's JSONL —
    correct even when several panes share a cwd. Falls back to the newest
    matching JSONL in the pane's cwd when no session id is recorded. Returns
    {session_id, jsonl_path, messages} or None."""
    target = f"{session}:{index}"
    try:
        meta = tmux("display-message", "-t", target, "-p",
                    "#{pane_id}\t#{pane_current_path}").strip()
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

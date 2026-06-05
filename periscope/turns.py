"""Pane -> Claude transcript resolver. Maps a tmux pane to the SPECIFIC Claude
session running in it, then parses that session's JSONL into turn messages.

cwd alone can't identify the session: many panes share one cwd (several Claude
sessions in the same repo), so newest-mtime-in-cwd returns the same transcript
for all of them. The pane -> session id mapping is published by pane_session_hook.py
into the pane_sessions table in periscope.db — the hook reads session_id from
Claude's hook payload (current + authoritative, unlike inherited env which
cross-contaminates from tool/subagent subprocesses). Falls back to
newest-mtime-in-cwd when a pane has no recorded session (hook not yet
installed, or the pane predates the first SessionStart firing).

Imports only periscope.* / history.* — never `from server import`."""
import periscope.activity as activity
from history.search import messages_from_jsonl
from periscope.tmux import tmux


def session_id_for_pane(pane_id: str) -> str | None:
    """The pane's Claude session id, or None if no row in pane_sessions."""
    return activity.get_pane_session(pane_id)


def _jsonl_for_session(session_id: str):
    """The <session_id>.jsonl anywhere under ~/.claude/projects, or None. Search
    by glob, NOT by encoding the pane's current cwd: Claude keys the JSONL dir on
    the cwd the session STARTED in, so a pane that has since `cd`'d (e.g. into a
    worktree) has its transcript under a different encoded dir. session ids are
    globally unique, so a glob finds the one true file regardless."""
    matches = list(activity._PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
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
    jsonl = _jsonl_for_session(sid) if sid else None
    if jsonl is None:
        jsonl = activity.live_transcript_for(cwd)
    if jsonl is None:
        return None
    return {
        "session_id": jsonl.stem,
        "jsonl_path": str(jsonl),
        "messages": messages_from_jsonl(str(jsonl)),
    }

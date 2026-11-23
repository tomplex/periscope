"""Idle close: each night, close the Claude panes idle for a set number of
weekdays, to free their memory. A closed pane keeps a `closed_panes` row, from
which Resume brings it back with its pid, name, track and wrapper profile.

Spec: docs/superpowers/specs/2026-11-19-idle-close-design.md.
"""

from periscope import activity, session_status
from periscope.turns import jsonl_for_session


def came_back(record: activity.ClosedPane, live_pids: set[str]) -> bool:
    """A live window wears the record's pid, or a live Claude runs its session.
    Both, because a typed `claude --resume` can keep or change the session id."""
    return record["pid"] in live_pids or session_status.claude_proc_for(record["session_id"]) is not None


def prune_closed(live_pids: set[str]) -> int:
    """Delete the records that Resume can no longer use: Claude deleted the
    transcript, or the session came back another way. Returns how many."""
    dropped = 0
    for record in activity.closed_panes():
        if jsonl_for_session(record["session_id"]) is None or came_back(record, live_pids):
            activity.delete_closed_pane(record["session_id"])
            dropped += 1
    return dropped

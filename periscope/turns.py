"""Stateless turn-transcript resolver: a pane's cwd -> its live Claude
transcript messages. No cache (full-resend per poll; see the design spec for
why a since_ts/parse cache is deferred). Imports only periscope.* / history.*
— never `from server import`."""
from history.search import messages_from_jsonl
from periscope.activity import live_transcript_for


def get_turns_for_pane(cwd: str) -> dict | None:
    """Resolve `cwd` to its newest matching Claude JSONL and parse it.

    Returns {session_id, jsonl_path, messages} or None when no live transcript
    matches (history not indexed / no JSONL / cwd has no recorded match)."""
    jsonl = live_transcript_for(cwd)
    if jsonl is None:
        return None
    return {
        "session_id": jsonl.stem,
        "jsonl_path": str(jsonl),
        "messages": messages_from_jsonl(str(jsonl)),
    }

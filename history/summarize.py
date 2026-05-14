"""Generate session summaries via the Anthropic SDK with forced tool-use."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from .extract import SessionRecord

log = logging.getLogger(__name__)

SUMMARIZE_TOOL = {
    "name": "save_session_summary",
    "description": "Persist a summary and topic tags for an indexed Claude Code session.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "2-3 sentences in past tense. Concrete: file names, error messages, "
                    "decisions made, what was actually fixed/built. Describe the work done, "
                    "not what the user asked for."
                ),
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 5,
                "description": "3-5 lowercase tags: project, technology, area, action.",
            },
        },
        "required": ["summary", "tags"],
    },
}

SUMMARIZE_SYSTEM_PROMPT = (
    "You summarize Claude Code coding sessions for a search index. "
    "Output is consumed by a developer searching their own history later. "
    "Bias toward concrete specifics (file names, error messages, decisions) "
    "over generic descriptions. Always call save_session_summary."
)

# Token budget for user messages in the prompt body. Crude char proxy: ~4 chars/token.
MAX_USER_MSGS_CHARS = 6000 * 4


@dataclass
class SummaryResult:
    summary: str
    tags: list[str]
    model: str


def build_summary_prompt(rec: SessionRecord) -> str:
    """Construct the per-session prompt body. The stable framing is in
    SUMMARIZE_SYSTEM_PROMPT (cached); only this varies per call."""
    files = json.loads(rec.files_touched)[:15]
    cmds = json.loads(rec.notable_cmds)[:10]
    user_msgs = rec.user_messages_blob
    if len(user_msgs) > MAX_USER_MSGS_CHARS:
        # Keep head and tail; drop the middle.
        head = user_msgs[: MAX_USER_MSGS_CHARS // 2]
        tail = user_msgs[-MAX_USER_MSGS_CHARS // 2:]
        user_msgs = f"{head}\n\n[... truncated ...]\n\n{tail}"
    final_msg = (rec.final_assistant_msg or "").strip()
    return "\n".join([
        "SESSION:",
        f"  project: {rec.project_path}",
        f"  branch: {rec.branch or '(none)'}",
        f"  duration: {rec.duration_s // 60} min",
        f"  files touched: {files}",
        f"  notable commands: {cmds}",
        "",
        "USER MESSAGES (concatenated, may be truncated):",
        user_msgs,
        "",
        "FINAL ASSISTANT MESSAGE (outcome signal, truncated):",
        final_msg,
        "",
        "Call save_session_summary with concrete details from this session.",
    ])


def call_summarizer(client, rec: SessionRecord, *,
                    model: str = "claude-haiku-4-5",
                    max_retries: int = 1) -> SummaryResult | None:
    """Single Haiku call with forced tool-use. Returns None on persistent failure
    (caller stores summary=NULL and logs)."""
    body = build_summary_prompt(rec)
    last_failure: str | None = None
    for attempt in range(max_retries + 1):
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=512,
                # cache_control here may be silently ignored: Anthropic prompt
                # caching has a ~1024-token minimum prefix and our system+tools
                # together fall short. Task 13 verifies actual caching via
                # response.usage.cache_*_input_tokens — if cache_read is 0 we
                # either expand the prefix or drop this marker.
                system=[{
                    "type": "text",
                    "text": SUMMARIZE_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=[SUMMARIZE_TOOL],
                tool_choice={"type": "tool", "name": SUMMARIZE_TOOL["name"]},
                messages=[{"role": "user", "content": body}],
            )
        except Exception as e:
            last_failure = f"API error: {e!r}"
            log.warning("summarize: API error on attempt %d for %s: %s",
                        attempt + 1, rec.session_id, e)
            continue
        for block in getattr(msg, "content", []):
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == SUMMARIZE_TOOL["name"]:
                data = getattr(block, "input", None) or {}
                summary = data.get("summary")
                tags = data.get("tags")
                if isinstance(summary, str) and isinstance(tags, list):
                    # Normalize + filter empty tags (the schema's minItems=3 makes
                    # this rare, but a stray empty string would still index).
                    norm_tags = [s for s in (str(t).lower().strip() for t in tags) if s]
                    return SummaryResult(summary=summary.strip(),
                                          tags=norm_tags,
                                          model=model)
        stop_reason = getattr(msg, "stop_reason", "?")
        last_failure = f"no valid tool_use block (stop_reason={stop_reason})"
        log.warning("summarize: %s on attempt %d for %s",
                    last_failure, attempt + 1, rec.session_id)
    # All attempts exhausted — always emit a single ERROR so backfill summaries
    # of failures are countable via `grep ERROR` on the log stream.
    log.error("summarize: gave up after %d attempts for %s — %s",
              max_retries + 1, rec.session_id, last_failure)
    return None

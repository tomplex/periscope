"""Generate session summaries via the Anthropic SDK with forced tool-use."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .extract import SessionRecord

log = logging.getLogger(__name__)

_OUTCOMES = {"shipped", "partial", "abandoned", "explored", "blocked"}
_CATEGORIES = {"feature", "bugfix", "refactor", "debugging",
               "research", "ops", "docs", "review"}

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
            "outcome": {
                "type": "string",
                "enum": ["shipped", "partial", "abandoned", "explored", "blocked"],
                "description": (
                    "How the session ended. shipped: work landed/committed. "
                    "partial: real progress, left unfinished. abandoned: started "
                    "then dropped. explored: investigation or Q&A, no code change "
                    "intended. blocked: stuck on an external problem."
                ),
            },
            "category": {
                "type": "string",
                "enum": ["feature", "bugfix", "refactor", "debugging",
                         "research", "ops", "docs", "review"],
                "description": "The primary kind of work in the session.",
            },
            "notable": {
                "type": "boolean",
                "description": (
                    "true if the session is substantial or worth revisiting; "
                    "false for routine, trivial, or false-start work."
                ),
            },
            "topics": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 4,
                "description": (
                    "1-4 canonical lowercase topic tags — prefer the project "
                    "name or a broad tech/area term; singular, deduplicated."
                ),
            },
        },
        "required": ["summary", "tags", "outcome", "category", "notable", "topics"],
    },
}

SUMMARIZE_SYSTEM_PROMPT = (
    "You summarize and classify Claude Code coding sessions for a search "
    "index. Output is consumed by a developer searching their own history "
    "later. Bias toward concrete specifics (file names, error messages, "
    "decisions) over generic descriptions. Also classify the session's "
    "outcome, category, notability, and topics per the tool schema — the "
    "final assistant message is the strongest outcome signal. Always call "
    "save_session_summary."
)

# Token budget for user messages in the prompt body. Crude char proxy: ~4 chars/token.
MAX_USER_MSGS_CHARS = 6000 * 4


@dataclass
class SummaryResult:
    summary: str
    tags: list[str]
    model: str
    outcome: str | None = None
    category: str | None = None
    notable: bool = False
    topics: list[str] = field(default_factory=list)


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
                    max_retries: int = 3) -> SummaryResult | None:
    """Single Haiku call with forced tool-use. Returns None on persistent failure
    (caller stores summary=NULL and logs)."""
    body = build_summary_prompt(rec)
    last_failure: str | None = None
    for attempt in range(max_retries + 1):
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=1024,
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
            # If the API surfaced a 429 even after the SDK's own backoff,
            # sleep longer before our wrapper retries — bursting another call
            # immediately would just re-trigger the same limit.
            if "429" in str(e) or "rate_limit" in str(e).lower():
                import time as _t
                sleep_s = 30 * (attempt + 1)  # 30s, 60s, 90s...
                log.info("summarize: rate-limited, sleeping %ds before retry", sleep_s)
                _t.sleep(sleep_s)
            continue
        # Walk the response. Track WHY parsing fell through so the error log
        # tells us whether the model didn't call the tool, or called it but
        # with malformed/truncated input.
        found_block = False
        bad_block_reason = None
        for block in getattr(msg, "content", []):
            if getattr(block, "type", None) != "tool_use":
                continue
            if getattr(block, "name", None) != SUMMARIZE_TOOL["name"]:
                bad_block_reason = f"unexpected tool name {getattr(block, 'name', None)!r}"
                continue
            found_block = True
            data = getattr(block, "input", None)
            if not isinstance(data, dict):
                bad_block_reason = f"input is {type(data).__name__}, expected dict"
                continue
            summary = data.get("summary")
            tags = data.get("tags")
            if not isinstance(summary, str):
                bad_block_reason = f"summary is {type(summary).__name__}, expected str (keys={list(data.keys())})"
                continue
            if not isinstance(tags, list):
                bad_block_reason = f"tags is {type(tags).__name__}, expected list"
                continue
            # Normalize + filter empty tags
            norm_tags = [s for s in (str(t).lower().strip() for t in tags) if s]
            # Facets — unknown enum values degrade to None, not a crash.
            outcome = data.get("outcome")
            if outcome not in _OUTCOMES:
                outcome = None
            category = data.get("category")
            if category not in _CATEGORIES:
                category = None
            notable = bool(data.get("notable"))
            raw_topics = data.get("topics")
            topics = ([s for s in (str(t).lower().strip() for t in raw_topics) if s]
                      if isinstance(raw_topics, list) else [])
            return SummaryResult(summary=summary.strip(),
                                  tags=norm_tags,
                                  model=model,
                                  outcome=outcome,
                                  category=category,
                                  notable=notable,
                                  topics=topics)
        stop_reason = getattr(msg, "stop_reason", "?")
        if not found_block:
            last_failure = f"no tool_use block in response (stop_reason={stop_reason})"
        elif bad_block_reason:
            last_failure = f"tool_use block malformed: {bad_block_reason} (stop_reason={stop_reason})"
        else:
            last_failure = f"tool_use block found but skipped (stop_reason={stop_reason})"
        log.warning("summarize: %s on attempt %d for %s",
                    last_failure, attempt + 1, rec.session_id)
    # All attempts exhausted — always emit a single ERROR so backfill summaries
    # of failures are countable via `grep ERROR` on the log stream.
    log.error("summarize: gave up after %d attempts for %s — %s",
              max_retries + 1, rec.session_id, last_failure)
    return None

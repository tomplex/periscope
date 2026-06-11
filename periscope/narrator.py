"""Narrator: semantic per-pane status line + auto-rename on divergence.

Once per activity-worker tick (prod only), decide which Claude panes need
a fresh one-line status, generate each via one Haiku call, persist to the
pane_status table, and apply guarded tmux window renames. The decision
core below is pure (no DB / tmux / API) so the whole regeneration policy
is unit-testable with zero fixtures; tick() is the only place IO happens.

Tick-to-tick state lives in pane_status (activity.py owns the schema) so
statuses survive restarts. The only in-process state is the one-shot
disabled latch.

Import discipline: this module imports activity; activity calls
narrator.tick via a function-level import (cycle avoidance).
"""

import json
import os
import re
import time
from dataclasses import dataclass, replace
from typing import Literal

import periscope.activity as activity
from periscope.activity import PaneStatusRow
# Imported into this namespace so tests monkeypatch narrator.claude_complete,
# narrator.tmux, etc. — the tests/test_activity.py pattern.
from periscope.git_pr import cached_git_state, cached_pr_state
from periscope.log import log
from periscope.rename_ai import (
    RENAME_RULES, claude_complete, transcript_summary_from_path,
)
from periscope.tmux import tmux

MIN_INTERVAL_S = 90
MAX_PER_TICK = 5
RENAME_COOLDOWN_S = 1800
STATUS_MAX_LEN = 72

Regen = Literal["session_switch", "grew", "first_sight"]

# 1-3 lowercase dash-words — the build_rename_prompt taste rules, enforced.
_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+){0,2}$")

_enabled_checked: bool | None = None  # None = not yet checked


def _enabled() -> bool:
    """One-shot ANTHROPIC_API_KEY check. get_anthropic raises per call;
    the narrator instead disables itself with exactly one log line so a
    keyless dashboard works exactly as before, silently."""
    global _enabled_checked
    if _enabled_checked is None:
        _enabled_checked = bool(os.environ.get("ANTHROPIC_API_KEY"))
        if not _enabled_checked:
            log.info("narrator disabled: ANTHROPIC_API_KEY not set")
    return _enabled_checked


@dataclass(frozen=True)
class NarratorResult:
    status: str
    rename: str | None


def should_regenerate(row: PaneStatusRow | None, *, session_id: str,
                      jsonl_size: int, now: int) -> Regen | None:
    """Why (if at all) this pane needs a fresh status. The reason matters:
    on "session_switch" the shell resets jsonl_size/renamed_at (a recycled
    pane id must not inherit the previous occupant's cooldown)."""
    if row is None:
        return "first_sight"
    if now - row.generated_at < MIN_INTERVAL_S:
        return None
    # A placeholder row (manual rename before any generation) has
    # session_id=None — that is NOT a session switch; resetting would wipe
    # the cooldown the stamp just wrote. It regenerates below: its
    # jsonl_size=0 differs from any real transcript.
    if row.session_id is not None and session_id != row.session_id:
        return "session_switch"
    if jsonl_size != row.jsonl_size:
        return "grew"
    return None


def pick_regenerations(candidates: list[tuple[int, str]], *,
                       cap: int = MAX_PER_TICK) -> list[str]:
    """candidates: (generated_at, pane_id). Oldest first, at most `cap` —
    a 25-pane storm degrades to slightly-stale statuses, never a Haiku
    bill spike."""
    return [pane_id for _, pane_id in sorted(candidates)[:cap]]


def parse_response(raw: str) -> NarratorResult | None:
    """Model output is an external boundary — the ONLY defensive parsing
    in this module. None means: keep the previous status, retry next tick
    naturally. A non-string rename drops just the rename, not the status."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned,
                         flags=re.MULTILINE)
    try:
        d = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    status = d.get("status")
    if not isinstance(status, str):
        return None
    status = status.strip()
    if not status or len(status) > STATUS_MAX_LEN:
        return None
    rename = d.get("rename")
    rename = rename.strip() or None if isinstance(rename, str) else None
    return NarratorResult(status=status, rename=rename)


def rename_decision(suggestion: str | None, *, current_name: str,
                    row: PaneStatusRow | None, now: int) -> str | None:
    """Code-side guards on the model's rename suggestion. The failure mode
    to fear is name churn, not staleness — every guard errs toward None."""
    if not suggestion or suggestion == current_name:
        return None
    if len(suggestion) > 25 or not _NAME_RE.match(suggestion):
        return None
    if (row is not None and row.renamed_at is not None
            and now - row.renamed_at < RENAME_COOLDOWN_S):
        return None
    return suggestion


def is_external_rename(row: PaneStatusRow, current_name: str) -> bool:
    """Someone renamed the window since the narrator last looked (human,
    tmux-native, or another route that didn't stamp). seen_name is updated
    at every generation AND at narrator-apply time, so any divergence is
    external."""
    return row.seen_name is not None and current_name != row.seen_name


def build_narrator_prompt(*, window_name: str, branch: str | None,
                          pr: int | None, cwd: str, signals: dict) -> str:
    """One pane's status+rename prompt. The rename half splices
    rename_ai.RENAME_RULES so taste can't drift from the manual surface."""
    lines = [
        "You watch one developer terminal pane running Claude Code and keep",
        "a one-line status for a dashboard of many such panes.",
        "",
        "Write the status under these rules:",
        f"  - Max {STATUS_MAX_LEN} characters.",
        "  - Present-progressive, e.g. 'fixing flaky reconcile test in tmux_mirror'.",
        "  - Concept-level: what is being accomplished, not which file is open.",
        "    e.g. 'migrating usage scrape to OAuth endpoint', not 'editing usage.py'.",
        "  - No terminal/pane/window/tmux jargon.",
        "  - Describe the most recent WORK even if the pane has since gone quiet —",
        "    the dashboard already shows busy/idle; never mention busy/idle state.",
        "",
        "Also decide whether the window deserves a NEW NAME. Suggest one ONLY",
        "when the work has meaningfully diverged from the current name. Rules:",
        *[f"  {r}" for r in RENAME_RULES],
        "  - Most calls should return null for rename — name churn is worse than",
        "    a slightly stale name. Example: current_name='fs-liveness', recent",
        "    work is still feature-store liveness checks → return",
        '    {"status": "...", "rename": null}.',
        "",
        f"current_name: {window_name}",
        f"cwd: {cwd}",
    ]
    if branch:
        lines.append(f"branch: {branch}" + (f", PR #{pr}" if pr else ""))
    prompts = signals.get("recent_user_prompts") or []
    if prompts:
        lines.append("recent user prompts (oldest→newest):")
        lines += [f"  {i}. {p}" for i, p in enumerate(prompts, 1)]
    tool_calls = signals.get("recent_tool_calls") or []
    if tool_calls:
        lines.append("recent tool calls (oldest→newest):")
        lines += [f"  - {tc}" for tc in tool_calls]
    files = signals.get("files_touched") or []
    if files:
        lines.append(f"files touched: {', '.join(files)}")
    lines += [
        "",
        'Return ONLY a JSON object: {"status": "<status line>",'
        ' "rename": null | "<new-name>"}.',
        "No markdown fences, no commentary, just the JSON object.",
    ]
    return "\n".join(lines)

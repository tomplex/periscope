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
from pathlib import Path
from typing import Literal

import periscope.activity as activity
from periscope.activity import PaneStatusRow

# Imported into this namespace so tests monkeypatch narrator.claude_complete,
# narrator.tmux, etc. — the tests/test_activity.py pattern.
from periscope.git_pr import cached_git_state, cached_pr_state
from periscope.log import log
from periscope.rename_ai import (
    RENAME_RULES,
    claude_complete,
    transcript_summary_from_path,
)
from periscope.tmux import tmux
from periscope.turns import jsonl_for_session

MIN_INTERVAL_S = 90
MAX_PER_TICK = 5
RENAME_COOLDOWN_S = 1800
STATUS_MAX_LEN = 72
RAIL_MAX_LEN = 28
GOAL_MAX_LEN = 120
ARC_MAX = 6   # status lines kept as thread-divergence evidence

Regen = Literal["session_switch", "size_changed", "first_sight"]

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
    rail: str | None = None
    goal: str | None = None   # None = model gave none; caller carries previous


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
        return "size_changed"
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
    naturally. A non-string rename drops just the rename, and a bad rail
    drops just the rail — never the status."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned,
                         flags=re.MULTILINE)
    try:
        d = json.loads(cleaned)
    except ValueError:
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
    rail = d.get("rail")
    if isinstance(rail, str):
        rail = rail.strip()
        if not rail or len(rail) > RAIL_MAX_LEN:
            rail = None
    else:
        rail = None
    goal = d.get("goal")
    if isinstance(goal, str):
        goal = goal.strip()
        if not goal or len(goal) > GOAL_MAX_LEN:
            goal = None
    else:
        goal = None
    return NarratorResult(status=status, rename=rename, rail=rail, goal=goal)


def update_arc(history: list[dict], status: str, now: int, *,
               cap: int = ARC_MAX) -> list[dict]:
    """Append this tick's status to the thread arc (newest last), skipping a
    consecutive duplicate so a quiet stretch re-emitting the same line never
    crowds out earlier phases. Keeps only the last `cap` entries."""
    arc = list(history)
    if not arc or arc[-1].get("s") != status:
        arc.append({"t": now, "s": status})
    return arc[-cap:]


def load_arc(raw: str | None) -> list[dict]:
    """Parse a stored history column. Model/DB boundary — any malformed value
    degrades to an empty arc, never raises."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and "s" in e]


def _fmt_age(seconds: int) -> str:
    """Coarse relative age for the thread-arc lines in the prompt."""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


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
                          pr: int | None, cwd: str, signals: dict,
                          workspace_name: str | None = None,
                          sibling_names: list[str] | None = None,
                          goal: str | None = None,
                          arc: list[dict] | None = None, now: int = 0) -> str:
    """One pane's status+rename prompt. The rename half splices
    rename_ai.RENAME_RULES so taste can't drift from the manual surface.
    `goal` (the persistent thread carried across ticks) and `arc` (recent
    status lines) are the memory that keeps the name at goal-altitude instead
    of drifting to whatever step is happening right now."""
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
        "Also write `rail`: an ultra-short cut of the status for a narrow",
        "sidebar row, rendered directly under the window name. Rules:",
        f"  - Max {RAIL_MAX_LEN} characters.",
        "  - Lead with the current action, e.g. 'comparing lookup hit rates'.",
        f"  - The name '{window_name}' sits right above it — never repeat that",
        "    name's concept; give the differentiating detail instead.",
        "  - No trailing punctuation or ellipsis. All lowercase.",
        "",
        "Also maintain `goal`: ONE sentence naming the overarching THREAD of",
        "work in this tab — the persistent objective that outlives any single",
        "step. A brainstorm → spec → implementation run on one feature is ONE",
        "goal the whole way through. Rules:",
        f"  - Max {GOAL_MAX_LEN} characters, plain prose (not a tab-name).",
        "  - You are given 'goal so far' below when one exists. Echo it back",
        "    UNCHANGED unless the work has genuinely moved to a different",
        "    objective — not merely a new phase or sub-task of the same one.",
        "  - With no goal yet, infer it from the EARLIEST intent you can see in",
        "    the thread arc and prompts, not the latest action.",
        "",
        "Also decide whether the window deserves a NEW NAME. The name tracks the",
        "GOAL, never the current step. Suggest a rename ONLY when the goal itself",
        "changed — never for a new phase, file, or sub-task within one goal.",
        *[f"  {r}" for r in RENAME_RULES],
        "  - Most calls MUST return rename: null — name churn is worse than a",
        "    slightly stale name. Example: goal is still 'redesign the rail',",
        "    current step is wiring a filter into it → keep the name, rename: null.",
        "",
        f"current_name: {window_name}",
        f"cwd: {cwd}",
    ]
    if goal:
        lines.append(f"goal so far: {goal}")
    if arc:
        lines.append("thread arc so far (oldest→newest):")
        lines += [f"  - {_fmt_age(now - e.get('t', now))}: {e.get('s', '')}"
                  for e in arc]
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
    if workspace_name:
        sibs = ", ".join(n for n in (sibling_names or []) if n) or "(none yet)"
        lines += [
            "",
            f"This pane is part of the workspace GOAL: \"{workspace_name}\".",
            f"Sibling tabs in this workspace: {sibs}.",
            "  - The goal is shared context — do NOT repeat it in the name.",
            "  - Name what distinguishes THIS tab from its siblings.",
        ]
    lines += [
        "",
        'Return ONLY a JSON object: {"status": "<status line>",'
        ' "rail": "<short fragment>", "goal": "<thread sentence>",'
        ' "rename": null | "<new-name>"}.',
        "No markdown fences, no commentary, just the JSON object.",
    ]
    return "\n".join(lines)

# --- impure shell --------------------------------------------------------

def tick(panes: list[tuple[dict, dict]]) -> None:
    """One narrator pass over the worker's (window, parsed) Claude panes.
    Called synchronously from activity._worker_tick (already off-loop).
    Per-pane try/except — one bad pane never starves the rest."""
    if not _enabled():
        return
    now = int(time.time())
    # ONE bulk read of every stored status — the oldest-first cap selection
    # needs them all anyway, and a per-pane SELECT would acquire
    # activity._LOCK N times per tick.
    rows = {r.pane_id: r for r in activity.all_pane_statuses()}
    # Workspace context: pane_id → workspace_id (one bulk read) plus a
    # per-workspace sibling-name index built from this tick's panes, so each
    # tab can be named against its goal + siblings instead of repeating them.
    tag_map = activity.pane_workspace_map()
    from periscope.workspaces import all_workspaces
    ws_names = {k: v["name"] for k, v in all_workspaces().items()}
    siblings: dict[str, list[str]] = {}
    for w, _parsed in panes:
        wid = tag_map.get(w.get("pane_id") or "")
        if wid:
            siblings.setdefault(wid, []).append(w.get("name") or "")
    work: dict[str, tuple[dict, str, Path, int, PaneStatusRow | None, Regen]] = {}
    candidates: list[tuple[int, str]] = []
    for w, _parsed in panes:
        pane_id = w.get("pane_id") or ""
        if not pane_id:
            continue
        try:
            sid = activity.get_pane_session(pane_id)
            if not sid:
                # No hook-recorded session. Deliberately NO cwd fallback:
                # on a shared cwd a wrong-session status is worse than no
                # status, and the hook self-corrects on the next prompt.
                continue
            jsonl = jsonl_for_session(sid)
            if jsonl is None:
                continue
            size = jsonl.stat().st_size
            row = rows.get(pane_id)
            reason = should_regenerate(row, session_id=sid,
                                       jsonl_size=size, now=now)
            if reason is None:
                continue
            work[pane_id] = (w, sid, jsonl, size, row, reason)
            candidates.append((row.generated_at if row else 0, pane_id))
        except Exception:
            log.exception("narrator candidate scan failed for %s", pane_id)
    for pane_id in pick_regenerations(candidates):
        w, sid, jsonl, size, row, reason = work[pane_id]
        wid = tag_map.get(pane_id)
        workspace_name = ws_names.get(wid) if wid else None
        sibling_names = siblings.get(wid) if wid else None
        try:
            _generate(w, pane_id=pane_id, sid=sid, jsonl=jsonl, size=size,
                      row=row, reason=reason, now=now,
                      workspace_name=workspace_name,
                      sibling_names=sibling_names)
        except Exception:
            log.exception("narrator generation failed for %s", pane_id)


def _generate(w: dict, *, pane_id: str, sid: str, jsonl: Path, size: int,
              row: PaneStatusRow | None, reason: Regen, now: int,
              workspace_name: str | None = None,
              sibling_names: list[str] | None = None) -> None:
    """One pane's regeneration: signals → one Haiku call → persist row,
    maybe rename. Raises freely; tick()'s per-pane guard logs and keeps
    the previous row (natural retry next tick)."""
    current_name = w.get("name") or ""
    cwd = w.get("cwd") or ""
    # Session switch (/clear, recycled pane id) is a fresh thread: the prior
    # occupant's goal and arc are as stale as its cooldown, so we start blank.
    fresh_session = reason == "session_switch"
    prev_goal = None if fresh_session else (row.goal if row else None)
    prev_arc = [] if fresh_session else load_arc(row.history if row else None)
    signals = transcript_summary_from_path(jsonl)
    git = cached_git_state(cwd) or {}
    pr = (cached_pr_state(cwd, git.get("branch")) or {}).get("pr")
    raw = claude_complete(build_narrator_prompt(
        window_name=current_name, branch=git.get("branch"), pr=pr,
        cwd=cwd, signals=signals, workspace_name=workspace_name,
        sibling_names=sibling_names, goal=prev_goal, arc=prev_arc, now=now))
    result = parse_response(raw)
    if result is None:
        log.warning("narrator: unparseable response for %s; keeping previous "
                    "status", pane_id)
        return
    # A parse that drops `goal` (None) carries the previous one forward — never
    # wipe the thread memory on a single bad/omitted field.
    goal = result.goal or prev_goal
    arc = update_arc(prev_arc, result.status, now)
    # Session switch also resets the cooldown AND the external-rename memory: a
    # recycled pane id (or /clear) must not inherit the previous occupant's
    # renamed_at, and its seen_name is equally stale.
    renamed_at = None if fresh_session else (row.renamed_at if row else None)
    seen_name = current_name
    suggestion = result.rename
    if not fresh_session and row is not None and is_external_rename(row, current_name):
        # Someone renamed the window since we last looked — never clobber;
        # record the new name and start the cooldown instead of renaming.
        renamed_at = now
        suggestion = None
    gate_row = replace(row, renamed_at=renamed_at) if row is not None else None
    new_name = rename_decision(suggestion, current_name=current_name,
                               row=gate_row, now=now)
    if new_name:
        # current_name and row are snapshots from tick start, and a tick can
        # run many seconds (sequential Haiku calls). Re-read the live window
        # name and the live row immediately before applying: a human rename
        # mid-tick — direct tmux, or a rename route that stamped the cooldown
        # — must win over our stale snapshot.
        live_name = tmux("display-message", "-t",
                         f"{w['session']}:{w['index']}", "-p",
                         "#{window_name}").strip()
        live_row = activity.get_pane_status(pane_id)
        stamped = (live_row is not None and live_row.renamed_at is not None
                   and now - live_row.renamed_at < RENAME_COOLDOWN_S)
        if live_name != current_name or stamped:
            log.info("narrator: rename of %s preempted mid-tick; keeping %r",
                     pane_id, live_name)
            new_name = None
            if live_row is not None and live_row.renamed_at is not None:
                renamed_at = live_row.renamed_at
            if live_row is not None and live_row.seen_name is not None:
                seen_name = live_row.seen_name
    if new_name:
        tmux("rename-window", "-t", f"{w['session']}:{w['index']}", new_name)
        activity.record("pane", pane_id, "rename",
                        f"renamed: {current_name} → {new_name}")
        log.info("narrator: renamed %s %r → %r", pane_id, current_name, new_name)
        renamed_at = now
        seen_name = new_name
    activity.upsert_pane_status(PaneStatusRow(
        pane_id=pane_id, session_id=sid, status=result.status,
        generated_at=now, jsonl_size=size, seen_name=seen_name,
        renamed_at=renamed_at, rail=result.rail, goal=goal,
        history=json.dumps(arc)))
    log.info("narrator: %s status %r", pane_id, result.status)

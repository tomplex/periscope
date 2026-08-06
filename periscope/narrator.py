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
from periscope.store import get_window
from periscope.tmux import tmux
from periscope.turns import jsonl_for_session, session_id_for_pane

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
# Scaffolding words that appear in tracks, branches, and worktree dirs without
# naming anything: they must not make a container token set look richer than it
# is, or an echo slips through ('worktree-world-model' vs 'world-model').
_ECHO_STOPWORDS = frozenset({"worktree", "worktrees", "wt", "claude", "dev",
                             "tc", "repo", "main", "master"})

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
    name: str | None          # the model's best name for the goal (may == current);
                              # rename_decision diffs it against current_name
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
    naturally. A non-string name drops just the name (→ keep current), and a
    bad rail drops just the rail — never the status."""
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
    name = d.get("name")
    name = name.strip() or None if isinstance(name, str) else None
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
    return NarratorResult(status=status, name=name, rail=rail, goal=goal)


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


def _tokens(s: str | None) -> set[str]:
    """Lowercase word tokens of a name/label, minus the noise words that
    carry no identity ('worktree-world-model' and 'world-model' are the same
    concept wearing a container prefix)."""
    if not s:
        return set()
    parts = re.split(r"[^a-z0-9]+", s.lower())
    return {p for p in parts if p and p not in _ECHO_STOPWORDS}


def container_tokens(*, track_name: str | None, branch: str | None,
                     cwd: str | None) -> set[str]:
    """Every token already visible ABOVE a tab in the rail: its track header,
    its branch subgroup row, and its worktree directory."""
    return (_tokens(track_name) | _tokens(branch)
            | _tokens(os.path.basename(cwd or "")))


def siblings_excluding(members: list[tuple[str, str]], pane_id: str) -> list[str]:
    """Names of the OTHER tabs under a track — self excluded by pane id (not
    by name; colliding names are exactly the case that matters), deduped,
    order preserved."""
    out: list[str] = []
    for pid, name in members:
        if pid == pane_id or not name or name in out:
            continue
        out.append(name)
    return out


def is_echo(suggestion: str, container: set[str]) -> bool:
    """True when the name says nothing the rail doesn't already show — every
    token of it is already in the header, branch, or worktree name above it.
    A name carrying at least one new token is kept: it distinguishes."""
    toks = _tokens(suggestion)
    return bool(toks) and bool(container) and toks <= container


# tmux `automatic-rename` names a window after its running command; for Claude
# Code that renders as the bare version ("2.1.220"). `claude` is what periscope
# itself passes to `new-window -n`.
_PLACEHOLDER_NAME_RE = re.compile(r"^\d+(\.\d+)+$")


def is_placeholder_name(name: str | None) -> bool:
    """This window name says nothing about the work happening in it."""
    n = (name or "").strip()
    return not n or n == "claude" or bool(_PLACEHOLDER_NAME_RE.match(n))


def rename_decision(suggestion: str | None, *, current_name: str,
                    row: PaneStatusRow | None, now: int,
                    locked: bool = False,
                    container: set[str] | None = None) -> str | None:
    """Code-side guards on the model's rename suggestion. The failure mode
    to fear is name churn, not staleness — every guard errs toward None."""
    if locked:
        return None
    if not suggestion or suggestion == current_name:
        return None
    if len(suggestion) > 25 or not _NAME_RE.match(suggestion):
        return None
    # The prompt already forbids echoing the track/branch/worktree name, and
    # Haiku still does it — five sibling tabs in one worktree all converged on
    # 'world-model'. Prompt taste is advisory; this guard is not.
    #
    # Unless the name it would protect says nothing: a pane whose work genuinely
    # matches its branch has NO non-echoing name available, so the guard fired
    # every tick and left the window wearing tmux's automatic name — a Claude
    # version string — indefinitely. An echo beats "2.1.220".
    if container and not is_placeholder_name(current_name) and is_echo(suggestion, container):
        return None
    if (row is not None and row.renamed_at is not None
            and now - row.renamed_at < RENAME_COOLDOWN_S):
        return None
    return suggestion


def is_name_pinned(w: dict) -> bool:
    """This window's name was chosen deliberately — a human, a spawning lead,
    or the pane itself — and the narrator must never rename it.

    A lock, not a cooldown: nothing re-asserts a deliberate name. A lead names
    its worker's task and then usually exits, so a narrator drift is permanent
    and silently breaks the lineage chip (which labels a spawner by its window
    name); a name Tom typed drifted away 30 minutes later, five times in one
    afternoon on the orchestrator pane. The pin survives later renames — it
    marks the WINDOW as hand-named, not one particular string — and is
    released only by an explicit unpin.

    Reads `pid_raw` as well as `pid`: the narrator's windows come straight
    from `list_windows()`, which carries only the raw `@periscope_id` stamp —
    `pid` is attached later by `resolve_pids`, which the worker tick never
    calls (it mints ids and writes state.json, so it can't run in the tick's
    thread). Keying on `pid` alone made this lock dead in prod while the unit
    tests, which hand-build `{"pid": ...}` dicts, kept passing."""
    pid = w.get("pid") or w.get("pid_raw") or ""
    if not pid:
        return False
    return bool(get_window(pid).get("name_pinned"))


def is_external_rename(row: PaneStatusRow, current_name: str) -> bool:
    """Someone renamed the window since the narrator last looked (human,
    tmux-native, or another route that didn't stamp). seen_name is updated
    at every generation AND at narrator-apply time, so any divergence is
    external."""
    return row.seen_name is not None and current_name != row.seen_name


def build_narrator_prompt(*, window_name: str, branch: str | None,
                          pr: int | None, cwd: str, signals: dict,
                          track_name: str | None = None,
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
        "Finally output `name`: the 1-3 word tab name that best fits the GOAL",
        "above. This is NOT a yes/no rename decision — you are just naming the",
        "goal, and the dashboard renames the tab only if your name differs from",
        "current_name. (Haiku is reliably good at naming and bad at deciding to",
        "rename — so name the goal, and let the diff decide.)",
        "  - If current_name already describes the goal, return it UNCHANGED.",
        "    A stable goal keeps its name — most ticks return current_name as-is.",
        "  - If current_name does NOT describe the goal — wrong topic, or the",
        "    goal moved on (e.g. current_name 'brainstorm-skill' but the goal is",
        "    now a data-normalization pipeline) — return the name that fits it.",
        "  - Judge fit against the GOAL, not the current step: a new phase, file,",
        "    or sub-task within the same goal keeps the SAME name (no churn).",
        *[f"  {r}" for r in RENAME_RULES],
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
    if track_name:
        sibs = ", ".join(n for n in (sibling_names or []) if n) or "(none yet)"
        lines += [
            "",
            f'This tab renders under its track\'s header row, labeled "{track_name}".',
            f"Sibling tabs under the same header: {sibs}.",
            "  - The header label — and the branch, shown as a subgroup row — are",
            "    ALREADY VISIBLE right above this tab. A tab name that echoes the",
            "    track, branch, or worktree name (or an abbreviation of it) says",
            "    nothing: never return one, and if current_name is such an echo it",
            "    does NOT count as describing the goal — replace it.",
            "  - Name the sub-thread that sets THIS tab apart from its siblings.",
        ]
    lines += [
        "",
        'Return ONLY a JSON object: {"status": "<status line>",'
        ' "rail": "<short fragment>", "goal": "<thread sentence>",'
        ' "name": "<1-3 word tab name for the goal>"}.',
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
    # Track context: resolve each pane's track (explicit tag or repo-default
    # fallback — same resolution the rail groups by) and build a per-track
    # sibling-name index from this tick's panes, so each tab is named against
    # the header label already shown above it + its siblings instead of
    # echoing them. Wired to TRACKS, not the legacy pane_workspaces table —
    # the old wiring left track-grouped tabs without sibling context and
    # Haiku named every tab after its branch.
    from periscope import tracks as tracks_mod
    pane_tracks: dict[str, str] = {}
    # (pane_id, name) pairs, not bare names: a pane must be excluded from its
    # OWN sibling list by identity. Listing a tab's own name back to it read as
    # endorsement — with five tabs all called 'world-model', the prompt was
    # asking Haiku to differentiate from a list that was mostly itself.
    members: dict[str, list[tuple[str, str]]] = {}
    for w, _parsed in panes:
        pid = w.get("pane_id") or ""
        if not pid:
            continue
        try:
            tid = tracks_mod.resolve_track_for_window(w)
        except Exception:
            log.exception("narrator track resolve failed for %s", pid)
            continue
        pane_tracks[pid] = tid
        members.setdefault(tid, []).append((pid, w.get("name") or ""))
    work: dict[str, tuple[dict, str, Path, int, PaneStatusRow | None, Regen]] = {}
    candidates: list[tuple[int, str]] = []
    for w, _parsed in panes:
        pane_id = w.get("pane_id") or ""
        if not pane_id:
            continue
        try:
            sid = session_id_for_pane(pane_id)
            if not sid:
                # Resolver order is live-claude-first, hook-recorded row as
                # fallback: Claude rotates the session id on resume/compaction
                # and the hook does not always record the successor, which left
                # such panes permanently unnarrated when this read the row
                # directly. Still NO cwd fallback — on a shared cwd a
                # wrong-session status is worse than no status.
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
        tid = pane_tracks.get(pane_id)
        track_name = tracks_mod.track_label(tid) if tid else None
        sibs = siblings_excluding(members.get(tid) or [], pane_id) if tid else None
        try:
            _generate(w, pane_id=pane_id, sid=sid, jsonl=jsonl, size=size,
                      row=row, reason=reason, now=now,
                      track_name=track_name, sibling_names=sibs)
        except Exception:
            log.exception("narrator generation failed for %s", pane_id)


def _generate(w: dict, *, pane_id: str, sid: str, jsonl: Path, size: int,
              row: PaneStatusRow | None, reason: Regen, now: int,
              track_name: str | None = None,
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
        cwd=cwd, signals=signals, track_name=track_name,
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
    # The model names the goal every tick (may == current_name); rename_decision
    # turns that into an actual rename only when it differs and passes guards.
    suggestion = result.name
    if not fresh_session and row is not None and is_external_rename(row, current_name):
        # Someone renamed the window since we last looked — never clobber;
        # record the new name and start the cooldown instead of renaming.
        renamed_at = now
        suggestion = None
    gate_row = replace(row, renamed_at=renamed_at) if row is not None else None
    new_name = rename_decision(suggestion, current_name=current_name,
                               row=gate_row, now=now,
                               locked=is_name_pinned(w),
                               container=container_tokens(
                                   track_name=track_name,
                                   branch=git.get("branch"), cwd=cwd))
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
    # Append-only thread log: one 'status' event when the status or goal
    # actually changes (an unchanged regeneration writes nothing). Kept out of
    # the live timeline by events_for; status_log_for() reads the history.
    if result.status != (row.status if row else None) or goal != prev_goal:
        activity.record("pane", pane_id, "status", result.status,
                        at=now, detail=goal)
    activity.upsert_pane_status(PaneStatusRow(
        pane_id=pane_id, session_id=sid, status=result.status,
        generated_at=now, jsonl_size=size, seen_name=seen_name,
        renamed_at=renamed_at, rail=result.rail, goal=goal,
        history=json.dumps(arc)))
    log.info("narrator: %s status %r", pane_id, result.status)

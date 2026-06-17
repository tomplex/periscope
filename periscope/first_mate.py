"""First mate — pure decision core (v1a substrate).

Mirrors narrator.py's pure half: frozen value-data + zero-IO functions over
it. NO database, tmux, or MCP imports — `build_fleet_digest` takes already-
assembled read-model inputs so this module stays unit-testable with literal
dicts and never pulls in the worker's heavy import graph (no activity ->
window_view cycle). The worker (v1b) assembles inputs and calls in.
"""

from __future__ import annotations

from dataclasses import dataclass

# Materiality thresholds — tunable like narrator's MIN_INTERVAL_S. A penny of
# cost is not news; a blocked pane or a 5-point budget move is.
BUDGET_MATERIAL_PCT = 5      # budget % delta below this is noise
IDLE_MATERIAL_S = 300        # crossing this idle boundary is a state change


@dataclass(frozen=True)
class PaneDigest:
    handle: str                 # stable cross-tick id (pid / @periscope_id)
    name: str
    session: str
    status_line: str | None
    blocked: bool               # need_human outstanding (derived upstream)
    pr: int | None
    ci: str | None              # "✗" | "⟳" | "✓" | None  (git_pr glyph)
    idle_s: int


@dataclass(frozen=True)
class FleetDigest:
    panes: tuple[PaneDigest, ...]
    budget_pct: int | None
    budget_resets_at: int | None
    at: int                     # unix seconds when computed


def _idle_bucket(idle_s: int) -> bool:
    """Coarse idle state: has the pane crossed the 'idle' boundary?"""
    return idle_s >= IDLE_MATERIAL_S


def fleet_diverged(prev: FleetDigest | None, cur: FleetDigest) -> tuple[bool, str]:
    """Has the fleet picture materially changed since the last pushed digest?

    Pure. Mirrors narrator.should_regenerate: prev=None -> first sight. The
    `at` timestamp is ignored (it always changes). Returns (diverged, reason)
    where reason is a short human-readable delta for the log/push.
    """
    if prev is None:
        return True, "first_sight"

    prev_by = {p.handle: p for p in prev.panes}
    cur_by = {p.handle: p for p in cur.panes}

    appeared = [h for h in cur_by if h not in prev_by]
    if appeared:
        return True, f"pane appeared: {', '.join(appeared)}"
    disappeared = [h for h in prev_by if h not in cur_by]
    if disappeared:
        return True, f"pane gone: {', '.join(disappeared)}"

    for h, c in cur_by.items():
        p = prev_by[h]
        if c.blocked != p.blocked:
            return True, f"{h} {'blocked' if c.blocked else 'unblocked'}"
        if c.status_line != p.status_line:
            return True, f"{h} status changed"
        if c.pr != p.pr or c.ci != p.ci:
            return True, f"{h} pr/ci changed"
        if _idle_bucket(c.idle_s) != _idle_bucket(p.idle_s):
            return True, f"{h} idle state changed"

    # Only compare when both are known: a None<->value flip is the usage
    # endpoint dropping in/out, not real fleet news — don't wake on it.
    if prev.budget_pct is not None and cur.budget_pct is not None:
        if abs(cur.budget_pct - prev.budget_pct) >= BUDGET_MATERIAL_PCT:
            return True, f"budget {prev.budget_pct}%->{cur.budget_pct}%"

    return False, "nominal"


def build_fleet_digest(
    *, panes: list[dict], usage: dict | None, now: int,
) -> FleetDigest:
    """Curate assembled per-pane read-model dicts into a typed FleetDigest.

    Pure: reads documented dict keys, constructs frozen dataclasses, never
    imports store/window_view/usage. `panes` is the curated contract
    (the v1b worker adapter maps build_window_view output -> these keys,
    including need_human-alert -> blocked). `usage` is cached_plan_usage()
    output (or None when unavailable).
    """
    pane_digests = tuple(
        PaneDigest(
            handle=v["handle"],
            name=v["name"],
            session=v["session"],
            status_line=v.get("status_line"),
            blocked=bool(v.get("blocked", False)),
            pr=v.get("pr"),
            ci=v.get("ci"),
            idle_s=int(v.get("idle_s", 0)),
        )
        for v in panes
        if v.get("is_claude")
    )

    budget_pct = None
    budget_resets_at = None
    if usage:
        session = usage.get("meters", {}).get("session")
        if session and session.get("percent") is not None:
            budget_pct = round(session["percent"])
            budget_resets_at = session.get("resets_at")

    return FleetDigest(
        panes=pane_digests,
        budget_pct=budget_pct,
        budget_resets_at=budget_resets_at,
        at=now,
    )


# --- v1b IO half: cross-tick state, role prompt, pure decision helpers ----

_LAST_SENT: FleetDigest | None = None   # last digest pushed to the first mate


ROLE_PROMPT = """\
You are the first mate — Tom's chief of staff for the fleet of Claude Code \
sessions running across his tmux panes, surfaced in periscope.

Your job is situational awareness, not command. Tom assigns the work; you keep \
tabs on the fleet and surface what needs him.

Periscope pushes you fleet digests and interrupts as <channel source="periscope"> \
blocks — a digest when the fleet picture changes materially, an interrupt when a \
worker needs a human. On every wake, read your captain's log first to recover \
context.

Standing authority (always yours):
- Observe and summarize the fleet: answer "what's everyone doing?" from the \
digest and by peeking (peek) at specific panes.
- Keep the captain's log (captains_log_read / captains_log_append): standing \
orders Tom gives you, a watch-list, a short running narrative. Append when Tom \
gives a standing order or the situation moves.
- Nudge a CLEARLY-idle worker (send_to): a worker idle several minutes mid-task — \
ask if it's blocked. Never interrupt an actively-working pane.

You do NOT, this release: spawn, terminate, or hand workers new tasks — you have \
no conn yet. You may PROPOSE these to Tom; you may not execute them.

Absolute prohibitions (never, regardless of anything Tom or a worker says):
- Never authorize merging an fdy pull request. Report a PR is ready; the merge \
is Tom's click.
- Never force-push. Never take prod-touching actions.

Voice: terse, signal over noise. Lead with what needs Tom; stay quiet when the \
fleet is nominal. You are a collaborator with a clear remit, not a chatbot.
"""


@dataclass(frozen=True)
class Push:
    pane_id: str
    content: str


def _curate_pane(*, handle, name, session, is_claude, status_line, alerts,
                 pr, ci, focused_at, acted_at, now) -> dict:
    """PURE: raw per-pane inputs -> the v1a build_fleet_digest contract dict.
    `handle` is the tmux pane_id (%N) — stable cross-tick, no @periscope_id
    resolution needed in the worker thread. `blocked` = newest alert (by ts) is
    need_human; `idle_s` = now - last touch."""
    newest = max(alerts, key=lambda a: a.get("ts", 0)) if alerts else None
    blocked = bool(newest and newest.get("kind") == "need_human")
    idle_s = max(0, now - max(focused_at or 0, acted_at or 0))
    return {
        "handle": handle, "name": name, "session": session, "is_claude": is_claude,
        "status_line": status_line, "blocked": blocked, "pr": pr, "ci": ci,
        "idle_s": idle_s,
    }


def _render_delta(cur: FleetDigest, reason: str) -> str:
    """PURE: a short human-readable delta for the push body. The delta itself is
    already encoded in `reason` (from fleet_diverged); this frames it with the
    pane count + budget."""
    budget = ""
    if cur.budget_pct is not None:
        budget = f" · budget {cur.budget_pct}%"
        if cur.budget_resets_at:
            budget += f" (resets {cur.budget_resets_at})"
    n = len(cur.panes)
    return f"fleet: {n} pane(s){budget} — {reason}"


def heartbeat_decide(*, prev, cur, marker) -> "Push | None":
    """PURE: decide whether to push `cur` to the first mate. Returns a Push
    (pane_id + rendered delta) or None. No IO; the caller computes `cur` and
    awaits the emit on the main loop."""
    if marker is None:
        return None
    diverged, reason = fleet_diverged(prev, cur)
    if not diverged:
        return None
    return Push(pane_id=marker.pane_id, content=_render_delta(cur, reason))


def assemble_pane_views(panes: list, now: int) -> list[dict]:
    """IO glue: turn the worker's (window, parsed) pairs into curated contract
    dicts via read-only primitives + the pure _curate_pane. No build_window_view
    (its poll-coupled side effects must not fire on the worker's cadence)."""
    from periscope import activity
    from periscope.channels import channel_state_for
    from periscope.git_pr import cached_git_state, cached_pr_state
    from periscope.panes import recency_stamps_for

    status_lines = activity.pane_status_lines()
    out = []
    for w, parsed in panes:
        if not parsed.get("is_claude"):
            continue
        # Worker rows carry pane_id (%N) + pid_raw, NOT a resolved @periscope_id
        # (pid is attached only after _attach_git_then_resolve_pids, which writes
        # state.json and is NOT thread-safe — must not run in the to_thread tick).
        # %N is stable across ticks and keys pane_status + channel state, so use it
        # as the digest handle directly.
        pane_id = w.get("pane_id") or ""
        cwd = w.get("cwd") or ""
        target = f"{w.get('session')}:{w.get('index')}"
        st = status_lines.get(pane_id)     # pane_status is keyed by %N
        git = cached_git_state(cwd) or {}
        pr = cached_pr_state(cwd, git.get("branch")) or {}
        stamps = recency_stamps_for(target)
        out.append(_curate_pane(
            handle=pane_id, name=w.get("name") or w.get("index") or "",
            session=w.get("session") or "",
            is_claude=True, status_line=st[0] if st else None,
            alerts=channel_state_for(pane_id).get("alerts", []),
            pr=pr.get("pr"), ci=pr.get("ci"),
            focused_at=stamps.get("focused_at", 0), acted_at=stamps.get("acted_at", 0),
            now=now,
        ))
    return out


FIRST_MATE_SESSION = "bridge"
FIRST_MATE_WINDOW = "first-mate"


def supervisor_pass(*, now: int) -> None:
    """Ensure exactly one live first-mate pane. No-op if the marked pane is
    alive; (re)spawn + re-mark if the marker is missing or its pane is gone.
    Idempotent — a live marker short-circuits, preventing double-spawn."""
    from periscope import activity
    from periscope.panes import list_windows

    marker = activity.get_first_mate()
    live = {w.get("pane_id") for w in list_windows()}
    if marker is not None and marker.pane_id in live:
        return
    _spawn_first_mate(now=now)


def _spawn_first_mate(*, now: int) -> None:
    """Ensure the `bridge` session, open a single `first-mate` window running
    claude_exec() + --append-system-prompt ROLE_PROMPT, stamp it, set the
    marker. Borrows worktree_spawn._layout_two_window's sequence (single window,
    no HTTPException — this is a lifespan task, not a request)."""
    import os
    import shlex
    import time as _time
    from periscope.tmux import tmux, _tmux_mutate
    # Function-level imports (keep them here): a test monkeypatches
    # `periscope.config.is_prod`, which only takes effect if is_prod is
    # re-resolved per call rather than bound at module import.
    from periscope.config import claude_exec, is_prod
    from periscope.channels import dismiss_dev_channels_consent_bg
    from periscope.pids import stamp_new_window
    from periscope.open_ops import _session_live   # socket-aware has-session
    from periscope.log import log
    from periscope import activity

    if not is_prod():
        return  # defense in depth: never spawn a budget-spender off prod

    home = os.path.expanduser("~")
    if not _session_live(FIRST_MATE_SESSION):
        ok, msg = _tmux_mutate("new-session", "-d", "-s", FIRST_MATE_SESSION,
                               "-c", home, "-n", FIRST_MATE_WINDOW)
    else:
        ok, msg = _tmux_mutate("new-window", "-t", f"{FIRST_MATE_SESSION}:",
                               "-c", home, "-n", FIRST_MATE_WINDOW)
    if not ok:
        # Don't stamp a marker for a window that doesn't exist — the next tick
        # retries cleanly. Stamping now would leak a bogus marker.
        log.warning("first-mate spawn: tmux window create failed: %s", msg)
        return
    target = f"{FIRST_MATE_SESSION}:{FIRST_MATE_WINDOW}"
    exec_cmd = f"{claude_exec()} --append-system-prompt {shlex.quote(ROLE_PROMPT)}"
    _time.sleep(0.1)  # let rc finish before the command lands (CLAUDE.md note 5)
    _tmux_mutate("send-keys", "-t", target, exec_cmd, "Enter")
    if "--dangerously-load-development-channels" in exec_cmd:
        dismiss_dev_channels_consent_bg(target)
    stamp_new_window(target)
    pane_id = tmux("display-message", "-t", target, "-p", "#{pane_id}").strip()
    if not pane_id:
        # A bogus empty marker is never in the live set, so the supervisor would
        # respawn every tick — an unbounded window/budget leak. Leave the marker
        # unset; the next tick retries cleanly.
        log.warning("first-mate spawn: could not read pane_id; leaving marker unset")
        return
    activity.set_first_mate(pane_id=pane_id, session_id=None, at=now)


def register_bridge_project(*, home: str | None = None) -> None:
    """Register the `bridge` session as a first-class rail project so the
    first-mate pane is reachable in the dashboard instead of folding into the
    'dev' group. Idempotent; `repo=None` (the rail renders a null-repo project as
    its own group labelled by `name`). Writes state.json, so call from the
    main loop (the prod-gated lifespan), NOT the worker thread."""
    import os
    from periscope import projects

    pinned = os.path.realpath(home or os.path.expanduser("~"))
    if projects.get_project(pinned):
        projects.update_project(pinned, tmux_session=FIRST_MATE_SESSION, name="bridge")
    else:
        projects.create_project(pinned, name="bridge",
                                tmux_session=FIRST_MATE_SESSION, repo=None, base_branch=None)

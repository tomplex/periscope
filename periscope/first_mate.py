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

    if prev.budget_pct is not None and cur.budget_pct is not None:
        if abs(cur.budget_pct - prev.budget_pct) >= BUDGET_MATERIAL_PCT:
            return True, f"budget {prev.budget_pct}%->{cur.budget_pct}%"

    return False, "nominal"

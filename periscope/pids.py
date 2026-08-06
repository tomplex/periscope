"""Periscope window-ids (@periscope_id).

Each tmux window gets a periscope-managed id stored as a tmux user
option `@periscope_id`. The id survives renames + moves within a tmux
server lifetime; the rebind heuristic recovers ids across tmux server
restarts. Time-to-live is 30 days — older state.json entries get GC'd.
"""

import time
import uuid

from periscope import session_status
from periscope import store as _store
from periscope.git_pr import cached_git_state
from periscope.log import log
from periscope.tmux import tmux

_PID_TTL_S = 30 * 86400  # 30 days — GC retention for state.json window entries

# Rebind eligibility is a DIFFERENT question from GC retention, and much
# shorter. Rebind exists so persisted state reattaches when the tmux server
# restarts (window options are lost, so @periscope_id vanishes and every window
# is re-sighted unstamped) — that reattachment happens seconds-to-minutes after
# boot, not weeks. Sharing the 30-day GC TTL meant a fresh Claude at fdy master
# matched ANY entry from the last month on the (branch, cwd) fallback and
# inherited its immunity fields — surfacing as a brand-new pane wearing a stale
# PR and Linear ticket.
_REBIND_TTL_S = 15 * 60  # 15 minutes


def _mint_pid() -> str:
    return uuid.uuid4().hex[:8]


def _is_pid(raw: str) -> bool:
    """True for a well-formed minted id (8 lowercase hex chars)."""
    return len(raw) == 8 and all(c in "0123456789abcdef" for c in raw)


def _stamp_pid(target: str, pid: str) -> None:
    """Fire-and-forget set-option. If it fails (window gone, tmux racy),
    the next poll repeats the attempt. Uses the project's read-style
    `tmux()` helper because we don't need stderr-surfacing here."""
    tmux("set-option", "-w", "-t", target, "@periscope_id", pid)


def stamp_new_window(target: str) -> str:
    """Mint a fresh periscope id, stamp it onto the tmux window at `target`,
    and return it. Used by handlers that need to write `state.windows[pid]`
    fields synchronously after creating a window — without this, the pid
    wouldn't be assigned until the next /api/state poll runs resolve_pids.

    Re-stamping a window that already has an `@periscope_id` is harmless
    (this function unconditionally mints + stamps), but resolve_pids will
    accept either id on the next poll. Callers should only invoke this on
    freshly-created windows.
    """
    pid = _mint_pid()
    _stamp_pid(target, pid)
    return pid


def _rebind_pid(
    windows_block: dict,
    session: str,
    name: str,
    branch: str | None,
    cwd: str | None,
    taken_pids: set[str],
) -> str | None:
    """Look for an orphan id in state's `windows` block that matches the
    sighted window on (session, name) — or as a softer fallback,
    (branch, cwd). Returns the matched pid, or None if no candidate
    matches."""
    now = time.time()
    # Pass 1: strong match on (session, name).
    # Pass 2: secondary match on (branch, cwd) when both are set.
    for pass_n in (1, 2):
        for pid, entry in windows_block.items():
            if pid in taken_pids:
                continue
            ls = entry.get("last_seen") or {}
            ts = ls.get("ts")
            if not ts or now - ts > _REBIND_TTL_S:
                continue
            if pass_n == 1:
                if ls.get("session") == session and ls.get("name") == name:
                    return pid
            else:
                if not branch or not cwd:
                    continue
                if ls.get("branch") == branch and ls.get("cwd") == cwd:
                    return pid
    return None


# Immunity fields: any of these set means the row carries state the user
# expects to persist past archive (per the project-model spec §"GC
# extension"). `notes`/`tags` were the v1 list; phase 1 added the
# channels-MCP fields so archiving a project doesn't silently erase its
# PR/Linear linkage.
_IMMUNITY_FIELDS = (
    "notes", "tags",
    "linked_pr", "linked_linear",
    "acked_at", "completed_at",
    "alias", "is_fork",
    # spawned_by is written by spawn_claude's provenance step the instant the
    # child window is created — BEFORE any poll stamps the entry's last_seen.
    # Without immunity, a GC pass straddling the spawn reaps the fresh
    # (last_seen-less) entry and the breadcrumb is lost. Durable channel
    # metadata, same category as linked_pr/linked_linear.
    "spawned_by",
    # spawn_name is written in the same breath as spawned_by and for the same
    # reason — GC straddling a fresh spawn would reap it and the narrator would
    # start renaming a window whose name the lead chose deliberately.
    "spawn_name",
)


def _resolve_one(
    w: dict, wblock: dict, taken: set[str], carried: set[str], now_ts: int
) -> bool:
    """Resolve one window's pid in place and refresh its `last_seen`.

    Picks a pid (reuse @periscope_id / rebind / mint), stamps tmux when the
    id was synthesized, records `pid` on `w`, strips the internal `pid_raw`,
    and updates `wblock[pid]["last_seen"]`. Mutates `taken` with the chosen
    pid. Returns True when something dirtying state.json changed (a stamp or
    an identity change in last_seen) — a pure `ts` bump is not dirtying.

    Runs inside the caller's `_STATE_LOCK`; does NOT acquire it itself.
    """
    target = f"{w['session']}:{w['index']}"
    pid_raw = (w.get("pid_raw") or "").strip()
    pid: str | None = None
    if _is_pid(pid_raw):
        # Reject a pid_raw already claimed earlier in this pass —
        # tmux occasionally ends up with the same @periscope_id on
        # multiple windows (session-copy, swap-window, set-option
        # racing with new-window, etc.). Without this check, every
        # duplicate window resolves to the same persisted state
        # entry, so acted_at / completed_at / linked_pr / notes all
        # collapse onto a single record and the grid sort can't
        # distinguish the windows.
        if pid_raw not in taken:
            pid = pid_raw
        else:
            # Loud on purpose: a re-mint changes the window's identity, so
            # any pid-keyed UI state (detail-pane selection, pinned rows)
            # silently detaches. Reported as "detail pane closes on cd";
            # never caught in the act — this tripwire names the window if
            # it happens again.
            log.warning(
                "duplicate @periscope_id %s on %s — re-minting", pid_raw, target
            )
    if pid is None:
        # Exclude `carried` as well as `taken`: a pid that ANY window in this
        # pass still has stamped in tmux is not an orphan, even though this
        # pass hasn't reached that window yet. `taken` alone grows
        # incrementally, so a window resolved later was an eligible rebind
        # candidate — and a brand-new window sitting earlier in the list would
        # match it on (session, name), or on the (branch, cwd) fallback that a
        # spawn into the caller's own worktree hits by construction, and steal
        # a LIVE window's identity. That is what manufactured the duplicates
        # the gate above only cleans up after the fact: the victim silently
        # re-mints on the next poll, detaching pid-keyed UI state and orphaning
        # the `spawned_by` breadcrumb `report()` routes on.
        pid = _rebind_pid(
            wblock,
            session=w["session"],
            name=w["name"],
            branch=w.get("branch"),
            cwd=w.get("cwd"),
            taken_pids=taken | carried,
        )
    if pid is None:
        pid = _mint_pid()
    dirty = False
    # Stamp tmux only when we synthesized the id (mint or rebind).
    # Target the WINDOW ID (@N), never session:index. Indices renumber under
    # move-window — which the tracks migration does in bulk, and which moving a
    # tab between tracks does again. A stamp aimed at session:index after a
    # renumber lands on a DIFFERENT window, so the duplicate that triggered the
    # re-mint is never cleared and the next poll re-mints again: a self-
    # sustaining loop (observed 683 times on one window in three days).
    if pid != pid_raw:
        _stamp_pid(w.get("window_id") or target, pid)
        dirty = True
    taken.add(pid)
    w["pid"] = pid
    # `pid_raw` was internal — strip it before emit.
    w.pop("pid_raw", None)
    # Refresh last_seen. Only flag dirty if something *other than*
    # `ts` changed — a pure ts bump every 3s would thrash state.json
    # to disk thousands of times an hour for no semantic gain.
    entry = wblock.setdefault(pid, {})
    prev = entry.get("last_seen") or {}
    new_seen = {
        "session": w["session"],
        "name": w["name"],
        "branch": w.get("branch"),
        "cwd": w.get("cwd"),
        "ts": now_ts,
    }
    identity_changed = (
        "last_seen" not in entry
        or any(prev.get(k) != new_seen[k] for k in ("session", "name", "branch", "cwd"))
    )
    entry["last_seen"] = new_seen
    if identity_changed:
        dirty = True
    return dirty


def _gc_windows(wblock: dict, taken: set[str], now_ts: int) -> bool:
    """GC the `windows` block: drop entries that (a) carry no immunity
    fields, AND (b) weren't refreshed this pass, AND (c) have a last_seen
    older than 30 days. Annotated entries are immune — losing one would
    lose notes. Returns True if anything was deleted.

    Runs inside the caller's `_STATE_LOCK`; does NOT acquire it itself.
    """
    dirty = False
    cutoff = now_ts - _PID_TTL_S
    for pid in list(wblock.keys()):
        if pid in taken:
            continue
        entry = wblock[pid]
        if any(entry.get(k) for k in _IMMUNITY_FIELDS):
            continue
        ts = (entry.get("last_seen") or {}).get("ts") or 0
        if ts < cutoff:
            del wblock[pid]
            dirty = True
    return dirty


def _gc_projects(projects: dict, now_ts: int) -> bool:
    """GC the `projects` block: drop archived projects whose archived_at is
    older than 30 days (spec §"GC" rule 2). Auto-archive itself is a
    phase-6 feature; this just collects what the user (or future phases)
    explicitly archived. `__main__` is invariant — even if some future bug
    wrote archived_at on it, the GC must not delete it. Returns True if
    anything was deleted.

    Runs inside the caller's `_STATE_LOCK`; does NOT acquire it itself.
    """
    from periscope.projects import MAIN_KEY as _MAIN_KEY

    dirty = False
    for key in list(projects.keys()):
        if key == _MAIN_KEY:
            continue
        row = projects[key]
        archived_at = row.get("archived_at")
        if archived_at and now_ts - archived_at > _PID_TTL_S:
            del projects[key]
            dirty = True
    return dirty


def _gc_workspaces(workspaces: dict, now_ts: int) -> bool:
    """GC the `workspaces` block: drop workspaces archived more than 30 days
    ago. Mirrors `_gc_projects` — runs inside the caller's `_STATE_LOCK`,
    does NOT acquire it or write state, returns True if anything was deleted.
    """
    dirty = False
    cutoff = now_ts - _PID_TTL_S
    for key in list(workspaces.keys()):
        archived_at = workspaces[key].get("archived_at")
        if archived_at and archived_at < cutoff:
            del workspaces[key]
            dirty = True
    return dirty


def resolve_pids(windows: list[dict],
                 session_hints: dict[str, dict] | None = None,
                 full_roster: bool = False) -> None:
    """Mutates `windows` in place, adding a `pid` field to every entry.

    For each window:
      1. If @periscope_id is non-empty, use it.
      2. Else attempt rebind from state.json's `windows` block.
      3. Else mint a fresh id.
    In cases 2 and 3, stamp the chosen id onto the tmux window (`set-option
    -w @periscope_id`) so subsequent polls take the fast path.

    Always updates the pid's `last_seen` block with (session, name, branch,
    cwd, now) — but only flags `dirty` when something other than the `ts`
    field changed, to avoid thrashing state.json on every 3s poll.

    Callers MUST have populated each window's `branch` (from
    cached_git_state) before calling, or rebind falls back to the
    session/name-only path.

    `session_hints` maps tmux pane id -> {"sid": str|None, "resume": str|None}.
    The hints are computed by the CALLER (see _attach_git_then_resolve_pids)
    because building them forks tmux/ps — under _STATE_LOCK those forks would
    stall every route, and list_claudes runs this resolve on the event loop.

    `full_roster` marks a pass that saw every live window; track-row
    maintenance in a later task must never run from a partial (single-window)
    pass, which would read absence as death.
    """
    hints = session_hints or {}
    _ = hints, full_roster   # plumbing only — resolution consumes these in Task 4
    if not windows:
        return
    now_ts = int(time.time())
    # Everything that reads/writes _STATE goes through _STATE_LOCK. We hold
    # the lock for the full resolve pass — it's cheap (kilobyte-scale JSON
    # write at the end) and gives us a single consistent snapshot of the
    # windows block to score rebinds against.
    #
    # Module-qualified `_store._STATE` access (instead of `from … import _STATE`)
    # so test-time monkeypatching of `periscope.store._STATE` propagates here.
    with _store._STATE_LOCK:
        wblock = _store._STATE.setdefault("windows", {})
        taken: set[str] = set()
        # Every id tmux currently has stamped, gathered BEFORE the pass so
        # rebind can't hand out an id a live window is still wearing.
        carried = {p for w in windows if _is_pid(p := (w.get("pid_raw") or "").strip())}
        dirty = False
        # Phase 1: per-window pid resolution + last_seen refresh.
        for w in windows:
            if _resolve_one(w, wblock, taken, carried, now_ts):
                dirty = True
        # Phase 2: window-entry GC.
        if _gc_windows(wblock, taken, now_ts):
            dirty = True
        # Phase 3: archived-project GC.
        projects = _store._STATE.setdefault("projects", {})
        if _gc_projects(projects, now_ts):
            dirty = True
        # Phase 4: archived-workspace GC.
        wss = _store._STATE.setdefault("workspaces", {})
        if _gc_workspaces(wss, now_ts):
            dirty = True
        if dirty:
            _store._write_state(_store._STATE)


def _attach_git_then_resolve_pids(windows: list[dict],
                                  full_roster: bool = False) -> None:
    """resolve_pids relies on `branch` for its secondary match and on session
    hints (live sid + resume lineage) for its primary one. Both are attached
    HERE, before resolve takes _STATE_LOCK: hint-building forks tmux/ps on a
    cold cache, and those forks must not run under the lock or on the event
    loop (list_claudes resolves in the loop).

    Despite the query-sounding name, this performs I/O: resolve_pids stamps
    `@periscope_id` onto tmux windows and may write state.json."""
    for w in windows:
        git = cached_git_state(w.get("cwd", "")) or {}
        if "branch" in git:
            w["branch"] = git["branch"]
    resumes = session_status.pane_resume_ids()
    hints: dict[str, dict] = {}
    for w in windows:
        pane_id = w.get("pane_id") or ""
        if pane_id:
            hints[pane_id] = {
                "sid": session_status.live_session_id_for_pane(pane_id),
                "resume": resumes.get(pane_id),
            }
    resolve_pids(windows, session_hints=hints, full_roster=full_roster)

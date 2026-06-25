"""One-shot consolidation of every periscope-managed tmux window into the
single shared `config.MANAGED_SESSION`.

Prod+flag-gated and run once at boot, BEFORE serving. After tracks land,
grouping is metadata (pane_tracks), not the tmux session — so the physical
session boundaries that predate tracks are collapsed into one session here.

Safety (do NOT relax — the 2026-06-24 wrong-kill incident):
- Moves by STABLE `#{window_id}`, never `session:index`. `renumber-windows on`
  makes indices drift mid-batch; window_id and pane_id are stable across
  `move-window`, so snapshotting once and moving each by its resolved id is
  safe even as siblings move.
- Gated on BOTH config.is_prod() AND a persisted flag — never fires in dev or
  tests. The flag is set only after a successful pass.
"""
from __future__ import annotations

from periscope import config, store, tracks
from periscope.log import log
from periscope.panes import list_windows
from periscope.tmux import _tmux_mutate, tmux


def run_if_needed() -> None:
    """Prod+flag-gated one-shot. No-op unless config.is_prod() AND the
    single_session_done flag is unset. Moves all managed windows into
    MANAGED_SESSION, seeds tracks, sets the flag.

    Lets failures raise to the lifespan's try/except (which logs) rather than
    swallowing them silently — a half-moved tmux is something we want to see.
    """
    if not config.is_prod():
        return
    if store.is_single_session_migration_done():
        return
    moved = _move_managed_windows()
    seeded = tracks.seed_tracks(list_windows())
    _mark_done()
    log.info("single-session migration: moved %d window(s), seeded %d track tag(s)",
             moved, seeded)


def _move_managed_windows() -> int:
    """Move every managed window into MANAGED_SESSION. Returns the count moved.

    A managed window is any window list_windows() returns (it already filters
    INPUT_CTL_SESSION) whose session is neither MANAGED_SESSION nor a usage
    scraper session (USAGE_SESSION_PREFIX). Snapshots the window list once,
    then moves each by its resolved stable `#{window_id}` — safe as siblings
    move because window_id/pane_id don't change across move-window.
    """
    snapshot = list_windows()

    # Ensure the target session exists. Capture the auto-created blank window's
    # id (-P -F "#{window_id}") so we can kill it after moving real windows in —
    # the same adopt-flow pattern as routes/projects.py:promote.
    #
    # `=` forces EXACT-match target resolution: bare `has-session -t periscope`
    # prefix-matches `periscope-usage-*`, so without it we'd skip creating the
    # real session AND move windows into the usage session — a wrong-session
    # mutation in prod. Every -t against MANAGED_SESSION below uses `=` for the
    # same reason.
    created_blank_id = ""
    if not _tmux_mutate("has-session", "-t", f"={config.MANAGED_SESSION}")[0]:
        ok, msg = _tmux_mutate(
            "new-session", "-d", "-s", config.MANAGED_SESSION,
            "-P", "-F", "#{window_id}",
        )
        if not ok:
            raise RuntimeError(f"failed to create {config.MANAGED_SESSION}: {msg}")
        created_blank_id = msg.strip()

    moved = 0
    for w in snapshot:
        session = w["session"]
        if session == config.MANAGED_SESSION:
            continue
        if session.startswith(config.USAGE_SESSION_PREFIX):
            continue
        pane_id = w.get("pane_id")
        if not pane_id:
            continue
        # Resolve the STABLE window id for this window via its pane_id. Moving
        # by session:index would race renumber-windows as siblings move.
        window_id = tmux("display-message", "-t", pane_id, "-p", "#{window_id}").strip()
        if not window_id:
            log.warning("single-session migration: no window_id for pane %s (%s); skipping",
                        pane_id, session)
            continue
        ok, msg = _tmux_mutate(
            "move-window", "-s", window_id, "-t", f"={config.MANAGED_SESSION}:",
        )
        if ok:
            moved += 1
        else:
            # One bad move must not abort the batch — log loudly, keep going.
            log.warning("single-session migration: move-window %s failed: %s",
                        window_id, msg)

    # Kill the auto-created blank window — but only if we actually moved real
    # windows in (don't leave an empty session AND don't kill its last window).
    if created_blank_id and moved:
        _tmux_mutate("kill-window", "-t", created_blank_id)

    return moved


def _mark_done() -> None:
    store.mark_single_session_migration_done()

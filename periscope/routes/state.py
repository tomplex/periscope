"""GET /api/state — the main dashboard poll endpoint.

Fan-out + assembly: list_windows, then build_window_view per pane
(in periscope.window_view), then batched stamp persistence + resume GC.

Polled every 3s from the browser; everything underneath is cached on
its own clock, so this handler is mostly orchestration.
"""

import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter

from periscope.activity import pane_status_lines
from periscope.channels import _channel_gc
from periscope.panes import (
    RESUME_EXPIRY_S,
    _resuming,
    all_pane_ids,
    list_windows,
    update_focus_from_windows,
)
from periscope.pids import _attach_git_then_resolve_pids
from periscope.projects import all_projects
from periscope.store import set_window_fields_bulk
from periscope.usage import annotate_hot_panes, cached_claude_usage, cached_plan_usage
from periscope.window_view import build_window_view

router = APIRouter()


def _safe_build(w: dict, now_ts: int) -> tuple[dict, tuple[str, int, int] | None]:
    """Per-worker exception isolation for the parallel fan-out.

    build_window_view already catches capture()/parse_pane() failures, but the
    git / PR / LGTM / affiliation calls run outside that guard. In executor.map
    one worker raising would re-raise on the join and 500 the whole /api/state
    response. Convert any per-pane exception into an error view so one bad pane
    can't sink the board.
    """
    try:
        return build_window_view(w, now_ts)
    except Exception as e:
        target = f"{w['session']}:{w['index']}"
        return (
            {**w, "target": target, "state": "error", "is_claude": False, "error": str(e)},
            None,
        )


def build_state() -> dict:
    """Assemble the full dashboard state blob.

    The body of GET /api/state, lifted out so the state hub's broadcast loop
    (periscope.state_hub) can compute the same blob on the server's own clock
    and push it over /ws/state. Blocking (tmux subprocess + 32-thread capture
    fan-out); callers off the event loop run it in an executor. Concurrent
    execution is already tolerated — multiple browser tabs hit /api/state at
    once today — so the hub running it alongside a REST poll adds no new race.
    """
    windows = list_windows()
    update_focus_from_windows(windows)
    _attach_git_then_resolve_pids(windows)
    now_ts = int(time.time())

    # Parallel fan-out: capture()+parse per pane is the only per-poll
    # subprocess and dominates wall-clock. The serial git-warm + pid mint
    # above (_attach_git_then_resolve_pids — writes state.json + tmux options)
    # is NOT thread-safe and stays before this pool; the stamp write below
    # stays single-threaded after the join. executor.map preserves input
    # order, so `result` matches `windows`. _safe_build isolates any per-pane
    # exception (capture, git, affiliation) into an error view.
    if windows:
        with ThreadPoolExecutor(max_workers=min(32, len(windows))) as pool:
            built = list(pool.map(lambda w: _safe_build(w, now_ts), windows))
    else:
        built = []

    result = [view for view, _ in built]
    annotate_hot_panes(result)

    # Narrator status merge: ONE bulk read here, NOT per-pane inside
    # build_window_view — the 32-thread fan-out would serialize on
    # activity._LOCK. status_at lets the UI dim stale lines.
    statuses = pane_status_lines()
    if statuses:
        for view in result:
            s = statuses.get(view.get("pane_id") or "")
            if s:
                view["status_line"], view["status_at"], rail = s
                # Absent-key contract (same as status_line): rows generated
                # before the rail column, or with model-rejected rails, send
                # nothing and the UI falls back to status_line.
                if rail:
                    view["status_rail"] = rail

    stamp_updates: list[tuple[str, int, int]] = [
        stamp for _, stamp in built if stamp is not None
    ]

    # GC against ALL live panes, not the active-pane-per-window set `windows`
    # carries — otherwise a split window's background Claude pane has its alerts
    # dropped every poll. Skip entirely on an empty result (a tmux hiccup must
    # not wipe every pane's alerts).
    live_panes = all_pane_ids()
    if live_panes:
        _channel_gc(live_panes)

    # Batched stamp persistence: single lock + single write across every
    # pane in this poll. set_window_fields_bulk skips the write when no
    # field actually changed.
    set_window_fields_bulk({
        pid: {"completed_at": completed, "acked_at": acked}
        for pid, completed, acked in stamp_updates
    })

    # Garbage-collect stale resumes: targets that are no longer in tmux's
    # list-windows output, or older than 30 min.
    now = int(time.time())
    live_targets = {f"{w['session']}:{w['index']}" for w in windows}
    for sid in list(_resuming):
        entry = _resuming[sid]
        if entry["target"] not in live_targets or now - entry["started_at"] > RESUME_EXPIRY_S:
            del _resuming[sid]

    # Map session→pinned_dir for the frontend's "unmanaged tmux session"
    # detection: any session that appears in `windows` but isn't owned by
    # any non-archived project gets the adopt affordance.
    projects = all_projects()
    projects_view = [
        {"pinned_dir": k, **v}
        for k, v in projects.items()
        if not v.get("archived_at")
    ]

    from periscope.workspaces import all_workspaces
    workspaces_view = [
        v for v in all_workspaces().values() if not v.get("archived_at")
    ]

    # Track registry rows (non-archived), so the rail can render EMPTY goal
    # tracks — live windows alone can't surface a track with no tabs yet, and
    # a freshly created track must be visible to receive its first tab.
    from periscope.activity import all_tracks
    tracks_view = [
        {"id": t["id"], "name": t["name"], "repo": t["repo"]}
        for t in all_tracks()
        if not t.get("archived_at")
    ]

    # Alerts ride the state blob so the dashboard has one transport and one
    # clock — notify() kicks the hub, so a need_human surfaces immediately.
    # Reuses `windows` above rather than re-running the tmux fan-out.
    from periscope.channels import recent_alerts

    return {
        "windows": result,
        "projects": projects_view,
        "workspaces": workspaces_view,
        "tracks": tracks_view,
        "alerts": recent_alerts(windows),
        "ts": int(time.time()),
        "usage": cached_claude_usage(),
        "usage_plan": cached_plan_usage(),
    }


@router.get("/api/state")
def state():
    return build_state()

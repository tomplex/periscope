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
    _resuming, RESUME_EXPIRY_S,
    list_windows, update_focus_from_windows,
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


@router.get("/api/state")
def state():
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

    _channel_gc({w["pane_id"] for w in windows if w.get("pane_id")})

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

    # Drop the hidden commander pane from the SHIPPED list only. The raw
    # `windows` above must keep it so update_focus_from_windows, pid attach,
    # and _channel_gc all see it (gc'ing the commander's channel state every
    # 3s would tear down its MCP registration).
    from periscope import activity
    result = [w for w in result if not activity.is_commander_pane(w.get("pane_id", ""))]

    return {
        "windows": result,
        "projects": projects_view,
        "workspaces": workspaces_view,
        "ts": int(time.time()),
        "usage": cached_claude_usage(),
        "usage_plan": cached_plan_usage(),
    }

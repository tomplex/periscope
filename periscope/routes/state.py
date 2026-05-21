"""GET /api/state — the main dashboard poll endpoint.

Fan-out + assembly: list_windows, then build_window_view per pane
(in periscope.window_view), then batched stamp persistence + resume GC.

Polled every 3s from the browser; everything underneath is cached on
its own clock, so this handler is mostly orchestration.
"""

import time

from fastapi import APIRouter

from periscope.channels import _channel_gc
from periscope.panes import (
    _resuming, RESUME_EXPIRY_S,
    list_windows, update_focus_from_windows,
)
from periscope.pids import _attach_git_then_resolve_pids
from periscope.projects import all_projects
from periscope.store import set_window_fields_bulk
from periscope.usage import cached_claude_usage, cached_scraped_usage
from periscope.window_view import build_window_view

router = APIRouter()


@router.get("/api/state")
def state():
    windows = list_windows()
    update_focus_from_windows(windows)
    _attach_git_then_resolve_pids(windows)
    now_ts = int(time.time())

    result = []
    stamp_updates: list[tuple[str, int, int]] = []
    for w in windows:
        view, stamp_update = build_window_view(w, now_ts)
        result.append(view)
        if stamp_update is not None:
            stamp_updates.append(stamp_update)

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

    return {
        "windows": result,
        "projects": projects_view,
        "ts": int(time.time()),
        "usage": cached_claude_usage(),
        "usage_scrape": cached_scraped_usage(),
    }

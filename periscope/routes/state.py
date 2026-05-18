"""GET /api/state — the main dashboard poll endpoint.

Fan-out + assembly: list_windows, then build_window_view per pane
(in periscope.views), then batched stamp persistence + resume GC.

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
from periscope.store import _STATE, _STATE_LOCK, _write_state
from periscope.usage import cached_claude_usage, cached_scraped_usage
from periscope.views import build_window_view

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

    # Single lock acquisition + single write covers every pane in this poll.
    if stamp_updates:
        with _STATE_LOCK:
            wblock = _STATE.setdefault("windows", {})
            dirty = False
            for pid, completed, acked in stamp_updates:
                entry = wblock.setdefault(pid, {})
                if int(entry.get("completed_at") or 0) != completed:
                    entry["completed_at"] = completed
                    dirty = True
                if int(entry.get("acked_at") or 0) != acked:
                    entry["acked_at"] = acked
                    dirty = True
            if dirty:
                _write_state(_STATE)

    # Garbage-collect stale resumes: targets that are no longer in tmux's
    # list-windows output, or older than 30 min.
    now = int(time.time())
    live_targets = {f"{w['session']}:{w['index']}" for w in windows}
    for sid in list(_resuming):
        entry = _resuming[sid]
        if entry["target"] not in live_targets or now - entry["started_at"] > RESUME_EXPIRY_S:
            del _resuming[sid]

    return {
        "windows": result,
        "ts": int(time.time()),
        "usage": cached_claude_usage(),
        "usage_scrape": cached_scraped_usage(),
    }

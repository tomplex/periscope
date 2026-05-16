"""GET /api/state — the main dashboard poll endpoint.

Aggregates list_windows + per-pane capture + parse_pane + git/PR/LGTM
caches + channel state + persisted annotations into one payload. Also
runs the done-vs-idle refinement (using per-pid stamps persisted via
state.json) and GCs stale `_resuming` entries on each tick.

Polled every 3s from the browser; everything underneath is cached on
its own clock, so this handler is mostly fan-out + assembly.
"""

import time

from fastapi import APIRouter

from periscope.channels import (
    _CHANNELS_LOCK, _CHANNEL_REPLIES, _CHANNEL_UNREAD, _MCP_SESSIONS,
    _channel_gc,
)
from periscope.git_pr import cached_git_state, cached_pr_state
from periscope.lgtm import cached_lgtm_state
from periscope.panes import (
    _acted_at, _completed_at, _focused_at, _prev_state, _resuming,
    RESUME_EXPIRY_S,
    list_windows, parse_pane,
    smooth_is_claude, smooth_spinner,
    update_focus_from_windows,
)
from periscope.pids import _attach_git_then_resolve_pids
from periscope.store import _STATE, _STATE_LOCK, _write_state
from periscope.tmux import capture
from periscope.usage import cached_claude_usage, cached_scraped_usage

router = APIRouter()


@router.get("/api/state")
def state():
    windows = list_windows()
    update_focus_from_windows(windows)
    _attach_git_then_resolve_pids(windows)
    now_ts = int(time.time())
    # Accumulate (pid, completed_at, acked_at) tuples for stamp persistence
    # at the end of the loop. Single lock acquisition + single write covers
    # every pane in this poll.
    stamp_updates: list[tuple[str, int, int]] = []
    result = []
    for w in windows:
        target = f"{w['session']}:{w['index']}"
        pid = w.get("pid") or ""
        try:
            content = capture(target)
            parsed = parse_pane(content)
        except Exception as e:
            parsed = {"error": str(e), "state": "error", "is_claude": False}
        # Hysteresis: smooth out per-poll detection gaps so cards / modal
        # subtitles don't flicker between "thinking" and idle.
        parsed["spinner"] = smooth_spinner(target, parsed.get("spinner"))
        # is_claude stickiness: dialogs hide the bottom status line; without
        # this the card would flip to "shell" mid-prompt and lose its state
        # coloring + needs-input classification.
        parsed["is_claude"] = smooth_is_claude(target, parsed.get("is_claude", False))
        if not parsed["is_claude"]:
            parsed["state"] = "shell"
        # Spinner hysteresis can promote a momentarily-blank parse back to
        # "working" — but only if we're not already in a louder state.
        # needs-input must never be downgraded back to working: the dialog
        # commonly lingers below a stale spinner glyph in scrollback.
        if (
            parsed.get("is_claude")
            and parsed.get("spinner")
            and parsed.get("state") not in ("working", "needs-input")
        ):
            parsed["state"] = "working"

        # done-vs-idle refinement. Uses per-pid stamps (persisted via
        # state.json) so a server restart preserves the "Claude finished
        # something you haven't looked at" signal across the gap.
        #
        # Edge detection: if the previous parse was busy and now we're idle,
        # stamp `_completed_at` so the refinement below promotes us to
        # "done" until the user acknowledges via a periscope action.
        # Targets without a pid (rare — only if pid resolution failed)
        # skip persistence; the in-memory value still works for the
        # current process lifetime.
        prev = _prev_state.get(pid) if pid else None
        cur = parsed.get("state")
        if pid and prev in ("working", "needs-input") and cur == "idle":
            _completed_at[target] = now_ts
        if pid:
            _prev_state[pid] = cur

        # Pull persisted stamps; in-memory may be ahead (just bumped) or
        # behind (fresh process, never observed a transition this run).
        wblock = _STATE.get("windows", {})
        persisted = wblock.get(pid, {}) if pid else {}
        completed = max(_completed_at.get(target, 0), int(persisted.get("completed_at") or 0))
        acked = max(_acted_at.get(target, 0), int(persisted.get("acked_at") or 0))

        if cur == "idle" and parsed.get("is_claude") and completed > acked:
            parsed["state"] = "done"

        # Schedule a state.json write if either stamp is newer than what's
        # on disk. The write itself runs once, under the lock, after the
        # loop.
        if pid and (
            completed > int(persisted.get("completed_at") or 0)
            or acked > int(persisted.get("acked_at") or 0)
        ):
            stamp_updates.append((pid, completed, acked))

        git = cached_git_state(w.get("cwd", "")) or {}
        pr = cached_pr_state(w.get("cwd", ""), git.get("branch")) or {}
        lgtm = cached_lgtm_state(w.get("cwd", ""))

        # Channel state (added by 2026-05-14-channels-design.md).
        pane_id = w.get("pane_id") or ""
        with _CHANNELS_LOCK:
            channel_attached = pane_id in _MCP_SESSIONS if pane_id else False
            channel_unread = _CHANNEL_UNREAD.get(pane_id, 0) if pane_id else 0
            channel_replies = list(_CHANNEL_REPLIES.get(pane_id, [])) if pane_id else []

        # Persisted Claude-driven links (via the link_pr / link_linear MCP
        # tools). `linked_pr` overrides the auto-detected `pr` field — when
        # Claude has explicitly told us "this pane is for PR #N", we trust
        # that over heuristic title-bar parsing.
        linked_pr = persisted.get("linked_pr")
        linked_linear = persisted.get("linked_linear")
        if linked_pr:
            pr = dict(pr)
            pr["pr"] = str(linked_pr)
            pr["pr_linked"] = True
            # `ci` (CI glyph) is keyed to the auto-detected PR; an explicit
            # linked PR may not have a fresh CI signal until a future poll
            # resolves it. Drop the stale glyph rather than mislead.
            pr.pop("ci", None)

        result.append(
            {
                **w, **parsed, **git, **pr,
                "target": target,
                "focused_at": _focused_at.get(target, 0),
                # 0 means "never engaged through periscope" — stream view
                # filters these out; grid view sorts cards within each session
                # by acted_at desc (most-recently-opened leftmost).
                "acted_at": acked,
                "completed_at": completed,
                "channel_attached": channel_attached,
                "channel_unread": channel_unread,
                "channel_replies": channel_replies,
                "linked_linear": linked_linear,
                "lgtm": lgtm,
            }
        )
    _channel_gc({w["pane_id"] for w in windows if w.get("pane_id")})
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

"""Per-window view assembly for /api/state.

`build_window_view(w, now_ts)` is the per-pane assembly function
extracted from routes/state.py. It mutates panes._completed_at and
panes._prev_state as part of the done-vs-idle edge detection — that's
intentional (the route would do the same mutations inline) and
documented here so callers don't expect it to be pure.

Returns `(view_dict, stamp_update | None)`. The view_dict goes straight
into the /api/state response payload; stamp_update is a `(pid, completed,
acked)` triple when the state.json entry needs writing. The route
batches stamp_updates across all panes and writes them under one
_STATE_LOCK acquisition for efficiency.
"""

from typing import Optional

from periscope.channels import (
    _CHANNELS_LOCK, _CHANNEL_REPLIES, _CHANNEL_UNREAD, _MCP_SESSIONS,
)
from periscope.git_pr import cached_git_state, cached_pr_state
from periscope.lgtm import cached_lgtm_state
from periscope.panes import (
    _acted_at, _completed_at, _focused_at, _prev_state,
    parse_pane, smooth_is_claude, smooth_spinner,
)
from periscope.projects import resolve_project_for_window, get_project
from periscope.store import get_window
from periscope.tmux import capture
from periscope.worktrees import affiliation


def build_window_view(
    w: dict, now_ts: int,
) -> tuple[dict, Optional[tuple[str, int, int]]]:
    """Build the per-window dict the dashboard renders, plus an optional
    (pid, completed_at, acked_at) tuple if state.json needs persisting.

    Side effects (intentional — match the in-route behavior before
    extraction):
      - panes._completed_at[target] bumps on busy→idle edge
      - panes._prev_state[pid] records the current state for next poll
    """
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
    prev = _prev_state.get(pid) if pid else None
    cur = parsed.get("state")
    if pid and prev in ("working", "needs-input") and cur == "idle":
        _completed_at[target] = now_ts
    if pid:
        _prev_state[pid] = cur

    # Pull persisted stamps; in-memory may be ahead (just bumped) or
    # behind (fresh process, never observed a transition this run).
    persisted = get_window(pid) if pid else {}
    completed = max(_completed_at.get(target, 0), int(persisted.get("completed_at") or 0))
    acked = max(_acted_at.get(target, 0), int(persisted.get("acked_at") or 0))

    if cur == "idle" and parsed.get("is_claude") and completed > acked:
        parsed["state"] = "done"

    stamp_update: Optional[tuple[str, int, int]] = None
    if pid and (
        completed > int(persisted.get("completed_at") or 0)
        or acked > int(persisted.get("acked_at") or 0)
    ):
        stamp_update = (pid, completed, acked)

    git = cached_git_state(w.get("cwd", "")) or {}
    pr = cached_pr_state(w.get("cwd", ""), git.get("branch")) or {}
    lgtm = cached_lgtm_state(w.get("cwd", ""))

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
    linked_linear_title = persisted.get("linked_linear_title")
    linked_linear_status = persisted.get("linked_linear_status")
    if linked_pr:
        pr = dict(pr)
        pr["pr"] = str(linked_pr)
        pr["pr_linked"] = True
        # `ci` (CI glyph) is keyed to the auto-detected PR; an explicit
        # linked PR may not have a fresh CI signal until a future poll
        # resolves it. Drop the stale glyph rather than mislead.
        pr.pop("ci", None)

    project_key = resolve_project_for_window(w)
    project = get_project(project_key) if project_key else {}
    # `project_key` is already canonical (post-migration / post-create); pass
    # it directly without re-realpath. `affiliation` realpaths the cwd as
    # part of its classification, which is the only realpath this code
    # path needs to pay per poll.
    pinned_for_aff = project_key if project_key and project_key != "__main__" else None
    aff = affiliation(w.get("cwd", ""), pinned_for_aff, project.get("repo"))

    view = {
        **w, **parsed, **git, **pr,
        "target": target,
        # 0 means "never engaged through periscope" — stream view
        # filters these out; grid view sorts cards within each session
        # by acted_at desc (most-recently-opened leftmost).
        "focused_at": _focused_at.get(target, 0),
        "acted_at": acked,
        "completed_at": completed,
        "channel_attached": channel_attached,
        "channel_unread": channel_unread,
        "channel_replies": channel_replies,
        "linked_linear": linked_linear,
        "linked_linear_title": linked_linear_title,
        "linked_linear_status": linked_linear_status,
        "lgtm": lgtm,
        "project_pinned_dir": project_key,
        "project_name": project.get("name"),
        "project_archived": bool(project.get("archived_at")),
        "worktree_affiliation": aff,
    }
    return view, stamp_update

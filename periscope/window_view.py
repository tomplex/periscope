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


from periscope.channels import channel_state_for
from periscope.git_pr import cached_git_state, cached_pr_state
from periscope.lgtm import cached_lgtm_state
from periscope.panes import (
    parse_pane,
    recency_stamps_for,
    record_state_transition,
    smooth_is_claude,
    smooth_spinner,
)
from periscope.projects import get_project, resolve_project_for_window
from periscope.session_status import session_state_for
from periscope.store import get_window
from periscope.tmux import capture
from periscope.tracks import resolve_track_for_window
from periscope.turns import session_id_for_pane
from periscope.worktrees import affiliation

# Cache of the parsed-pane dict, keyed by (target, pane_id). Skips
# capture()+parse_pane()+smoothing on a poll when tmux reports no new output
# (window_activity unchanged) AND the cached state is quiet (idle/shell) — the
# only states where re-running smoothing / record_state_transition would be a
# no-op. Working/needs-input/error panes are never skipped. Keying on pane_id
# (not just target) avoids serving a stale parse if a closed window's
# session:index is reused by a new pane whose activity coincidentally matches.
# Bounded by pane count; stale entries for closed panes are harmless. Cleared
# between tests.
#
# Known minor staleness: a Claude pane that exits to a shell and then goes
# silent can stay cached as is_claude=True/idle until its next output (the
# 120s smooth_is_claude expiry only fires on a recapture). The card still
# shows idle; only the is_claude coloring lags. Accepted — the next real
# output recaptures and corrects it.
_view_cache: dict[tuple[str, str], dict] = {}

_QUIET_STATES = ("idle", "shell")


def build_window_view(
    w: dict, now_ts: int,
) -> tuple[dict, tuple[str, int, int] | None]:
    """Build the per-window dict the dashboard renders, plus an optional
    (pid, completed_at, acked_at) tuple if state.json needs persisting.

    Side effects (intentional — match the in-route behavior before
    extraction):
      - panes._completed_at[target] bumps on busy→idle edge
      - panes._prev_state[pid] records the current state for next poll
    """
    target = f"{w['session']}:{w['index']}"
    pid = w.get("pid") or ""

    activity = w.get("activity", 0)
    cache_key = (target, w.get("pane_id", ""))
    cached = _view_cache.get(cache_key)
    parsed: dict  # unify the cache-hit (dict copy) and capture/error branches
    if (
        cached is not None
        and cached["activity"] == activity
        and cached["parsed"].get("state") in _QUIET_STATES
    ):
        # No new output since last poll and the pane is quiet — reuse the
        # parsed result, skip the capture() subprocess + smoothing. Downstream
        # assembly (stamps, git, channel) still runs every poll below.
        parsed = dict(cached["parsed"])
    else:
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

        _view_cache[cache_key] = {"activity": activity, "parsed": dict(parsed)}

    # Authoritative state from the session status file (sessions/<pid>.json),
    # which replaces the scraped working/needs-input/idle signal whenever the
    # pane maps to a live Claude session. Applied every poll (outside the cache
    # branch) so a busy→waiting→idle transition shows even on a cache hit. Falls
    # through to the scraped state for shell/unknown status or unmapped panes.
    # A mapped live session is also proof the pane IS Claude (a dialog that
    # blanks the bottom status line no longer flips the card to "shell").
    sess = session_state_for(session_id_for_pane(w.get("pane_id", "")))
    if sess:
        parsed["is_claude"] = True
        parsed["state"] = sess["state"]
        parsed["needs_input"] = sess["state"] == "needs-input"
        parsed["asked_question"] = False
        parsed["waiting_for"] = sess.get("waiting_for")

    # done-vs-idle refinement. Uses per-pid stamps (persisted via
    # state.json) so a server restart preserves the "Claude finished
    # something you haven't looked at" signal across the gap.
    cur = parsed.get("state")
    record_state_transition(pid, target, cur, now_ts)

    # Pull persisted stamps; in-memory may be ahead (just bumped) or
    # behind (fresh process, never observed a transition this run).
    stamps = recency_stamps_for(target)
    persisted = get_window(pid) if pid else {}
    completed = max(stamps["completed_at"], int(persisted.get("completed_at") or 0))
    acked = max(stamps["acted_at"], int(persisted.get("acked_at") or 0))

    if cur == "idle" and parsed.get("is_claude") and completed > acked:
        parsed["state"] = "done"

    stamp_update: tuple[str, int, int] | None = None
    if pid and (
        completed > int(persisted.get("completed_at") or 0)
        or acked > int(persisted.get("acked_at") or 0)
    ):
        stamp_update = (pid, completed, acked)

    git = cached_git_state(w.get("cwd", "")) or {}
    pr = cached_pr_state(w.get("cwd", ""), git.get("branch")) or {}
    lgtm = cached_lgtm_state(w.get("cwd", ""))

    channel = channel_state_for(w.get("pane_id") or "")

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
        # Keep `pr` an int to match the auto-detected path (pr_state_for
        # yields gh's `number` as an int); linked_pr is persisted as int too.
        pr["pr"] = int(linked_pr)
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

    # Function-scope import: workspaces → activity, avoiding any import-order
    # surprise at module load. Resolves the pane's workspace tag (or None).
    from periscope.workspaces import resolve_workspace_for_window
    workspace_id = resolve_workspace_for_window(w)

    view = {
        **w, **parsed, **git, **pr,
        "target": target,
        # 0 means "never engaged through periscope" — stream view
        # filters these out; grid view sorts cards within each session
        # by acted_at desc (most-recently-opened leftmost).
        "focused_at": stamps["focused_at"],
        "acted_at": acked,
        "completed_at": completed,
        "channel_attached": channel["attached"],
        "channel_unread": channel["unread"],
        "channel_alerts": channel["alerts"],
        "open_tabs": persisted.get("open_tabs") or [],
        "active_tab": persisted.get("active_tab") or "pane",
        "linked_linear": linked_linear,
        "linked_linear_title": linked_linear_title,
        "linked_linear_status": linked_linear_status,
        "lgtm": lgtm,
        # The track this tab belongs to (the new organizational primitive).
        # workspace_id / project_pinned_dir are kept until the frontend stops
        # reading them (Task 12/13); track_id supersedes both.
        "track_id": resolve_track_for_window(w),
        "workspace_id": workspace_id,
        "project_pinned_dir": project_key,
        "project_name": project.get("name"),
        "project_archived": bool(project.get("archived_at")),
        "worktree_affiliation": aff,
    }
    return view, stamp_update

"""Channels: in-process MCP server bound to /tmp/periscope-mcp.sock.

Each Claude pane spawns a thin `channel_shim.py` subprocess (the documented
stdio MCP entry point Claude requires), which connects to the unix socket
and proxies bytes between Claude's stdio and our socket. All MCP logic —
tool registration, capability declaration, notification emission — lives
here.

Locking: `_CHANNELS_LOCK` (threading.Lock) protects the alert log and
session registry. Separate from `_STATE_LOCK` because channel state is
touched from both sync request handlers (FastAPI threadpool) and async
MCP handlers; threading.Lock works correctly from both whereas
asyncio.Lock only blocks coroutines.

State.json writes go through `periscope.store.set_window_fields`; this
module never touches `_STATE` directly. `list_windows`/`note_focus`/
`note_action` come from `periscope.panes`, `_attach_git_then_resolve_pids`
from `periscope.pids` — no bridges to server.py remain in this module.

Socket lifecycle: this module never `os.unlink`s `MCP_SOCKET_PATH` on
shutdown. The FastAPI `lifespan` in `server.py` owns shutdown cleanup; the
listener here only removes a stale socket file on startup before binding.
"""

import asyncio
import contextlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, TypedDict

from periscope import store, tracks, usage
from periscope import tmux as tmux_mod
from periscope.config import MCP_SOCKET_PATH
from periscope.log import log
from periscope.panes import list_windows, note_action, note_focus
from periscope.pids import _attach_git_then_resolve_pids, stamp_new_window
from periscope.store import get_window, set_window_fields
from periscope.tabs import open_tab
from periscope.tmux import _run, _tmux_mutate, tmux

CHANNEL_INSTRUCTIONS = """\
You are running inside periscope, a dashboard the user has open in their
browser that watches every tmux pane on this machine. Periscope shows the
user what every Claude session is doing across all their panes at once.
The pane this channel is attached to is identified by $TMUX_PANE on the
server side; you don't need to address it explicitly.

You have several tools that mutate periscope's UI for this pane. Use them
proactively — the user doesn't need to ask. The triggers below are
specific enough that you won't over-call them:

- link_pr(number): when you identify the user is working on a specific
  GitHub PR — a #N reference in their message, in `git status` / `git log`
  / branch name, or in CLAUDE.md — AND periscope's pane card doesn't
  already show a #PR badge. Periscope auto-detects PRs from Claude's
  title bar; if you know there's one and it isn't surfaced, link it.

- link_linear(id, title?, status?): when you identify a Linear ticket
  in the user's message, branch name, or commit history (TEAM-123
  format, e.g. FAR-456). Periscope doesn't auto-detect Linear tickets,
  so explicit linking is the only way to surface them on the card. Pass
  `title` and `status` whenever you know them — if you fetched the
  ticket through the Linear MCP, you already have both.

- notify(message, kind="done"): when you finish a substantial task the
  user asked for. One-sentence summary in `message`. Lets the user see
  at-a-glance that this pane is done and what you did, without opening
  the modal.

- notify(message, kind="need_human"): when you're blocked and waiting on
  input. Pulses the pane card with a red border so the user notices the
  alert across a busy dashboard.

- notify(message, kind="info"): for status updates worth glancing at but
  not blocking on (e.g., "tests pass, about to commit"). Use sparingly
  — this is the lowest-signal kind and adds dashboard noise if overused.

- open_document(path, line?): open a file as a preview tab on this
  pane's periscope card — same as the user clicking it in the Files
  section. Use when you've produced or substantially edited a document
  the user will want to read (spec, design doc, report, HTML output).
  Quiet: the tab appears on this pane without stealing focus.

- spawn_claude(prompt, workspace?, session?, cwd?, name?, workspace_id?):
  launch a fresh Claude session in a new tmux window with the given prompt
  as its first message. The new window appears on the dashboard. Use when
  the user asks you to delegate, parallelize, or "spin up another session"
  — or when the task at hand decomposes into independent sub-tasks that
  each deserve their own focused context. `workspace` controls where it
  lands: "same" (default) nests it under YOUR card as fan-out/related
  sub-work (even if `cwd` is a different worktree); "new" makes it its
  own top-level dashboard item anchored to `cwd`'s worktree (new tab if
  that worktree already has a session) — for DISTINCT work tracked on its
  own. `workspace_id` (distinct from `workspace`) tags the spawned tab
  into a goal-scoped periscope workspace — get one from list_workspaces.
  Default `cwd` is your pane's working directory. Keep the returned
  target/pid so you can refer to the spawned pane later.

- list_workspaces(): list periscope workspaces (goal-scoped rail groups)
  with their ids, names, base repo/worktree, and live tagged-tab counts.
  Call to discover a workspace_id to pass to spawn_claude.

- search_history(query, project?, since?, limit?): full-text search
  over every past Claude session on this machine. Use it before
  re-debugging an error that smells familiar, when the user references
  past work ("like we did before", "that thing from last month", "how
  did we fix X"), or before re-deriving a non-obvious command or
  procedure a previous session likely worked out. Past sessions only —
  the current session and other live panes are not indexed. Follow up
  with get_history_session(session_id) to read the relevant
  conversation; default is the last 30 messages, page with offset.

- resume_session(session_id, tmux_session?): continue a past session
  found via search_history — spawns `claude --resume` in a new tmux
  window in the session's original project directory. Use when the
  user wants to pick old work back up ("continue where we left off
  on X"); for merely consulting past work, get_history_session is
  the right tool. The resumed session shows up on the dashboard;
  this session keeps running.

Messages going the other direction (periscope → you) arrive as
<channel source="periscope" ...> blocks at the start of each turn. A
<channel> block with meta.kind="dropped" is an infrastructure notice
(periscope's queue overflowed); it is not a message from the user.
"""

_CHANNELS_LOCK = threading.Lock()
# pane_id -> list[dict]   alert log (kind, severity, message, ts)
_CHANNEL_ALERTS: dict[str, list[dict]] = {}
# pane_id -> int          unread alert count, cleared when modal opens
_CHANNEL_UNREAD: dict[str, int] = {}
# pane_id -> MCP ServerSession reference. Presence is the "channel
# attached" indicator and the route for notification emission. Typed Any
# because the SDK's BaseSession-derived objects aren't reliably importable
# at module load (we lazy-load mcp); attribute access happens in
# emit_channel_event where the runtime shape is what matters.
_MCP_SESSIONS: dict[str, Any] = {}


def channel_state_for(pane_id: str) -> dict:
    """Channel state for a pane: whether an MCP session is attached, the
    unread-alert count, and a copy of the alert log. Empty when `pane_id`
    is blank. Holds `_CHANNELS_LOCK` internally so callers (window_view,
    routes/pane) need not reach into the channel dicts directly."""
    if not pane_id:
        return {"attached": False, "unread": 0, "alerts": []}
    with _CHANNELS_LOCK:
        return {
            "attached": pane_id in _MCP_SESSIONS,
            "unread": _CHANNEL_UNREAD.get(pane_id, 0),
            "alerts": list(_CHANNEL_ALERTS.get(pane_id, [])),
        }


def recent_alerts(windows: list[dict], limit: int = 100) -> list[dict]:
    """Flatten `_CHANNEL_ALERTS` into one reverse-chronological feed, joined
    against `windows` so alerts for dead panes drop out (matches _channel_gc's
    invariant). Takes the window list rather than calling list_windows() itself
    so build_state() can reuse the fan-out it already paid for.
    """
    by_pane = {w["pane_id"]: w for w in windows if w.get("pane_id")}

    with _CHANNELS_LOCK:
        snapshot = {pid: list(rs) for pid, rs in _CHANNEL_ALERTS.items()}

    items: list[dict] = []
    for pane_id, alerts in snapshot.items():
        w = by_pane.get(pane_id)
        if not w:
            continue
        items.extend(
            {
                "id": r.get("id") or "",
                "ts": int(r.get("ts") or 0),
                "kind": r.get("kind") or "info",
                "severity": r.get("severity") or "info",
                "message": r.get("message") or "",
                "pane_id": pane_id,
                "target": f"{w['session']}:{w['index']}",
                "session": w["session"],
                "index": w["index"],
                "name": w["name"],
            }
            for r in alerts
        )

    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


def _tool_result(body: dict) -> list:
    """Wrap a tool-result dict in the MCP TextContent list shape every tool
    handler returns. One place to change if the wire format ever grows an
    `isError` flag or a structured-content field."""
    from mcp import types

    return [types.TextContent(type="text", text=json.dumps(body))]


def _channel_gc(known_pane_ids: set[str]) -> None:
    """Drop alert state for panes that no longer exist. Session registry is
    GC'd by the connection handler on disconnect, not here."""
    with _CHANNELS_LOCK:
        for d in (_CHANNEL_ALERTS, _CHANNEL_UNREAD):
            for stale in [k for k in d if k not in known_pane_ids]:
                d.pop(stale, None)


def _record_alert(pane: str, message: str, kind: str = "info",
                  severity: str = "info") -> dict:
    """Append an alert to `pane`'s feed: write-through cache, durable mirror,
    and a hub kick so it lands on the dashboard now. Returns the entry.

    Shared by `notify` and by `report`'s fallback — a delegated result whose
    lead is gone still has to reach the user somehow."""
    entry = {
        "id": uuid.uuid4().hex,
        "message": message,
        "kind": kind,
        "severity": severity,
        "ts": int(time.time()),
    }
    with _CHANNELS_LOCK:
        _CHANNEL_ALERTS.setdefault(pane, []).append(entry)
        _CHANNEL_UNREAD[pane] = _CHANNEL_UNREAD.get(pane, 0) + 1

    # Durable mirror (survives restart, feeds the modal's merged Activity
    # stream). _CHANNEL_ALERTS above stays as the write-through cache for
    # the unread badge. record()'s positional args are
    # (scope_kind, scope_key, event_kind, text); the alert kind
    # (done/need_human/info) rides in `detail`.
    try:
        from periscope import activity
        activity.record("pane", pane, "alert", message,
                        detail=kind, at=entry["ts"])
    except Exception:
        log.warning("activity.record failed for notify()", exc_info=True)

    # Alerts ride the pushed state blob — kick the hub so a need_human lands on
    # the dashboard (and fires its native notification) now rather than on the
    # next steady tick. That immediacy is the whole point of an alert.
    from periscope import state_hub
    state_hub.kick()
    return entry


def _do_notify_tool(pane: str, arguments: dict):
    """Tool implementation for `notify` — appends to the per-pane alert log
    and bumps the unread count. Surfaces in periscope's UI on next poll."""
    kind = arguments.get("kind", "info")
    severity = arguments.get("severity", "info")
    _record_alert(pane, arguments["message"], kind, severity)
    return _tool_result({"ok": True, "kind": kind, "severity": severity})


def rehydrate_alerts_from_events(max_age_s: int = 86400) -> int:
    """Repopulate the in-memory alert cache from the durable events log on boot.
    _CHANNEL_ALERTS is a write-through cache; after a `bin/periscope restart` it
    starts empty while tmux (and its pane ids) live on, so the alert feed would
    show nothing until the next notify(). Reload recent alerts so a pane that
    called notify(need_human) before the restart still surfaces. id/severity
    aren't persisted — id is regenerated, severity defaults to info; the feed
    and the need_human badge only key on message/kind/ts. Returns the count."""
    from periscope import activity
    try:
        rows = activity.alert_events_since(int(time.time()) - max_age_s)
    except Exception:
        log.warning("alert rehydrate failed", exc_info=True)
        return 0
    n = 0
    with _CHANNELS_LOCK:
        # Panes with live alerts before rehydrate (a notify() racing startup)
        # are skipped WHOLESALE — snapshot once, not per-row, or the first
        # appended alert would mask a pane's remaining rows and double-count.
        preexisting = {p for p, rs in _CHANNEL_ALERTS.items() if rs}
        for pane_id, at, message, kind in rows:
            if pane_id in preexisting:
                continue
            _CHANNEL_ALERTS.setdefault(pane_id, []).append({
                "id": uuid.uuid4().hex, "message": message,
                "kind": kind, "severity": "info", "ts": at,
            })
            _CHANNEL_UNREAD[pane_id] = _CHANNEL_UNREAD.get(pane_id, 0) + 1
            n += 1
    return n


def is_commander(handle: str) -> bool:
    """True iff `handle` is a commander handle (the cmdr: prefix). The handle is a
    self-asserted token set at dispatch; we trust the prefix because the MCP
    socket is owner-only (0o600). The job-table cross-check was dropped: a
    commander's cmdr:<token> is a fresh per-dispatch uuid, distinct from claude's
    own session id, so it isn't in the jobs table to validate against."""
    return handle.startswith("cmdr:")


def _resolve_window(match) -> tuple[str, str]:
    """Find the first `list_windows()` entry satisfying `match`, resolve its
    persistent @periscope_id (minting one if the window is new), and return
    `(pid, pane_id)`. Returns `("", "")` if no window matches — e.g. the
    pane has vanished from tmux's list-windows.

    `match` is a predicate over a window dict; callers vary only in the
    lookup key (pane_id %N vs (session, index))."""
    for w in list_windows():
        if match(w):
            _attach_git_then_resolve_pids([w])
            return w.get("pid") or "", w.get("pane_id") or ""
    return "", ""


def _resolve_pid_for_pane(pane_id: str) -> str:
    """Find the persistent @periscope_id (pid) for a tmux %N pane id.
    Mints a fresh pid if the window hasn't been seen before. Returns ""
    if the pane has vanished from tmux's list-windows."""
    pid, _ = _resolve_window(lambda w: w.get("pane_id") == pane_id)
    return pid


def _stale_handle_error(handle: str) -> str:
    """Error text for an unresolvable handle. Names the recovery, because the
    bare 'no live window' was read as 'the agent is dead': one session
    respawned a duplicate worker onto a task the original was still doing."""
    return (
        f"no live window for handle {handle} — this usually means the handle "
        "changed, NOT that the pane exited. Call list_claudes to re-resolve it "
        "(match on name/cwd), or pass the pane id (%N) as the handle, which is "
        "stable. Verify with peek before concluding anything died."
    )


def _resolve_window_by_pid(handle: str) -> tuple[str, str, dict]:
    """Resolve an @periscope_id handle to (pid, pane_id, window).

    Matches on `pid_raw` — the stamped @periscope_id on the raw list_windows
    row — BEFORE resolution, because resolution attaches `pid` only after a
    match (raw rows carry pid_raw, not pid). peek/terminate read
    session/index off the returned window dict. Returns ("", "", {}) when no
    live window matches.

    Also accepts a tmux pane id (`%N`) as the handle. A pid can change under a
    pane — re-mint, or a rebind that misses after a tmux server restart — and
    the observed failure was always the same shape: `spawn_claude` returned a
    pid, minutes later `send_to` said "no live window" while the pane was alive
    at the SAME pane_id under a new pid. Callers already hold that pane_id
    (spawn_claude and list_claudes both return it), so accepting it turns a
    dead end into a working address. One session read the dead handle as death
    and spawned a duplicate agent onto the same task."""
    if not handle:
        return "", "", {}
    wins = list_windows()
    match = next((w for w in wins if w.get("pid_raw") == handle), None)
    if match is None and handle.startswith("%"):
        match = next((w for w in wins if w.get("pane_id") == handle), None)
    if match is None:
        return "", "", {}
    # Resolve against the FULL window list, never just the match: a
    # single-window pass has an incomplete `carried` set, so an unstamped
    # window could rebind onto an id another live window is still wearing.
    _attach_git_then_resolve_pids(wins, full_roster=True)
    return match.get("pid") or "", match.get("pane_id") or "", match


def _do_link_pr_tool(pane: str, arguments: dict):
    """Persist a linked PR number on the window's state.json entry.
    Overrides the auto-detected `pr` field when present."""
    try:
        number = int(arguments["number"])
    except (KeyError, TypeError, ValueError):
        body = {"ok": False, "error": "number must be an integer"}
        return _tool_result(body)

    pid = _resolve_pid_for_pane(pane)
    if not pid:
        body = {"ok": False, "error": f"could not resolve pid for pane {pane}"}
        return _tool_result(body)

    set_window_fields(pid, linked_pr=number)
    body = {"ok": True, "linked_pr": number, "pid": pid}
    return _tool_result(body)


def _do_link_linear_tool(pane: str, arguments: dict):
    """Persist a linked Linear ticket on the window's state.json entry.

    `id` is required; `title` and `status` are optional metadata. Each call
    fully describes the link — omitted title/status clear any prior value
    (set_window_fields drops keys set to None), so a re-link with just an id
    won't leave stale metadata pointing at a different ticket."""
    ticket_id = str(arguments.get("id", "")).strip()
    if not ticket_id:
        body = {"ok": False, "error": "id is required and must be non-empty"}
        return _tool_result(body)

    title = str(arguments.get("title", "")).strip()
    status = str(arguments.get("status", "")).strip()

    pid = _resolve_pid_for_pane(pane)
    if not pid:
        body = {"ok": False, "error": f"could not resolve pid for pane {pane}"}
        return _tool_result(body)

    set_window_fields(
        pid,
        linked_linear=ticket_id,
        linked_linear_title=title or None,
        linked_linear_status=status or None,
    )
    body = {
        "ok": True,
        "linked_linear": ticket_id,
        "linked_linear_title": title,
        "linked_linear_status": status,
        "pid": pid,
    }
    return _tool_result(body)


def _do_open_document_tool(pane: str, arguments: dict):
    """Open a file as a preview tab on the pane's card — same as the user
    clicking it in the Inspector's Files section. Tabs are server-owned
    state (periscope.tabs, persisted per-pid in state.json), so the open
    lands on the browser's next /api/state poll, survives page refresh and
    server restart, and works even when no browser is currently open.
    Quiet: no rail-selection change."""
    path = str(arguments.get("path", "")).strip()
    if not path:
        body = {"ok": False, "error": "path is required and must be non-empty"}
        return _tool_result(body)

    line = arguments.get("line")
    if line is not None:
        try:
            line = int(line)
        except (TypeError, ValueError):
            body = {"ok": False, "error": "line must be an integer"}
            return _tool_result(body)

    if not os.path.isabs(path):
        cwd = tmux(
            "display-message", "-t", pane, "-p", "#{pane_current_path}"
        ).strip()
        path = os.path.join(cwd or os.path.expanduser("~"), path)
    path = os.path.normpath(path)

    if not os.path.isfile(path):
        body = {"ok": False, "error": f"no such file: {path}"}
        return _tool_result(body)

    pid = _resolve_pid_for_pane(pane)
    if not pid:
        body = {"ok": False, "error": f"could not resolve pid for pane {pane}"}
        return _tool_result(body)

    open_tab(pid, path, line)
    body = {"ok": True, "path": path, "line": line, "pid": pid}
    return _tool_result(body)


def _plain_pane_snapshot(target: str) -> str:
    """Capture the visible tail of `target` without SGR escapes.

    `periscope.tmux.capture()` preserves escapes (`-e`) for parse_pane's
    color-sensitive logic. We need a plain string for substring searches
    against rendered TUI text — the channel-consent dialog title gets
    interleaved with `\\x1b[...m` codes under `-e`, breaking
    `"Loading development channels" in snap` checks.
    """
    return tmux("capture-pane", "-t", target, "-p", "-S", "-30")


def _dev_channels_consent_visible(target: str) -> bool:
    """Whether Claude's --dangerously-load-development-channels consent
    dialog is currently up in `target`:

        WARNING: Loading development channels
        ...
        ❯ 1. I am using this for local development
           2. Exit
        Enter to confirm · Esc to cancel

    Default selection is option 1; bare Enter confirms. Until dismissed
    the input chooser accepts only digit keys, so any send-keys text or
    paste-buffer aimed at the window is silently discarded."""
    return "Loading development channels" in _plain_pane_snapshot(target)


def _folder_trust_visible(target: str) -> bool:
    """Whether Claude's first-run 'Do you trust the files in this folder?'
    dialog is up in `target` (it precedes the dev-channels consent when the
    cwd isn't an already-trusted Claude folder — e.g. the commander's $HOME):

        Do you trust the files in this folder?
        ...
        ❯ 1. Yes, I trust this folder
           2. No, exit

    Default selection is the trust option; bare Enter confirms. Like the
    consent chooser, it swallows non-digit input until dismissed."""
    snap = _plain_pane_snapshot(target)
    return "trust this folder" in snap or "Do you trust" in snap


def dismiss_dev_channels_consent_bg(target: str, max_wait_s: float = 5.0) -> None:
    """Fire a background thread that polls `target` for the consent dialog
    and sends Enter to confirm option 1. No-op if the dialog never appears
    within `max_wait_s` (e.g. future Claude versions that persist the ack).

    Used by the window-spawn routes (`/api/window/new`, `+claude` tab,
    worktree spawn) so the user doesn't have to manually dismiss the
    dialog before typing. `_do_spawn_claude_tool` inlines the same poll
    (with a follow-up wait for the input handler) because it has a
    queued prompt to paste."""
    from periscope.log import _bg

    def _worker() -> None:
        # 100ms-grain polling matches the spawn_claude tool's pace.
        # Total wall-clock work is dominated by `tmux capture-pane`, not
        # the sleep, so a tighter interval mostly burns CPU for no win.
        steps = max(1, int(max_wait_s / 0.1))
        for _ in range(steps):
            time.sleep(0.1)
            if _dev_channels_consent_visible(target):
                tmux("send-keys", "-t", target, "Enter")
                return

    _bg(f"dismiss-dev-channels-consent[{target}]", _worker)


def _spawner_label(parent_pid: str) -> str:
    """Human-readable name for a spawner handle — its tmux window name, falling
    back to the bare handle. A name ('world-model-driver') tells the child what
    it is working under; a hex handle tells it nothing."""
    for w in list_windows():
        if w.get("pid_raw") == parent_pid:
            return w.get("name") or parent_pid
    return parent_pid


async def _do_spawn_claude_tool(pane: str, arguments: dict):
    """Spawn a new tmux window running `claude`, deliver an initial prompt.

    Async because Claude's TUI needs ~1.5s to mount before it can absorb a
    paste — using time.sleep here would block the event loop for every
    other pane's MCP connection sharing it."""
    prompt = str(arguments.get("prompt", "")).strip()
    if not prompt:
        body = {"ok": False, "error": "prompt is required and must be non-empty"}
        return _tool_result(body)

    # Caller's pane → its session + cwd. Commanders have no pane (cmdr:<id> is
    # not a tmux target), so skip the derivation — they always pass explicit cwd.
    # For a real pane: if it has vanished (rare — the connection would normally
    # drop first), tmux returns empty and we fall through to defaults.
    commander_caller = is_commander(pane)
    if commander_caller:
        caller_session, caller_cwd = "", ""
    else:
        info = tmux(
            "display-message", "-t", pane, "-p", "#{session_name}|#{pane_current_path}",
        ).strip()
        caller_session, _, caller_cwd = info.partition("|")

    cwd = str(arguments.get("cwd") or caller_cwd or os.path.expanduser("~")).strip()
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    name = str(arguments.get("name") or "").strip()

    # Worktree creation: when `branch` is given, create the worktree off `repo`
    # first and spawn the worker INTO it — cwd is forced to the new worktree path
    # (workspace="new", its own rail item). One call = worktree + placed worker,
    # so the commander owns worktree creation rather than delegating it.
    branch = str(arguments.get("branch") or "").strip()
    if branch:
        repo = str(arguments.get("repo") or "").strip()
        if not repo:
            return _tool_result({"ok": False, "error": "branch requires repo (the repo to branch from)"})
        from periscope import worktree_spawn
        try:
            wt = worktree_spawn.spawn_worktree(repo, branch)
        except Exception as e:
            return _tool_result({"ok": False, "error": f"worktree create failed: {e}"})
        cwd = wt["path"]
        arguments = {**arguments, "workspace": "new"}

    # Where the spawned pane lands in the dashboard. "same" (default) keeps it
    # a window in the caller's session, so fan-out / related sub-work nests
    # under the caller's rail item (the rail is session-anchored). "new"
    # anchors it to its cwd's worktree as its OWN rail item — a separate
    # project-backed session — for distinct work tracked on its own. The
    # session name is created fresh, or new-tabbed into when it already owns
    # the worktree; resolve_worktree_session registers the project + dedupes a
    # foreign-name clash, and returns None when cwd isn't in a git repo (no
    # worktree to anchor → fall back to the caller's session).
    from periscope import activity, open_ops
    workspace = str(arguments.get("workspace") or "same").strip().lower()
    if commander_caller:
        # The commander is ALWAYS cwd-anchored: it has no pane (cmdr:<id> is not
        # a tmux target), so there is no caller session to derive from. Force the
        # cwd-resolution path and refuse a non-git cwd rather than fall back to a
        # (nonexistent) caller session.
        anchored = open_ops.resolve_worktree_session(cwd)
        if anchored is None:
            body = {"ok": False,
                    "error": "cwd is not in a git repo — the commander's spawns "
                             "must target a repo/worktree dir"}
            return _tool_result(body)
        session, project = anchored
    else:
        anchored = open_ops.resolve_worktree_session(cwd) if workspace == "new" else None
        if anchored:
            session, project = anchored
        else:
            session = str(arguments.get("session") or caller_session or "spawned").strip()

    # Create the session if missing, otherwise add a window to it. Both
    # paths use `-P -F #{window_index}` so we know the spawned slot — with
    # base-index 1 in tmux.conf, hardcoding :0 would silently target the
    # wrong window (see /api/window/new resume notes).
    # An omitted account means "wherever there is room", not "the default": a
    # spawning Claude cannot see either subscription's limits, and the second
    # one exists precisely so work lands where the first has none left.
    # best_account degrades to "default" when usage is unknown, so a spawn is
    # never blocked or delayed on it.
    config_dir = store.account_config_dir(
        arguments.get("account") or usage.best_account()
    )
    code, _ = _run(["tmux", "has-session", "-t", session])
    if code != 0:
        ok, msg = _tmux_mutate(
            "new-session", "-d", "-s", session, "-c", cwd,
            *tmux_mod.env_args(config_dir),
            "-P", "-F", "#{window_index}",
            *(["-n", name] if name else []),
        )
        # `new-session -e` sets the SESSION env, so without this every later
        # window spawned into this session inherits this account — silently
        # billing them to the wrong subscription. This window's shell already
        # forked with the value; `new-window -e` has no such spillover.
        if ok and config_dir:
            tmux_mod.scrub_session_env(session)
    else:
        ok, msg = _tmux_mutate(
            "new-window", "-t", f"{session}:", "-c", cwd,
            *tmux_mod.env_args(config_dir),
            "-P", "-F", "#{window_index}",
            *(["-n", name] if name else []),
        )
    if not ok:
        body = {"ok": False, "error": msg}
        return _tool_result(body)
    try:
        index = int(msg)
    except ValueError:
        body = {"ok": False, "error": f"tmux returned unexpected index: {msg!r}"}
        return _tool_result(body)
    target = f"{session}:{index}"

    # Let the shell rc finish before the `claude` command line arrives, so
    # it runs as a real prompt entry rather than mid-rc echoed text. The
    # dev-channels flag is what makes the spawned Claude connect back to
    # periscope's MCP socket.
    from periscope.config import CLAUDE_EXEC
    await asyncio.sleep(0.1)
    tmux("send-keys", "-t", target, CLAUDE_EXEC, "Enter")

    # Dismiss the consent dialog (see _dev_channels_consent_*).
    for _ in range(50):  # up to 5s
        await asyncio.sleep(0.1)
        if _dev_channels_consent_visible(target):
            tmux("send-keys", "-t", target, "Enter")
            break

    # Even after dismissal the React TUI takes another beat to mount its
    # keyboard handler — `❯` (the input glyph) and `auto mode on` (status
    # line) both render before paste-buffer is reliably accepted. Wait
    # for the post-dialog state, then a small settle window.
    for _ in range(50):  # up to 5s
        await asyncio.sleep(0.1)
        snap = _plain_pane_snapshot(target)
        if "auto mode on" in snap and "Loading development channels" not in snap:
            await asyncio.sleep(0.5)
            break

    # Deliver the prompt via paste-buffer because send-keys strips embedded
    # newlines (see CLAUDE.md key invariant 5). Same buffer-name uuid trick
    # as `_send_to_target` so concurrent spawns don't trample each other.
    buf = f"spawn-{uuid.uuid4().hex[:8]}"
    tmux("set-buffer", "-b", buf, prompt)
    tmux("paste-buffer", "-d", "-p", "-b", buf, "-t", target)
    # Same 100ms bracketed-paste delay as /api/send — without it the Enter
    # can land before the paste applies, submitting empty input.
    await asyncio.sleep(0.1)
    tmux("send-keys", "-t", target, "Enter")

    note_focus(target)
    note_action(target)

    # Mint + stamp a fresh unique @periscope_id for the brand-new window.
    # Do NOT resolve via rebind here: a just-created window has no stamp yet,
    # and resolving a single window (empty taken-set) lets _rebind_pid match it
    # to a LIVE window's entry on (branch, cwd) — stealing that pid. Since the
    # child inherits the caller's cwd by default, that collision is the common
    # case, and the provenance write below would then corrupt the caller's own
    # row (spawned_by pointing at itself). stamp_new_window mints a
    # guaranteed-unique id, sidestepping rebind entirely.
    pid = stamp_new_window(target)
    pane_id = tmux("display-message", "-t", target, "-p", "#{pane_id}").strip()

    # An explicit spawn name is the lead's deliberate label for the task it
    # delegated — recorded so the narrator locks it instead of drifting it
    # (narrator.is_spawn_named). Only when `name` was passed: without it tmux
    # auto-names off the command/dir, which carries no intent to protect.
    if name and pid:
        set_window_fields(pid, spawn_name=name)

    # Provenance breadcrumb: record who spawned this child so report() knows
    # where "back" is. Pure metadata, no ownership — a severed child simply
    # never calls report(). Guard on both ids so a vanished caller doesn't
    # write a junk link.
    parent_pid = _resolve_pid_for_pane(pane)
    if parent_pid and pid:
        set_window_fields(pid, spawned_by=parent_pid)
        # Tell the child it HAS a lead. `report` is in its tool list either way,
        # but nothing otherwise says the tool applies to it or that returning a
        # result is expected — so every brief had to hand-write "report to your
        # spawner" and hope. Pushed rather than prepended to the prompt: the
        # prompt is the user's brief and stays verbatim.
        await emit_channel_event(
            pane_id,
            f"You were spawned by another Claude ({_spawner_label(parent_pid)}). "
            "When you finish, are blocked, or hit a decision above your remit, "
            "call the `report` tool — it routes back to them automatically and "
            "needs no handle from you.",
            {"kind": "provenance"},
        )

    # workspace="new": persist the rail placement now that the window is
    # stamped. The item already surfaces from live state (the project is
    # registered, so the session groups under its repo), but place_in_rail
    # records the ordering — same server-side placement the unified-open
    # route does. pid_raw is the just-stamped @periscope_id.
    if anchored:
        pane_pids = [w["pid_raw"] for w in list_windows()
                     if w["session"] == session and w.get("pid_raw")]
        open_ops.place_in_rail(session, project, pane_pids or [pid])

    # Tag the spawned pane into a track (the grouping authority). Precedence:
    # an explicit workspace_id arg (a track id now) wins; else the anchored
    # ("new") path tags the repo-default track for the worktree's repo; else
    # ("same") the spawn nests under the CALLER's track so a fan-out from a
    # promoted tab inherits that tab's track rather than falling back to the
    # repo-default. Every tag keys on the just-stamped @periscope_id, never
    # the rotating %N.
    tagged_workspace = None
    ws_id = str(arguments.get("workspace_id") or "").strip()
    if pid and ws_id:
        # workspace_id is a track id: validate it exists and isn't archived.
        row = activity.get_track(ws_id)
        if row and not row.get("archived_at"):
            tracks.move_pane(pid, ws_id)
            tagged_workspace = ws_id
    elif pid and anchored:
        tracks.move_pane(pid, tracks.repo_default_track(project["repo"]))
    elif pid and not commander_caller:
        # "same" mode: inherit the caller's track. parent_pid can be "" when
        # the caller vanished — resolve falls through to the cwd's
        # repo-default, logged so a mis-bucketed spawn is diagnosable.
        if not parent_pid:
            log.info("spawn tag: caller pid unresolved, %s falls to repo-default", pid)
        tracks.move_pane(pid, tracks.resolve_track_for_window(
            {"pid": parent_pid, "cwd": caller_cwd}))

    body = {
        "ok": True,
        "target": target,
        "session": session,
        "index": index,
        "pid": pid,
        "pane_id": pane_id,
        "workspace_id": tagged_workspace,
    }
    return _tool_result(body)


def _do_create_workspace_tool(pane: str, arguments: dict):
    """Create a track (goal-scoped rail group). Response key stays `workspace_id`
    so the spawn/resume arg stays compatible — a commander creates a track here,
    then passes its id as `workspace_id`."""
    name = str(arguments.get("name", "")).strip()
    if not name:
        return _tool_result({"ok": False, "error": "name is required"})
    base_repo = (arguments.get("base_repo") or None)
    tk = tracks.create_track(name=name, repo=base_repo)
    return _tool_result({"ok": True, "workspace_id": tk["id"], "name": tk["name"]})


def _open_descriptor(arguments: dict):
    """dict -> open_ops.Descriptor. Mirrors routes/open.py:_to_descriptor but
    over a tool-args dict and raising ValueError (the tool maps it to an error)."""
    from periscope import open_ops
    path = arguments.get("path")
    repo = arguments.get("repo")
    branch = arguments.get("branch")
    pr = arguments.get("pr")
    if path and not (repo or branch or pr):
        return open_ops.PathTarget(path=str(path))
    if repo and branch and pr is None and not path:
        return open_ops.BranchTarget(repo=str(repo), branch=str(branch))
    if repo and pr is not None and not (path or branch):
        return open_ops.PRTarget(repo=str(repo), pr=int(pr))
    raise ValueError("exactly one of {path | repo+branch | repo+pr} required")


def _do_open_tool(pane: str, arguments: dict):
    """Open a path / branch / PR into the rail (creates a worktree for repo+branch)."""
    from periscope import open_ops
    # Broad except on purpose: _open_descriptor raises ValueError for bad args,
    # but open_target's branch/PR paths (spawn_worktree / fetch_pr_into_worktree)
    # raise arbitrary git/subprocess errors — all become a clean tool error frame.
    try:
        descriptor = _open_descriptor(arguments)
        result = open_ops.open_target(descriptor)
    except Exception as e:
        return _tool_result({"ok": False, "error": str(e)})
    return _tool_result({"ok": True, "tmux_session": result.tmux_session,
                         "repo": result.repo, "pane_id":
                         getattr(result, "agent_pane_id", result.claude_pane_id)})


def _do_catalog_tool(pane: str, arguments: dict):
    """List discoverable repos + their worktrees (dormant + live)."""
    from periscope import open_ops
    return _tool_result({"ok": True, **open_ops.build_catalog()})


def _do_search_history_tool(pane: str, arguments: dict):
    """FTS search over the history index, trimmed for token economy: the
    full normalized row carries UI-only fields (counts, rerank metadata,
    jsonl_path); an LLM caller needs just enough to pick a session to
    drill into."""
    query = str(arguments.get("query", "")).strip()
    if not query:
        return _tool_result({"ok": False, "error": "query is required"})

    # Lazy import, same as routes/history.py — keeps channel startup
    # independent of the history package's sqlite deps.
    import history
    rows = history.search(
        query,
        project=arguments.get("project") or None,
        since=arguments.get("since"),
        limit=int(arguments.get("limit", 10)),
    )
    results = [{
        "session_id": r["session_id"],
        "project_path": r["project_path"],
        "branch": r["branch"],
        "started_at": r["started_at"],
        "duration_s": r["duration_s"],
        "summary": r["summary"],
        "tags": r["tags"],
        "files_touched": r["files_touched"][:10],
        "first_user_msg": r["first_user_msg"],
    } for r in rows]
    return _tool_result({"ok": True, "results": results})


def _do_get_history_session_tool(pane: str, arguments: dict):
    """One indexed session: metadata + a compact slice of its messages.

    Slicing happens on the compacted list (post tool-result stripping) so
    offset/limit count meaningful messages, and the default tail-30 lands
    on the end of the conversation where resolutions live."""
    session_id = str(arguments.get("session_id", "")).strip()
    if not session_id:
        return _tool_result({"ok": False, "error": "session_id is required"})

    from history.search import compact_messages, get_session
    data = get_session(session_id)
    if data is None:
        return _tool_result({"ok": False, "error": "unknown session_id"})

    msgs = compact_messages(data["messages"])
    total = len(msgs)
    offset = int(arguments.get("offset", -30))
    limit = max(1, int(arguments.get("limit", 30)))
    start = offset if offset >= 0 else max(0, total + offset)
    meta = {k: v for k, v in data.items()
            if k not in ("messages", "jsonl_path")}
    # notable_cmds entries are full multi-line scripts — live testing showed
    # them dominating the payload (~25 scripts dwarfed the 30-message slice).
    meta["notable_cmds"] = [
        c if len(c) <= 150 else c[:150] + "…[truncated]"
        for c in (meta.get("notable_cmds") or [])[:10]
    ]
    body = {
        "ok": True,
        **meta,
        "total_messages": total,
        "offset": start,
        "messages": msgs[start:start + limit],
    }
    return _tool_result(body)


def _do_resume_session_tool(pane: str, arguments: dict):
    """Resume a past session via the same guarded path as the dashboard's
    resume button — liveness check, double-resume check, sentinel-session
    auto-create all live in `_window_new_resume`; this just adapts its
    HTTPException contract to the tool-result shape."""
    session_id = str(arguments.get("session_id", "")).strip()
    if not session_id:
        return _tool_result({"ok": False, "error": "session_id is required"})
    tmux_session = str(arguments.get("tmux_session") or "resumes").strip()
    ws_id = str(arguments.get("workspace_id") or "").strip()

    # Lazy import: routes/sessions.py imports from this module at load time
    # (dismiss_dev_channels_consent_bg) — a top-level import back at it
    # would be circular.
    from fastapi import HTTPException

    from periscope.config import CLAUDE_EXEC
    from periscope.routes.sessions import _window_new_resume
    try:
        result = _window_new_resume(
            tmux_session, f"{CLAUDE_EXEC} --resume {session_id}",
            session_id, "resume",
            # Resuming is a Claude launch like any other: unnamed account means
            # "where there is room", not the exhausted default.
            account=arguments.get("account") or usage.best_account(),
        )
    except HTTPException as e:
        return _tool_result({"ok": False, "error": str(e.detail)})

    # Tag the resumed pane into a track (the grouping authority) so it surfaces
    # under that group instead of the default "dev" bucket — the same tagging
    # spawn_claude does. workspace_id is a track id now. pane_tracks keys on the
    # @periscope_id, so stamp the brand-new window first (mint-fresh, no rebind
    # — same reasoning as _do_spawn_claude_tool) and tag by the returned pid.
    if ws_id:
        from periscope import activity
        row = activity.get_track(ws_id)
        if row and not row.get("archived_at"):
            target = result.get("target") or f"{result.get('session')}:{result.get('index')}"
            new_pid = stamp_new_window(target)
            tracks.move_pane(new_pid, ws_id)
            result = {**result, "workspace_id": ws_id, "pid": new_pid}
    return _tool_result(result)


async def emit_channel_event(pane: str, content: str, meta: dict | None = None) -> bool:
    """Push a `notifications/claude/channel` event to the Claude connected
    on `pane`. Returns True on send, False if no session attached.

    On a successful send the full message is mirrored into the pane's
    Activity timeline (a 'channel' event) so the user can see what periscope
    pushed in."""
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCNotification

    with _CHANNELS_LOCK:
        session = _MCP_SESSIONS.get(pane)
    if session is None:
        return False

    notification = JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params={"content": content, "meta": meta or {}},
    )
    try:
        await session._write_stream.send(  # type: ignore[attr-defined]
            SessionMessage(message=JSONRPCMessage(notification))
        )
    except Exception:
        return False

    kind = (meta or {}).get("kind") or "message"
    try:
        from periscope import activity
        activity.record("pane", pane, "channel", content, detail=kind)
    except Exception:
        log.warning("activity.record failed for channel push", exc_info=True)
    return True


async def _mcp_listener() -> None:
    """Bind the unix socket and accept connections from channel_shim.py.
    Each connection runs a fresh per-pane MCP Server in _handle_mcp_connection."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(MCP_SOCKET_PATH)

    server = await asyncio.start_unix_server(
        _handle_mcp_connection, path=MCP_SOCKET_PATH
    )
    os.chmod(MCP_SOCKET_PATH, 0o600)
    log.info("MCP listener bound to %s", MCP_SOCKET_PATH)
    try:
        async with server:
            await server.serve_forever()
    finally:
        # Tearing the listener down on uvicorn reload needs all three steps:
        # close() stops accept, close_clients() closes the transports, and a
        # bounded wait_closed() lets the per-pane handlers unwind. The bound
        # is essential — anyio-bridged MCP handlers don't always notice their
        # underlying socket is gone, and an unbounded wait_closed() wedges
        # the lifespan handler so every dev save hangs the server.
        server.close()
        server.close_clients()  # ty: ignore[unresolved-attribute]  # asyncio.Server.close_clients() exists since 3.13
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(server.wait_closed(), timeout=1.0)


async def _handle_mcp_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Per-connection MCP handler: read hello frame, dispatch to
    _run_mcp_for_pane, clean up session registry on close."""
    pane = ""
    # Filled in by _run_mcp_for_pane when it registers this connection's
    # session; read back in the finally to deregister only our own.
    registered: dict[str, Any] = {}
    try:
        # Hello frame: a single JSON line {"pane": <handle>}. The "pane" key is a
        # private wire-contract name — the value is an opaque caller handle, either
        # a tmux pane id (%N) or a commander id (cmdr:<session_id>). `pane` below
        # may therefore hold a cmdr: handle, NOT a real tmux target.
        hello_line = await reader.readline()
        if not hello_line:
            return
        try:
            hello = json.loads(hello_line.decode().strip())
            pane = hello.get("pane", "")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not (pane.startswith(("%", "cmdr:"))):
            return

        await _run_mcp_for_pane(reader, writer, pane, registered)
    except Exception as e:
        log.warning("MCP connection %s failed: %s", pane or "<no-pane>", e)
    finally:
        if pane:
            with _CHANNELS_LOCK:
                # Deregister ONLY the session this connection registered.
                # `_MCP_SESSIONS` is keyed by pane, and the shim reconnects on
                # the same pane after a periscope restart — so an unconditional
                # pop lets a dying connection evict the live successor that
                # already replaced it. The pane then reads attached=False and
                # every inbound push (send_to, report, provenance) is refused
                # for a Claude that is in fact connected.
                if _MCP_SESSIONS.get(pane) is registered.get("sess"):
                    _MCP_SESSIONS.pop(pane, None)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def pane_channel_ready(pane_id: str) -> bool | None:
    """Whether the claude in `pane_id` can actually RECEIVE inbound pushes.
    None when no claude resolves for the pane (nothing to assert).

    Registering for `notifications/claude/channel` is gated client-side on the
    server being named in the session's channel flags, so a Claude started
    WITHOUT `config.CHANNEL_FLAG` drops every push while still connecting the
    shim — which leaves `_MCP_SESSIONS` populated and `attached` true. Periscope
    can't learn that from the notification (notifications have no response), but
    the flag is right there in the process's argv.

    The common way to land flagless: `claude` is a zsh function resolved at
    shell startup, so a long-lived shell keeps a stale copy and re-running
    `claude` in it launches without the flag no matter how recently. Panes
    periscope spawned go through `config.CLAUDE_EXEC` and are always ready.
    """
    if is_commander(pane_id):
        return None
    from periscope import config, session_status
    pid = session_status.pane_claude_pids().get(pane_id)
    if pid is None:
        return None
    _kids, cmds = session_status.proc_table()
    cmd = cmds.get(pid)
    if cmd is None:
        return None
    return config.CHANNEL_FLAG in cmd


async def _deliver(pane_id: str, message: str, caller_pane: str) -> dict:
    """Shared send_to/report delivery: self-send guard, channel push, and the
    not-attached error mapping. Returns the tool body dict (callers augment
    success with their own fields)."""
    if pane_id == caller_pane:
        return {"ok": False, "error": "refusing to send to your own pane"}
    # A flagless target accepts the push and silently discards it, so reporting
    # success here is a lie the sender acts on — it waits for a reply that can
    # never come, or assumes a delegated instruction was received. Refuse
    # BEFORE sending, and say what the operator has to do about it.
    # to_thread: the readiness probe forks `ps`/`tmux` on a cache miss, and
    # this runs on the event loop — a blocking call here stalls every pane
    # mirror and MCP connection for the duration.
    if await asyncio.to_thread(pane_channel_ready, pane_id) is False:
        return {"ok": False, "error": (
            "target Claude was started without periscope's channel flag, so it "
            "cannot receive messages — it must be restarted in a FRESH shell "
            "(its current shell holds a stale `claude` wrapper). Reach it by "
            "another route, or ask the user to restart that pane."
        )}
    sent = await emit_channel_event(pane_id, message)
    if not sent:
        return {"ok": False, "error": "target not attached to periscope channel"}
    return {"ok": True}


async def _do_send_to_tool(pane: str, arguments: dict):
    """Deliver a message to another live Claude by handle (pid). Wakes the
    recipient via the channel rail."""
    handle = str(arguments.get("handle", "")).strip()
    message = str(arguments.get("message", "")).strip()
    if not handle:
        return _tool_result({"ok": False, "error": "handle is required"})
    if not message:
        return _tool_result({"ok": False, "error": "message is required"})
    _pid, pane_id, _w = _resolve_window_by_pid(handle)
    if not pane_id:
        return _tool_result({"ok": False, "error": _stale_handle_error(handle)})
    body = await _deliver(pane_id, message, pane)
    if body.get("ok"):
        # "queued", never "delivered": the recipient only surfaces a channel
        # notification on its NEXT turn, so a busy pane can take minutes. The
        # old bare ok:true was read as delivered-and-read, and when the target
        # didn't react the sender re-sent the same directive four times.
        body = {
            "ok": True, "handle": handle, "pane_id": pane_id,
            "delivery": "queued",
            "verify": ("peek(handle) — your message appears as a turn with "
                       "role 'channel' once the target actually processes it. "
                       "That block is written by the recipient's own Claude, "
                       "so it is proof of receipt. Give a busy pane time "
                       "before concluding anything."),
        }
    return _tool_result(body)


async def _do_report_tool(pane: str, arguments: dict):
    """Report back to the pane that spawned this one. Sugar over send_to,
    routed via the spawned_by provenance breadcrumb — the worker doesn't carry
    the parent's handle, the server knows it."""
    message = str(arguments.get("message", "")).strip()
    if not message:
        return _tool_result({"ok": False, "error": "message is required"})
    caller_pid = _resolve_pid_for_pane(pane)
    spawned_by = get_window(caller_pid).get("spawned_by") if caller_pid else None
    if not spawned_by:
        reason = "this pane has no recorded spawner"
    else:
        _pid, pane_id, _w = _resolve_window_by_pid(spawned_by)
        if not pane_id:
            reason = f"spawner {spawned_by} is no longer live"
        else:
            body = await _deliver(pane_id, message, pane)
            if body.get("ok"):
                return _tool_result({"ok": True, "delivered_to": spawned_by})
            reason = str(body.get("error"))

    # A lead that exits before its worker finishes is the NORM, not an edge
    # case — leads routinely delegate and quit. Hard-failing here destroyed the
    # result: the worker had done the work, had nowhere to put it, and fell
    # back to hand-writing a file or messaging a pane it guessed at. Surface it
    # to the user instead, so a report always lands somewhere.
    _record_alert(pane, message, kind="done")
    return _tool_result({
        "ok": True, "delivered_to": "user", "reason": reason,
        "note": ("Could not reach a spawner, so your report was surfaced to the "
                 "user as an alert on this pane instead. It was NOT lost, and "
                 "no further action is needed."),
    })


def _mark_shared_trees(rows: list[dict], trees: list[str]) -> list[dict]:
    """Stamp each row with the handles of the OTHER rows sitting in the same
    working tree. `trees` is parallel to `rows`.

    Two Claudes in one tree is invisible otherwise, and it is not a benign
    coincidence: a supervisor ran a verification `cargo test` against a tree a
    worker was mid-edit in, the shared target-dir lock SIGTERM'd the worker's
    running test binary, and the worker read that death as a hang in its own
    work — which is what the supervisor then interrupted it over. The gate
    result was worthless too: a tree someone else is mutating can go green on
    a half-written state as easily as red."""
    by_tree: dict[str, list[str]] = {}
    for row, tree in zip(rows, trees, strict=True):
        if tree:
            by_tree.setdefault(tree, []).append(row["handle"])
    for row, tree in zip(rows, trees, strict=True):
        peers = [h for h in by_tree.get(tree, []) if h != row["handle"]]
        row["cwd_shared_with"] = peers
    return rows


async def _do_list_claudes_tool(pane: str, arguments: dict):
    """List all live Claude panes with their handles, so the caller can
    discover, message (send_to), peek, or terminate them. Flat — supports peer
    discovery and handoff, not just a spawn subtree.

    Claude identity is probed per pane with a stateless capture+parse_pane — NOT
    build_window_view, which mutates poll state-transition tracking. The
    capture fan-out is offloaded to a thread so it doesn't block the event
    loop; pid resolution (which writes state.json and is not thread-safe) runs
    in the loop first, mirroring the /api/state route's ordering."""
    from periscope.activity import pane_status_lines
    from periscope.git_pr import cached_git_signal
    from periscope.panes import parse_pane
    from periscope.tmux import capture

    windows = list_windows()
    _attach_git_then_resolve_pids(windows, full_roster=True)  # attaches pid, strips pid_raw (not thread-safe)
    statuses = pane_status_lines()

    def _collect():
        out = []
        trees: list[str] = []   # parallel to `out`: each row's working tree
        for w in windows:
            target = f"{w['session']}:{w['index']}"
            try:
                parsed = parse_pane(capture(target))
            except Exception:
                continue
            if parsed.get("agent") != "claude":
                continue
            pane_id = w.get("pane_id") or ""
            status = statuses.get(pane_id)
            pid = w.get("pid") or ""
            signal = cached_git_signal(w.get("cwd") or "") or {}
            out.append({
                "handle": pid,
                "name": w.get("name"),
                "session": w.get("session"),
                "cwd": w.get("cwd"),
                # Progress signal, absent for a non-git cwd. `dirty` separates
                # reading/stuck (0) from actively writing (n>0);
                # head_committed_at is epoch seconds so staleness is judged
                # against the wall clock rather than a caller's poll count.
                "head": signal.get("head"),
                "head_subject": signal.get("head_subject"),
                "head_committed_at": signal.get("head_committed_at"),
                "dirty": signal.get("dirty"),
                # Named `_inferred` because it is a Haiku paraphrase of recent
                # activity, NOT the pane's own report of what it is doing. Read
                # as first-person intent it has caused a supervising Claude to
                # interrupt and nearly terminate a worker that was working
                # correctly — the summary named a test the worker had never
                # touched. The suffix is the whole fix: it travels with the
                # value into any context that reads this tool's output.
                "status_line_inferred": status[0] if status else None,
                "attached": channel_state_for(pane_id)["attached"],
                # `attached` (a shim is connected) and `channel_ready` (that
                # Claude registered for inbound pushes) are DIFFERENT states,
                # and a pane can be attached-but-deaf. False here means send_to
                # and report will refuse — pick another route rather than
                # delegating into a hole.
                "channel_ready": pane_channel_ready(pane_id),
                "spawned_by": get_window(pid).get("spawned_by"),
            })
            # Worktree root when the cwd is in a repo, else the resolved cwd —
            # so two panes in different subdirs of one tree still group.
            trees.append(signal.get("toplevel")
                         or os.path.realpath(w.get("cwd") or "") or "")
        return _mark_shared_trees(out, trees)

    claudes = await asyncio.to_thread(_collect)
    return _tool_result({"ok": True, "claudes": claudes})


def _do_list_workspaces_tool(pane: str, arguments: dict):
    """List tracks (goal-scoped rail groups) with their ids, so the caller can
    pass a workspace_id to spawn_claude and fan tabs into a goal. `tagged_tabs`
    counts the track's currently-live tagged tabs (db rows for dead panes are
    excluded by intersecting with live pane ids). The LOOSE catchall is excluded.
    Response key stays `workspaces` for arg compatibility."""
    from periscope.activity import all_tracks, pane_track_map

    # pane_tracks keys on @periscope_id — liveness intersects the stamped ids
    # of live windows, not %N pane ids.
    live_pids = {p for w in list_windows()
                 if (p := (w.get("pid_raw") or "").strip())}
    counts: dict[str, int] = {}
    for pid, tid in pane_track_map().items():
        if pid in live_pids:
            counts[tid] = counts.get(tid, 0) + 1

    out = [
        {
            "id": tk["id"],
            "name": tk.get("name"),
            "base_repo": tk.get("repo"),   # tracks have no base_worktree
            "tagged_tabs": counts.get(tk["id"], 0),
        }
        for tk in all_tracks()
        if not tk.get("archived_at") and tk["id"] != tracks.LOOSE_KEY
    ]
    return _tool_result({"ok": True, "workspaces": out})


# A `full` peek inlines every tool input AND result, which measured 52-59KB of
# single-line JSON for ~20 turns of a real session — once large enough to blow
# the caller's whole tool budget, leaving it to re-extract the last few turns
# with a throwaway script. Summary detail and a small default limit are what
# make peek usable as the "what is this pane ACTUALLY doing" probe that
# list_claudes' inferred status line points callers at.
_PEEK_DEFAULT_LIMIT = 10
_PEEK_MAX_LIMIT = 200


def _do_peek_tool(pane: str, arguments: dict):
    """Read another Claude's recent transcript by handle, without messaging it.
    Reads directly off the pane's recorded session id — refuses when there is
    none rather than guessing by cwd (which on a shared cwd would return a
    sibling pane's transcript). Bypasses get_turns_for_pane precisely because
    that helper re-derives pane_id and has the cwd fallback.

    `summary` detail reuses history.search.compact_messages — the same reshape
    the MCP history tools use: text plus one-line tool summaries, results
    dropped except a Bash head."""
    from history.search import compact_messages
    from periscope.turns import (
        channel_messages_from_jsonl,
        jsonl_for_session,
        messages_from_jsonl,
        session_id_for_pane,
    )

    handle = str(arguments.get("handle", "")).strip()
    if not handle:
        return _tool_result({"ok": False, "error": "handle is required"})
    detail = str(arguments.get("detail") or "summary")
    if detail not in ("summary", "full"):
        return _tool_result({"ok": False, "error": "detail must be 'summary' or 'full'"})
    raw_limit = arguments.get("limit")
    try:
        # `is None`, not falsiness: `limit: 0` must clamp to 1 rather than
        # silently widening to the default.
        limit = _PEEK_DEFAULT_LIMIT if raw_limit is None else int(raw_limit)
    except (TypeError, ValueError):
        return _tool_result({"ok": False, "error": "limit must be an integer"})
    limit = max(1, min(limit, _PEEK_MAX_LIMIT))

    _pid, pane_id, _w = _resolve_window_by_pid(handle)
    if not pane_id:
        return _tool_result({"ok": False, "error": _stale_handle_error(handle)})
    sid = session_id_for_pane(pane_id)
    if sid is None:
        return _tool_result({"ok": False, "error": f"no recorded session for handle {handle}"})
    jsonl = jsonl_for_session(sid)
    if jsonl is None:
        return _tool_result({"ok": False, "error": "session transcript not found"})
    messages = messages_from_jsonl(str(jsonl))
    # Compact BEFORE slicing: compact_messages drops turns that carry neither
    # text nor a non-skipped tool, so slicing first would silently return
    # fewer than `limit` useful turns.
    if detail == "summary":
        messages = compact_messages(messages)
    # Merge in the channel pushes that landed. Both parsers drop them (they are
    # meta events), which made peek useless as the delivery check it is reached
    # for: the sender sees no block and reads that as "my message vanished".
    # Merged by timestamp so a reply reads in order against what prompted it.
    inbound = channel_messages_from_jsonl(str(jsonl))
    if inbound:
        messages = sorted([*messages, *inbound], key=lambda m: m.get("ts_ms") or 0)
    return _tool_result({
        "ok": True, "handle": handle, "detail": detail,
        "session_id": sid,
        # The receipt: a channel block exists only because the recipient's own
        # Claude processed the notification. Absent = not delivered (yet).
        "channel_messages_seen": len(inbound),
        "turns": messages[-limit:],
    })


def _do_terminate_tool(pane: str, arguments: dict):
    """Kill another Claude's tmux window by handle — cleanup after delegation,
    or tear down a stuck worker. Refuses to kill the caller's own pane."""
    handle = str(arguments.get("handle", "")).strip()
    if not handle:
        return _tool_result({"ok": False, "error": "handle is required"})
    _pid, pane_id, window = _resolve_window_by_pid(handle)
    if not pane_id:
        return _tool_result({"ok": False, "error": f"no live window for handle {handle}"})
    if pane_id == pane:
        return _tool_result({"ok": False, "error": "refusing to terminate your own pane"})
    target = f"{window['session']}:{window['index']}"
    ok, msg = _tmux_mutate("kill-window", "-t", target)
    if not ok:
        return _tool_result({"ok": False, "error": msg})
    return _tool_result({"ok": True, "terminated": handle})


# --- MCP tool registry ---
# Each record co-locates a tool's name, JSON schema, and handler. `_list_tools`
# maps it to `types.Tool` objects (mcp `types` is lazy-imported, so the registry
# itself stays plain data); `_call_tool` dispatches by iterating it. Adding a
# tool is one record here plus one `_do_*` handler — no separate schema list and
# dispatch branch to keep in sync.
class _ChannelTool(TypedDict):
    name: str
    description: str
    inputSchema: dict[str, Any]
    handler: Callable[..., Any]


_CHANNEL_TOOLS: list[_ChannelTool] = [
    {
        "name": "notify",
        "description": (
            "Surface a message in periscope's UI for this pane. "
            "Use kind=\"need_human\" when blocked and waiting on the user, "
            "kind=\"done\" when the current task is complete, "
            "otherwise kind=\"info\"."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["info", "need_human", "done"],
                    "default": "info",
                },
                "severity": {
                    "type": "string",
                    "enum": ["info", "good", "warning", "bad"],
                    "default": "info",
                },
            },
            "required": ["message"],
        },
        "handler": _do_notify_tool,
    },
    {
        "name": "link_pr",
        "description": (
            "Link this pane to a GitHub PR by number. Use when the user "
            "is working on a specific PR and periscope's auto-detection "
            "hasn't surfaced it on the pane card (periscope reads PR "
            "URLs from Claude's title-bar status line; if the card "
            "shows no #PR badge but you know there is one, link it). "
            "Overrides the auto-detected PR until the user removes the "
            "link."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "number": {"type": "integer", "minimum": 1},
            },
            "required": ["number"],
        },
        "handler": _do_link_pr_tool,
    },
    {
        "name": "link_linear",
        "description": (
            "Link this pane to a Linear ticket. Use when the user is "
            "working on a Linear ticket. Periscope doesn't auto-detect "
            "Linear tickets, so this is the only way to surface one on "
            "the pane card. Pass `title` and `status` when you know "
            "them (e.g. you fetched the ticket via the Linear MCP) so "
            "the card shows what the ticket is, not just its id. Each "
            "call fully describes the link — omitting title/status "
            "clears any previously set value."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "pattern": r"^[A-Z]+-\d+$",
                    "description": "Ticket id, TEAM-123 format (e.g. FAR-456).",
                },
                "title": {
                    "type": "string",
                    "description": "Ticket title, shown alongside the id on the pane card.",
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Workflow state, free-form (e.g. 'In Progress', "
                        "'In Review', 'Done'). Rendered as a pill in the modal."
                    ),
                },
            },
            "required": ["id"],
        },
        "handler": _do_link_linear_tool,
    },
    {
        "name": "open_document",
        "description": (
            "Open a file as a preview tab on this pane's periscope card — "
            "the same view the user gets by clicking the file in the "
            "Inspector's Files section. Use when you've produced or "
            "substantially edited a document the user will want to read: "
            "a spec, design doc, report, README, or HTML output. Opens "
            "quietly — the tab appears on this pane without stealing the "
            "user's focus from whatever they're viewing. Path may be "
            "absolute or relative to this pane's working directory; "
            "optional `line` jumps the source view to that line."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File to open, absolute or relative to the pane's cwd.",
                },
                "line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional line number to jump to (forces source view).",
                },
            },
            "required": ["path"],
        },
        "handler": _do_open_document_tool,
    },
    {
        "name": "spawn_claude",
        "description": (
            "Spawn a fresh Claude Code session in a new tmux window "
            "and deliver an initial prompt to it. The new window "
            "appears on periscope's dashboard alongside this one. "
            "Use when: (1) the user explicitly asks to delegate, "
            "parallelize, or spin up another Claude session; "
            "(2) the current task decomposes into independent "
            "sub-tasks that benefit from focused, isolated "
            "contexts running concurrently. Set `workspace` to "
            "control where the spawn lands on the dashboard: "
            "\"same\" (default) for fan-out / sub-work that's part "
            "of YOUR current task — it nests under your card, even "
            "in a different worktree; \"new\" when the spawn is "
            "DISTINCT work the user would track separately — it "
            "becomes its own top-level item anchored to its "
            "worktree. Pass `workspace_id` to tag the spawned tab "
            "into a goal-scoped periscope workspace. Returns target "
            "/ session / index / pid / pane_id for the spawned pane "
            "— keep them so you can address it again later. "
            "The spawned session can send results back with the "
            "`report` tool, which routes to you automatically — it "
            "does NOT need your handle, so don't spend prompt "
            "budget telling it how to find you. Say what to report "
            "and when; `report` handles the addressing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Initial message to send to the spawned Claude session.",
                },
                "workspace": {
                    "type": "string",
                    "enum": ["same", "new"],
                    "description": (
                        "Where the spawn lands on the dashboard. \"same\" "
                        "(default): a window in YOUR session — fan-out / "
                        "related sub-work nests under your card (even if "
                        "`cwd` is a different worktree). \"new\": its own "
                        "top-level dashboard item, a session anchored to "
                        "`cwd`'s worktree (new tab if that worktree already "
                        "has one) — for DISTINCT work tracked separately. "
                        "\"new\" requires `cwd` to be inside a git repo; "
                        "otherwise it behaves like \"same\"."
                    ),
                },
                "session": {
                    "type": "string",
                    "description": "tmux session to spawn into (\"same\" workspace only). Defaults to the caller's session. Created if it doesn't exist.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory for the spawned window. Defaults to the caller's pane cwd. With workspace=\"new\", its worktree anchors the new dashboard item.",
                },
                "repo": {
                    "type": "string",
                    "description": "With `branch`: absolute path of the repo to branch from. Required when `branch` is set.",
                },
                "branch": {
                    "type": "string",
                    "description": (
                        "Create a fresh worktree on this branch (off `repo`) and "
                        "spawn the worker INTO it — one call makes the worktree and "
                        "places the worker. Forces workspace=\"new\". Use this to put "
                        "a worker in a new worktree (vs. delegating worktree creation)."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "Optional name for the new tmux window.",
                },
                "account": {
                    "type": "string",
                    "enum": ["default", "b"],
                    "description": (
                        "Which Claude subscription to run the spawned pane on. "
                        "OMIT THIS unless the user named an account: omitting "
                        "picks whichever subscription has the most remaining "
                        "usage, which is almost always what you want."
                    ),
                },
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Optional periscope workspace id (e.g. \"ws_auth-refactor\") "
                        "to tag the spawned tab into — it then renders under that "
                        "goal-scoped rail group. Use to fan out several tabs toward "
                        "one goal. Distinct from `workspace` (which controls tmux "
                        "placement). Unknown ids are ignored."
                    ),
                },
            },
            "required": ["prompt"],
        },
        "handler": _do_spawn_claude_tool,
    },
    {
        "name": "search_history",
        "description": (
            "Full-text search over past Claude Code sessions on this "
            "machine — every project, indexed when each session ends "
            "(the current session and other live panes are NOT in it). "
            "Use before re-debugging something that feels familiar, or "
            "to find how a past session solved a problem. Returns "
            "summaries + session ids; drill in with get_history_session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Keywords — error text, command names, file names, "
                        "concepts. Tokens are prefix-matched and ANDed."
                    ),
                },
                "project": {
                    "type": "string",
                    "description": (
                        "Absolute project path to filter to. Omit for "
                        "cross-project search."
                    ),
                },
                "since": {
                    "type": "integer",
                    "description": "Unix timestamp lower bound on session start.",
                },
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
        "handler": _do_search_history_tool,
    },
    {
        "name": "get_history_session",
        "description": (
            "Fetch one indexed session by id: metadata plus a slice of "
            "its conversation (text + one-line tool summaries, results "
            "stripped). Sessions can run hundreds of messages — fetch a "
            "slice, not the whole thing. Default is the last 30 messages "
            "(resolutions usually live at the end); pass offset/limit to "
            "page. Negative offset counts from the end."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "offset": {
                    "type": "integer",
                    "default": -30,
                    "description": "Message slice start; negative = from the end.",
                },
                "limit": {"type": "integer", "default": 30},
            },
            "required": ["session_id"],
        },
        "handler": _do_get_history_session_tool,
    },
    {
        "name": "resume_session",
        "description": (
            "Resume a past Claude Code session (a session_id from "
            "search_history) in a new tmux window — runs `claude --resume` "
            "in the original project directory. The resumed session "
            "appears on the dashboard alongside this one; it does NOT "
            "replace this session. Use when past work should be continued, "
            "not just read — for reference, get_history_session is enough. "
            "Refuses if the session looks live (transcript written within "
            "the last minute) or is already resumed in another window."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "History session id (from search_history results).",
                },
                "tmux_session": {
                    "type": "string",
                    "description": (
                        "tmux session to open the window in; created if "
                        "missing. Defaults to 'resumes' (the dashboard's "
                        "convention). Prefer setting workspace_id over guessing "
                        "a session name — the tag controls rail grouping."
                    ),
                },
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "A periscope workspace id (from list_workspaces) to tag "
                        "the resumed pane into, so it groups under that workspace "
                        "in the rail instead of the default 'dev' bucket. Use this "
                        "when the user names a workspace to resume into."
                    ),
                },
                "account": {
                    "type": "string",
                    "enum": ["default", "b"],
                    "description": (
                        "Which Claude subscription to run the resumed pane on. "
                        "OMIT THIS unless the user named an account: omitting "
                        "picks whichever subscription has the most remaining "
                        "usage, which is almost always what you want."
                    ),
                },
            },
            "required": ["session_id"],
        },
        "handler": _do_resume_session_tool,
    },
    {
        "name": "send_to",
        "description": (
            "Send a message to another live Claude pane by its handle "
            "(the pid returned by spawn_claude / list_claudes). The message "
            "wakes the recipient and arrives as a channel block it acts on. "
            "Use to delegate a task to, or nudge, another Claude. Errors if "
            "the handle resolves to no live window or the target isn't "
            "attached to periscope's channel. Success means QUEUED, not read: "
            "the recipient surfaces it on its next turn, which for a busy pane "
            "can be minutes. Confirm with peek (the message shows up there as "
            "a role 'channel' turn) rather than re-sending."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string", "description": (
                    "Target pid (from spawn_claude/list_claudes). A tmux pane "
                    "id (%N) also works and is stabler — prefer it if you have "
                    "one, and fall back to it if a pid stops resolving."
                )},
                "message": {"type": "string", "description": "Message to deliver."},
            },
            "required": ["handle", "message"],
        },
        "handler": _do_send_to_tool,
    },
    {
        "name": "report",
        "description": (
            "Report a message back to the Claude that spawned this pane. Use "
            "when you were delegated a task and want to return your result to "
            "your lead — it wakes them with your message. Always lands: if this "
            "pane has no recorded spawner, or the spawner has exited or cannot "
            "receive messages, the report is surfaced to the USER as an alert "
            "on this pane instead. The result field `delivered_to` says which "
            "happened — a spawner handle, or \"user\"."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message to send to your spawner."},
            },
            "required": ["message"],
        },
        "handler": _do_report_tool,
    },
    {
        "name": "list_claudes",
        "description": (
            "List every live Claude pane periscope can see, with each one's "
            "handle (pid), name, session, cwd, latest status line, whether "
            "it's attached to periscope's channel (messageable via send_to), "
            "and its spawner handle. Use to discover other Claudes before "
            "messaging, peeking, or terminating them. "
            "For a pane in a git repo it also carries `head` / `head_subject` / "
            "`head_committed_at` (epoch seconds) and `dirty` (count of changed "
            "or untracked files) — `dirty` is the best available progress "
            "signal, separating a pane that is reading or stuck (0) from one "
            "actively writing (n>0). Age HEAD against the wall clock, not "
            "against how many times you have polled. "
            "`cwd_shared_with` lists other panes in the SAME working tree. "
            "When it is non-empty, do not run builds, tests, or any command "
            "that writes to that tree: you will fight the other pane over "
            "build locks and kill its running processes, and your own result "
            "is meaningless because the tree is being mutated under you. Wait "
            "for a clean boundary, or ask that pane first. "
            "`status_line_inferred` is a MODEL-GENERATED SUMMARY of a pane's "
            "recent activity — not that pane's own report of its intent. It is "
            "routinely wrong about specifics: it has named files and tests a "
            "pane never touched. Never act on it as if the pane said it. Use "
            "`peek` to find out what a pane is actually doing before you "
            "interrupt, correct, or terminate it."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _do_list_claudes_tool,
    },
    {
        "name": "list_workspaces",
        "description": (
            "List periscope workspaces — goal-scoped rail groups that several "
            "Claude tabs can be tagged into. Returns each workspace's id, name, "
            "base repo/worktree, and how many live tabs are currently tagged "
            "into it. Call this to discover a workspace_id to pass to "
            "spawn_claude (its workspace_id arg) so a spawned tab joins that "
            "goal — do it first when fanning work out across a workspace."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _do_list_workspaces_tool,
    },
    {
        "name": "peek",
        "description": (
            "Read the recent transcript of another Claude pane by its handle, "
            "without sending it anything — use to check on a delegated "
            "worker's progress instead of waiting for a report. This is the "
            "SOURCE OF TRUTH for what a pane is doing; list_claudes' "
            "status_line_inferred is only a summary and is often wrong about "
            "specifics. Peek before you interrupt, correct, or terminate. "
            "Refuses if the pane has no recorded session yet. "
            "This is ALSO the delivery check for send_to: a message you sent "
            "appears here as a turn with role 'channel' once the target "
            "processes it, and `channel_messages_seen` counts them. That block "
            "is written by the recipient's own Claude, so its presence is "
            "proof of receipt and its absence means not-yet-delivered — do "
            "NOT re-send on a first miss, a busy pane surfaces a notification "
            "only on its next turn."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string", "description": (
                    "Target pid (from spawn_claude/list_claudes). A tmux pane "
                    "id (%N) also works and is stabler — prefer it if you have "
                    "one, and fall back to it if a pid stops resolving."
                )},
                "limit": {
                    "type": "integer",
                    "description": (
                        f"How many recent turns to return (default "
                        f"{_PEEK_DEFAULT_LIMIT}, max {_PEEK_MAX_LIMIT})."
                    ),
                },
                "detail": {
                    "type": "string",
                    "enum": ["summary", "full"],
                    "description": (
                        "summary (default): text plus one-line tool summaries. "
                        "full: every tool input and result inlined — tens of KB "
                        "for even a few turns, so pass a small limit with it."
                    ),
                },
            },
            "required": ["handle"],
        },
        "handler": _do_peek_tool,
    },
    {
        "name": "terminate",
        "description": (
            "Kill another Claude's tmux window by its handle. Use to clean up "
            "a worker after it's delegated its result, or to tear down a stuck "
            "one. Refuses to terminate your own pane. This is destructive — the "
            "window and its Claude session are gone."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string", "description": (
                    "Target pid (from spawn_claude/list_claudes). A tmux pane "
                    "id (%N) also works and is stabler — prefer it if you have "
                    "one, and fall back to it if a pid stops resolving."
                )},
            },
            "required": ["handle"],
        },
        "handler": _do_terminate_tool,
    },
    {
        "name": "create_workspace",
        "description": ("Create a periscope workspace — a goal-scoped rail group "
                        "that spawned tabs can be tagged into (pass the returned "
                        "workspace_id to spawn_claude). Args: name, optional base_repo."),
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string"},
            "base_repo": {"type": "string", "description": "absolute repo path (optional)"},
        }, "required": ["name"]},
        "handler": _do_create_workspace_tool,
    },
    {
        "name": "open",
        "description": ("Materialize a session into the rail. Exactly one of: "
                        "{path} to open a directory; {repo, branch} to open (and "
                        "create if absent) a worktree; {repo, pr} to fetch a PR "
                        "into a worktree. 'create a worktree for X' = open(repo, branch=X)."),
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}, "repo": {"type": "string"},
            "branch": {"type": "string"}, "pr": {"type": "integer"},
        }},
        "handler": _do_open_tool,
    },
    {
        "name": "catalog",
        "description": ("List discoverable repos and their worktrees (dormant + "
                        "live) to ground placement decisions. Git-subprocess-heavy "
                        "— call once per command and reuse; do not poll."),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _do_catalog_tool,
    },
]


async def _run_mcp_for_pane(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    pane: str,
    registered: dict[str, Any] | None = None,
) -> None:
    """Run a per-pane MCP Server over the given asyncio socket streams.

    Bridges asyncio byte streams ↔ anyio object streams that the MCP SDK
    consumes. Tool handlers close over `pane` so they target the right
    per-pane state without needing thread-local lookups."""
    # Lazy-imported — MCP SDK is heavy; only loaded once any pane connects.
    import anyio
    from mcp import types
    from mcp.server import Server
    from mcp.server.models import InitializationOptions
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, ServerCapabilities, ToolsCapability

    server = Server("periscope")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        # Capture this connection's session for notification emission. Claude
        # always calls tools/list during init, so this fires reliably and
        # before any tools/call could need it.
        try:
            sess = server.request_context.session  # type: ignore[attr-defined]
            with _CHANNELS_LOCK:
                _MCP_SESSIONS[pane] = sess
                if registered is not None:
                    registered["sess"] = sess
        except LookupError:
            pass
        return [
            types.Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in _CHANNEL_TOOLS
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        for tool in _CHANNEL_TOOLS:
            if tool["name"] == name:
                handler = tool["handler"]
                if asyncio.iscoroutinefunction(handler):
                    return await handler(pane, arguments)
                return handler(pane, arguments)
        raise ValueError(f"unknown tool: {name}")

    # Bridge: asyncio socket → anyio object stream of SessionMessage.
    read_send, read_recv = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_send, write_recv = anyio.create_memory_object_stream[SessionMessage](0)

    async def socket_reader_loop() -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = JSONRPCMessage.model_validate_json(line.decode())
                    await read_send.send(SessionMessage(message=msg))
                except Exception as e:
                    await read_send.send(e)
        finally:
            await read_send.aclose()

    async def socket_writer_loop() -> None:
        try:
            async with write_recv:
                async for sm in write_recv:
                    data = sm.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    ) + "\n"
                    writer.write(data.encode())
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass

    init_options = InitializationOptions(
        server_name="periscope",
        server_version="0.1.0",
        capabilities=ServerCapabilities(
            experimental={"claude/channel": {}},
            tools=ToolsCapability(listChanged=False),
        ),
        instructions=CHANNEL_INSTRUCTIONS,
    )

    async with anyio.create_task_group() as tg:
        tg.start_soon(socket_reader_loop)
        tg.start_soon(socket_writer_loop)
        try:
            await server.run(read_recv, write_send, init_options)
        finally:
            tg.cancel_scope.cancel()

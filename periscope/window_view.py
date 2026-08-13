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


import threading
import time
from dataclasses import dataclass
from pathlib import Path

from periscope import activity, session_status
from periscope.agent_processes import codex_process_for_pane
from periscope.channels import channel_state_for
from periscope.codex_sessions import codex_home
from periscope.codex_state import reconcile_codex_state, rollout_edge_for
from periscope.config import MEM_BAD_RSS_KB, MEM_WARN_AGE_S, MEM_WARN_RSS_KB
from periscope.git_pr import (
    cached_git_state,
    cached_linked_pr_state,
    cached_pr_state,
)
from periscope.lgtm import cached_lgtm_state
from periscope.panes import (
    clear_agent_state_transition,
    forget_agent,
    parse_pane,
    recency_stamps_for,
    record_agent_state_transition,
    record_state_transition,
    smooth_parsed,
)
from periscope.session_status import claude_proc_for, session_state_for
from periscope.store import get_accounts, get_window
from periscope.tmux import capture
from periscope.tracks import resolve_track_for_window, track_kind, track_label
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
# silent can stay cached as agent="claude"/idle until its next output (the
# 120s smooth_agent expiry only fires on a recapture). The card still
# shows idle; only the agent coloring lags. Accepted — the next real
# output recaptures and corrects it.
_view_cache: dict[tuple[str, str], dict] = {}

_QUIET_STATES = ("idle", "shell")
CODEX_UNKNOWN_GRACE_S = 5
_TRUSTED_CODEX_BINDING_EVIDENCE = frozenset(
    {"resume-explicit", "rollout-fallback", "launch-explicit"}
)


@dataclass(frozen=True)
class _CodexObservation:
    state: str
    session_id: str
    turn_id: str


# pane id -> (last valid observation, observed_at).  This is render-only
# hysteresis.  The causal completion baseline lives in panes and is updated
# only for valid observations.
_codex_last_valid: dict[str, tuple[_CodexObservation, int]] = {}
_codex_pid_panes: dict[str, str] = {}


def _codex_observation(
    pane_id: str, codex_live: bool | None
) -> _CodexObservation | None:
    """Resolve a trusted binding and rollout edge for one live Codex pane."""
    if codex_live is not True:
        return None
    binding = activity.get_agent_session(pane_id)
    if (
        binding is None
        or binding.provider != "codex"
        or binding.evidence not in _TRUSTED_CODEX_BINDING_EVIDENCE
        or not binding.session_path
    ):
        return None
    sessions_root = codex_home() / "sessions"
    edge = rollout_edge_for(
        Path(binding.session_path),
        session_id=binding.session_id,
        sessions_root=sessions_root,
    )
    reconciled = reconcile_codex_state(
        session_id=binding.session_id,
        process="live",
        rollout_edge=edge,
    )
    if (
        reconciled is None
        or reconciled.state not in {"working", "idle"}
        or edge is None
    ):
        return None
    return _CodexObservation(
        reconciled.state, binding.session_id, edge.turn_id
    )


def _apply_codex_state(
    parsed: dict,
    *,
    pane_id: str,
    pid: str,
    target: str,
    codex_live: bool | None,
    now_ts: int,
) -> bool:
    """Apply structured Codex state; return whether it was a valid opinion."""
    prior_pane = _codex_pid_panes.get(pid)
    if prior_pane is not None and prior_pane != pane_id:
        clear_agent_state_transition(pid)
        _codex_last_valid.pop(prior_pane, None)
    if pid:
        _codex_pid_panes[pid] = pane_id
    observation = _codex_observation(pane_id, codex_live)
    if observation is not None:
        parsed.update(
            state=observation.state,
            needs_input=False,
            asked_question=False,
            waiting_for=None,
        )
        record_agent_state_transition(
            pid,
            target,
            provider="codex",
            session_id=observation.session_id,
            turn_id=observation.turn_id,
            state=observation.state,
            now_ts=now_ts,
        )
        _codex_last_valid[pane_id] = (observation, now_ts)
        return True

    # Unknown has no transition opinion.  Briefly retain the last valid render
    # to avoid flicker, then make uncertainty explicit without manufacturing
    # idle/done.
    last = _codex_last_valid.get(pane_id)
    if last and now_ts - last[1] <= CODEX_UNKNOWN_GRACE_S:
        parsed["state"] = last[0].state
    else:
        parsed["state"] = "unknown"
    parsed.update(needs_input=False, asked_question=False, waiting_for=None)
    return False


# One account scan per POLL, not per window. `pane_config_dirs` forks `ps`
# once for the process table plus once more per candidate claude;
# build_window_view runs across ~20 windows on a 32-thread fan-out every 3s, so
# per-window would be hundreds of forks a poll. Same TTL-snapshot shape as
# session_status._claude_procs, plus a lock — without one the fan-out's cold
# start stampedes 32 concurrent scans instead of 31 waiting on the first.
# The scan costs ~100ms — two `ps` forks, and the cost is the forks themselves,
# not the number of processes probed. That lands on the /api/state hot path,
# where up to 32 fan-out threads block on it. A long TTL is safe because the
# data is immutable: a process's environment cannot change after exec, so a
# pane's account is fixed for the life of its Claude. Only a NEW process changes
# the mapping, and a new pane waiting one interval for its chip is harmless.
_ACCOUNTS_TTL_S = 15.0
_pane_accounts_lock = threading.Lock()
_pane_accounts_cache: tuple[float, dict[str, str], dict[str, str]] | None = None


def _pane_env_labels() -> tuple[dict[str, str], dict[str, str]]:
    """(accounts, profiles), each tmux pane id -> label for panes NOT on the
    default. Both come off the same live-process env scan, under one lock: they
    share a `ps eww` snapshot downstream, and two separately-locked callers
    would each pay the stampede this lock exists to prevent.

    Derived live via `session_status.pane_config_dirs` / `pane_profiles` —
    reused rather than reimplemented because they encode the `ps eww` trap
    (command and environment are concatenated with no delimiter, so a naive
    regex matches the variable NAME appearing inside some other process's
    command text).

    Never persisted. tmux recycles pane ids across a server restart, so a stored
    pane->account row would eventually be inherited by an unrelated later pane
    and mislabel which subscription it bills; the process environment is the one
    reading that cannot go stale.

    A config dir no registry entry claims reports "unknown", not the default:
    such a pane demonstrably is NOT on the default account (whose config_dir is
    ""), and reporting default would hide the chip — asserting the opposite of
    what is true, which is the exact mislabel the chip exists to prevent. The
    profile needs no such mapping: its env value IS the profile id.
    """
    global _pane_accounts_cache
    now = time.time()
    with _pane_accounts_lock:
        cached = _pane_accounts_cache
        if cached is not None and now - cached[0] < _ACCOUNTS_TTL_S:
            return cached[1], cached[2]
        by_dir: dict[str, str] = {}
        for a in get_accounts():
            cfg, aid = a.get("config_dir"), a.get("id")
            if cfg and aid:
                by_dir[cfg] = aid
        accounts = {
            pane_id: by_dir.get(cfg, "unknown")
            for pane_id, cfg in session_status.pane_config_dirs().items()
        }
        profiles = dict(session_status.pane_profiles())
        _pane_accounts_cache = (now, accounts, profiles)
        return accounts, profiles


def mem_signal(proc: dict | None) -> dict | None:
    """Cycle-hint for a claude process, from claude_proc_for's stats dict:
    "bad" at ≥MEM_BAD_RSS_KB, "warn" at ≥MEM_WARN_RSS_KB or ≥MEM_WARN_AGE_S,
    None otherwise — None renders nothing, so a healthy pane costs no rail
    space. Policy lives here (config thresholds); measurement stays in
    session_status, which is a stdlib-only leaf that can't import config."""
    if not proc:
        return None
    rss, age = proc["rss_kb"], proc["age_s"]
    if rss >= MEM_BAD_RSS_KB:
        tier = "bad"
    elif rss >= MEM_WARN_RSS_KB or age >= MEM_WARN_AGE_S:
        tier = "warn"
    else:
        return None
    return {"tier": tier, "rss_gb": round(rss / (1024 * 1024), 1), "age_s": age}


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

    window_activity = w.get("activity", 0)
    cache_key = (target, w.get("pane_id", ""))
    cached = _view_cache.get(cache_key)
    parsed: dict  # unify the cache-hit (dict copy) and capture/error branches
    if (
        cached is not None
        and cached["activity"] == window_activity
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
            parsed = {"error": str(e), "state": "error", "agent": None}

        # Hysteresis: smooth out per-poll detection gaps so cards / modal
        # subtitles don't flicker between "thinking" and idle.
        smooth_parsed(pane_id=w.get("pane_id", ""), parsed=parsed)

        _view_cache[cache_key] = {
            "activity": window_activity,
            "parsed": dict(parsed),
        }

    # Process evidence is independent of terminal output, so evaluate it even
    # on quiet cache hits. Claude's distinctive parser wins for overlapping
    # synthetic content; otherwise a live Codex executable identifies Codex.
    codex_live = codex_process_for_pane(
        w.get("pane_id", ""), w.get("pane_pid")
    )
    if codex_live is True and parsed.get("agent") != "claude":
        parsed["agent"] = "codex"
        if parsed.get("state") == "shell":
            parsed["state"] = "idle"
    elif codex_live is False and parsed.get("agent") == "codex":
        forget_agent(w.get("pane_id", ""), "codex")
        clear_agent_state_transition(pid)
        _codex_last_valid.pop(w.get("pane_id", ""), None)
        _codex_pid_panes.pop(pid, None)
        parsed["agent"] = None
        parsed["state"] = "shell"

    # Authoritative state from the session status file (sessions/<pid>.json),
    # which replaces the scraped working/needs-input/idle signal whenever the
    # pane maps to a live Claude session. Applied every poll (outside the cache
    # branch) so a busy→waiting→idle transition shows even on a cache hit. Falls
    # through to the scraped state for shell/unknown status or unmapped panes.
    # A mapped live session is also proof the pane IS Claude (a dialog that
    # blanks the bottom status line no longer flips the card to "shell").
    sid = session_id_for_pane(w.get("pane_id", ""))
    sess = session_state_for(sid)
    if sess:
        parsed["agent"] = "claude"
        parsed["state"] = sess["state"]
        parsed["needs_input"] = sess["state"] == "needs-input"
        parsed["asked_question"] = False
        parsed["waiting_for"] = sess.get("waiting_for")

    # done-vs-idle refinement. Uses per-pid stamps (persisted via
    # state.json) so a server restart preserves the "Claude finished
    # something you haven't looked at" signal across the gap.
    codex_valid = False
    if parsed.get("agent") == "codex":
        codex_valid = _apply_codex_state(
            parsed,
            pane_id=w.get("pane_id", ""),
            pid=pid,
            target=target,
            codex_live=codex_live,
            now_ts=now_ts,
        )
    cur = parsed.get("state")
    if parsed.get("agent") != "codex":
        record_state_transition(pid, target, cur, now_ts)

    # Pull persisted stamps; in-memory may be ahead (just bumped) or
    # behind (fresh process, never observed a transition this run).
    stamps = recency_stamps_for(target)
    persisted = get_window(pid) if pid else {}
    completed = max(stamps["completed_at"], int(persisted.get("completed_at") or 0))
    acked = max(stamps["acted_at"], int(persisted.get("acked_at") or 0))

    if (
        cur == "idle"
        and parsed.get("agent")
        and (parsed.get("agent") != "codex" or codex_valid)
        and completed > acked
    ):
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
    if parsed.get("agent") == "codex":
        # Codex participates in lifecycle attention, not Claude's MCP channel
        # alert/unread system.
        channel = {"attached": False, "unread": False, "alerts": []}

    # Resolve the spawner's NAME server-side, off the persisted block rather
    # than the live window list. Leads exit — on this box the only surviving
    # lineage is a 3-link chain whose middle pane is already dead — so a
    # live-only join renders nothing for exactly the "chain of four sessions,
    # three terminated" case lineage exists to make legible. `last_seen.name`
    # outlives the pane (GC'd at _PID_TTL_S, 30 days).
    spawned_by = persisted.get("spawned_by")
    spawner_name = None
    if spawned_by:
        spawner_name = (get_window(spawned_by).get("last_seen") or {}).get("name")

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
        # Resolve the linked PR by NUMBER (its own SWR cache) — the branch-keyed
        # `ci`/state above is for a different query and may not even be this PR.
        # A cold cache returns None on this poll (badge shows the bare number,
        # as before) and warms for the next. Once resolved, a merged/closed PR
        # carries `pr_state` so the rail stops showing it as a live open PR.
        linked_state = cached_linked_pr_state(w.get("cwd", ""), linked_pr)
        if linked_state:
            pr["pr_state"] = linked_state.get("pr_state")
            pr["ci"] = linked_state.get("ci")
        else:
            pr.pop("ci", None)

    # The track is the grouping authority. A repo-default track has id == repo
    # (the anchor / pinned dir); a goal track carries its repo on the row (or
    # None — spans repos, so no worktree affiliation). Source the affiliation
    # chip from the track instead of the retired project registry.
    track_id = resolve_track_for_window(w)
    track_row = activity.get_track(track_id) or {}
    aff_repo = track_row.get("repo")
    pinned_for_aff = aff_repo if aff_repo else None
    aff = affiliation(w.get("cwd", ""), pinned_for_aff, aff_repo)

    pane_accounts, pane_profiles = _pane_env_labels()

    view = {
        **w, **parsed, **git, **pr,
        "target": target,
        # 0 means "never engaged through periscope" — stream view
        # filters these out; grid view sorts cards within each session
        # by acted_at desc (most-recently-opened leftmost).
        "focused_at": stamps["focused_at"],
        "acted_at": acked,
        "completed_at": completed,
        "mem": mem_signal(claude_proc_for(sid)),
        "channel_attached": channel["attached"],
        "channel_unread": channel["unread"],
        "channel_alerts": channel["alerts"],
        "open_tabs": persisted.get("open_tabs") or [],
        "active_tab": persisted.get("active_tab") or "pane",
        # Provenance: the handle of the pane that spawned this one (written by
        # spawn_claude). Recorded since the tool shipped but never surfaced, so
        # a chain of delegated sessions read as unrelated tabs — the dashboard
        # could not show that pane A's work is continued by pane B.
        "spawned_by": persisted.get("spawned_by"),
        "spawner_name": spawner_name,
        # This name was chosen (typed here, set by the pane, or passed to
        # spawn_claude) and the narrator is locked out of it. Surfaced so the
        # rail can offer the unpin — the lock is otherwise invisible, and an
        # invisible lock is indistinguishable from a narrator that has simply
        # not gotten around to renaming yet.
        "name_pinned": bool(persisted.get("name_pinned")),
        "linked_linear": linked_linear,
        "linked_linear_title": linked_linear_title,
        "linked_linear_status": linked_linear_status,
        "lgtm": lgtm,
        # The track this tab belongs to — the sole organizational primitive.
        # (project_pinned_dir / workspace_id / project_name / project_archived
        # are gone; the frontend groups purely by track_id.)
        "track_id": track_id,
        "track_name": track_label(track_id),
        # "loose" | "repo" | "goal" — the rail hides lifecycle actions on the
        # two catchalls. Server-derived: a repo-default's name is
        # basename(repo), which collides with the goal track a user is most
        # likely to name after the repo, so the label can't carry this.
        "track_kind": track_kind(track_id),
        "worktree_affiliation": aff,
        # Which Claude subscription this pane bills. The whole point of pooling
        # two accounts is balancing work across two weekly limits, and nothing
        # else on the card says which one a pane is spending. Snapshot lookup —
        # the scan behind it runs once per poll (see _pane_env_labels).
        "account": pane_accounts.get(w.get("pane_id") or "", "default"),
        # Which `claude` wrapper profile this pane runs — i.e. which plugin set
        # and system prompt. Same reasoning as the account: nothing else on the
        # card distinguishes a lab pane from a normal one, and the difference
        # changes what the pane can do.
        "profile": pane_profiles.get(w.get("pane_id") or "", "default"),
    }
    return view, stamp_update

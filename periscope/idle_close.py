"""Idle close: each night, close the Claude panes idle for a set number of
weekdays, to free their memory. A closed pane keeps a `closed_panes` row, from
which Resume brings it back with its pid, name, track and wrapper profile.

Spec: docs/superpowers/specs/2026-11-19-idle-close-design.md.
"""

import asyncio
from collections.abc import Collection, Mapping
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import TypedDict

from periscope import activity, config, resurrect, session_status, state_hub, store
from periscope.log import _bg, log
from periscope.panes import drop_target_focus, list_windows
from periscope.pids import stamped_pids
from periscope.store import Settings
from periscope.tmux import _tmux_mutate
from periscope.turns import jsonl_for_session, session_id_for_pane

SWEEP_AT = time(4, 0)
_GRACE_MIN = 90
_TICK_S = 60.0


def came_back(record: activity.ClosedPane, live_pids: set[str]) -> bool:
    """A live window wears the record's pid, or a live Claude runs its session.
    Both, because a typed `claude --resume` can keep or change the session id."""
    return record["pid"] in live_pids or session_status.claude_proc_for(record["session_id"]) is not None


def prune_closed(live_pids: set[str]) -> int:
    """Delete the records that Resume can no longer use: Claude deleted the
    transcript, or the session came back another way. Returns how many."""
    dropped = 0
    for record in activity.closed_panes():
        if jsonl_for_session(record["session_id"]) is None or came_back(record, live_pids):
            activity.delete_closed_pane(record["session_id"])
            dropped += 1
    return dropped


class Pane(TypedDict):
    """What the sweep reads about one Claude pane."""

    pid: str
    pane_id: str
    name: str
    session_id: str | None
    state: str
    panes: int  # in its window
    idle_since: int | None
    transcript_mtime: float | None
    mem_mb: int | None
    profile: str | None  # wrapper profile, None for the default


def weekday_hours(start: datetime, end: datetime) -> float:
    """The hours from `start` to `end` on Monday to Friday, in local time."""
    hours = 0.0
    while start < end:
        midnight = datetime.combine(start.date() + timedelta(days=1), time())
        upto = min(midnight, end)
        if start.weekday() < 5:
            hours += (upto - start).total_seconds() / 3600
        start = upto
    return hours


def idle_since(transcript_mtime: float | None, completed_at: int, acted_at: int) -> int | None:
    """When the pane was last used, or None when nothing says. 0 is no stamp."""
    return int(max(transcript_mtime or 0, completed_at, acted_at)) or None


def verdict(pane: Pane, at: datetime, weekdays: int, pinned: Collection[str]) -> str | None:
    """What the sweep does with `pane` at `at`: "closes", or why it stays. None
    for a pane that is not near the limit."""
    limit = weekdays * 24
    if pane["idle_since"] is None:
        return None
    since = datetime.fromtimestamp(pane["idle_since"])
    if (at - since).total_seconds() / 3600 < limit:
        return None
    if not pane["session_id"] or pane["transcript_mtime"] is None:
        return "no-transcript"  # Resume refuses it, so a record could never resume
    if pane["pid"] in pinned:
        return "pinned"
    if pane["panes"] > 1:
        return "split-window"  # the window keeps the pid after kill-pane
    if pane["state"] != "idle":
        return pane["state"]
    return "closes" if weekday_hours(since, at) >= limit else "weekend"


def due(now: datetime, settings: Settings, log: Mapping[str, object]) -> bool:
    """True for 90 minutes from SWEEP_AT, when the setting is on and the sweep
    did not run today. A Mac that sleeps past it skips the night."""
    if not settings.get("idle_close_weekdays"):
        return False
    start = SWEEP_AT.hour * 60 + SWEEP_AT.minute
    return start <= now.hour * 60 + now.minute < start + _GRACE_MIN and log.get("date") != now.strftime("%Y-%m-%d")


def next_sweep(now: datetime) -> datetime:
    at = datetime.combine(now.date(), SWEEP_AT)
    return at if now < at else at + timedelta(days=1)


def _mtime(transcript: Path | None) -> float | None:
    """None when Claude has no transcript, or deleted it."""
    try:
        return transcript.stat().st_mtime if transcript else None
    except OSError:
        return None


def pane_from_view(view: dict) -> Pane | None:
    """The sweep's view of a dashboard pane; None for a pane that is not Claude."""
    if view.get("agent") != "claude":
        return None
    session_id = session_id_for_pane(view["pane_id"])
    mtime = _mtime(jsonl_for_session(session_id) if session_id else None)
    return {
        "pid": view["pid"],
        "pane_id": view["pane_id"],
        "name": view["name"],
        "session_id": session_id,
        "state": view["state"],
        "panes": view["panes"],
        "idle_since": idle_since(mtime, view["completed_at"], view["acted_at"]),
        "transcript_mtime": mtime,
        "mem_mb": (view.get("res") or {}).get("mem_mb"),
        "profile": config.profile_env(view["profile"]) or None,
    }


def _now() -> datetime:
    return datetime.now()


def _pinned() -> set[str]:
    return set(store.get_ui().get("pinned_pids") or [])


def _close(pane: Pane, at: datetime) -> Path | None:
    """Close `pane`, and return its transcript. None when it changed since the
    sweep read it (the views can be 13 seconds old), or when the kill failed."""
    session_id = pane["session_id"]
    window = next((w for w in list_windows() if w["pane_id"] == pane["pane_id"]), None)
    if not session_id or window is None or window["panes"] != 1:
        return None
    if session_id_for_pane(pane["pane_id"]) != session_id:
        return None
    if (session_status.session_state_for(session_id) or {}).get("state") != "idle":
        return None
    transcript = jsonl_for_session(session_id)
    # The only check that sees a short turn that started and ended in between.
    if transcript is None or _mtime(transcript) != pane["transcript_mtime"]:
        return None
    assert pane["idle_since"] is not None  # verdict() closes no pane without it
    activity.record_closed_pane(
        {
            "session_id": session_id,
            "pid": pane["pid"],
            "mem_mb": pane["mem_mb"],
            "profile": pane["profile"],
            "idle_since": pane["idle_since"],
            "closed_at": int(at.timestamp()),
        }
    )
    # By pane id: `renumber-windows on` moves the indexes during the loop.
    ok, msg = _tmux_mutate("kill-pane", "-t", pane["pane_id"])
    if not ok:
        activity.delete_closed_pane(session_id)
        log.warning("idle close: could not kill %s (%s): %s", pane["pane_id"], pane["name"], msg)
        return None
    drop_target_focus(f"{window['session']}:{window['index']}")
    log.info("idle close: closed %s (%s), session %s", pane["pane_id"], pane["name"], session_id)
    return transcript


def sweep(views: list[dict], weekdays: int) -> None:
    """Worker-thread body: close every pane whose verdict is "closes"."""
    at = _now()
    prune_closed(stamped_pids(list_windows()))
    pinned = _pinned()
    transcripts: list[Path] = []
    for view in views:
        try:
            pane = pane_from_view(view)
            if pane and verdict(pane, at, weekdays, pinned) == "closes" and (transcript := _close(pane, at)):
                transcripts.append(transcript)
        except Exception:
            log.exception("idle close: pane %s failed", view.get("pane_id"))
    if not transcripts:
        return
    # A reboot before the next continuum save would bring the closed panes back.
    resurrect.save_now(force=True)
    from history.indexer import index_one

    # Claude's SessionEnd hook may not fire when tmux kills the pane.
    for transcript in transcripts:
        try:
            index_one(str(transcript), force=True)
        except Exception:
            log.exception("idle close: index_one failed for %s", transcript)


def preview(views: list[dict], weekdays: int) -> list[dict]:
    """One row for each Claude pane that the next sweep decides about."""
    at = next_sweep(_now())
    pinned = _pinned()
    rows = []
    for view in views:
        pane = pane_from_view(view)
        if pane is None or pane["idle_since"] is None:
            continue
        code = verdict(pane, at, weekdays, pinned)
        if code is None:
            continue
        since = datetime.fromtimestamp(pane["idle_since"])
        rows.append(
            {
                "name": pane["name"],
                "idle_since": pane["idle_since"],
                "weekday_hours": weekday_hours(since, at),
                "mem_mb": pane["mem_mb"],
                "verdict": code,
            }
        )
    return rows


async def _tick() -> None:
    now, settings = _now(), store.get_settings()
    if not due(now, settings, store.get_idle_close_log()):
        return
    # Before the work: a failing pane, or a sweep longer than a tick, must not
    # start a second sweep.
    store.record_idle_close({"date": now.strftime("%Y-%m-%d"), "at": int(now.timestamp())})
    views = await state_hub.current_windows()
    _bg("idle-close", sweep, views, settings["idle_close_weekdays"])


async def run() -> None:
    """Lifespan task, prod only (registered in app.lifespan)."""
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("idle close tick failed")
        await asyncio.sleep(_TICK_S)

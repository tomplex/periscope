"""Spawn Claude with a prompt in a branch's worktree, once per (repo, branch).

The spawn path of POST /api/reviews/prepare and POST /api/linear/start. Both routes share one guard,
keyed on (repo label, branch); each keeps its own check for a Claude on the branch, worktree step, window
fields and answer.
"""

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException

from periscope import channels, open_ops, tracks
from periscope.log import log

# A spawned Claude attaches its channel ~10-20 s after the spawn returns; until a caller sees it, a second
# spawn on the branch within this long is refused.
RECENT_SPAWN_S = 60.0

_in_flight: set[tuple[str, str]] = set()
_recent_spawn: dict[tuple[str, str], float] = {}


@contextmanager
def one_at_a_time(key: tuple[str, str]) -> Iterator[None]:
    # A spawn awaits ~10 s: a second request (a double press) sees no Claude yet and spawns a twin.
    if key in _in_flight:
        raise HTTPException(409, "a Claude is already starting on this branch")
    _in_flight.add(key)
    try:
        yield
    finally:
        _in_flight.discard(key)


def refuse_or_clear_recent_spawn(key: tuple[str, str], claude: dict | None) -> None:
    """No Claude: refuse (409) a spawn on `key` within RECENT_SPAWN_S of the last one. An attached Claude
    clears the record. An unattached one leaves it, so a prepare, which needs an attached Claude, still waits."""
    if claude is None:
        age = time.monotonic() - _recent_spawn.get(key, -RECENT_SPAWN_S)
        if age < RECENT_SPAWN_S:
            raise HTTPException(409, f"a pane was spawned {int(age)} s ago and hasn't attached yet")
    elif claude.get("channel_attached"):
        _recent_spawn.pop(key, None)


async def spawn(key: tuple[str, str], worktree: str, prompt: str) -> channels.Spawned:
    """Spawn Claude in `worktree` with `prompt`, in the repo's default track, and place it in the rail."""
    anchored = await asyncio.to_thread(open_ops.resolve_worktree_session, worktree)
    if not anchored:
        # The worktree exists, so this is git failing in periscope, not a bad request.
        raise HTTPException(502, f"{worktree} is not inside a git repo")
    session, project = anchored
    try:
        spawned = await channels.spawn_claude_with_prompt(
            worktree, tracks.repo_default_track(project["repo"]), prompt, session=session
        )
    except channels.SpawnFailed as e:
        raise HTTPException(502, f"spawn failed: {e}") from e
    _recent_spawn[key] = time.monotonic()
    try:
        await asyncio.to_thread(open_ops.place_session_in_rail, session, project, spawned["pid"])
    except Exception:
        # Placement only orders the rail; the Claude exists, and an error would invite a retry that spawns a twin.
        log.warning("branch_spawn: rail placement failed for %s", worktree, exc_info=True)
    return spawned

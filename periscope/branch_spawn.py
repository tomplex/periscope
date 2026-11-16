"""Spawn Claude with a prompt in a branch's worktree, once per (repo, branch).

The spawn path of POST /api/reviews/prepare and POST /api/linear/start. Each route keeps one
BranchSpawns, and its own owner check, worktree step, window fields and answer.
"""

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException

from periscope import channels, open_ops, tracks
from periscope.log import log

# A spawned Claude shows as the owner only once its channel attaches and the next state lands
# (~10-20 s after the request returns); a request in that window is refused, not spawned twice.
RECENT_SPAWN_S = 60.0


class BranchSpawns:
    """One route's spawns. `name` ("prepare", "start") names the route in its 409 and its log."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.in_flight: set[tuple[str, str]] = set()
        self.recent_spawn: dict[tuple[str, str], float] = {}

    @contextmanager
    def one_at_a_time(self, key: tuple[str, str]) -> Iterator[None]:
        # A spawn awaits ~10 s: a second request (a double press) sees no owner yet and spawns a twin.
        if key in self.in_flight:
            raise HTTPException(409, f"{self.name} already running")
        self.in_flight.add(key)
        try:
            yield
        finally:
            self.in_flight.discard(key)

    def forget_spawn(self, key: tuple[str, str]) -> None:
        """The owner attached: the recent-spawn refusal no longer applies."""
        self.recent_spawn.pop(key, None)

    def refuse_recent_spawn(self, key: tuple[str, str]) -> None:
        age = time.monotonic() - self.recent_spawn.get(key, -RECENT_SPAWN_S)
        if age < RECENT_SPAWN_S:
            raise HTTPException(409, f"a pane was spawned {int(age)} s ago and hasn't attached yet")

    async def spawn(self, key: tuple[str, str], worktree: str, prompt: str) -> channels.Spawned:
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
        self.recent_spawn[key] = time.monotonic()
        try:
            await asyncio.to_thread(open_ops.place_session_in_rail, session, project, spawned["pid"])
        except Exception:
            # Placement only orders the rail; the Claude exists, and an error would invite a retry that spawns a twin.
            log.warning("%s: rail placement failed for %s %s", self.name, *key, exc_info=True)
        return spawned

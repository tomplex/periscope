# Start a Claude on a Linear issue from another app

## Goal

Let another local app (todo-hub) start work on a Linear issue in periscope in one call: make the worktree for the issue's branch, start Claude in it, send Claude a prompt, and link the issue to the pane. Then let that app ask periscope's UI to select the pane.

Periscope already does each step for review walkthroughs (`POST /api/reviews/prepare`). This change gives the same steps a route of their own, for a branch and a Linear issue, and adds a way to select a pane from outside the UI.

## Scope

In:

- `POST /api/linear/start`: worktree, Claude, prompt, Linear link, rail placement.
- `POST /api/select`: a one-time request that the UI selects a pane.

Out:

- A Linear API client in periscope. The caller sends the branch, the issue ID and the title.
- Auth. Periscope's API listens only on 127.0.0.1 and has none. Any local process can already start Claude (`/api/open`) and type into it (`/api/send`), so these routes add no new risk.
- A shell window next to the Claude. `/api/reviews/prepare` also starts only the Claude window.
- Bringing the app to the front. The caller does that (`open -b com.tom.periscope.app`).

## `POST /api/linear/start`

New file `periscope/routes/linear.py`.

Body:

```json
{ "repo": "/Users/me/dev/api",
  "branch": "me/eng-9512-retry-stuck-uploads",
  "ticket": "ENG-9512",
  "title": "Retry stuck uploads after a 502 from the ingest worker",
  "prompt": "Read Linear ticket ENG-9512 …" }
```

`repo` is a main-checkout path, as `GET /api/open/catalog` lists it.

The route is `async`, as prepare is: `spawn_claude_with_prompt` awaits Claude's screen, and the git steps run in `asyncio.to_thread`.

Steps:

1. **In-flight guard.** If a start for the same `(repo, branch)` is running, answer 409 "start already running". A double press in the caller makes this case.
2. **Running Claude.** Look for the owner with `reviews.owner_for(windows, label, branch)`: a channel-attached Claude on that repo label and branch, read from `state_hub._last_blob`. If there is one, answer `{"mode": "focused", "pid": <its pid_raw>}`. Do not paste the prompt: that Claude can be busy.
3. **Recent-spawn guard.** A spawned Claude shows as the owner only after its channel attaches and the next state lands, about 10 to 20 s after the spawn. If this route spawned a Claude for `(repo, branch)` less than 60 s ago and step 2 found no owner, answer 409 "a pane was spawned N s ago and hasn't attached yet". This copies `_recent_spawn` in `routes/reviews.py`.
4. **Worktree.** `worktree_spawn.spawn_worktree(repo, branch)`. It forks a new branch from `origin/<default branch>`, checks out a branch that exists, and returns the worktree that is already on the branch (`reused: true`). A `ValueError` becomes 400 with its message.
5. **Session.** `open_ops.resolve_worktree_session(path)`. `None` becomes 409 "<path> is not inside a git repo".
6. **Claude.** `channels.spawn_claude_with_prompt(path, tracks.repo_default_track(project["repo"]), prompt, session=session)`. `SpawnFailed` becomes 502 "spawn failed: …". Record the spawn time for step 3.
7. **Link.** `store.set_window_fields(pid, linked_linear=ticket, linked_linear_title=title)`. The card shows the issue at once. Without this, it shows only the ID that `git_pr.linear_ticket_for` reads from the branch name.
8. **Rail.** `open_ops.place_in_rail(session, project, pane_pids)`, as prepare does. A failure here logs a warning and does not fail the route: the Claude exists, and an error would invite a retry that spawns a second one.
9. Answer `{"mode": "spawned", "pid", "path", "reused"}`. If the spawn returned no pid, answer the same with `"pid": null` and log a warning, for the same reason as step 8.

The route does not select the pane. The caller asks for that with `POST /api/select` when the user wants it.

## `POST /api/select`

Body: `{"pid": "<@periscope_id>"}`.

- The server keeps one select request in memory: `{"pid", "at": <epoch ms>}`. A new request replaces the old one. The route calls `state_hub.kick()`, so the request reaches the UI on the next push (about 1 s).
- `routes/state.py` `build_state()` adds it to the blob as `select` (or `null`).
- `static/src/poll.js` `applyState` applies a request when its `at` is new to this tab and less than 30 s old. It sets `railSelection` to `pane:<pid>` and calls `prefs.setLastSelected({kind: "pane", pid})`, as `OpenOmnibox.post()` does after ⌘K. The age limit stops a page reload from applying an old request.
- The decision is a pure helper, `selectToApply(select, lastAt, nowMs)`, so vitest can test it.
- A request for a pid that is not live does nothing: the rail has no row to select.

## Changes

- New `periscope/routes/linear.py`, registered in `app.py` with the other routers.
- `periscope/routes/state.py`: `POST /api/select`, the in-memory request, and `select` in `build_state()`.
- `static/src/poll.js`: `selectToApply` and its use in `applyState`. Rebuild and commit `static/dist/`.
- `CLAUDE.md`: no new module rows are needed (routes are "one APIRouter per file"). Add `routes/linear.py` to the `docs/unified-open.md` row of the reference table, because it spawns worktrees.
- `README.md`: the two routes, in the API section.

## Tests

`tests/routes/test_linear.py`, with stubs for `spawn_worktree`, `spawn_claude_with_prompt`, `place_in_rail` and the windows, as `tests/routes/test_reviews.py` does for prepare:

- Spawn path: calls each step in order, stamps `linked_linear` and `linked_linear_title`, answers `spawned` with the pid, path and `reused`.
- Focused path: an attached Claude on the branch gives `focused` with its pid. Nothing is spawned and no prompt is sent.
- A second start while one runs gives 409.
- A start less than 60 s after a spawn, with no owner yet, gives 409.
- `spawn_worktree` raising `ValueError` gives 400. `SpawnFailed` gives 502.
- A `place_in_rail` failure still answers `spawned`.

`tests/routes/test_state.py`: after `POST /api/select`, `build_state()` carries `select` with the pid. A second request replaces the first.

New `static/src/__tests__/poll.test.js`: `selectToApply` applies a new, fresh request once; it ignores the same `at` again, a request older than 30 s, and `null`.

## Assumptions

- `reviews.owner_for` is the right test for "a Claude is already on this branch". It counts only channel-attached Claudes, which is why step 3 exists.
- Only one select request at a time matters. Two quick requests from two apps keep only the last.
- `state_hub.kick()` is safe to call from a sync route. It schedules the wake on the hub's loop with `call_soon_threadsafe` (read in `state_hub.py`).
- The caller sends a branch that is safe for git. `spawn_worktree` already refuses a name that starts with `-`, and git refuses other bad names.

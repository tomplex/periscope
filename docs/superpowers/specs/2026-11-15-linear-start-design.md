# Start a Claude on a Linear issue from another app

## Goal

Let another local app start work on a Linear issue in periscope in one call: make the worktree for the issue's branch, start Claude in it, send Claude a prompt, and link the issue to the pane. Then let that app ask periscope's UI to select the pane.

Periscope already does each step for review walkthroughs (`POST /api/reviews/prepare`). This change gives the same steps a route of their own, for a branch and a Linear issue, and adds a way to select a pane from outside the UI.

## Scope

In:

- `POST /api/linear/start`: worktree, Claude, prompt, Linear link, rail placement.
- `POST /api/select`: a request that the UI selects a pane.

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

Steps 1, 3, 5, 6 and 7 are in `periscope/branch_spawn.py`. `/api/reviews/prepare` uses the same code, with its own owner step and worktree step. The two routes share one guard (steps 1 and 3), keyed on the repo label and the branch: prepare knows a repo only by its label.

Steps:

1. **In-flight guard.** If a start or a prepare for the same `(repo label, branch)` is running, answer 409 "a Claude is already starting on this branch". A double press in the caller makes this case.
2. **Running Claude.** Look for a Claude on the branch with `reviews.claude_on_branch`, over the windows whose `repo_key` is the realpath of `repo`. Any Claude counts, with or without a channel: the route only reports the pane, and `/api/select` needs only a live pane. The windows come from `state_hub.current_windows()`: the last blob while a dashboard tab subscribes and the blob is less than two ticks old, else a fresh `build_state()`, because the hub computes nothing while no tab is open. If there is a Claude, answer `{"mode": "existing", "pid": <its pid>}`, and clear the recent-spawn record (step 3) if its channel is attached. Do not paste the prompt: that Claude can be busy.
3. **Recent-spawn guard.** A spawned Claude shows in the windows about one tick after the spawn, and as prepare's owner only after its channel attaches, about 10 to 20 s after the spawn. If either route spawned a Claude for `(repo label, branch)` less than 60 s ago and step 2 found no Claude, answer 409 "a pane was spawned N s ago and hasn't attached yet". Only a channel-attached Claude clears the record: this route sees its own Claude before the channel attaches, and a clear then would let a prepare spawn a second Claude.
4. **Worktree.** `worktree_spawn.spawn_worktree(repo, branch)`. It forks a new branch from `origin/<default branch>`, checks out a branch that exists, and returns the worktree that is already on the branch (`reused: true`). A `ValueError` becomes 400 with its message: `spawn_worktree` refused the request (a bad branch, a path that is not a git repo, or a failed `git worktree add`). `/api/open` maps it the same way.
5. **Session.** `open_ops.resolve_worktree_session(path)`. `None` becomes 502 "<path> is not inside a git repo". At this point `spawn_worktree` has made the worktree, so `None` means git failed in periscope, not that the request is bad.
6. **Claude.** `channels.spawn_claude_with_prompt(path, tracks.repo_default_track(project["repo"]), prompt, session=session)`. It loads the prompt into a tmux buffer over stdin (`tmux.load_buffer`): as an argument, tmux reads a leading `-` as flags and refuses about 16 KB. If the load fails, it kills the new window and raises `SpawnFailed`. `SpawnFailed` becomes 502 "spawn failed: …". Record the spawn time for step 3.
7. **Rail.** `open_ops.place_session_in_rail(session, project, pid)`. A failure here logs a warning and does not fail the route: the Claude exists, and an error would invite a retry that spawns a second one.
8. **Link.** `store.set_window_fields(pid, linked_linear=ticket, linked_linear_title=title)`. An empty title is stored as `None`. The card shows the issue at once. Without this, it shows only the ID that `git_pr.linear_ticket_for` reads from the branch name.
9. Answer `{"mode": "spawned", "pid", "path", "reused"}`. `spawn_claude_with_prompt` always returns a pid, because `pids.stamp_new_window` mints one.

The route does not select the pane. The caller asks for that with `POST /api/select` when the user wants it.

## `POST /api/select`

Body: `{"pid": "<@periscope_id>"}`.

- The server keeps the last select request in memory: `{"pid", "at": <epoch ms>}`. Nothing clears it. A new request replaces it. The route calls `state_hub.kick()`, so the request reaches the UI on the next push (about 1 s).
- `routes/state.py` `build_state()` adds it to the blob as `select` (or `null`).
- `static/src/poll.js` `applyState` applies a request when its `at` is new to this tab and less than 30 s old. It selects the pane with `alertFeed.selectPane`, as a click on its rail row does. The age limit stops a page reload from applying an old request.
- The decision is a pure helper, `selectToApply(select, lastAt, nowMs, windows)`, in `static/src/select.js`. It returns `{window, lastAt}`: the live window to select, or null, and the `lastAt` that `applyState` stores, moved on only when a window is returned. Vitest tests it.
- A request for a pid that is not live does nothing: the rail has no row to select. If the pane appears on a later state while the request is still fresh, the request applies then.

## Changes

- New `periscope/routes/linear.py`, registered in `app.py` with the other routers.
- New `periscope/branch_spawn.py`: steps 1, 3, 5, 6 and 7, with one guard for this route and `/api/reviews/prepare`.
- `periscope/routes/reviews.py`: prepare uses `branch_spawn`. A session that git cannot resolve after the worktree exists is 502, not 409. The in-flight 409 reads "a Claude is already starting on this branch".
- `periscope/reviews.py`: `claude_on_branch`, the agent-and-branch match. `owner_for` adds prepare's channel and repo-label filter to it.
- `periscope/open_ops.py`: `place_session_in_rail(session, project, pid)` places every stamped pane of a session. The open (`_open_path`), the `spawn_claude` tool and `branch_spawn` use it.
- `periscope/channels.py`: the `spawn_claude` tool places its pane with `place_session_in_rail`. `spawn_claude_with_prompt` loads the prompt with `tmux.load_buffer`.
- `periscope/tmux.py`: `load_buffer`, which loads text over stdin and raises when tmux fails. `spawn_claude_with_prompt` and `/api/send` use it.
- `periscope/routes/state.py`: `POST /api/select`, the in-memory request, and `select` in `build_state()`.
- `static/src/select.js`: `selectToApply`.
- `static/src/poll.js`: its use in `applyState`.
- `periscope/state_hub.py`: `last_windows()`, which `routes/reviews.py` now uses too, and `current_windows()`. `_broadcast` records when it sent the blob.
- Rebuild and commit `static/dist/`.
- `CLAUDE.md`: a `branch_spawn.py` module row. Add `routes/linear.py` and `branch_spawn.py` to the `docs/unified-open.md` row of the reference table, because they make worktrees and place panes in the rail.
- `docs/unified-open.md`: `place_session_in_rail` and its callers.
- `README.md`: the two routes, in "Opening work".

## Tests

`tests/routes/test_linear.py`, with stubs for `spawn_worktree`, `spawn_claude_with_prompt`, `place_in_rail` and the windows, as `tests/routes/test_reviews.py` does for prepare:

- Spawn path: calls each step in order, stamps `linked_linear` and `linked_linear_title`, answers `spawned` with the pid, path and `reused`.
- Existing path: an attached Claude on the branch gives `existing` with its pid. Nothing is spawned and no prompt is sent. The answer clears the recent-spawn record.
- A Claude without a channel on the branch gives `existing` too. A shell on the branch, or a Claude on the branch in another repo with the same folder name, does not count.
- A `repo` with a trailing slash still finds the Claude (this pins the realpath).
- A second start while one runs gives 409, and only one Claude spawns.
- A start less than 60 s after a spawn, with no Claude in the windows yet, gives 409.
- A prepare right after a start on the same repo label and branch gives 409. It still does after a second start saw the new Claude before its channel attached.
- `spawn_worktree` raising `ValueError` gives 400. `SpawnFailed` gives 502. A session that git cannot resolve gives 502.
- A `place_in_rail` failure still answers `spawned`.
- An empty title stamps only the ticket.

`tests/test_state_hub.py`: `current_windows` returns the last blob while a tab subscribes and does not build. With no subscriber, or with a blob older than two ticks, it returns a fresh build, not the stale blob.

`tests/test_tmux.py`: `load_buffer` sends a prompt that starts with `-` and a 20 KB prompt over stdin. `tests/test_channels.py`: a failed load raises `SpawnFailed` and kills the new window.

`tests/routes/test_state.py`: after `POST /api/select`, `build_state()` carries `select` with the pid. The test checks that `at` is in epoch ms. A second request replaces the first. An empty pid gives 422.

New `static/src/__tests__/select.test.js`: `selectToApply` applies a fresh request once and applies a newer request after an older one. It ignores the same `at`, a request older than 30 s, a pid that is not live, and `null`. A request whose pane shows one state late applies then, once.

## Assumptions

- Any Claude on the branch in the same `repo_key` is "a Claude already on this branch", with or without a channel. A second Claude would edit the same worktree, and the route never sends the first one a prompt.
- Every window on a git cwd carries `repo_key`, the realpath of the main checkout, also for a linked worktree (`gitutil.resolve_repo`). When git cannot answer, `resolve_repo` falls back to the cwd, and that window does not match.
- The shared guard keys on the repo label, so two repos with the same folder name and the same branch name share it: a spawn in one holds a spawn in the other for up to 60 s.
- Only the last select request matters. Two quick requests from two apps keep only the last.
- `state_hub.kick()` is safe to call from a sync route. It schedules the wake on the hub's loop with `call_soon_threadsafe` (read in `state_hub.py`).
- `spawn_worktree` checks only local branches. A branch that exists only on origin (for example after cleanup deleted the local branch) is forked fresh from the default branch, and a later push is rejected. This is existing behavior.
- The caller sends a branch that is safe for git. `spawn_worktree` already refuses a name that starts with `-`, and git refuses other bad names.

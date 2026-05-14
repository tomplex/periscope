# Persistent config layer — design

## Context

Periscope currently has two ad-hoc persistence stories:

- Client-side `localStorage` holds session order and collapsed-session state under `periscope:*` keys.
- Server-side hardcoded values cover the new-window command set (`claude`, `shell`), poll intervals, and similar.

Neither survives a browser-data wipe or supports per-window metadata, and there is no path to make the new-window command list user-editable. This design replaces both with a single server-managed JSON state file that the frontend reads and writes through a small REST surface.

## Goals

1. Move UI prefs (session order, collapsed sessions, future filter/sort prefs) from `localStorage` to server-side storage.
2. Support per-window annotations (notes, tags) keyed to a periscope-assigned identifier that survives rename / move / reorder.
3. Make the new-window command list user-editable from the UI (replacing the hardcoded `claude` and `shell` buttons).

## Non-goals

- Multi-user. Periscope is single-user, localhost-only.
- Sync between machines. The state file is per-host.
- Hand-editability of the state file. The file is written by the server in response to UI mutations.

## Storage

A single JSON file at `${XDG_CONFIG_HOME:-~/.config}/periscope/state.json`.

```json
{
  "version": 1,
  "ui": {
    "session_order": ["tc/foo", "tc/bar"],
    "collapsed_sessions": ["tc/baz"]
  },
  "windows": {
    "a1b2c3d4": {
      "notes": "deploys staging",
      "tags": ["infra"],
      "last_seen": {
        "session": "tc/foo",
        "name": "gitops",
        "branch": "main",
        "cwd": "/Users/tom/dev/foo",
        "ts": 1747200000
      }
    },
    "ffeedd00": {
      "last_seen": { "session": "tc/bar", "name": "vim", "branch": null, "cwd": "...", "ts": 1747100000 }
    }
  },
  "commands": [
    { "label": "claude", "exec": "claude" },
    { "label": "shell", "exec": "$SHELL -l" }
  ]
}
```

Notes on the shape:

- `version` is reserved for future schema migrations. Server reads it; on mismatch, applies an in-place migration.
- `windows` entries have mixed cardinality. An id with an annotation carries `notes`/`tags`. An id with no annotation carries only `last_seen` — it exists purely to support the rebind heuristic (see below). The two cases coexist in one map for simplicity.
- `commands` is an ordered list. Order determines button order in the new-window tile.
- **Missing keys default to their empty/default value.** A phase-1 state file with no `windows` or `commands` key is valid; later phases that consume those keys treat absence as empty (with `commands` further defaulting to the seeded `claude`/`shell` entries — see phase 4).

### Write discipline

All writes go through a single module-level `asyncio.Lock` in `server.py`. Inside the lock, the server reads the file, mutates the dict, writes to `state.json.tmp`, then `os.replace()` onto the live path. The lock is held for the full read-modify-write — the file is tiny (kilobytes), so coarse locking is fine.

### GC

On server startup, drop any `windows` entry that (a) has no `notes` and no `tags`, and (b) `last_seen.ts` is older than 30 days. This keeps the file from accumulating dead orphan-rebind hints indefinitely.

## Identity model

### Periscope id

Every tmux window periscope sees acquires a periscope-assigned 8-character hex id, stored as a tmux user option on the window: `@periscope_id`. The id is opaque — clients treat it as a string.

Why a periscope-owned id rather than tmux's `#{window_id}` (e.g. `@7`):

- Same survival semantics for rename / move-between-sessions / reorder.
- Decouples annotation storage from tmux internals. If the rebind heuristic later evolves into a cross-restart mechanism, the namespace is ours to extend.
- Cost is one extra `set-option` per *new* window — negligible.

### Assignment

The `list-windows` format string in `server.py` gains `#{@periscope_id}`. For every window seen during a poll:

1. If the value is non-empty, use it as the `pid`.
2. If empty, attempt rebind (next section). If rebind hits, reuse the matched id. Otherwise mint a fresh 8-char hex uuid.
3. In either case where we synthesized an id (rebind or mint), `tmux set-option -wt <target> @periscope_id <pid>`. This is fire-and-forget; if it fails (e.g. the window is gone), the next poll repeats the attempt.

### Rebind heuristic

Tmux user options live for the lifetime of the tmux server. Server restart (reboot, manual `tmux kill-server`, OOM) wipes them. The rebind heuristic survives this without requiring tmux-resurrect cooperation.

When a window is seen with no `@periscope_id`, before minting a new id:

1. Iterate `windows` entries in the state file.
2. Filter to entries whose `last_seen.ts` is within 30 days.
3. Filter to entries no live window currently claims (i.e. no other window in this poll already resolved to that id).
4. Score remaining candidates against the current window:
   - Match on `(session, name)`: strong signal.
   - Match on `(branch, cwd)` when both are set: secondary signal.
5. If any candidate matches on `(session, name)`, reuse its id. If none matches on `(session, name)` but one matches on `(branch, cwd)`, reuse it. Otherwise mint a new id.

### Last-seen update

After resolving a window's `pid` for the current poll, the server updates that pid's `last_seen` block with the current `(session, name, branch, cwd, now)`. This is what makes the rebind heuristic improve over time — every poll refreshes the hint.

### Known consequence

If a window is killed and a different window is later created with the same `(session, name)` within 30 days, the new window inherits the old annotations. This is intentional: from the user's perspective, naming a window `gitops` in session `tc/foo` again is them saying "this is that window." Documented behavior; not a bug.

## Server endpoints

All routes are JSON. All write endpoints take the lock, mutate, atomic-write, then return the affected slice.

```
GET    /api/prefs                       # full state blob (for client boot)
PATCH  /api/prefs/ui                    # merge partial { session_order?, collapsed_sessions? }
PUT    /api/prefs/windows/{pid}         # set/replace annotation { notes?, tags? }
DELETE /api/prefs/windows/{pid}         # remove annotation (preserves last_seen)
POST   /api/prefs/commands              # append { label, exec }; rejects duplicate label
PUT    /api/prefs/commands/{label}      # update label/exec
DELETE /api/prefs/commands/{label}
```

`PATCH /api/prefs/ui` merges only the fields present in the body. Sending `{ "session_order": [...] }` does not touch `collapsed_sessions`.

`DELETE /api/prefs/windows/{pid}` only removes `notes` and `tags`. `last_seen` is left intact so the rebind heuristic still works if the window is later recreated.

The existing `/api/state` poll endpoint gains a `pid` field on each window object.

## Frontend changes

### New module `static/prefs.js`

Owns the in-memory cache of `/api/prefs`. The full v1 surface listed below is built up incrementally — phase 1 ships only the UI-prefs methods, later phases add annotation and command methods alongside their endpoints.

Exposes:

- `loadPrefs()` — called once during boot, populates the cache.
- `getSessionOrder()`, `setSessionOrder(arr)`
- `getCollapsed()`, `setCollapsed(set)`
- `getCommands()`
- `getAnnotation(pid)`, `setAnnotation(pid, { notes, tags })`, `deleteAnnotation(pid)`
- `addCommand({ label, exec })`, `updateCommand(label, { ... })`, `deleteCommand(label)`

All mutators update the local cache eagerly and issue the corresponding API call in the background. A failed write reverts the cache and surfaces an error via the existing `apiCall()` helper.

### Migration from localStorage

On the first `loadPrefs()` after upgrade, if the server returned an empty `ui` block AND `localStorage` contains `periscope:sessionOrder` or `periscope:collapsedSessions`, the client pushes those values to `/api/prefs/ui`, then removes the localStorage keys. The migration shim lives in `prefs.js` until the operator confirms the migration ran, then is deleted in a follow-up commit.

### Consumers

- `state.js` — drops `loadOrder` / `saveOrder` / `loadCollapsed` / `saveCollapsed` exports. `state.collapsedSessions` becomes a derived view sourced from `prefs.getCollapsed()`.
- `grid.js` — calls `prefs.setSessionOrder(...)` and `prefs.setCollapsed(...)` instead of the localStorage helpers. Uses `w.pid` (from `/api/state`) for any annotation-related rendering.
- New-window tile — renders one button per `prefs.getCommands()` entry, replacing the hardcoded `+ claude` / `+ shell` markup.

### Annotations UI

Each card gains a small `i` button next to the existing `✕` kill button. Clicking opens an inline popover (positioned over the card) with:

- A `<textarea>` for `notes`.
- A free-text tag input that splits on space/comma into the `tags` array.
- Save / cancel.

Cards with non-empty `notes` or `tags` show a subtle indicator (e.g. a tinted bubble in the corner of the card head). The card snippet area is untouched — annotations are visible on hover or open, not in the default card body, to keep the dashboard density unchanged.

### Commands UI

A gear icon in the header (row 2, next to the existing action buttons) opens a modal with:

- A list of current commands. Each row: label input, exec input, delete button.
- An "+ add" row at the bottom.
- Save persists all rows via the appropriate POST/PUT/DELETE calls.

The modal reuses the existing modal infrastructure (`modal.js`) where it makes sense.

## Phasing

Four mergeable phases. Each ships independently and leaves the system in a coherent state.

### Phase 1 — storage + UI prefs migration

- `state.json` load/save in `server.py`, atomic writes, lock.
- `GET /api/prefs`, `PATCH /api/prefs/ui`.
- `prefs.js` with UI-prefs-only surface; localStorage migration shim.
- Grid switched to call `prefs.set*` instead of localStorage helpers.

Visible behavior is unchanged. Verifiable by clearing localStorage and confirming session order/collapse state survive a browser reload.

### Phase 2 — periscope ids + rebind heuristic

- Add `#{@periscope_id}` to `list-windows` format string.
- Implement assignment, rebind, and `last_seen` update logic in `parse_pane`.
- Add `pid` to the window payload on `/api/state`.
- No UI changes.

Phase 2 is pure plumbing for phase 3. Verifiable by inspecting `state.json` and confirming ids appear in `tmux show-options -wv -t <target> @periscope_id`.

### Phase 3 — annotations UI

- `PUT`/`DELETE /api/prefs/windows/{pid}`.
- `i` button on each card, popover editor, annotation indicator.

Verifiable by adding a note, renaming the window, reloading — note persists.

### Phase 4 — configurable commands

- `POST`/`PUT`/`DELETE /api/prefs/commands/*`.
- Gear icon + commands modal.
- New-window tile reads from `prefs.getCommands()`.
- Default-seed the existing `claude` / `shell` entries on first boot if `commands` is empty.

Verifiable by adding a `vim` command and confirming the new button appears on every new-window tile.

## Open questions

None blocking. Two minor decisions that fall out during implementation:

- Exact byte budget for the state file before we switch to SQLite. The current shape is comfortably under a megabyte for thousands of windows; not a near-term concern.
- Whether commands need a `cwd` field (run vim in `$HOME` vs the session's last cwd). Defer until someone wants it.

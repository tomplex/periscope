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
    "collapsed_sessions": ["tc/baz"],
    "view": "grid"
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
    { "label": "shell", "exec": "" },
    { "label": "vim", "exec": "vim" }
  ]
}
```

Notes on the shape:

- `version` is reserved for future schema migrations. Server reads it; on mismatch, applies an in-place migration.
- `windows` entries have mixed cardinality. An id with an annotation carries `notes`/`tags`. An id with no annotation carries only `last_seen` — it exists purely to support the rebind heuristic (see below). The two cases coexist in one map for simplicity.
- `commands` is an ordered list. Order determines button order in the new-window tile, and the **first entry is the primary button** (larger hit area, top of the tile) — matching the current `+ claude` primary / `+ shell` / `+ vim` stacked layout. Empty `exec` means "open a bare shell, send no keys" (the current `mode=shell` semantics).
- **Missing keys default to their empty/default value.** A phase-1 state file with no `windows` or `commands` key is valid; later phases that consume those keys treat absence as empty (with `commands` further defaulting to the seeded `claude`/`shell` entries — see phase 4).

### Write discipline

All writes go through a single module-level `asyncio.Lock` in `server.py`. Inside the lock, the server reads the file, mutates the dict, writes to `state.json.tmp`, then `os.replace()` onto the live path. The lock is held for the full read-modify-write — the file is tiny (kilobytes), so coarse locking is fine.

### Load discipline

On startup, the server attempts to read and JSON-parse `state.json`. On parse failure, it renames the bad file to `state.json.corrupt-<unix-ts>`, logs loudly, and continues with empty defaults. This protects against a hand-edit, a torn write from an earlier crash, or disk corruption: the next save writes a fresh valid file and the user can recover annotations from the saved corrupt file if they care.

### Schema migrations

`version` exists for future evolution. v1 has no migration paths. When v2 lands, a `_migrations` dict keyed by `(from_version, to_version)` is added to the load path; ad-hoc inline migrations are explicitly disallowed.

### GC

After each poll (post-pid-resolution), drop any `windows` entry that (a) has no `notes` and no `tags`, and (b) `last_seen.ts` is older than 30 days, and (c) was not refreshed this poll. This keeps the file from accumulating dead orphan-rebind hints indefinitely. Running GC after pid resolution (rather than at startup) means we never drop an entry that's about to rebind to a window we just polled. The cost of accidentally dropping a hint-only entry is at most a re-mint — annotations are not at risk because annotated entries are excluded by clause (a).

## Identity model

### Periscope id

Every tmux window periscope sees acquires a periscope-assigned 8-character hex id, stored as a tmux user option on the window: `@periscope_id`. The id is opaque — clients treat it as a string. Throughout this spec and the wire payload the field is named `pid` (short for "periscope id" — not Unix process id, despite the name collision; an inline comment in `server.py` calls this out).

Why a periscope-owned id rather than tmux's `#{window_id}` (e.g. `@7`):

- Same survival semantics for rename / move-between-sessions / reorder.
- Decouples annotation storage from tmux internals. If the rebind heuristic later evolves into a cross-restart mechanism, the namespace is ours to extend.
- Cost is one extra `set-option` per *new* window — negligible.

### Assignment

The `list-windows` format string in `server.py` (consumed by `list_windows()`) gains `#{@periscope_id}` as a new tab-separated field. Pid resolution does **not** live in `parse_pane()` — that function only knows the capture-pane text. Instead, a new helper `resolve_pids(windows)` runs after `cached_git_state` has populated each window's `branch`/`cwd`, and is called by every endpoint that produces window objects (`/api/state`, `/api/auto-rename-session`, `/api/auto-rename-window`). For each window:

1. If `#{@periscope_id}` is non-empty, use it as the `pid`.
2. If empty, attempt rebind (next section). If rebind hits, reuse the matched id. Otherwise mint a fresh 8-char hex uuid.
3. In either case where we synthesized an id (rebind or mint), invoke tmux with flags split house-style: `tmux("set-option", "-w", "-t", target, "@periscope_id", pid)`. Fire-and-forget; if it fails (e.g. the window is gone), the next poll repeats the attempt.
4. Update the pid's `last_seen` block with current `(session, name, branch, cwd, now)`.

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

The `last_seen` update in step 4 of assignment is what makes the rebind heuristic improve over time — every poll refreshes the hint, so a recreated window matches the freshest available `(session, name, branch, cwd)`.

### Known consequences

1. If a window is killed and a different window is later created with the same `(session, name)` within 30 days, the new window inherits the old annotations. Intentional: from the user's perspective, naming a window `gitops` in session `tc/foo` again is them saying "this is that window."

2. Between the moment we mint a fresh pid for an orphaned window and the moment tmux commits `@periscope_id` to that window, a parallel poll could theoretically re-attempt the match. In practice this is not a problem because the matching is deterministic (same `(session, name)` → same orphan candidate), so any racing pollers converge on the same pid. After the `set-option` lands, subsequent polls take the fast path at step 1 and the question is moot.

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

The existing `/api/state`, `/api/auto-rename-session`, and `/api/auto-rename-window` endpoints all gain `pid` on each window object (they share the `resolve_pids` helper described above).

### `/api/window/new` contract change (phase 4)

The current endpoint takes `mode: "claude" | "shell" | "vim"` and hardcodes a separate `send-keys` branch for each. In phase 4 this is replaced by an `exec` parameter:

```
POST /api/window/new?session=<s>&exec=<command>
```

The server spawns a new window with no command attached, then (after the existing 100ms post-create sleep) sends `<command>\n` via `send-keys` if `exec` is non-empty and non-whitespace. Empty `exec` = "just open a shell, don't run anything" (replaces today's `mode=shell` path). The three hardcoded mode branches in `window_new()` collapse into a single conditional `send-keys` call.

The frontend new-window tile reads `prefs.getCommands()` and renders one button per entry — the first command in the list becomes the primary (top of the tile, larger hit area), the rest stack below. The click handler calls `/api/window/new` with `exec` set to the row's `exec` string. The legacy `mode` query param is dropped at the same time the new-window tile switches to command-driven rendering.

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

Three legacy keys to migrate: `periscope:sessionOrder`, `periscope:collapsedSessions`, `periscope:view`.

On every successful `loadPrefs()` after upgrade:

1. If the server returned an empty `ui` block AND any of the three legacy keys exist, push their values to `/api/prefs/ui` (mapping `view` to `ui.view`, etc.).
2. Regardless of step 1, delete all three legacy keys. Once the server has authoritative state, the client copies are noise — leaving them around lets a "fresh state.json" event silently re-migrate stale data.

The shim lives in `prefs.js` until the operator confirms migration ran, then is deleted in a follow-up commit.

### `loadPrefs()` failure mode

If `loadPrefs()` fails (server down, network error, parse error), the in-memory cache is marked **not-loaded**. The grid still renders using whatever the server's `/api/state` poll returns. Mutators (`setSessionOrder`, `setCollapsed`, etc.) refuse to issue writes while not-loaded; they retry the initial `loadPrefs()` first and only proceed on success. The error surfaces in the existing `last-update` slot ("prefs load failed: …"). This avoids the footgun where a transient server hiccup at boot causes the user's first mutation to overwrite real server state with empty defaults.

### Consumers

- `state.js` — drops `loadOrder` / `saveOrder` / `loadCollapsed` / `saveCollapsed` exports. `state.collapsedSessions` becomes a derived view sourced from `prefs.getCollapsed()`.
- `grid.js` — calls `prefs.setSessionOrder(...)` and `prefs.setCollapsed(...)` instead of the localStorage helpers. Uses `w.pid` (from `/api/state`) for any annotation-related rendering.
- New-window tile — renders one button per `prefs.getCommands()` entry, replacing the hardcoded `+ claude` / `+ shell` markup.

### Annotations UI

Annotations live in the **modal sidebar** (`#modal-side`), as a new section alongside the existing "Linked" and "Activity" sections. The modal is the user's natural editing surface — opening a card already shows the live terminal plus PR/CI/activity context, and notes belong with that context, not as a separate popover on the grid.

The new section "Notes" renders:

- A `<textarea>` for `notes`, auto-saving on blur (debounced 600ms during typing).
- A free-text tag input that splits on space/comma into the `tags` array, with the existing tags rendered as removable chips above the input.

Save uses the same eager-local + background-PUT pattern as the other prefs mutators. Failed PUTs revert the cache and surface via `apiCall()`.

Grid cards (and stream rows, where applicable) get a small unobtrusive indicator (e.g. a `📝` glyph in the card-head row) when the card's pid has non-empty `notes` or `tags`. The indicator is purely a visual cue; clicking it opens the modal like a regular card click. Card density and layout stay unchanged.

The annotation lookup hangs off the `pid` field that `/api/state` and `/api/pane` already emit (after phase 2 lands).

### Commands UI

A gear icon in the filters row (next to the view-switch — both are "global UI controls" and visually belong together) opens a modal with:

- A list of current commands. Each row: label input, exec input, drag handle, delete button.
- An "+ add" row at the bottom.
- Save persists all rows via the appropriate POST/PUT/DELETE calls.

Drag handles let the user reorder; the first row in the list becomes the new-window tile's primary button.

`modal.js` is bespoke for the terminal pane (xterm wiring, `state.activeTarget`, header polling). Rather than overload it, phase 4 adds:

- A second `#commands-modal` `<div>` in `index.html` with its own structure.
- A new `commands-modal.js` module that owns open/close + form state for that modal.
- The Escape-closes-modal handler in `modal.js` is extracted into a tiny shared helper (`overlay.js`) that both modals register against. This is the only refactor `modal.js` takes on; everything else stays as-is.

A separate, larger refactor that extracts a generic overlay primitive can happen later if a third modal ever appears — explicitly deferred.

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

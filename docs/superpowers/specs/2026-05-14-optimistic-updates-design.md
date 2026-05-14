# Optimistic UI updates

## Problem

Every dashboard interaction waits one poll cycle (up to 3s on `/api/state`) before the UI reflects what just happened. Tom uses the dashboard constantly to hop between modals; the dominant complaint is action-feedback lag.

The most visible case: closing a modal should snap the just-viewed card to the far-left of its session (highest recency). The server already does the right thing — `/ws/pane` accept bumps `_acted_at` (`server.py:1991`), and the grid sorts cards within a session by `acted_at` desc, `index` asc as tiebreak (`grid.js:359-363`). `focused_at` is display-only ("viewed Xm" label, session header recency). But the `acted_at` bump doesn't reach the browser until the next poll.

The same shape applies to kill, rename, and new-window: each fires a request and then sits on stale `state.lastWindows` until `poll()` resolves.

## Goals

1. **Modal interactions feel instant.** Opening a modal moves the card to the far-left of its session before the next poll.
2. **Mutations feel instant.** Kill removes the card immediately; rename updates the name immediately; new-window inserts a placeholder card immediately.
3. **Reconcile cleanly with the authoritative server poll.** No double-rendering, no stuck-pending state.
4. **No protocol changes.** No new endpoints, no server work, no SSE/WS broadcast layer.

## Non-goals

- Reducing the poll cadence.
- Server push (SSE / WebSocket broadcast). Separate, later.
- Activity-gated `capture-pane` server-side. Separate, later.
- Removing the 220ms dblclick-to-rename deferral. Separate, can ship independently.
- Auto-rename optimistic UI. The ✨ button already has explicit `thinking…` / `✓ renamed N` progress, which is the right pattern for a server-side LLM call.

## Approach: client-side pending overlay merged at render time

Maintain two client-side maps of "things we expect to be true but the server hasn't confirmed yet":

```js
// state.js
export const pendingByTarget = new Map();  // target -> PendingAction
export const pendingNew      = new Map();  // tempId -> PendingNew
```

Mutations write entries immediately. `render()` merges `state.lastWindows` with these maps to produce the rendered window list. Each successful `poll()` runs a `reconcile()` step that prunes confirmed entries; a hard TTL prunes anything that overstays.

```
state.lastWindows  ───┐
                      ├─► mergePending() ──► sort ──► render()
pendingByTarget    ───┤
pendingNew         ───┘
```

### PendingAction types

```ts
// Keyed by target in pendingByTarget. One entry per target — later writes overwrite.
type PendingAction =
  | { kind: "touch";  ts: number; acted_at: number }
  | { kind: "kill";   ts: number }
  | { kind: "rename"; ts: number; name: string };

// Keyed by tempId in pendingNew.
type PendingNew = {
  ts: number;
  tempId: string;     // synthetic target string, e.g. "pending:tc-foo:1731555231"
  session: string;
  exec: string;       // "" for shell, "claude" for claude, etc.
};
```

`ts` is `Date.now()/1000` at issuance. Used for TTL.

### mergePending(windows): WindowState[]

Pure function, no side effects. Pseudocode:

```js
function mergePending(windows) {
  const out = [];
  for (const w of windows) {
    const p = pendingByTarget.get(w.target);
    if (p?.kind === "kill") continue;                          // hide killed
    if (p?.kind === "rename") out.push({ ...w, name: p.name });
    else if (p?.kind === "touch")
      out.push({ ...w, acted_at: Math.max(w.acted_at || 0, p.acted_at) });
    else out.push(w);
  }
  for (const pn of pendingNew.values()) {
    out.push(placeholderWindow(pn));                            // synthetic card
  }
  return out;
}
```

`placeholderWindow()` produces a `WindowState`-shaped object with:
- `target: pn.tempId`, `session: pn.session`
- `index: Number.MAX_SAFE_INTEGER` (within-session sort is `a.index - b.index` numeric; a string `"…"` produces `NaN` and undefined ordering — `grid.js:362`)
- `name: pn.exec ? "(starting claude…)" : "(starting…)"`
- `state: "shell"`, `is_claude: false`
- `acted_at: pn.ts` (so the placeholder sorts as most-recent in the session — `acted_at` is the primary key, dominates index)

The card renderer:
- emits the card with classes `card card-pending state-shell` (keeps `.card` so flex layout works)
- omits the ✕ kill button
- the click handler in `grid.js` gets a new first branch: `if (e.target.closest(".card-pending")) return;` — short-circuits the modal-open and dblclick-rename paths. Without this gate, a click would call `openModal(card.dataset.target)` with a `pending:…` target that `targetQuery()` would happily mangle into a 404.

### reconcile(serverWindows, pollStartedAt)

Called from `poll()` after `state.lastWindows = data.windows`. Walks `pendingByTarget` and `pendingNew` and drops entries the server has caught up to.

**Critical guard against the in-flight-poll race:** `poll()` stamps `t0 = Date.now()/1000` *before* calling `fetch`. Reconcile receives `t0` and **skips any pending entry with `ts > t0`** — those were issued after this poll started, so its response can't possibly reflect them. This eliminates the "mutate during in-flight poll → reconcile drops the pending → 3s of stale UI" failure mode.

| Pending | Drop when |
|-|-|
| `touch` | the corresponding target is present in `serverWindows` *and* the entry was issued before this poll started (`pending.ts <= t0`). The server confirms-by-existence; we don't compare `acted_at` values because server `_acted_at` is integer-seconds (`server.py:206`) and the client's `pending.acted_at` is `Date.now()/1000` — equality / `>=` comparison is unreliable under clock skew and second-rounding. |
| `kill`  | target absent from `serverWindows` *and* `pending.ts <= t0`. |
| `rename`| server's `w.name === pending.name` *and* `pending.ts <= t0`. |
| `new`   | a real window with `target === pn.realTarget` appears in `serverWindows` (real target is captured from the `/api/window/new` response, see below). The `t0` guard isn't needed here — the request must have returned (with realTarget) before this can match. |

Hard TTL: drop any pending entry older than **8 seconds** unconditionally, regardless of the rules above. This is the safety net for cases the server never catches up to: a kill that didn't actually take, a rename the server overrode silently, or a `touch` whose WS-accept stalled longer than two poll cycles. Why 8s and not less: WS-accept happens asynchronously, on a separate connection that can stall under load — `_acted_at` is bumped only on `await websocket.accept()` (`server.py:1986`), not on the modal-open API call. With a 3s poll cadence and worst-case WS-accept latency of 1–2s, 8s gives two clean poll cycles to confirm even the slowest interaction.

#### `new`-window mapping heuristic

When `/api/window/new` returns, we get back the real `{session, index, target}`. Save it in the `PendingNew` as `realTarget`. On the next poll, if `serverWindows` contains a window with that target, drop the pending entry — the real card replaces the placeholder cleanly.

If the request hasn't returned yet by the time a poll arrives, the placeholder stays. If the request fails, the rollback path (below) deletes the placeholder.

### Failure rollback

Mutations that already use `apiCall()` (kill window, kill session, new window) get a uniform wrapper:

```js
async function withOptimistic(pending, request) {
  // Caller decided whether to write to pendingByTarget or pendingNew already.
  render(state.lastWindows);
  const result = await request();                  // returns null on apiCall failure
  if (!result) {
    rollback(pending);                              // delete from whichever map
    render(state.lastWindows);
  }
  return result;
}
```

`apiCall()` already surfaces a user-visible alert on HTTP / `data.ok === false` failure; rollback just keeps the grid honest.

**Rename stays as raw `fetch`.** The existing `startRename` (card) and `startModalRename` (modal) handlers catch silently and rely on `poll()` to resync. The spec-review draft proposed switching to `apiCall()` for rollback symmetry, but that introduces user-visible alerts on rename failure where today there are none. That's a behavior change Tom hasn't asked for. Instead: the optimistic rename entry stays in `pendingByTarget`; if the server didn't apply it, the 8s TTL drops it and the card reverts. Slightly less responsive on rename failure than the other mutations, but consistent with today's "silent recovery via poll" semantics.

## Per-interaction changes

| Interaction | Pending entry | File | Notes |
|-|-|-|-|
| `openModal(target)` | `touch { acted_at: now }` | `modal.js` | grid behind modal re-renders; card is far-left before user closes |
| `closeModal()` | — | `modal.js` | no extra action needed; open already bumped it |
| `handleKillWindow` | `kill` | `grid.js` | replaces existing `await + poll()` |
| `handleKillSession` | `kill` × N (one per window in session) | `grid.js` | session header disappears once all its windows are filtered |
| `startRename` finish | `rename` | `grid.js` | applied to card-name; modal mirror sees it via poll |
| `startModalRename` finish | `rename` | `modal.js` | also re-renders grid via existing `poll()` chain |
| `handleNewWindow` | `new` | `grid.js` | placeholder card appears immediately; real card replaces it on poll |
| `handleAutoRename*` | — | (unchanged) | already has explicit progress UI |

## Files touched

- `state.js`: add `pendingByTarget`, `pendingNew` exports. ~5 lines.
- `grid.js`: `mergePending()`, `reconcile()`, placeholder card branch in `renderCard()`, `withOptimistic()` helper, updates to `handleKillWindow` / `handleKillSession` / `handleNewWindow` / `startRename`, sort key reads merged data, `poll()` calls `reconcile()`. ~120 lines net.
- `modal.js`: `pendingByTarget.set(touch)` on open; switch rename POST to `apiCall()` so rollback works; pending rename on rename finish. ~15 lines.

No server changes. No new endpoints. No CSS beyond a small `.card-pending` rule.

## Interactions with existing state

- **`state.editingTarget`** (pauses `poll()` while a card-name input is open). Unchanged. Rename optimistic update fires only on commit (`finish(true)`), at which point editing has already ended.
- **`state.modalRenaming`**. Same pattern.
- **`state.collapsedSessions`**. Untouched — pending entries don't alter session grouping.
- **The 3s `poll()` cadence.** Pending entries normally live ≤ 3s, occasionally up to 6s for poll-timing edge cases. The 8s TTL is a backstop.

## Edge cases worth naming

1. **Rapid modal cycling.** Open A (touch), close, open B (touch), close, open A (touch). Three pending entries; later overwrites earlier on the same target. Single map, no growth.
2. **Kill, then immediately new-window in the same session.** Independent maps, no interaction. Fine.
3. **Two new-window requests in flight at once.** Both create distinct `tempId`s, both render placeholders, each maps to its own `realTarget`. Fine.
4. **Server poll racing optimistic write.** The dangerous shape is *not* "poll completes before pending is set" — that's fine, the merge handles it. The dangerous shape is "poll started *before* pending was set, then resolves *after*; its response can't reflect the pending action, but reconcile would happily drop the pending entry as if it had been confirmed." Mitigated by the `t0` guard in `reconcile()`: entries newer than the poll's start are immune to that poll's reconciliation pass. Caller order remains `set pending → render → fire request`.
5. **Kill that doesn't take.** The window reappears at TTL expiry. User sees the card return after ~8s. Acceptable — kill failure is rare, and `apiCall()` would have alerted if the HTTP layer rejected; this is the case where HTTP said OK but tmux kept the window.

## Open questions

1. **Kill-failure UX.** If a window reappears after the optimistic kill, should it flash to draw attention, or silently come back? My take: silent. Kill failure is rare enough that animation cost > benefit.
2. **Placeholder visuals.** A muted `.card-pending` with no kill ✕ and no click handler. Should it spin? I'd default to no — the card *being there* is the feedback.
3. **`touch` on modal close in addition to open.** Open already suffices: by the time the modal closes, the grid has the bump. Only a long-running modal that outlives the 8s TTL would need a refresh, and the next user action will bump it again. Leave close alone.

## Out of scope, but related

- **Drop the 220ms card-name dblclick deferral.** Independent change. Move rename to the modal-only (already present there) and card-name clicks open the modal instantly. Should ship independently, before or after this spec.
- **Server-side push.** If even the post-poll lag (~3s worst case for Claude-driven state flips) still feels bad after this lands, the next layer is SSE / WS broadcast. Separate spec.
- **Activity-gated `capture-pane`.** Server-side perf, orthogonal.

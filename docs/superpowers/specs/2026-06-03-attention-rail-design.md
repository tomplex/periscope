# Attention rail — design spec

**Date:** 2026-06-03
**Status:** Draft for review
**Area:** `static/src/split/` (left rail), `periscope/routes/alerts.py`, `periscope/panes.py`, `periscope/store.py`

## Motivation

The split view is now the only view that gets used; grid mode is effectively
dead. But the split-view left rail (`#rail`, the Repo → Worktree → Pane tree)
is missing three things that keep pulling attention back to scanning or to the
modal:

1. **Alerts aren't surfaced where the eyes already are.** The notify() feed
   lives in a *right* rail (`#alerts-rail`). The user is always looking at the
   *left*. Alerts that matter go unseen.
2. **"This session needs me" is not definitive.** Periscope already *detects*
   the condition (permission dialogs, AskUserQuestion, replies ending in `?`)
   and renders it as a small pulsing dot on the row — but a 7px dot buried in a
   tall tree is too quiet. There is no aggregated, always-visible answer to
   "which sessions are waiting on me right now?"
3. **No way to pin the few tabs that matter.** Across many projects, a handful
   of tabs are the ones being actively driven. They should be reachable without
   hunting through the tree — while still living in their normal project.

All three are the same shape: **the top of the left rail should be an attention
zone** — a small set of always-visible, high-signal sections above the project
tree.

## The unified model

The rail becomes four stacked, collapsible regions, top to bottom:

```
┌─ #rail ───────────────────────────┐
│ ▾ ⚠ NEEDS YOU                 [2]  │  attention — red-tinted
│     ● periscope · rail-redesign    │    dialog (live needs-input)
│     ⚠ fdy · etl-job        4m  ×   │    need_human (event)
│ ▾ ★ PINNED                    [3]  │  convenience — subtle
│     ✻ periscope · main-server  ●   │
│     ✻ fdy · feature-store      ●   │
│ ▾ PROJECTS                         │  the existing tree (restyled)
│   ◆ periscope                      │
│     ⎇ rail-redesign                │
│       ✻ rail-redesign      ★   ●   │
│       ✻ build-fix          ☆   ●   │
│   ◆ fdy …                          │
│ ▸ ACTIVITY                    [7]  │  low-signal log — collapsed
└────────────────────────────────────┘
```

**Section semantics:**

| Section | Contents | Clears when |
|---|---|---|
| **Needs you** | Union of (a) panes in live `needs-input` state, and (b) unacked `need_human` notify() events | (a) self-clears when parsed state leaves `needs-input`; (b) clears when the pane is *visited* (see Ack model) |
| **Pinned** | Tabs the user explicitly starred. Each also keeps its normal in-tree row. | Unpinned (toggle the star) |
| **Projects** | The existing Repo → Worktree → Pane tree, unchanged in function | n/a |
| **Activity** | `done` / `info` notify() events — the low-signal log. Starts collapsed. | n/a (history feed) |

**Row vocabulary:**

- Out-of-tree rows (Needs you, Pinned) carry a `project · tab` label so their
  origin is unambiguous. In-tree rows stay bare.
- Needs-you and Pinned rows mirror the same live status dot as the tree, so a
  pinned/needy session still shows working/idle at a glance.
- Every section header is the *same* primitive: a collapse chevron, an
  uppercase label, and a right-aligned count badge.

This is `B · dedicated zone` + `A · merge by intent` from brainstorming: the
high-signal zone holds everything that wants action *now* (whether it arrived as
parsed state or an explicit need_human), and the noisy `done`/`info` events drop
to the Activity log.

## Build sequence — two phases

The phases ship and commit independently. **Phase 1 first**, because it
establishes the visual vocabulary (the section-header primitive, row spacing,
typography) that Phase 2's new sections are then built in — building the new
sections first would mean styling them once in the current look and again in the
restyle.

### Phase 1 — Foundation / polish

Restyle the existing rail toward the cleaner mockup aesthetic **while preserving
every functional affordance**. This phase adds no new behavior; it is purely
visual + the extraction of the shared section-header primitive.

**Changes:**
- Introduce a reusable **section-header** component/CSS: collapse chevron +
  uppercase label + right-aligned count badge. Apply it to a new top-level
  `PROJECTS` header that wraps the existing tree (the tree gains a collapsible
  root header it doesn't have today).
- Adopt the mockup's spacing and typography — more vertical breathing room,
  clearer indent hierarchy.
- Restyle status dots / row hover toward the mockup palette.

**Explicitly preserved (must not regress):**
- Worktree tree-connector lines + full session-path labels.
- PR badge + CI glyph, diff-stat chips, Linear chips, git-dirty indicator
  (`WorktreeMeta`).
- `review` rows (live `◉` / empty `○ start →`), `+ New tab` rows.
- Collapsed-worktree count badges.
- The full status palette (red/yellow/green/grey/none) and its meanings.
- Drag-reorder, inline rename, close — all interaction handlers unchanged.

**Open visual decisions (flagged for review, not blocking):**
- Tree-connector lines: keep (lightened) vs. drop. *Recommendation: keep,
  lightened* — they carry real worktree→pane hierarchy that the flat mockup lost.

### Phase 2 — Attention sections

Add the three new sections, built natively in the Phase-1 style, reusing the
section-header primitive.

**Needs you:**
- Data source per pane is already computed in `panes.py`: `state == "needs-input"`
  (covers both `_detect_needs_input` dialogs and `_detect_asked_question`).
- Reason label is derived client-side via `waitLabel(w.waiting_for)` — the view
  already exposes `waiting_for`, and `util.js:waitLabel` already maps its values
  (`"approve askuserquestion"` → "needs answer", `"permission prompt"` → "needs
  approval", `"dialog open"` → "needs input"). This **does** distinguish
  AskUserQuestion from a permission prompt (directly serving ask #2), and
  degrades to a generic "needs input" when `waiting_for` is absent. (Earlier
  draft assumed only a `dialog`/`asked` split from `parse_pane` was possible;
  `waiting_for` from the session state is the richer, real signal — note that
  `window_view.py` forces `asked_question=False` for mapped live sessions, so a
  flag-derived label would be dead in prod.)
- `need_human` events come from the alert feed (`/api/alerts/recent`, filtered
  to `kind == "need_human"`) and are merged in as event rows with a relative
  timestamp + a `×` dismiss. Each event carries a stable id (see Server-side
  work) so ack/dismiss can't collide two same-second alerts.
- Live needs-input rows self-clear (next poll without the state). Event rows
  clear per the Ack model below.
- Sort: live needs-input first (most urgent — a dialog is blocking *now*), then
  need_human events newest-first.

**Pinned:**
- Pin identity is the stable **`@periscope_id`** (`pids.py`), which survives
  rename/move — not the volatile tmux target. Pins persist in UI prefs
  (`prefs.js` / server prefs) as a list of periscope-ids.
- Affordance: hover any tree pane-row → a `☆` appears; click toggles pin
  (filled gold `★`). The in-tree row keeps its filled star when pinned.
- A pinned pane that no longer exists (id not in live state) is dropped from the
  section silently (same posture as dead-pane rows elsewhere).

**Activity:**
- `done` / `info` (and `milestone`) events from the existing feed, reverse-chron,
  collapsed by default. This is the relocated/repurposed content of today's
  right `#alerts-rail`, minus the need_human rows (those moved up to Needs you).
- The right `#alerts-rail` (`Alerts.jsx`) is removed once its content lives here.
  This is **not** a pure delete — three things must be re-homed or they regress:
  1. The `#alerts-toggle` / `#alerts-badge` markup is rendered by **`Header.jsx`**
     (not `Alerts.jsx`). Repurpose or remove it; the need_human count now drives
     the Needs-you badge instead.
  2. The **poll loop** (`/api/alerts/recent` every 3s) moves to whatever owns the
     new sections. The Needs-you badge must stay fresh even when sections are
     collapsed, exactly as today's badge polls regardless of panel open-state.
  3. **`maybeNativeNotify` + `setBadgeCount`** (the macOS dock badge + native
     notification on a need_human rising edge, Tauri-only) live *inside* the
     deleted component. They must move with the poll loop or native notifications
     regress. The first-poll dedupe sentinel (`seenAlertKeys = null`) moves intact.
- `--header-h` is **not** at risk — `Header.jsx` sets it authoritatively, so
  split-view's `top: var(--header-h)` survives the rail removal.

## Ack model (need_human events)

Live needs-input never needs an ack — it is derived state and disappears on its
own. `need_human` events do, because they are events, not state.

**Ack rule (computed client-side):** a need_human event is acked once
`max(focused_at, acted_at) > event.ts`. Both stamps are already in the per-pane
view payload (`recency_stamps_for`, panes.py), so no server work is needed.

- `acted_at` bumps on periscope-originated actions — `/api/send`, rename, paste,
  and the `/ws/pane` terminal mount (opening the pane in the detail view).
- `focused_at` bumps when the pane **becomes the active window in its session**
  (`update_focus_from_windows`, panes.py:113) — i.e. when the user switches to
  it directly in tmux. This is what makes "however you got there" mostly true:
  navigating to the pane in raw tmux *is* observable.
- **Residual gap (accepted):** if the pane was *already* the active tmux window
  when the alert fired and the user answers by typing straight into raw tmux,
  nothing re-stamps — periscope cannot see keystrokes in an already-focused
  pane. This is the least-urgent case (the user is already looking at it); the
  `×` dismiss is the escape hatch. We document this rather than pretend the rule
  is total.

A `×` on the row dismisses explicitly, keyed on the event id. Dismissed ids are
held client-side (the feed is in-memory and resets on restart, so no durable
ack store is warranted — this is a deliberate simplification over the
`set_window_fields_bulk` per-pid stamp machinery, which exists but isn't needed
here).

## Server-side work

Phase 2 is almost entirely frontend — the server already exposes the per-pane
state, the `focused_at`/`acted_at` stamps, and the alert feed. **One** server
change is required:

1. **Stable alert event id.** Alert records are currently `{message, kind,
   severity, ts}` with whole-second `ts` and no id, so the frontend synthesizes
   a collision-prone `target|ts|message[:60]` key. Stamp a `uuid4().hex` (uuid is
   already imported) on the record in `channels.py`'s notify-tool handler and
   surface it through `/api/alerts/recent`. Ack/dismiss then key on the id.

Everything else is client-side and needs **no** server change:
- **Kind split:** `kind` is already on every row; the frontend filters
  `need_human` → Needs you, `done`/`info`/`milestone` → Activity.
- **Ack:** computed from `max(focused_at, acted_at) > event.ts` using stamps
  already in the view payload (see Ack model).
- **Reason label:** `dialog` vs `asked` from existing `needs_input` /
  `asked_question` view fields.

## Out of scope (YAGNI)

- Sound / new native-notification channels for Needs-you. Native notify for
  need_human already exists in the Tauri shell; v1 reuses it unchanged.
- Pinning worktrees, repos, or review tabs — pin is **pane-only** for v1.
- Cross-restart alert history (the feed is already in-memory; unchanged).
- Grid-view parity — grid is not getting these sections.

## Testing strategy

- **Alert event id** (`channels.py`): extend `tests/test_channel_smoke.py` /
  the channels test to assert every notify() record carries a unique id, and
  that `/api/alerts/recent` surfaces it.
- **Needs-you union + sort** (the one pure frontend function worth a test):
  merging live needs-input rows with unacked need_human events, ordering
  (live-first, then events newest-first), and the client-side ack filter
  (`max(focused_at, acted_at) > event.ts` plus dismissed-id set).
- **Pin persistence:** pin/unpin round-trips through prefs keyed by
  periscope-id; dead-id pruning when a pinned pane is gone from live state.
- No server-side ack or reason-label tests — both moved client-side and need no
  new server plumbing.
- Everything else (section rendering, restyle, hover-star affordance) is
  verified in the browser per the project convention — UI work is eyeballed,
  not unit-tested.

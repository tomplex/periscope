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
│     ● periscope · rail-redesign    │    AskUserQuestion
│     ⚠ fdy · etl-job        4m  ×   │    need_human
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
  Reason label derives from which detector fired: dialog → `permission` /
  `AskUserQuestion`-style, `?`-reply → `asked`.
- `need_human` events come from the alert feed (`/api/alerts/recent`, filtered
  to `kind == "need_human"`) and are merged in as event rows with a relative
  timestamp + a `×` dismiss.
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
- The right `#alerts-rail` is removed once its content lives here. The header
  alerts-toggle button + badge behavior folds into the new sections (need_human
  count drives the Needs-you badge).

## Ack model (need_human events)

Live needs-input never needs an ack — it is derived state and disappears on its
own. `need_human` events do, because they are events, not state.

- **Visiting the pane acks its need_human, however the user got there:** rail
  selection, modal open, or typing directly into the tmux pane. The server
  already tracks this as **`_acted_at`** (panes.py) — a *user-action-only*
  recency stamp (distinct from `_focused_at`, which bumps on mere output). A
  need_human event is acked once `_acted_at[target] > event.ts`.
- A `×` on the row dismisses without visiting (escape hatch).
- Prior art for per-pane ack persistence: state.py already stamps
  `completed_at` / `acked_at` per periscope-id via `set_window_fields_bulk` —
  the need_human ack should follow the same persistence pattern rather than
  inventing a parallel store.

## Server-side work

Most of Phase 2 is frontend; the server already exposes what's needed, with two
gaps:

1. **Alert kind split for the feed.** `/api/alerts/recent` already returns
   `kind`. The frontend filters: `need_human` → Needs you, others → Activity.
   No server change strictly required, but consider an `acked` flag on
   need_human rows (see #2).
2. **need_human ack.** Either (a) compute acked-ness server-side by comparing
   the event ts against `_acted_at` and stamp it, or (b) expose `_acted_at` and
   let the client compute it. *Recommendation: server-side* — keeps the
   "however you got there" rule (which includes typing directly into tmux, a
   thing the client can't observe) authoritative on the server. A small
   `acked_at` stamp per (periscope-id, event) following the existing
   `set_window_fields_bulk` pattern.

The exact reuse boundary (extend the existing stamp machinery vs. a small new
store) is a structure-proposer question.

## Out of scope (YAGNI)

- Sound / new native-notification channels for Needs-you. Native notify for
  need_human already exists in the Tauri shell; v1 reuses it unchanged.
- Pinning worktrees, repos, or review tabs — pin is **pane-only** for v1.
- Cross-restart alert history (the feed is already in-memory; unchanged).
- Grid-view parity — grid is not getting these sections.

## Testing strategy

- `panes.py` reason-label derivation: extend `tests/test_panes.py` with
  fixtures that exercise `needs-input` → `(permission | AskUserQuestion | asked)`
  classification.
- Ack logic (`_acted_at > event.ts`): unit test the server-side ack computation
  with synthetic timestamps.
- Pin persistence: pin/unpin round-trips through prefs keyed by periscope-id;
  dead-id pruning.
- Frontend section rendering is verified in the browser (the project convention
  — UI work is eyeballed, not unit-tested), except the merge/sort logic for the
  Needs-you union, which is a pure function and gets a unit test.

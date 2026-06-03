# Attention rail — code structure proposal

**Date:** 2026-06-03
**Spec:** `docs/superpowers/specs/2026-06-03-attention-rail-design.md`
**Status:** Draft for Tom's review (structural blueprint; precedes the implementation plan)

Scales to a medium frontend feature with a one-line server change. Two phases, as the spec sequences them. The structure below is deliberately conservative: this is a single-user, fast-iteration tool, and the spec is already YAGNI-disciplined (pin is pane-only, no durable ack store, one server change). No new abstractions beyond a single shared `<SectionHeader>` primitive and one new pure module.

---

## 1. Spec pushback

Two structural assumptions in the spec I'd adjust. Neither blocks; both are cheap to settle now.

**1a. "the poll loop moves to whatever owns the new sections" — don't co-locate the poll with a component.** The spec (Phase 2, Activity bullet 2/3) talks about moving the `/api/alerts/recent` poll + `maybeNativeNotify`/`setBadgeCount` "to whatever owns the new sections." Today that machinery lives *inside* a component (`Alerts.jsx`), which is exactly why re-homing it is fiddly — the poll lifecycle, the dedupe sentinel, and the dock-badge side effects are all tangled into a component's `useEffect`. Putting them back inside another component (`Rail.jsx` or a new section component) repeats the mistake: the Needs-you badge must stay fresh while sections are collapsed, and native-notify must fire regardless of what's rendered, so this logic must **not** be owned by a component's render lifecycle.

**Alternative:** a new non-component module `static/src/split/alertFeed.js` that owns the poll loop and exports an `alertItems` signal (the read model) plus `startAlertFeed()` (idempotent, started once from `Split.jsx`'s existing `useEffect`, exactly like `startPolling()`). Native-notify/dock-badge are a pure side-effect the loop calls. Components *read* `alertItems.value`; they never own the timer. This matches the established `grid/poll.js` → `windows` signal pattern (the poll is a module, the signal is the read model) rather than the `Alerts.jsx`-owns-its-own-timer pattern, which the spec itself flags as the thing to untangle.

**1b. The Needs-you badge should not be wired by `getElementById`.** `Alerts.jsx` updates the header badge imperatively (`document.getElementById("alerts-badge")`). That by-id bridge existed because the badge lived in a different surface (`Header.jsx`) than the feed (`Alerts.jsx`). In the new design the count badge lives on the **Needs-you section header**, which `<Rail>` renders directly. So the badge becomes a normal reactive prop computed from the same merged Needs-you list — no `getElementById`, no cross-surface bridge. The `#alerts-toggle`/`#alerts-badge` markup in `Header.jsx` is deleted outright (see §5). This is strictly less plumbing than the spec's "repurpose or remove" framing implies — it's remove.

---

## 2. Assumptions

- **`alertFeed.js` keeps the existing `/api/alerts/recent?limit=100` cadence (3s) and failure-toast behavior verbatim.** The spec says "exactly as today's badge polls"; I'm treating the whole `Alerts.jsx` poll body (direct `fetch`, `pollFailed` edge-toast, keep-stale-on-error) as a verbatim move into `alertFeed.js`, not a rewrite.
- **`pid` is the pin identity surfaced to the frontend.** The spec says pins key on `@periscope_id` (`pids.py`), which survives rename/move. The window view's `w.pid` *is* that periscope-id (it's the `@periscope_id` stamp, used as the rail's pane key everywhere already — `pane:${w.pid}`). So "list of periscope-ids" == "list of `w.pid`". No new server field. If that equivalence is ever wrong, only `attention.js`'s pin-resolution and `prefs.js`'s pin list are affected.
- **The Needs-you "reason label" (`dialog` vs `asked`) is derived from `w.needs_input` + `w.asked_question`** — both already on the view (`window_view.py` spreads `parsed`, which carries `needs_input`/`asked_question`). `asked_question === true` → `asked`; otherwise (`needs_input` true, `asked_question` false) → `dialog`. Confirmed present in the payload; no server change.
- **`focused_at`/`acted_at` are on every window-view row** (`window_view.py:187-188`). The client ack rule reads them directly off `w`. Confirmed.
- **Phase 1's "PROJECTS root header" wraps the existing tree without changing the flat row sequence** — the new header is a sibling rendered *before* the first `RepoRow`, not a wrapper element around the repo rows (which would break the `child-row::before/::after` adjacency CSS — see §2 of the file-layout note and the invariant in §5).
- **Pinned/Needs-you out-of-tree rows reuse `statusDotClass` and the pane label vocabulary** but are **not** draggable and have **no** tree-connector pseudo-elements (they're flat lists, not tree children). They therefore must not carry the `child-row` class (which triggers the connector `::before/::after`). New flat-row classes instead (`attn-row`).

---

## 3. File layout

```
NEW   static/src/split/SectionHeader.jsx     Shared section-header primitive (chevron + label + count badge). Phase 1.
NEW   static/src/split/attention.js          Pure: Needs-you merge/sort/ack + pin resolution + Activity filter. Phase 2. UNIT-TESTED.
NEW   static/src/split/alertFeed.js          Non-component: /api/alerts/recent poll loop + native-notify/dock-badge + `alertItems` signal. Phase 2.
NEW   static/src/split/AttentionSections.jsx Needs-you + Pinned + Activity section components (read attention.js + signals). Phase 2.
NEW   static/src/split/__tests__/attention.test.js   Unit tests for the attention.js pure functions. Phase 2.

EDIT  static/src/split/Rail.jsx              Phase 1: PROJECTS header via SectionHeader. Phase 2: mount <AttentionSections> above tree.
EDIT  static/src/split/RailRows.jsx          Phase 1: restyle pass. Phase 2: hover-☆ pin affordance on PaneRow.
EDIT  static/src/prefs.js                     Phase 2: getPinnedPids / setPinnedPids / togglePin + pruneDeadPins; reuse getRailCollapsed/setRailCollapsedKey for section collapse.
EDIT  static/src/chrome/Header.jsx            Phase 2: delete #alerts-toggle / #alerts-badge button markup + its slot.
EDIT  static/src/overlays/Overlays.jsx        Phase 2: drop the <Alerts /> mount.
DEL   static/src/overlays/Alerts.jsx          Phase 2: removed once alertFeed.js + AttentionSections own its content.
EDIT  static/styles.css                       Phase 1: section-header + restyle CSS. Phase 2: attention-row + hover-star CSS; remove #alerts-rail block.

EDIT  periscope/channels.py                   Phase 2: uuid4().hex `id` on each notify alert record (_do_notify_tool).
EDIT  periscope/routes/alerts.py              Phase 2: surface `id` in the /api/alerts/recent row shape.
EDIT  tests/test_channels.py                  Phase 2: assert every notify record carries a unique `id`.
EDIT  tests/routes/test_alerts.py (or new)    Phase 2: assert /api/alerts/recent surfaces `id`. (Create if no route test exists.)
```

---

## 4. Per-module structure

### `SectionHeader.jsx` — **plain function component** (Phase 1)
The shared primitive. One presentational component, no state of its own.

```js
export function SectionHeader({ icon, label, count, collapsed, onToggle, tone })
//   icon     — optional glyph node (⚠ / ★ / undefined for PROJECTS)
//   label    — uppercase string ("NEEDS YOU" / "PINNED" / "PROJECTS" / "ACTIVITY")
//   count    — number | null  (null → no badge; 0 → no badge for convenience sections)
//   collapsed, onToggle — collapse state threaded from the caller (NOT owned here)
//   tone     — "alert" | "default"  → CSS class for the red-tint vs subtle palette
```
Rationale: rung 1 (function). It owns no state — collapse lives in prefs, count is computed by the caller. A class would earn nothing. The existing `.rail-head` becomes the `PROJECTS` instance of this; the repo/worktree rows keep their own `.rail-chev` inline (they're *rows*, not section headers — different CSS, different drag semantics, so do **not** retrofit them onto `SectionHeader`).

**Collapse threading:** reuse `getRailCollapsed()` / `setRailCollapsedKey(key, bool)` verbatim with reserved keys `"sec:needs"`, `"sec:pinned"`, `"sec:projects"`, `"sec:activity"`. These live in the same `rail_collapsed` map as `repo:*` / `wt:*` — no new pref field, no namespace collision (distinct prefix). Activity defaults collapsed: `getRailCollapsed()["sec:activity"] !== false` (absent → collapsed), the inverse default of the tree keys. That one inverted default is the only special case; encode it at the read site in `AttentionSections.jsx`, not in prefs.

### `attention.js` — **plain functions over plain data** (Phase 2, the testable core)
The one module the spec singles out for unit tests. Pure, no signals, no DOM — mirrors `railTree.js`'s posture exactly (consumed by render; testable in isolation).

```js
// Needs-you: union of live needs-input panes + unacked need_human events, sorted.
export function buildNeedsYou(windows, alertItems, dismissedIds)
//   → ordered array of rows:
//       { kind: "live",  pid, w, reason: "dialog"|"asked" }      // live needs-input
//       { kind: "event", id, target, w, message, ts }            // unacked need_human
//   Rule:
//     live  = windows.filter(w => w.state === "needs-input"),  reason from needs_input/asked_question
//     events = alertItems
//                .filter(r => r.kind === "need_human")
//                .filter(r => !dismissedIds.has(r.id))
//                .filter(r => !isAcked(r, windowsByTarget))     // ack model
//     sort  = [...live, ...events.sortBy(ts desc)]              // live first, events newest-first
//   `w` on event rows is the matched live window (by target) or null (dead pane → still shown w/ label from r.session/r.name)

export function isAcked(event, windowByTarget)
//   → max(w.focused_at, w.acted_at) > event.ts   (w looked up by event.target; missing w → not acked)

export function needsYouCount(needsYouRows)   // → rows.length  (drives the Needs-you badge)

// Pinned: resolve the pinned-pid list against live windows, drop dead ids.
export function resolvePinned(pinnedPids, windows)
//   → ordered array of live window objects in pinnedPids order; dead ids skipped silently

// Activity: the low-signal feed.
export function buildActivity(alertItems)
//   → alertItems.filter(r => r.kind === "done" || r.kind === "info" || r.kind === "milestone")
//     (already reverse-chron from the server sort; pass through)
```
Rationale: rung 1. These are stateless transforms over the `windows` signal value + the `alertItems` signal value + a `dismissedIds` Set. Keeping them pure is what makes the spec's "one frontend function worth a test" actually unit-testable without a browser or mocked signals — the Q1-2026-incident lesson applied to the frontend: the merge/sort/ack logic must be reachable with plain inputs, not only through a rendered component.

No typed-config object here (this is JS, not TS — the codebase is `.jsx`/`.js`, no TypeScript). Signatures stay positional but small (≤3 args); `dismissedIds` is a `Set` for O(1) membership, matching the `seenAlertKeys` Set already in the feed code.

### `alertFeed.js` — **non-component module + signal** (Phase 2)
The re-homed poll loop. See §1a. Lifts the entire poll/native-notify/badge body out of `Alerts.jsx` with **no behavior change**.

```js
export const alertItems = signal([]);          // read model — the feed (was Alerts.jsx's `items`)
export function startAlertFeed();              // idempotent; started once from Split.jsx useEffect
//   internal: poll() (verbatim — direct fetch, pollFailed edge-toast, keep-stale-on-error),
//             maybeNativeNotify(list), updateDockBadge(list), seenAlertKeys sentinel (=null first poll)
```
What moves verbatim from `Alerts.jsx`: `POLL_MS`, the `poll()` body, `pollFailed`, `maybeNativeNotify`, the `seenAlertKeys = null` sentinel and `alertKey`/bounded-Set maintenance, and `setBadgeCount` (dock badge). **Changes:** `alertKey()` is replaced by `r.id` now that the server stamps a stable uuid (the spec's whole point — `alertKey` was the collision-prone synthetic). `updateBadge`'s header-`getElementById` writes are **dropped** (badge is now a Rail prop, §1b); `setBadgeCount(count)` (dock) stays, fed by `needsYouCount` so the dock badge and the section badge agree. `revealPane` (split-selects or modal-falls-back) moves here too — it's called by native-notification clicks and by Activity/Needs-you row clicks. `trackHeaderHeight` does **not** move — `Header.jsx` already owns `--header-h` authoritatively (spec confirms), so it's deleted with `Alerts.jsx`.

`startAlertFeed()` is called from `Split.jsx`'s existing mount effect alongside `startPolling()` — both are double-start-guarded module-level loops. This is the natural owner: the feed is only needed where the split rail lives.

### `AttentionSections.jsx` — **three small function components, one file** (Phase 2)
Tightly-coupled, all read the same two signals + `attention.js`; they belong in one file (rung: functions; file = one concern = "the attention zone").

```js
export function AttentionSections()    // renders <NeedsYouSection/> <PinnedSection/> ... ordering note below
function NeedsYouSection()             // reads windows + alertItems + dismissedIds; buildNeedsYou; SectionHeader tone="alert"
function PinnedSection()               // reads windows + pinnedPids; resolvePinned
function ActivitySection()             // reads alertItems; buildActivity; default-collapsed
```
**One generic section-list component vs three?** Decided: **three concrete components, not one parameterized `<SectionList source={...}>`.** They share the *header* (already extracted as `SectionHeader`) but differ in row vocabulary (live-vs-event rows with a `×` dismiss; pinned panes with a hover origin label; activity log rows with relTime), empty-state copy, and click behavior. A generic row-source-parameterized component would need a render-prop or a row-kind switch that's longer than three honest components. This is the under-abstraction-is-fine call: shared contract is the header, and that's already shared. (If a 4th near-identical section ever appears, revisit — not now.)

**`dismissedIds`** is component-local transient state. Per the spec it resets on restart (no durable store). Hold it as a module-level `signal(new Set())` in `attention.js`'s consumer or in `AttentionSections.jsx` — put it in **`store.js`** as `dismissedAlertIds = signal(new Set())` (it's transient read-model state, which is exactly what `store.js` is for). The `×` handler adds the id and replaces the Set (new reference, so the signal fires).

**Mount point — inside `<Rail>`, not as a sibling in `<Split>`.** Decided: `<AttentionSections>` renders as the first children of `<aside id="rail">`, above the `PROJECTS` header, *inside* `Rail.jsx`'s returned `<aside>`. Reasons: (1) they're visually and semantically part of `#rail` (the spec's diagram is one `#rail` box with four stacked regions); (2) the `#rail` scroll container, width, and CSS variables (`--rail-indent`) apply to them; (3) mounting as a `<Split>` sibling would need a second scroll region and duplicate the `#rail` chrome. The flat-sibling CSS contract is preserved because the attention rows use **`attn-row`** classes, not `child-row` — they never enter the `child-row::before/::after` adjacency chain that the tree depends on. `<AttentionSections>` sits before the tree's first `RepoRow` as a plain run of sibling rows; the tree's first child-row is still adjacency-correct because attention rows aren't `child-row`s.

### `RailRows.jsx` edits — **hover-star on `PaneRow`** (Phase 2)
`PaneRow` gains a pin affordance: a `☆`/`★` button that appears on hover (CSS `:hover` reveal, like `.rail-close` already does) and toggles via a new `onTogglePin` prop threaded from `Rail.jsx` → `prefs.togglePin(w.pid)`. A pinned pane renders the filled `★` always (not only on hover). `statusDotClass` is reused as-is for the attention rows (exported already; the attention rows import it). No new row *kind* in the tree — the in-tree pane row just grows the star; the out-of-tree attention rows are new flat components living in `AttentionSections.jsx` (they're not tree rows, so they don't belong in `RailRows.jsx`).

### `prefs.js` edits — **pin list mutators** (Phase 2)
Pins are a UI pref: a list of periscope-ids (== `w.pid`). Reuse the `patchUI` persistence boundary verbatim — same pattern as `repo_order` et al.

```js
export function getPinnedPids()              // → [...(P().ui?.pinned_pids || [])]
export function setPinnedPids(list)          // → patchUI({ pinned_pids: list })
export function togglePin(pid)               // read, toggle membership, setPinnedPids
```
**Exact prefs shape:** `ui.pinned_pids: string[]` (ordered; pin order = insertion order, which doubles as the Pinned-section render order). One new `ui` field, consistent with the five existing rail fields.

**Dead-id pruning location:** *render-time, not write-time.* `resolvePinned(pinnedPids, windows)` (in `attention.js`) drops ids not in live `windows` silently — same posture as the tree's `if (!w) continue` (Rail.jsx:355) and `mergeLiveAndPrefs`'s live-filtering. Do **not** prune dead ids out of the *stored* pref on every poll: a pane that's transiently absent (between polls, or while a session restarts) would lose its pin permanently. Persisted pruning, if ever wanted, belongs in the throttled `syncRailPrefs` path — but the spec's "dropped from the section silently" is a *render* drop, so render-time resolution is the correct and minimal answer. The unit test ("dead-id pruning when a pinned pane is gone") tests `resolvePinned`, which is exactly the render-time filter.

### `channels.py` edit — **one line** (Phase 2)
In `_do_notify_tool`, add `"id": uuid.uuid4().hex` to the `entry` dict (uuid already imported, channels.py:30). That's the entire server change.

### `routes/alerts.py` edit — **surface the id** (Phase 2)
Add `"id": r.get("id") or ""` to the `items.append({...})` dict in the `_CHANNEL_ALERTS` loop (line ~43). Milestone events (the second loop) have no notify-uuid; give them a deterministic id from their existing fields — `f"milestone|{e['at']}|{target}"` — so Activity rows have stable keys too. (Milestones are Activity-only, never Needs-you, so a non-uuid id is fine there.)

---

## 5. Patterns

**Used:**
- **Shared presentational primitive** (`SectionHeader`) — extracted once in Phase 1, instanced 4× in Phase 2. The spec's central requirement.
- **Pure-transform module + signal read model** (`attention.js` + `store.js` signals) — mirrors `railTree.js` / `grid/poll.js`. Keeps the merge/sort/ack testable without a browser.
- **Module-owned poll loop + exported signal** (`alertFeed.js`) — mirrors `grid/poll.js` → `windows`. Replaces the component-owned timer in `Alerts.jsx`.
- **Persistence boundary reuse** (`patchUI` for pins; `getRailCollapsed`/`setRailCollapsedKey` for section collapse) — no new pref endpoint, no new field family.
- **Hover-reveal affordance via CSS** (the pin star) — same mechanism as `.rail-close`.

**Considered and rejected:**
- **Generic `<SectionList source={}>` for the three sections** — rejected (§4 AttentionSections): three honest components are shorter than one parameterized switch; shared part is the header, already shared.
- **A `PinStore` / `AttentionController` class** — rejected: no coupled mutable state. Pins are a pref list; the merge is a pure function; dismissed-ids is one transient signal. Rung 1 throughout.
- **Re-homing the poll into `Rail.jsx`** — rejected (§1a): badge/native-notify must run independent of render lifecycle.
- **A durable per-pid ack store** (`set_window_fields_bulk`) — rejected by the spec itself; client-side `dismissedIds` Set + the computed ack rule are sufficient because the feed is in-memory.
- **A custom alert-id type / server ack endpoint** — rejected: `uuid4().hex` string is enough; ack is computed client-side from stamps already in the payload. No new server plumbing.
- **`getElementById` badge bridge** — rejected (§1b): badge is now a Rail-rendered prop.

---

## 6. Test strategy

| Module | Test kind | Dependencies | Notes |
|---|---|---|---|
| `attention.js` | **Unit** (`__tests__/attention.test.js`, vitest) | None — plain inputs | The core. Cases: live-first-then-events-newest ordering; `isAcked` boundary (`max(focused,acted) > ts` exactly); dismissed-id filter; `reason` = dialog vs asked from the two flags; `resolvePinned` order + **dead-id drop**; `buildActivity` kind filter. Mirrors `filesTouched.test.js` shape. |
| `prefs.js` pins | **Unit** (extend prefs tests or attention test) | Mock `patchUI`/fetch like existing prefs tests | pin/unpin round-trip through `pinned_pids`; `togglePin` idempotency. Dead-id *pruning* is tested via `resolvePinned` (render-time), not prefs. |
| `channels.py` id | **Unit** (`tests/test_channels.py`) | Real `_do_notify_tool` (no mocks) | Extend existing `test_notify_tool_*`: assert `"id" in entry`, ids unique across two notifies. Matches the spec's testing-strategy bullet. |
| `routes/alerts.py` id | **Integration-ish** (route test) | Real `_CHANNEL_ALERTS` + `list_windows` (mocked windows as existing route tests do) | Assert `/api/alerts/recent` rows carry `id`. Create `tests/routes/test_alerts.py` if absent. |
| `SectionHeader.jsx` | **Browser** | — | Presentational; eyeballed per project convention. |
| `AttentionSections.jsx` | **Browser** | — | Rendering, collapse, `×` dismiss, hover-star, empty states — all eyeballed (UI convention). The *logic* under them is already unit-tested in `attention.js`. |
| `alertFeed.js` | **Browser** + leans on the verbatim move | — | No new logic to unit-test (it's a verbatim lift); native-notify/dock-badge are Tauri-only and eyeballed in the shell, as today. |
| Phase 1 restyle | **Browser** | — | Pure visual; eyeballed. |

**Testability flags:** none. The structure deliberately routes every piece of non-trivial logic through `attention.js` (pure) so nothing important is reachable only through a rendered component — the explicit guard against the mocked-test-passes-prod-fails failure mode, applied to the frontend.

---

## 7. Decisions to sanity-check

1. **`dismissedAlertIds` lives in `store.js` (transient signal), not in `AttentionSections.jsx` local state.** Alternative: component-local `useState`. Close because it's only read in one place today. Chose `store.js` because it's transient read-model state (store.js's exact remit) and because `alertFeed.js`/`revealPane` may want to clear a dismissal when a pane re-fires — a shared signal makes that reachable without prop-drilling. Low cost to move if you disagree.

2. **Section-collapse keys (`sec:*`) share the `rail_collapsed` map** rather than a new `section_collapsed` field. Alternative: a dedicated field. Close because mixing `sec:*` with `repo:*`/`wt:*` in one map is slightly less self-documenting. Chose reuse: zero new prefs surface, distinct prefix, and the Activity inverted-default is handled at one read site.

3. **Pin pruning is render-time only (`resolvePinned`), never persisted-pruned on poll.** Alternative: prune the stored `pinned_pids` in `syncRailPrefs`. Close because the tree *does* persist-prune dead entries there. Chose render-time because a transiently-absent pane (mid-poll, session restart) must not lose its pin permanently — and the spec's "dropped from the section silently" is a display rule, not a storage rule. Flagging because it's a deliberate divergence from the tree's persist-prune behavior.

4. **Three concrete section components, not one generic parameterized one.** Alternative: `<SectionList>` with a row-source prop. Close enough that it's worth a glance — but the row vocabularies genuinely differ (dismiss `×`, origin label, relTime) and the shared part (header) is already a primitive. Listed so the under-abstraction call is visible, not missed.

---

## Build-order note (for the plan-writer)

- **Phase 1 commits:** `SectionHeader.jsx` + CSS; `Rail.jsx` PROJECTS header; restyle pass on `RailRows.jsx`/CSS. No new behavior, no server change, no new tests — eyeballed. Ships independently.
- **Phase 2 commits (sequenceable):** (a) server `id` (channels.py + alerts.py + tests) — independent, can land first; (b) `alertFeed.js` + delete `Alerts.jsx` + `Header.jsx`/`Overlays.jsx` removals — moves the feed with no UI change yet (Activity not shown until c); (c) `attention.js` + `attention.test.js`; (d) `prefs.js` pins; (e) `AttentionSections.jsx` + `RailRows.jsx` star + `Rail.jsx` mount + CSS. (e) depends on b/c/d.

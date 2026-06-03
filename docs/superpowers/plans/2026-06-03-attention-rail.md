# Attention Rail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a left-rail "attention zone" to periscope's split view — three stacked sections (Needs you / Pinned / Activity) above the project tree — preceded by a polish pass that establishes a shared section-header primitive.

**Architecture:** Phase 1 restyles the existing rail and extracts a presentational `<SectionHeader>`. Phase 2 adds the three sections, backed by a pure `attention.js` module (the only unit-tested frontend logic), a non-component `alertFeed.js` poll loop (re-homed from the deleted `Alerts.jsx`), pins persisted as `ui.pinned_pids`, and a one-line server change adding a stable `id` to alert records.

**Tech Stack:** Preact + @preact/signals (built by Vite to `static/dist/app.js`), vitest for frontend logic, FastAPI + pytest backend.

**Spec:** `docs/superpowers/specs/2026-06-03-attention-rail-design.md`
**Structure:** `docs/superpowers/specs/2026-06-03-attention-rail-structure.md`

---

## Conventions for this plan

- **UI is eyeballed, not unit-tested** (project convention, periscope CLAUDE.md). Component/CSS tasks end in a build + browser-verify step, not a vitest assertion. Only pure logic (`attention.js`, pins in `prefs.js`) and the server change get tests.
- **Dev server:** run periscope dev on port 8766 in a worktree; verify at `http://localhost:8766/`. `npm run build` regenerates the committed `static/dist/app.js` (required — prod serves the committed bundle).
- **Frontend test command:** `npm test` (vitest run) or `npx vitest run <path>` for one file.
- **Commit straight to the working branch after each task.** Single-line messages.

---

# PHASE 1 — Foundation / polish

No new behavior, no server change. Establishes the section-header primitive and the restyle. Ships independently.

### Task 1: `SectionHeader` primitive + `PROJECTS` header

**Files:**
- Create: `static/src/split/SectionHeader.jsx`
- Modify: `static/src/split/Rail.jsx` (replace the `.rail-head` block with a `SectionHeader` instance)
- Modify: `static/styles.css` (add `.section-header` rules)

- [ ] **Step 1: Write `SectionHeader.jsx`**

```jsx
// Shared section-header primitive for the left rail: a collapse chevron, an
// uppercase label, and an optional right-aligned count badge. Presentational
// only — collapse state and count are owned by the caller (prefs / computed),
// never here. Instanced 4× in Phase 2 (NEEDS YOU / PINNED / PROJECTS / ACTIVITY).
export function SectionHeader({ icon, label, count, collapsed, onToggle, tone }) {
  const toneCls = tone === "alert" ? " section-header-alert" : "";
  return (
    <div
      class={`rail-row section-header${toneCls}`}
      onClick={onToggle}
    >
      <span class="rail-chev">{collapsed ? "▸" : "▾"}</span>
      {icon ? <span class="section-header-icon">{icon}</span> : null}
      <span class="section-header-label">{label}</span>
      {count != null && count > 0
        ? <span class="section-header-count">{count}</span>
        : null}
    </div>
  );
}
```

- [ ] **Step 2: Replace the `PROJECTS` header in `Rail.jsx`**

In `Rail.jsx`, add the import:

```jsx
import { SectionHeader } from "./SectionHeader.jsx";
```

The tree currently renders `<div class="rail-head"><span>Projects</span></div>` (both the empty-state branch and the main branch). Replace the main-branch one (the `<aside>` return) so the tree gains a collapsible root header. Add a collapse read at the top of the component body (near the other `prefs` reads):

```jsx
const projectsCollapsed = collapsed[`sec:projects`] === true;
```

Replace `<div class="rail-head"><span>Projects</span></div>` with:

```jsx
<SectionHeader
  label="PROJECTS"
  count={null}
  collapsed={projectsCollapsed}
  onToggle={() => toggleCollapse("sec:projects")}
/>
```

Wrap the `{repoOrder.map(...)}` block so it renders only when not collapsed:

```jsx
{!projectsCollapsed && repoOrder.map((repoKey) => { /* unchanged */ })}
```

Leave the empty-state branch's `.rail-head` as-is (no collapse needed there).

- [ ] **Step 3: Add CSS for `.section-header`**

In `static/styles.css`, near the existing `.rail-head` rules, add:

```css
.section-header {
  cursor: pointer;
  text-transform: uppercase;
  letter-spacing: 0.07em;
  font-size: 10px;
  color: var(--muted, #7b818c);
  user-select: none;
}
.section-header .section-header-label { font-weight: 600; }
.section-header .section-header-icon { margin-right: 4px; }
.section-header .section-header-count {
  margin-left: auto;
  border-radius: 9px;
  padding: 0 6px;
  font-size: 10px;
  font-weight: 700;
  background: var(--chip-bg, #2a2e37);
  color: var(--chip-fg, #9aa0ab);
}
.section-header-alert { color: #f0857d; }
.section-header-alert .section-header-count { background: #f85149; color: #fff; }
```

- [ ] **Step 4: Build and verify in browser**

Run: `npm run build`
Then at `http://localhost:8766/` (split view): the tree now has a collapsible `PROJECTS` header; clicking the chevron collapses/expands the whole tree, and the collapse state persists across reload (it's a `sec:projects` key in `rail_collapsed`).
Expected: header renders uppercase + chevron; collapse works; no console errors; all existing tree affordances (drag, rename, PR/CI chips, review rows, New tab) still present and unchanged.

- [ ] **Step 5: Commit**

```bash
git add static/src/split/SectionHeader.jsx static/src/split/Rail.jsx static/styles.css static/dist/app.js
git commit -m "rail: extract SectionHeader primitive + collapsible PROJECTS header (phase 1)"
```

---

### Task 2: Restyle pass on the existing rows

**Files:**
- Modify: `static/styles.css` (rail spacing/typography toward the mockup)
- Modify: `static/src/split/RailRows.jsx` (only if a class hook is missing; prefer CSS-only)

This task is purely visual and gets refined live. The goal is the mockup's breathing room and hierarchy **without dropping any functional chip** (PR/CI, Linear, git, review rows, New tab, status palette, connector lines).

- [ ] **Step 1: Apply spacing/typography CSS**

In `static/styles.css`, adjust the rail row rules toward more vertical breathing room and clearer indent hierarchy. Concrete starting point (tune live):

```css
#rail .rail-row { padding-top: 3px; padding-bottom: 3px; }
#rail .repo-row { margin-top: 6px; }
#rail .wt-row { color: #aeb4bf; }
#rail .child-row { padding-left: 34px; }
```

Keep the connector pseudo-elements (`.child-row::before/::after`) — per spec, connector lines stay, lightened. Lighten them:

```css
#rail .child-row::before,
#rail .child-row::after { opacity: 0.6; }
```

- [ ] **Step 2: Build and verify in browser**

Run: `npm run build`
At `http://localhost:8766/`: rows have more breathing room; hierarchy reads clearly; **every** chip still present — diff a real `fdy` session with a PR (`#NNNN ✓`), a Linear chip, a `review` row, and a `+ New tab` row to confirm none regressed.
Expected: cleaner look, zero functional regressions. Tune values until it matches the mockup feel.

- [ ] **Step 3: Commit**

```bash
git add static/styles.css static/src/split/RailRows.jsx static/dist/app.js
git commit -m "rail: restyle spacing/typography toward attention-rail mockup (phase 1)"
```

---

# PHASE 2 — Attention sections

### Task 3: Stable `id` on alert records (server, TDD)

**Files:**
- Modify: `periscope/channels.py` (`_do_notify_tool` — add `id`)
- Modify: `periscope/routes/alerts.py` (surface `id`, deterministic id for milestone rows)
- Modify: `tests/test_channels.py` (assert unique id)
- Create: `tests/routes/test_alerts.py` (assert `/api/alerts/recent` surfaces `id`)

- [ ] **Step 1: Write the failing channels test**

Add to `tests/test_channels.py`:

```python
def test_notify_tool_stamps_unique_id():
    # Real signature: _do_notify_tool(pane: str, arguments: dict).
    # Both alerts append under the same pane key in _CHANNEL_ALERTS (a dict
    # pane→list), so read the list under that pane, not the dict itself.
    _do_notify_tool("%5", {"message": "first", "kind": "done"})
    _do_notify_tool("%5", {"message": "second", "kind": "done"})
    entries = _CHANNEL_ALERTS["%5"]
    ids = [a["id"] for a in entries]
    assert all(isinstance(i, str) and i for i in ids)
    assert len(set(ids)) == len(ids)  # unique
```

(The `reset_channel_state` autouse fixture already in `tests/test_channels.py` clears `_CHANNEL_ALERTS` between tests.)

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_channels.py::test_notify_tool_stamps_unique_id -v`
Expected: FAIL with `KeyError: 'id'`.

- [ ] **Step 3: Add the id in `channels.py`**

In `_do_notify_tool`, where the alert `entry` dict is built (currently `{message, kind, severity, ts}`), add the id. `uuid` is already imported (channels.py:30):

```python
entry = {
    "id": uuid.uuid4().hex,
    "message": message,
    "kind": kind,
    # ...existing keys unchanged...
}
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/test_channels.py::test_notify_tool_stamps_unique_id -v`
Expected: PASS.

- [ ] **Step 5: Write the failing route test**

Create `tests/routes/test_alerts.py`. Two real constraints (both caught in review):
- `/api/alerts/recent` only emits rows for panes present in live tmux (`alerts.py` does `w = by_pane.get(pane_id); if not w: continue`). So the test MUST mock `list_windows` to return a pane whose `pane_id` matches the one passed to `_do_notify_tool`.
- `tests/routes/conftest.py` provides `client` but does NOT reset channel state — clear `_CHANNEL_ALERTS` in the test (or add a local fixture).

```python
from periscope.channels import _do_notify_tool, _CHANNEL_ALERTS, _CHANNELS_LOCK


def test_alerts_recent_surfaces_id(client, mocker):
    with _CHANNELS_LOCK:
        _CHANNEL_ALERTS.clear()
    mocker.patch(
        "periscope.routes.alerts.list_windows",
        return_value=[{
            "pane_id": "%5", "session": "tc/x", "index": "0",
            "name": "win", "cwd": "/tmp",
        }],
    )
    _do_notify_tool("%5", {"message": "hello", "kind": "need_human"})
    res = client.get("/api/alerts/recent?limit=10")
    assert res.status_code == 200
    items = res.json()["items"]
    assert items and all("id" in it for it in items)
```

(Confirm the exact mock target — `periscope.routes.alerts.list_windows` — and the window-dict keys the route reads, against the real `alerts.py` and an existing `tests/routes/test_*.py` that mocks `list_windows`, e.g. `test_pane.py`/`test_send.py`.)

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run pytest tests/routes/test_alerts.py -v`
Expected: FAIL (`id` not in row).

- [ ] **Step 7: Surface `id` in `routes/alerts.py`**

In the `_CHANNEL_ALERTS` loop where each row dict is appended, add `"id": r.get("id") or ""`. In the milestone loop (events without a notify-uuid), give a deterministic id from existing fields, e.g. `"id": f"milestone|{e['at']}|{target}"`.

- [ ] **Step 8: Run both tests to verify they pass**

Run: `uv run pytest tests/test_channels.py::test_notify_tool_stamps_unique_id tests/routes/test_alerts.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add periscope/channels.py periscope/routes/alerts.py tests/test_channels.py tests/routes/test_alerts.py
git commit -m "channels: stamp stable uuid id on alert records, surface via /api/alerts/recent"
```

---

### Task 4: `attention.js` pure module (TDD)

**Files:**
- Create: `static/src/split/attention.js`
- Create: `static/src/split/__tests__/attention.test.js`

- [ ] **Step 1: Write the failing tests**

Create `static/src/split/__tests__/attention.test.js`:

```js
import { describe, it, expect } from "vitest";
import {
  buildNeedsYou, isAcked, needsYouCount, resolvePinned, buildActivity,
} from "../attention.js";

const win = (over = {}) => ({
  pid: "p1", target: "tc/x:0", state: "idle",
  needs_input: false, asked_question: false,
  focused_at: 0, acted_at: 0, ...over,
});
const evt = (over = {}) => ({
  id: "a1", kind: "need_human", target: "tc/x:0", ts: 100,
  session: "tc/x", name: "win", message: "help", ...over,
});

describe("buildNeedsYou", () => {
  it("includes live needs-input panes, carrying the window for label/waiting_for", () => {
    const live = win({ state: "needs-input", waiting_for: "approve askuserquestion" });
    const rows = buildNeedsYou([live], [], new Set());
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe("live");
    expect(rows[0].w.waiting_for).toBe("approve askuserquestion");
  });

  it("excludes panes not in needs-input state", () => {
    const idle = win({ state: "idle" });
    expect(buildNeedsYou([idle], [], new Set())).toHaveLength(0);
  });

  it("includes unacked need_human events after live rows", () => {
    const live = win({ pid: "p1", target: "a:0", state: "needs-input", needs_input: true });
    const rows = buildNeedsYou([live], [evt({ target: "b:0" })], new Set());
    expect(rows.map((r) => r.kind)).toEqual(["live", "event"]);
  });

  it("drops dismissed event ids", () => {
    const rows = buildNeedsYou([], [evt({ id: "a1" })], new Set(["a1"]));
    expect(rows).toHaveLength(0);
  });

  it("drops acked events (max(focused,acted) > ts)", () => {
    const w = win({ target: "tc/x:0", focused_at: 200 });
    const rows = buildNeedsYou([w], [evt({ ts: 100, target: "tc/x:0" })], new Set());
    expect(rows).toHaveLength(0);
  });

  it("keeps events when stamps are at or below ts (boundary)", () => {
    const w = win({ target: "tc/x:0", focused_at: 100, acted_at: 100 });
    const rows = buildNeedsYou([w], [evt({ ts: 100, target: "tc/x:0" })], new Set());
    expect(rows).toHaveLength(1); // 100 > 100 is false → not acked
  });

  it("sorts events newest-first", () => {
    const rows = buildNeedsYou([], [evt({ id: "old", ts: 50 }), evt({ id: "new", ts: 90 })], new Set());
    expect(rows.map((r) => r.id)).toEqual(["new", "old"]);
  });

  it("non-need_human alerts never enter the zone", () => {
    expect(buildNeedsYou([], [evt({ kind: "done" })], new Set())).toHaveLength(0);
  });
});

describe("resolvePinned", () => {
  it("returns live windows in pin order, drops dead ids", () => {
    const a = win({ pid: "a" }), b = win({ pid: "b" });
    const out = resolvePinned(["b", "gone", "a"], [a, b]);
    expect(out.map((w) => w.pid)).toEqual(["b", "a"]);
  });
});

describe("buildActivity", () => {
  it("keeps done/info/milestone, drops need_human", () => {
    const items = [evt({ kind: "done" }), evt({ kind: "need_human" }), evt({ kind: "info" })];
    expect(buildActivity(items).map((r) => r.kind)).toEqual(["done", "info"]);
  });
});

describe("needsYouCount", () => {
  it("counts rows", () => {
    expect(needsYouCount([{ kind: "live" }, { kind: "event" }])).toBe(2);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run static/src/split/__tests__/attention.test.js`
Expected: FAIL ("Failed to resolve import ../attention.js").

- [ ] **Step 3: Implement `attention.js`**

```js
// Pure transforms for the left-rail attention zone — no signals, no DOM.
// Mirrors railTree.js's posture (consumed by render, testable in isolation).
// This is the one unit-tested frontend module; keep it pure.

// Union of live needs-input panes + unacked need_human events, ordered
// live-first then events newest-first.
export function buildNeedsYou(windows, alertItems, dismissedIds) {
  const byTarget = indexByTarget(windows);
  const live = (windows || [])
    .filter((w) => w.state === "needs-input")
    .map((w) => ({ kind: "live", pid: w.pid, w }));
  // No reason field here: the human label is rendered in the component via
  // waitLabel(w.waiting_for). window_view.py forces asked_question=False for
  // mapped live sessions, so a flag-derived label would be dead in prod;
  // waiting_for carries the real distinction (incl. AskUserQuestion).
  const events = (alertItems || [])
    .filter((r) => r.kind === "need_human")
    .filter((r) => !dismissedIds.has(r.id))
    .filter((r) => !isAcked(r, byTarget))
    .sort((a, b) => b.ts - a.ts)
    .map((r) => ({
      kind: "event",
      id: r.id,
      target: r.target,
      w: byTarget[r.target] || null,
      message: r.message,
      ts: r.ts,
      session: r.session,
      name: r.name,
    }));
  return [...live, ...events];
}

// An event is acked once the user has engaged the pane after it fired:
// max(focused_at, acted_at) > event.ts. Missing window → not acked.
// Note: the payload's `acted_at` already folds in the persisted modal-open
// "acked_at" stamp (window_view.py), so opening the modal also acks — this is
// intentionally more generous than the spec's literal rule, matching the
// "however you got there" goal.
export function isAcked(event, windowByTarget) {
  const w = windowByTarget[event.target];
  if (!w) return false;
  return Math.max(w.focused_at || 0, w.acted_at || 0) > event.ts;
}

export function needsYouCount(needsYouRows) {
  return needsYouRows.length;
}

// Resolve the pinned-pid list against live windows, in pin order; dead ids
// dropped silently (render-time pruning — never persist-prune, or a
// transiently-absent pane loses its pin).
export function resolvePinned(pinnedPids, windows) {
  const byPid = {};
  for (const w of windows || []) byPid[w.pid] = w;
  return (pinnedPids || []).map((pid) => byPid[pid]).filter(Boolean);
}

// The low-signal Activity feed: everything that isn't need_human.
export function buildActivity(alertItems) {
  return (alertItems || []).filter(
    (r) => r.kind === "done" || r.kind === "info" || r.kind === "milestone"
  );
}

function indexByTarget(windows) {
  const m = {};
  for (const w of windows || []) m[w.target] = w;
  return m;
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `npx vitest run static/src/split/__tests__/attention.test.js`
Expected: PASS (all cases).

- [ ] **Step 5: Commit**

```bash
git add static/src/split/attention.js static/src/split/__tests__/attention.test.js
git commit -m "rail: attention.js pure module (needs-you merge/ack, pin resolve, activity filter) + tests"
```

---

### Task 5: Pin mutators in `prefs.js`

**Files:**
- Modify: `static/src/prefs.js` (add `getPinnedPids` / `setPinnedPids` / `togglePin`)

No unit test here (deliberate). `patchUI` reverts its optimistic write on any network failure, so a no-network unit test of `togglePin` would assert the post-revert state and fail; mocking the round-trip would only re-test existing `patchUI` plumbing. The real pin invariants — render order and dead-id pruning — are covered by `resolvePinned`'s unit tests (Task 4). `togglePin` is a thin `patchUI` wrapper, verified end-to-end in the browser (Task 7 Step 5). This matches the project convention: don't unit-test trivial wrappers over already-tested infrastructure.

- [ ] **Step 1: Add mutators to `prefs.js`**

Near the other `ui` getters/mutators (e.g. by `getRailCollapsed`/`setRailCollapsedKey`):

```js
export function getPinnedPids() {
  return [...(P().ui?.pinned_pids || [])];
}
export function setPinnedPids(list) {
  return patchUI({ pinned_pids: list });
}
export function togglePin(pid) {
  const cur = getPinnedPids();
  const next = cur.includes(pid) ? cur.filter((p) => p !== pid) : [...cur, pid];
  return setPinnedPids(next);
}
```

- [ ] **Step 2: Sanity build**

Run: `npm run build`
Expected: builds clean (no syntax error). Behavior is exercised in Task 7.

- [ ] **Step 3: Commit**

```bash
git add static/src/prefs.js
git commit -m "prefs: pinned_pids list + getPinnedPids/setPinnedPids/togglePin"
```

---

### Task 6: `alertFeed.js` — re-home the poll loop; delete `Alerts.jsx`

**Files:**
- Create: `static/src/split/alertFeed.js`
- Modify: `static/src/store.js` (add `dismissedAlertIds` signal)
- Modify: `static/src/split/Split.jsx` (call `startAlertFeed()` in the mount effect)
- Modify: `static/src/chrome/Header.jsx` (delete `#alerts-toggle` / `#alerts-badge` markup)
- Modify: `static/src/overlays/Overlays.jsx` (drop the `<Alerts />` mount)
- Delete: `static/src/overlays/Alerts.jsx`

This is a behavior-preserving move: the poll body, native-notify, dock-badge, and dedupe sentinel move verbatim. Only two changes: `alertKey` → `r.id`, and the header-`getElementById` badge writes are dropped (the badge becomes a Rail prop in Task 7).

> **Ordering gate:** between this task's commit and Task 7's, `need_human`/`done`/`info` alerts have **no rendered surface** (right rail deleted, new sections not yet added) — only the Tauri dock badge reflects them. This is an acceptable *intermediate* state on the worktree branch. **Do not merge to `main` or restart prod between Task 6 and Task 7** — the phase must land whole.

- [ ] **Step 1: Add the transient signals to `store.js`**

```js
// Dismissed need_human alert ids (transient — resets on restart, the feed is
// in-memory anyway). The Needs-you section filters these out.
export const dismissedAlertIds = signal(new Set());
```

- [ ] **Step 2: Write `alertFeed.js`**

Lift the poll machinery out of `Alerts.jsx` verbatim. Full module:

```js
// The cross-pane alert feed: owns the /api/alerts/recent poll loop and the
// native-notify/dock-badge side effects, exposing `alertItems` as the read
// model. Non-component (mirrors grid/poll.js → windows) so the badge stays
// fresh and native-notify fires regardless of what's rendered. Started once
// from Split.jsx. Lifted from the former overlays/Alerts.jsx.
import { signal } from "@preact/signals";
import { showToast } from "../overlays/Toast.jsx";
import { setBadgeCount, notify, onNotificationClick, inTauri } from "../tauri.js";
import { view, windows, railSelection } from "../store.js";
import * as prefs from "../prefs.js";
import { openModal } from "../modal/Modal.jsx";

const POLL_MS = 3000;

export const alertItems = signal([]);

let pollFailed = false;
let seenAlertKeys = null;       // first-poll sentinel — see maybeNativeNotify
let started = false;

// Reveal a pane from an alert/native-notification click. Split → inline select;
// else modal fallback. (Moved verbatim from Alerts.jsx.)
export function revealPane(target) {
  if (!target) return;
  if (view.value === "split") {
    const w = (windows.value || []).find((x) => x.target === target);
    if (w?.pid) {
      railSelection.value = `pane:${w.pid}`;
      prefs.setLastSelected({ kind: "pane", pid: w.pid });
      return;
    }
  }
  openModal(target);
}

function maybeNativeNotify(list) {
  if (!inTauri()) return;
  const needHuman = list.filter((r) => r.kind === "need_human");
  if (seenAlertKeys === null) {
    seenAlertKeys = new Set(needHuman.map((r) => r.id));
    return;
  }
  for (const r of needHuman) {
    if (seenAlertKeys.has(r.id)) continue;
    seenAlertKeys.add(r.id);
    const paneLabel = `${r.session} · ${r.name || `:${r.index}`}`;
    notify({ title: `⚠ ${paneLabel}`, body: r.message || "", target: r.target });
  }
  const current = new Set(needHuman.map((r) => r.id));
  for (const k of seenAlertKeys) if (!current.has(k)) seenAlertKeys.delete(k);
}

async function poll() {
  try {
    const res = await fetch("/api/alerts/recent?limit=100");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (pollFailed) { pollFailed = false; showToast("notifications feed reconnected", "good"); }
    const list = data.items || [];
    maybeNativeNotify(list);
    setBadgeCount(list.filter((r) => r.kind === "need_human").length);
    alertItems.value = list;
  } catch (e) {
    if (!pollFailed) { pollFailed = true; showToast(`notifications feed unavailable: ${e.message}`, "bad"); }
  }
}

export function startAlertFeed() {
  if (started) return;
  started = true;
  onNotificationClick((target) => revealPane(target));
  poll();
  setInterval(poll, POLL_MS);
}
```

Note: `setBadgeCount` is fed by the raw need_human count here (dock badge). The in-app Needs-you section badge is computed separately in Task 7 from `needsYouCount` (which also subtracts acked/dismissed) — the two can differ slightly and that's fine; the dock badge is a coarse "something wants you" signal.

- [ ] **Step 3: Call `startAlertFeed()` from `Split.jsx`**

In `Split.jsx`'s mount `useEffect` (where `startPolling()` is called), add:

```jsx
import { startAlertFeed } from "./alertFeed.js";
// ...inside the effect, alongside startPolling():
startAlertFeed();
```

- [ ] **Step 4: Remove the header alerts button + the `<Alerts/>` mount**

In `Header.jsx`, delete the `#alerts-toggle` button and `#alerts-badge` span markup (the toggle slot). In `Overlays.jsx`, remove the `<Alerts />` element and its import. Delete `static/src/overlays/Alerts.jsx`.

- [ ] **Step 5: Build and verify in browser**

Run: `npm run build`
At `http://localhost:8766/`: no console errors; the right alerts-rail is gone; the old header alerts toggle/badge is gone; native notifications still fire in the Tauri shell (dock badge updates when a `need_human` arrives). Trigger a `notify(kind="need_human")` from a pane to confirm the feed still polls (you'll wire its visible surface in Task 7 — for now just confirm no errors and `alertItems` populates, observable via the dock badge / a temporary `console.log`).

- [ ] **Step 6: Commit**

```bash
git add static/src/split/alertFeed.js static/src/store.js static/src/split/Split.jsx static/src/chrome/Header.jsx static/src/overlays/Overlays.jsx static/dist/app.js
git rm static/src/overlays/Alerts.jsx
git commit -m "rail: re-home alert poll loop into alertFeed.js module; remove right alerts-rail + Alerts.jsx"
```

---

### Task 7: `AttentionSections.jsx` + pin star + mount + CSS

**Files:**
- Create: `static/src/split/AttentionSections.jsx`
- Modify: `static/src/split/RailRows.jsx` (hover-star on `PaneRow`)
- Modify: `static/src/split/Rail.jsx` (mount `<AttentionSections/>` above the tree; thread `onTogglePin`)
- Modify: `static/styles.css` (`.attn-*` rows + hover-star)

- [ ] **Step 1: Write `AttentionSections.jsx`**

```jsx
// The left-rail attention zone: NEEDS YOU + PINNED + ACTIVITY, stacked above
// the project tree, all rendered inside <Rail>'s <aside>. Reads the windows +
// alertItems signals through the pure transforms in attention.js. Out-of-tree
// rows use `attn-row` classes (NOT child-row) so they never enter the tree's
// connector-adjacency CSS.
import { windows, dismissedAlertIds, railSelection } from "../store.js";
import { alertItems, revealPane } from "./alertFeed.js";
import * as prefs from "../prefs.js";
import { relTime, waitLabel } from "../util.js";
import { SectionHeader } from "./SectionHeader.jsx";
import { buildNeedsYou, needsYouCount, resolvePinned, buildActivity } from "./attention.js";
import { statusDotClass } from "./RailRows.jsx";

function paneLabel(w) {
  return w?.name || (w?.is_claude ? "claude" : "shell");
}
function originLabel(w, fallbackSession, fallbackName) {
  const session = w?.session || fallbackSession || "";
  const name = paneLabel(w) || fallbackName || "";
  return `${session} · ${name}`;
}

// Mirror the rail's own selection write (string highlight signal + object pref).
// Static import of railSelection is safe — store.js imports nothing from here.
function selectPane(w) {
  if (!w?.pid) return;
  railSelection.value = `pane:${w.pid}`;
  prefs.setLastSelected({ kind: "pane", pid: w.pid });
}

export function AttentionSections() {
  const live = windows.value || [];
  const items = alertItems.value || [];
  const dismissed = dismissedAlertIds.value;
  const collapsed = prefs.getRailCollapsed();

  // NEEDS YOU
  const needsRows = buildNeedsYou(live, items, dismissed);
  const needsCollapsed = collapsed["sec:needs"] === true;

  // PINNED
  const pinned = resolvePinned(prefs.getPinnedPids(), live);
  const pinnedCollapsed = collapsed["sec:pinned"] === true;

  // ACTIVITY (default collapsed: absent → collapsed)
  const activity = buildActivity(items);
  const activityCollapsed = collapsed["sec:activity"] !== false;

  // Toggle takes the section's already-computed current state so the inverted
  // Activity default lives at exactly one place (the const above), not here.
  function toggle(key, currentlyCollapsed) {
    prefs.setRailCollapsedKey(key, !currentlyCollapsed);
  }
  function dismiss(id) {
    const next = new Set(dismissed);
    next.add(id);
    dismissedAlertIds.value = next;
  }

  return (
    <>
      {needsRows.length > 0 && (
        <>
          <SectionHeader
            icon="⚠" label="NEEDS YOU" tone="alert"
            count={needsYouCount(needsRows)}
            collapsed={needsCollapsed}
            onToggle={() => toggle("sec:needs", needsCollapsed)}
          />
          {!needsCollapsed && needsRows.map((r) =>
            r.kind === "live" ? (
              <div key={`live:${r.pid}`} class="rail-row attn-row attn-needs"
                   onClick={() => selectPane(r.w)}>
                <span class="attn-dot dot dot-alert dot-pulse"></span>
                <span class="attn-label">{originLabel(r.w)}</span>
                <span class="attn-reason">{waitLabel(r.w?.waiting_for)}</span>
              </div>
            ) : (
              <div key={`evt:${r.id}`} class="rail-row attn-row attn-needs attn-event"
                   onClick={() => revealPane(r.target)}>
                <span class="attn-ico">⚠</span>
                <span class="attn-label">{originLabel(r.w, r.session, r.name)}</span>
                <span class="attn-reason">need_human · {relTime(r.ts)}</span>
                <button class="attn-x" title="dismiss"
                        onClick={(e) => { e.stopPropagation(); dismiss(r.id); }}>×</button>
              </div>
            )
          )}
        </>
      )}

      {pinned.length > 0 && (
        <>
          <SectionHeader
            icon="★" label="PINNED" count={pinned.length}
            collapsed={pinnedCollapsed}
            onToggle={() => toggle("sec:pinned", pinnedCollapsed)}
          />
          {!pinnedCollapsed && pinned.map((w) => (
            <div key={`pin:${w.pid}`} class="rail-row attn-row attn-pinned"
                 onClick={() => selectPane(w)}>
              <span class="attn-ico">{w.is_claude ? "✻" : "$"}</span>
              <span class="attn-label">{originLabel(w)}</span>
              <span class={statusDotClass(w.state)} style="margin-left:auto"></span>
            </div>
          ))}
        </>
      )}

      <SectionHeader
        label="ACTIVITY" count={activity.length || null}
        collapsed={activityCollapsed}
        onToggle={() => toggle("sec:activity", activityCollapsed)}
      />
      {!activityCollapsed && activity.map((r) => (
        <div key={`act:${r.id}`} class={`rail-row attn-row attn-activity attn-${r.kind}`}
             onClick={() => revealPane(r.target)}>
          <span class="attn-ico">{r.kind === "done" ? "✓" : r.kind === "milestone" ? "★" : "•"}</span>
          <span class="attn-label">{originLabel(null, r.session, r.name)}</span>
          <span class="attn-reason">{relTime(r.ts)}</span>
        </div>
      ))}
    </>
  );
}
```

- [ ] **Step 2: Export `statusDotClass` from `RailRows.jsx` + add hover-star to `PaneRow`**

`statusDotClass` is currently module-private in `RailRows.jsx`. Add `export` to it. Then thread a pin affordance into `PaneRow`:

In `Rail.jsx`, where `<PaneRow ... />` is rendered, add:

```jsx
pinned={prefs.getPinnedPids().includes(w.pid)}
onTogglePin={() => prefs.togglePin(w.pid)}
```

In `RailRows.jsx`'s `PaneRow`, insert the star button **before the status-dot `<span>`** (so the order is icon · label · ★ · dot · ×, matching the mockup `✻ rail-redesign ★ ●`), not before `.rail-close`:

```jsx
<button
  class={`rail-pin${pinned ? " pinned" : ""}`}
  title={pinned ? "unpin" : "pin"}
  onClick={(e) => { e.stopPropagation(); onTogglePin(); }}
>{pinned ? "★" : "☆"}</button>
```

Add `pinned, onTogglePin` to `PaneRow`'s destructured props.

- [ ] **Step 3: Mount `<AttentionSections/>` in `Rail.jsx`**

Import it and render it as the first child of the `<aside id="rail">`, before the `PROJECTS` `SectionHeader`:

```jsx
import { AttentionSections } from "./AttentionSections.jsx";
// ...
<aside id="rail" aria-label="projects rail">
  <AttentionSections />
  <SectionHeader label="PROJECTS" ... />
  {/* tree */}
</aside>
```

- [ ] **Step 4: Add CSS**

```css
#rail .attn-row { display: flex; align-items: center; gap: 8px; padding: 3px 10px 3px 16px; cursor: pointer; }
#rail .attn-needs { background: #241316; }
#rail .attn-needs:hover { background: #2c181c; }
#rail .attn-pinned { background: #161a14; }
#rail .attn-label { color: #e6e9ef; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#rail .attn-reason { margin-left: auto; color: #8a6c6f; font-size: 11px; white-space: nowrap; }
#rail .attn-activity .attn-reason { color: #5b606b; }
#rail .attn-x { color: #6b5256; background: none; border: 0; cursor: pointer; }
#rail .attn-dot { width: 7px; height: 7px; border-radius: 50%; }

/* Pin star: hidden until row hover; always visible when pinned. */
#rail .child-row .rail-pin { opacity: 0; margin-left: auto; background: none; border: 0; cursor: pointer; color: #5b606b; }
#rail .child-row:hover .rail-pin { opacity: 1; }
#rail .child-row .rail-pin.pinned { opacity: 1; color: #d8b33a; }
```

(If `.rail-close` already uses `margin-left:auto`, the star and the close button + dot need their flex order arranged so the dot stays rightmost and the star sits left of it — adjust by removing `margin-left:auto` from `.rail-pin` and letting the existing layout flow, tuning live.)

- [ ] **Step 5: Build and verify in browser**

Run: `npm run build`
At `http://localhost:8766/`:
- A pane showing a permission dialog / AskUserQuestion appears in **NEEDS YOU** with reason `dialog`; answering it (in tmux or via periscope) removes it on the next poll.
- A `notify(kind="need_human")` appears as an event row with `× ` and a relative time; clicking the row reveals the pane and (after you've visited) it clears; `×` dismisses immediately.
- Hovering a tree pane-row shows `☆`; clicking pins it → it appears in **PINNED** and the in-tree star fills gold; clicking again unpins.
- `notify(kind="done")` / `info` land in **ACTIVITY** (collapsed by default; expand to see them).
- Section collapse states persist across reload.
Expected: all of the above; no console errors; tree unaffected.

- [ ] **Step 6: Commit**

```bash
git add static/src/split/AttentionSections.jsx static/src/split/RailRows.jsx static/src/split/Rail.jsx static/styles.css static/dist/app.js
git commit -m "rail: NEEDS YOU / PINNED / ACTIVITY attention sections + hover-star pinning (phase 2)"
```

---

## Final verification

- [ ] Run the full backend suite: `uv run pytest -q` — expect green (new alert-id tests included).
- [ ] Run the frontend suite: `npm test` — expect green (`attention.test.js`).
- [ ] `npm run build` once more; confirm `static/dist/app.js` is committed.
- [ ] **Only after Task 7 is complete:** merge the worktree branch to `main`; `bin/periscope restart`; smoke-test prod at `http://localhost:8765/`.

## Spec coverage self-check

- Needs you (live needs-input + need_human merge, reason label via waitLabel(waiting_for) — incl. AskUserQuestion distinction, ack via max(focused,acted)>ts, × dismiss) → Tasks 3,4,7.
- Pinned (pane-only, periscope-id, in-tree star, render-time dead-id prune) → Tasks 5,7.
- Activity (done/info/milestone, collapsed default) → Tasks 4,6,7.
- Section-header primitive + restyle + collapsible PROJECTS → Tasks 1,2.
- Native-notify/dock-badge re-home (no regression) → Task 6.
- Server: stable alert id → Task 3.

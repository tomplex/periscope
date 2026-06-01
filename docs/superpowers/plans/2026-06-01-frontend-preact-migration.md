# Frontend Preact Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Verification note:** This is a *manual-verified* frontend port (the project has no frontend test suite by convention — see CLAUDE.md). Tasks therefore end in a **Manual smoke gate** + a `npm run build` gate, not `pytest`. The testable risk lives server-side and is covered by the separate backend plan. Behavioral correctness is confirmed in the browser against the dev instance on :8766 — this is a hard gate, not optional.

**Goal:** Replace periscope's vanilla-JS frontend with a Preact + `@preact/signals` app (grid + split views only; stream cut), preserving every behavior catalogued in the behavior inventory, executed as per-surface cutover commits inside a worktree behind a temporary mount switch.

**Architecture:** All app JS goes through one Vite build → committed `static/dist/`. A signals store (`store.js`) is the single read model for transient state; `prefs.js` stays the persistence boundary but its cache becomes a signal. Imperative widgets (xterm, LGTM iframe) are wrapped in ref+`useEffect` components, keyed on pid so re-selection reconnects rather than remounts. During the port a `?preact=1` mount switch lets each surface cut over independently and revertibly; vanilla modules + the switch are deleted at the end.

**Tech Stack:** Preact, `@preact/signals`, JSX via `@preact/preset-vite`, Vite (build + dev watch), vendored xterm.js (unchanged, still a plain `<script>`).

**Source spec:** `docs/superpowers/specs/2026-06-01-frontend-architecture-design.md`
**Structure proposal:** `docs/superpowers/specs/2026-06-01-frontend-architecture-structure.md`
**Behavior inventory + synthesis:** produced by the `behavior-inventory-for-port` workflow; the per-surface **must-not-drop** lists below are lifted from it.

---

## Global conventions (every surface port obeys these)

These are the cross-cutting rules the inventory synthesis flagged. A reviewer checks every surface against them.

1. **Keyed lists by `pid`.** Cards and rail rows use `key={w.pid}` (the stable `@periscope_id`). This is what makes diffing real and what reproduces the single-xterm `sameMount` behavior.
2. **Signals are the read model; `prefs.js` is the persistence boundary.** Components read transient state from `store.js` signals and durable state from the `prefs` signal (Task 2). Mutations to durable state go through `prefs.patch*` (which POSTs + updates the signal). Never read a bare `prefs.getX()` that returns a fresh copy without subscribing.
3. **Preserve the CSS contract.** Do NOT rename: the `body[data-view]` attribute (CSS keys visibility off it — still set it from the `view` signal via an effect), `state-${w.state}` card classes, `dot-pulse`/`dot-alert`, `card-channel-${kind}`, `rail-dim`, and the `--header-h` custom prop (alerts.js sets it via ResizeObserver). A JSX port that renames a class silently breaks CSS-driven layout/behavior.
4. **Inline `onclick="event.stopPropagation()"` in HTML strings becomes a real JSX `onClick` that calls `e.stopPropagation()`.** PR/Linear anchors in grid + rail rely on this; dropping it makes clicking a PR link open the modal.
5. **`targetQuery`/`moveCard` split `"session:index"` on the LAST colon** (invariant #6 — session names contain `:` and `/`). Port `util.targetQuery` exactly; never `split(":")`.
6. **`needs-input` is never downgraded** to `working`/spinner — at the data layer (backend, already handled) AND the render layer (card status-label priority: needs-input beats spinner verb).
7. **Escape is a LIFO stack** (`useEscape` hook, Task 2) — never a per-component `window` keydown listener (that closes all modals at once).
8. **No `innerHTML`.** All rendering is JSX.

## File layout (from the structure proposal)

```
static/
  index.html                CHANGED: reference built bundle; keep styles.css + vendor/xterm <script> tags
  styles.css                unchanged (hand-authored)
  vendor/xterm.js, addon-fit.js, xterm.css   unchanged (plain <script>; window.Terminal)
  sw.js                     unchanged
  dist/                     NEW, COMMITTED: Vite build output
  src/                      NEW: Vite root, all app source
    main.jsx                entry: mounts <App> into #app
    store.js                signals (transient read model)
    prefs.js                PORTED: persistence boundary; cache is now a signal
    util.js                 PORTED: targetQuery, apiCall, relTime, prUrl, rewriteLgtmHost
    hooks/useEscape.js      LIFO escape-stack hook (replaces overlay.js)
    filter.js               passesFilter (single source; app.js inline copy reconciled)
    chrome/Header.jsx, FilterBar.jsx, ViewSwitch.jsx, UsagePill.jsx
    grid/Grid.jsx, Card.jsx, NewTile.jsx
    split/Rail.jsx, RailRows.jsx, Detail.jsx, railTree.js (mergeLiveAndPrefs, pure)
    terminal/Terminal.jsx   ref+useEffect wrapper over the imperative core
    terminal/terminalCore.js  PORTED from terminal.js + terminal-mount.js (imperative, verbatim)
    modal/Modal.jsx         one file: tab strip + sidebar inline (NOT exploded)
    overlays/Dialog.jsx, Toast.jsx, Alerts.jsx, CommandsModal.jsx, NewProjectModal.jsx,
             ReviewPrModal.jsx, CleanupModal.jsx, SettingsModal.jsx, OpenPickerModal.jsx,
             LauncherModal.jsx
    tauri.js                PORTED: native badge/notify init (imperative, called in an effect)
```

`history.js` / `history.html` are **out of scope** — they stay a separate plain-module SPA on their own route.

## Migration mechanism (per-surface cutover, revertible)

- All work happens in the worktree branch; prod stays on `main`/:8765 untouched until a single final merge.
- **Shared surface flag.** `index.html` sets `window.__PREACT_SURFACES__ = new Set((new URLSearchParams(location.search).get("preact")||"").split(",").filter(Boolean))` BEFORE either script runs. Both `main.jsx` and vanilla `app.js` consult it.
- **`main.jsx`** mounts the Preact component for each surface in `__PREACT_SURFACES__`, leaving the rest to the vanilla path.
- **Vanilla `app.js` must be gated** (see Must-fix from plan-reviewer): vanilla `app.js` calls `bootstrap()` at module top-level (`app.js:370`) which starts `initGrid()`'s 3s poll, `initModal()`, the global `keydown` listener (`app.js:31`), and the `viewSwitch`→`applyView`→`body[data-view]` writer (`app.js:360-368`). When a surface is Preact-owned, the vanilla equivalent MUST skip its init — otherwise you get two concurrent `/api/state` polls (doubling the fork storm), two keydown handlers fighting over Tab/Escape, and two writers to `body[data-view]`. Task 1 adds the gate.
- This lets each surface land + be browser-verified independently, and reverted by a single commit if wrong.
- The vanilla modules and the mount switch are **deleted in the final task** once every surface is verified; the end state is all-Preact with no flag.
- Commit after each surface (atomic, bisectable). The dev instance on :8766 stays runnable at every commit.

---

### Task 1: Build scaffold (Vite + Preact, committed dist)

**Files:**
- Create: `static/src/main.jsx`, `vite.config.js` (modify existing)
- Modify: `package.json`, `static/index.html`, `dev.sh`, `bin/periscope`, `CLAUDE.md`, `README.md`

- [ ] **Step 1: Add Preact deps + build script**

`package.json` gains `dependencies`: `preact`, `@preact/signals`; `devDependencies`: `@preact/preset-vite`; and `scripts`: `"build": "vite build"`.

**Vite config mechanics (pin these — the existing `vite.config.js` has `root: "static"`):** keep `root: "static"` (so `index.html` stays where it is and the existing `/api`+`/ws` dev proxy is unchanged); add `plugins: [preact()]`; set `build: { outDir: "dist", emptyOutDir: false, rollupOptions: { input: "static/src/main.jsx", output: { entryFileNames: "app.js", assetFileNames: "[name][extname]" } } }` so the build emits a stable `static/dist/app.js` (no content hash → `index.html` references a fixed path, no churn). The `.jsx` sources under `static/src/` are reachable via the StaticFiles mount but unreferenced — acceptable for a single-user localhost tool (don't move `root` to `src/` just to hide them). No `react`→`preact/compat` alias needed.

- [ ] **Step 2: Minimal entry that mounts an empty `<App>`**

```jsx
// static/src/main.jsx
import { render } from "preact";
function App() { return <div data-preact-root>periscope (preact scaffold)</div>; }
render(<App />, document.getElementById("app"));
```

`static/index.html`: add `<div id="app"></div>`, the `window.__PREACT_SURFACES__` initializer `<script>` (see Migration mechanism — must run before both app scripts), and `<script type="module" src="/static/dist/app.js">`. Keep the existing `styles.css` link and `vendor/xterm.js` `<script>` tags. The vanilla `app.js` `<script>` stays for now.

- [ ] **Step 2b: Gate vanilla `app.js` bootstrap (Must-fix)**

In `static/app.js`, wrap the top-level `bootstrap()` call (`app.js:370`) and the parts of bootstrap that own a Preact-claimed surface so they no-op when that surface is Preact-owned. Concretely: read `const P = window.__PREACT_SURFACES__ || new Set()`. Skip `initGrid()` (and its poll) when `P.has("grid")`; skip the global `keydown` registration + `viewSwitch`/`applyView` wiring when `P.has("chrome")`; skip `initModal()` when `P.has("modal")`; etc. The goal: exactly one `/api/state` poll, one keydown handler, one `body[data-view]` writer at all times. This gating code is deleted with the rest of vanilla in Task 9.

- [ ] **Step 3: dev + install wiring**

`dev.sh`: add `vite build --watch --emptyOutDir false` as a third background process under the existing `trap 'kill 0'` group. `bin/periscope install`: add a `command -v npm` precondition (mirror the `command -v uv` check) and run `npm install && npm run build` before writing the launchd plist. `CLAUDE.md`: replace "No bundling step in production" / "vanilla ES modules" claims with "the committed `static/dist/` bundle is the one build artifact; rebuild + commit it when `static/src/` changes." `README.md`: add `node` to prerequisites + `npm install && npm run build` to quick-start.

- [ ] **Step 4: Build gate**

Run: `npm install && npm run build`
Expected: `static/dist/app.js` is produced. Then `git add static/dist/` (no `dist/` rule exists in `.gitignore`, verified by plan-reviewer — no `-f` needed; the bundle is committed so `bin/periscope restart` needs no build step).

- [ ] **Step 5: Manual smoke gate**

`PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py`; open :8766 — the page still serves (vanilla dashboard intact), and visiting with the scaffold mounted shows the placeholder. No console errors.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "preact: build scaffold (vite + preset-preact, committed dist)"
```

---

### Task 2: Shared primitives (store, prefs-as-signal, util, useEscape, dialog, toast)

**Files:** Create `static/src/store.js`, `static/src/prefs.js`, `static/src/util.js`, `static/src/hooks/useEscape.js`, `static/src/overlays/Dialog.jsx`, `static/src/overlays/Toast.jsx`.

**Why first (synthesis phasing):** every surface depends on these; porting a view before them forces throwaway shims.

- [ ] **Step 1: `store.js` — transient signals**

```js
// static/src/store.js
import { signal } from "@preact/signals";
export const windows = signal([]);            // /api/state windows (poll-fed)
export const projects = signal([]);
export const currentFilter = signal("all");
export const view = signal("split");          // grid | split   (stream removed)
export const activeTarget = signal(null);     // modal/detail focused pane "session:index"
export const railSelection = signal(null);    // {kind:"pane",pid} | {kind:"review",worktree} | null
export const dragState = signal(null);
export const usage = signal(null);
// poll-pause flags (replace state.editingTarget / state.dragging guards):
export const editingTarget = signal(null);
export const modalRenaming = signal(false);
export const modalAutoRenaming = signal(false);
```

- [ ] **Step 2: `prefs.js` — port the persistence boundary, cache as a signal**

Port the existing `static/prefs.js` verbatim EXCEPT: replace the private `cache` object with `const prefsSignal = signal({ui:{}, windows:{}, commands:[], loaded:false})`. Every getter reads `prefsSignal.value`; every mutator (the optimistic-update-and-revert path) does `prefsSignal.value = {...prefsSignal.value, ...}`.

**Must-not-drop (synthesis):**
- The **`!loaded` guard**: mutators refuse to write when `!prefsSignal.value.loaded` (prevents wiping `session_order`/`collapsed`/`view` on a first toggle before prefs load).
- The **optimistic-update-then-revert**: eager-merge into the signal, await network, on failure restore the pre-write snapshot. The `setAnnotation` undefined-vs-object revert distinction must be preserved exactly.
- The **one-time localStorage→server migration** at boot.
- `loadPrefs()` is awaited in `main.jsx` before the first render/poll.

- [ ] **Step 3: `util.js` — port pure helpers verbatim**

Port `escapeHtml` (only where still needed — JSX auto-escapes, so most uses drop), `targetQuery` (LAST-colon split — convention #5), `apiCall` (dual-shape error normalization + toast), `relTime`, `prUrl`, `rewriteLgtmHost`. No behavior change.

- [ ] **Step 4: `useEscape.js` — LIFO stack hook**

Port `overlay.js`'s `pushEscape`/`popEscape` semantics as a module-level stack + a hook:

```js
// static/src/hooks/useEscape.js
import { useEffect } from "preact/hooks";
const stack = [];
function onKey(e){ if(e.key==="Escape" && stack.length){ e.stopPropagation(); stack[stack.length-1](); } }
if (typeof window !== "undefined") window.addEventListener("keydown", onKey, true);
export function useEscape(handler, active = true){
  useEffect(() => {
    if(!active) return;
    stack.push(handler);
    return () => { const i = stack.indexOf(handler); if(i>=0) stack.splice(i,1); };
  }, [handler, active]);
}
```

**Must-not-drop:** LIFO order — a dropdown opened over a modal pushes on top, so Escape closes the dropdown then the modal. One global listener, not per-component.

- [ ] **Step 5: `Dialog.jsx` + `Toast.jsx`**

Port `dialog.js` (Promise-returning `confirmDialog`/`promptDialog`/`alertDialog` — used by grid kill/send-bulk + app new-session, so this must exist before Grid) and `toast.js` (`showToast`, 6s). `Dialog` uses `useEscape` for cancel. Render `<Toaster>` + a `<DialogHost>` once in `<App>`.

- [ ] **Step 6: Manual smoke + commit**

No UI cutover yet; verify `npm run build` passes and importing these modules throws nothing. Commit: `preact: shared primitives (store, prefs-signal, util, useEscape, dialog, toast)`.

---

### Task 3: `<Terminal>` wrapper (riskiest unit — port + verify in isolation early)

**Files:** Create `static/src/terminal/terminalCore.js` (ported from `static/terminal.js` + `static/terminal-mount.js`), `static/src/terminal/Terminal.jsx`.

**Why early (synthesis):** both `<Modal>` and `<Detail>` mount it; it holds the hardest invariants. Get it right before consumers.

- [ ] **Step 1: Port the imperative core verbatim**

Copy `terminal.js` + `terminal-mount.js` into `terminalCore.js` essentially unchanged — it stays imperative. Keep `startLiveTerminal`/`stopLiveTerminal`/`mountTerminal`/`unmountTerminal`/`setTerminalContainer`/`setTerminalLinkCallback`. Do not "Preact-ify" the xterm/WS logic.

- [ ] **Step 2: Thin `<Terminal>` wrapper**

```jsx
// static/src/terminal/Terminal.jsx
import { useRef, useEffect } from "preact/hooks";
import { mountTerminal, unmountTerminal } from "./terminalCore.js";
export function Terminal({ target, onMdLink, onPaste }) {
  const ref = useRef(null);
  useEffect(() => {
    mountTerminal(ref.current, target, { onMdLink, onPaste });
    return unmountTerminal;      // mountTerminal self-unmounts too — don't double-tear-down
  }, []);                        // empty deps: mount ONCE for this component instance
  return <div ref={ref} class="modal-xterm" />;
}
```

Call sites **key on pid** so re-selecting the same pane preserves this instance (reproduces `detail.js:86`'s `sameMount` pid-keyed skip) and selecting a different pane unmounts+remounts → reconnect:

```jsx
<Terminal key={pid} target={target} onPaste={handlePaste} />
```

(For the modal, which is keyed per-open, the empty-deps effect already mounts once per open.) This matches the structure proposal §4; the earlier `[target]`-deps form is wrong — it would double-tear-down (since `mountTerminal` self-unmounts) and wouldn't reproduce pid-keyed `sameMount`.

**Must-not-drop (synthesis):**
- **Singleton xterm**: created once per component instance in the empty-deps effect, disposed in cleanup — NOT recreated on render ticks. Pid-keying at the call site is what makes re-selection reconnect-not-remount.
- **Initial-paint ordering** (invariant #3): `open → focus → fit → connect(cols,rows)` in the same tick before the WS connects — already in `terminalCore`, must not be reordered.
- **Reconnect FSM as refs**: `termIntentionalClose`, `termReconnectAttempt`, `termWsTarget`, the `[250,500,1000,2000]→4000` backoff, and the `ws !== termWs` stale-socket guard — these live in `terminalCore` module state (already ref-like), so the `onclose` closure reads live values. Cleanup suppresses-reconnect BEFORE close.
- **`\n`→`\r\n`** (invariant #4) and the capture-phase paste handler stay in the core.

- [ ] **Step 3: Isolation smoke gate**

Mount `<Terminal target="<a-live-pane>">` standalone behind `?preact=terminal`. Verify: live mirror renders, typing works, scrollback present, resize works, kill+restore the WS (restart periscope) and confirm reconnect + correct first-frame cols/rows. Commit: `preact: <Terminal> ref wrapper over imperative core`.

---

### Task 4: `filter.js` + chrome (`<Header>`, `<FilterBar>`, `<ViewSwitch>`, `<UsagePill>`)

**Files:** Create `static/src/filter.js`, `static/src/chrome/*.jsx`.

- [ ] **Step 1: `passesFilter` as the single source**

Port `grid.js:passesFilter` into `filter.js` as the one exported predicate. **Reconcile the `app.js:233-243` inline duplicate** (synthesis coupling #1): the bulk-send path imports `passesFilter` from `filter.js` — no second copy. Branches: all, needs-input, working, done, idle, claude, shell, ci-bad.

- [ ] **Step 2: Chrome components**

`<Header>` (filters, +new, history link, ⋯ overflow, alerts toggle, view switch). `<FilterBar>` writes `currentFilter` signal. `<ViewSwitch>` is **2-way (grid/split)** — stream removed; it writes the `view` signal and `prefs.setView`. `<UsagePill>` reads the `usage` signal (fed from the poll's `usage`/`usage_scrape`).

**Must-not-drop:** an effect mirrors the `view` signal onto `document.body.dataset.view` (CSS contract, convention #3). The Tab keybinding cycles grid↔split (2-way now).

- [ ] **Step 3: Smoke + commit** behind `?preact=chrome`. Commit: `preact: filter + chrome (header/filterbar/2-way viewswitch/usagepill)`.

---

### Task 5: `<Grid>` (owns the poll loop)

**Files:** Create `static/src/grid/Grid.jsx`, `Card.jsx`, `NewTile.jsx`.

**Why grid before modal/split (synthesis):** the 3s poll + render dispatcher are grid-owned; everything triggers a refresh through one poll loop.

- [ ] **Step 1: The single poll loop → store**

`<Grid>` (or `<App>`) owns ONE `setInterval(poll, 3000)` that fetches `/api/state`, writes `windows`/`projects`/`usage` signals. **Must-not-drop:** the poll does NOT commit new data while `editingTarget`/`dragState` signals are set (replaces the `state.editingTarget`/`state.dragging` guards) — preserves in-flight rename/drag. Expose a `poll()` other surfaces can call to force-refresh after a mutation (replaces the grid↔modal circular import — now a shared store action).

- [ ] **Step 2: `<Card>` + `<NewTile>`**

Port `renderCard`/`renderCardMeta`/`renderCardActivity`/`renderCardFooter`/`renderNewTile` as components, keyed by `pid`.

**Must-not-drop (inventory):** card status-label priority (needs-input > spinner verb > raw state; convention #6); unread-alert override + `card-channel-${kind}` tint; needs-attention pulse only for `need_human`; promote button gating (`project_pinned_dir==='__main__'` AND `aff.kind && !=='no-repo'`); PR/Linear anchors with **real `onClick` stopPropagation** (convention #4); CI-`✗` bundled into `card-pr-fail`; footer model parenthetical strip + `relTime(focused_at)`; `joinWithDots` meta separators; `orderedSessions` (saved order + fresh-at-top by max acted_at); session pill loudest-wins color; `sessionChannelAlert` rollup.

- [ ] **Step 3: Drag-reorder + rename + dblclick defer**

- **Two MIME types** (coupling #6): session reorder via `text/plain`, card-move via `application/periscope-card` (CARD_MIME); card drags omit `text/plain`; dragover/drop checks `CARD_MIME` first. Reproduce both; set `dragState` signal during drag.
- **220ms dblclick-vs-open defer** (must-not-drop #1): single click sets a 220ms timer to `openModal`; dblclick cancels it → rename; a second single-click while pending is ignored. Per-target timer map.
- **Inline rename** (coupling #3): `editingTarget` signal pauses poll; one-shot done/committed guard against blur-after-Enter double-submit; keydown `stopPropagation`; POST `/api/rename` then `poll()`. (Project rename is a third variant — POST `/api/projects/patch` — port it too.)

- [ ] **Step 4: Smoke + commit** behind `?preact=grid`. Verify: cards render + update; filter; collapse/reorder (both drag types); rename (no double-submit); 220ms single-vs-double click; kill via Dialog. Commit: `preact: <Grid> with poll loop, cards, drag, rename`.

---

### Task 6: `<Modal>` (one file — not exploded)

**Files:** Create `static/src/modal/Modal.jsx` (tab strip + sidebar inline, per structure decision — NOT separate `<TabStrip>`/`<Sidebar>`).

- [ ] **Step 1: Lifecycle + 1.5s header poll**

`openModal(target)` sets `activeTarget`, mounts `<Terminal>`, starts a 1.5s `/api/pane` poll. **Must-not-drop:** the poll does NOT commit while `modalRenaming`/`modalAutoRenaming` signals hold. `activeTarget` is the shared signal also set by Detail (coupling #5) and read by the shared paste handler.

- [ ] **Step 2: Tab strip + sidebar (the fragile part)**

Port the tab strip (terminal + LGTM-derived tabs + mounted-docs dropdown + `+ Start review`) and sidebar (PR card, Linear card, notes editor + tags, activity timeline, link-ask buttons).

**Must-not-drop (synthesis — highest fragility):**
- **Sidebar focus-guard**: skip re-rendering / do not clobber the notes/tags inputs when `document.activeElement` is inside the sidebar; **restore the activity-stream scrollTop** across updates. Use **uncontrolled inputs (refs)** so the 1.5s poll never resets in-flight typing.
- **LGTM iframe idempotency**: reuse the iframe, reassign `src` ONLY when the tab/doc key changes; never remount per poll (kills SSE); no `loading=lazy`. Key the iframe so Preact doesn't recreate it.
- Modal rename (coupling #3 guards) + auto-rename; image paste (capture-phase) writes temp path into terminal; `addLgtmDocFromTerminal` md-link flow.

- [ ] **Step 3: Smoke + commit** behind `?preact=modal`. Verify: open modal, terminal mirrors, type notes while polling (not clobbered), LGTM iframe doesn't reload each poll, rename, paste image. Commit: `preact: <Modal> (tabs + sidebar, focus-guard, iframe idempotency)`.

---

### Task 7: `<Rail>` + `<Detail>` (split view)

**Files:** Create `static/src/split/Rail.jsx`, `RailRows.jsx`, `Detail.jsx`, `railTree.js`.

- [ ] **Step 1: `railTree.js` — `mergeLiveAndPrefs` (pure)**

Port `mergeLiveAndPrefs` exactly (coupling #9/#10): live windows are membership source; prefs give order; repo order pref-first then live-new; `OTHER_REPO_KEY` pinned bottom (enforced at all four points: merge, isValidDropTarget, reorderRepos, repoRow dragAttr); auto-append `review` row for git-backed worktrees. It's consumed **twice** — to render AND as `currentMergedOrder()` seed for drag-reorder splices (not raw prefs). Keep it pure.

- [ ] **Step 2: `<Rail>` + `<RailRows>`**

Repo→Worktree→Pane+review tree. Status-dot rollup (`needs-input > working > done > idle > shell`). **Two-level filter** (`paneMatchesFilter` wraps `filter.passesFilter`; a repo/worktree shows if any child matches — grays non-matching in place via `rail-dim`, preserves layout). **Two deliberately-different selection shapes — do not cross them** (`rail.js:422-432`, `prefs.js:286`): the **persisted** `prefs.last_selected` is an OBJECT (`{kind:"pane",pid}` / `{kind:"review",worktree}`); the **highlight-key** the rows compare against is a STRING (`pane:${pid}` / `review:${worktree}`). The `railSelection` signal mirrors the string key for fast highlight; persistence stores the object. Storing the string into the pref (or the object into the signal) silently breaks restore/highlight. **Rail DnD carries reorder identity on the drag payload/props — NOT `previousElementSibling` DOM walks** (coupling #6 — those break under a component tree). PR/Linear anchors: real `onClick` stopPropagation.

- [ ] **Step 3: `<Detail>` — pane / review / empty**

`selectPane(pid)` sets `activeTarget` BEFORE the `sameMount` short-circuit (coupling #5 — paste targeting). **Single-xterm reuse**: re-selecting the same pid does NOT remount `<Terminal>` (key on pid); selecting a different pane reconnects. Review row: LGTM iframe (idempotent, same rules as modal) or `+ Start review` CTA → `POST /api/lgtm/start` → mount iframe on the CTA→live transition. Empty state. `lastSelected` restored on cold reload.

- [ ] **Step 4: Smoke + commit** behind `?preact=split`. Verify: rail tree builds + orders; collapse; two-level filter grays correctly; drag-reorder at all three levels; select pane (terminal reuses across re-select); review iframe; cold-reload restores selection. Commit: `preact: <Rail> + <Detail> split view`.

---

### Task 8: Secondary modals + alerts + tauri + sw

**Files:** Create `static/src/overlays/{CommandsModal,NewProjectModal,ReviewPrModal,CleanupModal,SettingsModal,OpenPickerModal,LauncherModal,Alerts}.jsx`, `static/src/tauri.js`.

> **Parallelizable batch** — once Tasks 2–7 lock the conventions, these are independent same-shape ports.

- [ ] **Step 1: Port each modal** following the `useEscape` + `apiCall` + Dialog patterns. **Must-not-drop:** `new-project` and `review-pr` submit handlers do the **deferred rail auto-add** (`setTimeout ~3500ms` to wait for the new tmux session's panes to appear in the poll — coupling: timing-coupled to the poll cadence, write to `worktrees_by_repo` + seed `panes_by_worktree`). `open-picker` lists tmux sessions not already railed. `launcher` reads `prefs.getCommands()` + POSTs `/api/window/new`. `cleanup`/`settings`/`commands` per their existing behavior. **`open-picker` and `launcher` currently have NO Escape handling** — decide deliberately: the unified `useEscape` adds Escape-close (behavior change, recommended) — note it in the commit.

- [ ] **Step 2: `Alerts.jsx` + `tauri.js`** — **Must-not-drop:** alerts `seenAlertKeys===null` first-poll sentinel (snapshot, do NOT notify on the backlog of `need_human` alerts at app open); the channel-unread/`need_human` gate shared between per-card pulse and grid-has-attention fade; `tauri.js` `setBadgeCount` + native notify via `window.__TAURI__` (init in an effect). `sw.js` registration preserved (PWA installability gate).

- [ ] **Step 3: Smoke + commit.** Verify each modal opens/submits; new-project/review-pr land in the rail after ~3.5s; no native-notify spam on app open. Commit: `preact: secondary modals + alerts + tauri + sw`.

---

### Task 9: Cutover + delete vanilla

**Files:** Delete `static/app.js`, `grid.js`, `modal.js`, `rail.js`, `detail.js`, `terminal.js`, `terminal-mount.js`, `stream.js`, `prefs.js`, `state.js`, `util.js`, `overlay.js`, `dialog.js`, `toast.js`, `alerts.js`, `usage-pill.js`, `commands-modal.js`, `new-project-modal.js`, `review-pr-modal.js`, `cleanup-modal.js`, `settings-modal.js`, `open-picker-modal.js`, `launcher-modal.js`, `modal-shell.js`, `tauri.js` (the vanilla ones). Modify `static/index.html` (drop vanilla `<script>` + the mount switch), `main.jsx` (always mount full `<App>`).

- [ ] **Step 1: Remove the mount switch**; `main.jsx` mounts the full `<App>` unconditionally.
- [ ] **Step 2: Delete vanilla modules + `stream.js`** and the stream keybindings/`prefs.view==="stream"` path (stream cut — synthesis: existing stream users fall back to split).
- [ ] **Step 3: `npm run build` + commit `static/dist/`.**
- [ ] **Step 4: Full manual smoke checklist** (the silent-regression risks the synthesis flagged — no automated coverage):
  - 220ms dblclick-vs-open on a card title.
  - Drag both MIME types (session reorder + card move into session).
  - Rename pauses poll; no double-submit.
  - Modal notes typing survives the 1.5s poll (not clobbered).
  - LGTM iframe does not reload each poll (SSE stays up).
  - Terminal reconnect after periscope restart + correct first-frame cols/rows.
  - Single-xterm reuse on rail re-select (no WS churn).
  - new-project / review-pr land in the rail.
  - No native-notify spam on app open.
  - PR/Linear links open the link, not the modal.
  - 2-way view switch (grid↔split) + Tab cycle; `body[data-view]` CSS intact.
- [ ] **Step 5: Commit** `preact: cutover — delete vanilla frontend, stream view removed`.

---

## Self-review notes

- **Spec coverage:** Preact + signals (Tasks 1–8), grid+split ported / stream cut (Tasks 4,5,7,9), `prefs`-as-signal (Task 2), committed `dist` + build step (Task 1), mount-switch cutover (Tasks 1,9), `<Terminal>` ref wrapper (Task 3). All §Architecture—Frontend items mapped.
- **Synthesis must-not-drop:** every item is attached to the surface that owns it (220ms defer→T5, focus-guard+iframe idempotency→T6, terminal FSM→T3, mergeLiveAndPrefs+rail-DnD-payload→T7, prefs `!loaded`/revert→T2, useEscape LIFO→T2, alerts sentinel→T8, targetQuery last-colon→T2, needs-input priority→T5, stopPropagation→T5/T7).
- **Couplings:** passesFilter single source + app.js dup (T4), grid↔modal poll via shared store action (T5), activeTarget shared (T6/T7), two DnD systems kept separate (T5 grid / T7 rail).
- **No automated frontend tests** (project convention) — each task ends in a manual smoke gate + `npm run build`; the build is the only mechanical gate.

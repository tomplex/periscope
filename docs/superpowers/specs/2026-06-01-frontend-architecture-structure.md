# Frontend Re-architecture — Code Structure Proposal

**For:** `2026-06-01-frontend-architecture-design.md`
**Status:** structure proposal, awaiting Tom review
**Scope:** decides code shape only. Sequencing is the plan's job; behavioral verification is spec-reviewer's.

Locked decisions are taken as given: Preact + `@preact/signals`, big-bang in a worktree, commit `static/dist/`, keep 3s poll, stream cut, single Vite pipeline. This proposal does not relitigate them.

---

## 1. Spec pushback

**1a. The store is not "one `store.js`" — `prefs.js` is left out of the store, and that split is wrong as written.** The spec (§Signals) says transient state moves into `store.js` signals while `prefs.js` "keeps its current persistence boundary" and "components read it." That leaves components reading two sources — signals for transient state, `prefs.js` getters for durable state — and the durable getters (`getRailCollapsed`, `getRepoOrder`, `getLastSelected`, `getCollapsed`) are plain function calls that return fresh copies and do **not** trigger re-render. The rail tree is derived from prefs (`mergeLiveAndPrefs` in `rail.js:39`); if a `setRailCollapsedKey` write doesn't notify subscribers, the rail won't re-render on collapse. That is the exact bug class signals exist to kill.

   **Alternative:** make the **prefs cache a signal too**. `prefs.js` stays the persistence boundary (it still owns `loadPrefs` / `patchUI` / the optimistic-update-and-revert dance and the localStorage migration — that logic is good and load-bearing, see `prefs.js:103-123`). But its private `cache` object becomes a single `prefsSignal = signal({ui, windows, commands, loaded})`, and every getter reads `prefsSignal.value`, every mutator does `prefsSignal.value = {...}` after the server confirms (or on optimistic write, with revert). Components read durable state through `prefsSignal` (or thin `computed`s over it), never through bare getter calls. Net: one reactive read model, `prefs.js` still owns persistence. This is the only way the spec's stated goal ("single source of truth," "components read prefs through the store") is actually true.

**1b. Don't introduce `<TabStrip>`, `<Sidebar>` as standalone components on day one (modal).** The spec's component table explodes `modal.js` into `<Modal>` + `<TabStrip>` + `<Sidebar>` + per-tab components. `modal.js` is 1180 LOC but it is *one* modal with tabs, not five reusable widgets. A `<TabStrip>` with one consumer is a speculative reuse seam — the project's hard-YAGNI rule forbids it. Port `<Modal>` as one component file that renders its own tab buttons and sidebar inline; split a child component out only when a tab's body is genuinely large and self-contained (the terminal tab → `<Terminal>`, which *is* shared with detail, earns its own file). See §4 for the concrete cut.

**1c. The poll-optimization "per-window view cache" is not a class.** The spec says "Cache per-window `{last_activity, parsed_view}`." A module-level `dict` plus two functions is the right shape (rung 1) — there is no lifecycle, no polymorphism, one process-global instance. A `WindowViewCache` class would be a singleton-as-class, which the taste rules flag. Detail in §4.

No pushback on: imperative xterm staying imperative (correct), keyed-by-pid lists (correct), `useEscape` hook replacing `overlay.js` (correct — it's genuinely a hook-shaped concern).

---

## 2. Assumptions

- **JSX, not `htm`.** The corpus-fluency criterion that motivates Preact applies equally to JSX syntax; `htm` (no-build tagged templates) would undercut the "idiomatic Preact, zero ramp" goal and we already accept a build step. Vite + `@preact/preset-vite` (or esbuild's `jsx` with `jsxImportSource: "preact"`).
- **`static/styles.css` is not ported.** It stays a single hand-authored stylesheet loaded by `index.html` as today (`index.html:13`). Component-scoped CSS / CSS-modules is out of scope — not mentioned in the spec, and converting it is gratuitous churn against a behavior-port.
- **The ~10 secondary modals** (`commands-modal`, `new-project-modal`, `review-pr-modal`, `cleanup-modal`, `settings-modal`, `open-picker-modal`, `launcher-modal`, `dialog`, `alerts`, `toast`) **are in scope** for the port — they're wired in `app.js:328-345` and `<App>` must mount their replacements or the dashboard loses function. The spec's component table only names a subset; I assume all of them port. `modal-shell.js` (the `createModalShell` factory shared by 4 of them) is **dropped** — its job (open/close/escape/error-row plumbing) is what Preact + `useEscape` does natively.
- **`tauri.js`** stays a non-component imperative module (it talks to `window.__TAURI__`); `<App>` calls its init in an effect. Not a component.
- **`history.js` / `history.html` are out of scope.** Separate SPA, separate entry, not mentioned for porting. It keeps loading as a plain module. (Confirm — if Tom wants it ported too, that's a second Vite entry, additive.)
- **`@periscope_id` (`pid`) is present and stable on every window-view row** — invariant the keyed lists depend on; verified it's threaded through `list_windows` → `build_window_view` (`window_view.py:42`, the view spreads `**w`).
- **The `ThreadPoolExecutor` is created per-request, not module-global.** `/api/state` is the only caller; a per-request pool with `max_workers` ~ pane count is simpler than a shared pool and avoids lifespan ownership questions. (Flagged in §7 — a shared pool is the alternative.)

---

## 3. File layout

```
static/
  index.html                 CHANGED: drop app.js module tag; reference built bundle (see §5)
  styles.css                 unchanged (hand-authored, loaded as-is)
  vendor/xterm.js, addon-fit.js, xterm.css   unchanged (still plain <script>; window.Terminal)
  sw.js                      unchanged
  dist/                      NEW, COMMITTED: Vite build output (assets/index-<hash>.js + .css?)
  src/                       NEW: all app source, the Vite root
    main.jsx                 entry: render(<App/>, #root); SW register; tauri init
    app.jsx                  <App> — view switch (2-way), global keydown, mounts chrome + modals
    store.js                 transient signals (windows, projects, filter, view, activeTarget, …)
    poll.js                  /api/state poll loop → writes store signals; pause guards
    prefs.js                 CHANGED: cache becomes a signal; persistence boundary preserved
    util.js                  PORTED ~as-is (escapeHtml, apiCall, relTime, prUrl, rewriteLgtmHost, targetQuery)
    hooks/
      useEscape.js           replaces overlay.js escape-stack (one shared stack, hook API)
    chrome/
      Header.jsx             header bar + toolbar dropdowns (the app.js dropdown machinery)
      FilterBar.jsx          state-filter pills + send-bulk + collapse-all
      ViewSwitch.jsx         2-way grid/split toggle
      UsagePill.jsx          usage-pill.js port
    grid/
      Grid.jsx               <Grid> → maps sessions; reads store.windows + prefs
      SessionGroup.jsx       <SessionGroup> (collapse, drag-reorder)
      Card.jsx               <Card> (keyed by pid) + the renderCard* meta/activity/footer logic
      NewTile.jsx            <NewTile> (+ new worktree affordance)
    split/
      Rail.jsx               <Rail>; owns the merge/order derivation (mergeLiveAndPrefs ported here)
      RailRows.jsx           <RepoRow>/<WorktreeRow>/<PaneRow>/<ReviewRow> (small, tightly coupled → one file)
      Detail.jsx             <Detail> dispatch → pane / review-live / review-empty / empty
    terminal/
      terminal.js            PORTED ~as-is: imperative xterm core (the 6 exports)
      terminal-mount.js      PORTED ~as-is: mount/unmount contract (terminal-mount.js:27-55)
      Terminal.jsx           <Terminal> ref+useEffect wrapper over terminal-mount (keyed on pid)
    modal/
      Modal.jsx              <Modal> — tabs + sidebar inline; per §1b not pre-split
      modalActions.js        non-render helpers: handleModalImagePaste, addLgtmDocFromTerminal, rename
    overlays/
      Toaster.jsx            toast.js port (signal-backed toast queue)
      AlertsPanel.jsx        alerts.js port
      Dialog.jsx             dialog.js port (confirm/prompt/alert → promise-returning, one component)
      CommandsModal.jsx      commands-modal.js port
      NewProjectModal.jsx    new-project-modal.js port
      ReviewPRModal.jsx      review-pr-modal.js port
      CleanupModal.jsx       cleanup-modal.js port
      SettingsModal.jsx      settings-modal.js port
      OpenPickerModal.jsx    open-picker-modal.js port
      LauncherModal.jsx      launcher-modal.js port
    tauri.js                 PORTED ~as-is: imperative window.__TAURI__ bridge; called from main.jsx

  (DELETED at step 5: static/app.js, state.js, grid.js, modal.js, rail.js, detail.js,
   overlay.js, modal-shell.js, stream.js, and all the old root-level *-modal.js / alerts.js /
   toast.js / dialog.js / usage-pill.js once their src/ ports land)

periscope/
  window_view.py             CHANGED: build_window_view gains a cached fast-path (see §4)
  panes.py                   CHANGED: list_windows format gains #{window_activity}
  routes/state.py            CHANGED: ThreadPoolExecutor fan-out; pass activity map to cache
  (no new backend module — spec non-goal "no backend module reshape" honored)

vite.config.js               CHANGED: build.outDir=../static/dist, rollup input=src/main.jsx, preact preset
package.json                 CHANGED: add preact, @preact/signals, @preact/preset-vite; build script
dev.sh                       CHANGED: command -v npm guard; vite build --watch path (see §5)
bin/periscope                CHANGED: install rebuilds dist; restart stays build-free
```

Why `static/src/` (not flat `static/`): once *all* app JS goes through Vite, the served tree (`dist/`, `vendor/`, `styles.css`, `index.html`) and the source tree must be visually separate or the StaticFiles mount (`app.py:110`, serves `static/` at `/`) would expose `.jsx` source at public URLs. `src/` is the Vite `root`; `dist/` is the served artifact. This differs from workspace-v1's scoped bundle precisely because the whole app is bundled now.

---

## 4. Per-module structure

### Frontend

**`store.js` — rung 1 (module of signals + functions).** One module, flat exports, exactly as the spec sketches (minus stream signals). No class, no `createStore()` factory — a Preact-signals store *is* a module of `signal()` calls; wrapping it adds nothing. Domain split is not warranted at this size (~8 signals); a single file is one concern. Subscription is **direct signal import** in components (`import { windows } from '../store.js'; windows.value`), the idiomatic Preact-signals pattern — no `useStore()` hook indirection. Derived values (filtered windows, rail tree inputs) are `computed()` exported from where they're consumed, not pre-built in the store.

**`poll.js` — rung 1 (functions).** Splits the data-fetch loop out of the old `grid.js:poll`. `startPolling()` sets the interval; the fetch writes `windows.value` / `projects.value` / `usage.value`. The pause guards (`editingTarget`, `dragging`, `modalRenaming`) become **signals in the store** that `poll.js` checks before *applying* inbound data (spec §Signals — "guards on applying inbound data"), preserving `grid.js:1134-1135` intent. Rationale for a separate module: the store is the read model, the poll is the writer; keeping them apart is what lets event-driven push (§Deferred) swap the writer without touching the store or components.

**`prefs.js` — rung 1, cache-as-signal (per §1a).** Keep every existing function. Change only the backing store: private `cache` object → `const prefs = signal({loaded, ui, windows, commands})`. Getters read `prefs.value.ui.*`; mutators do the optimistic `prefs.value = {...prefs.value, ui:{...}}` then revert on `apiCall` failure (the existing `patchUI` revert logic at `prefs.js:110-122` maps directly). The localStorage migration (`prefs.js:203-240`) ports verbatim. No new abstraction; the persistence boundary is intact.

**`Card.jsx` / `SessionGroup.jsx` / `Grid.jsx` — rung 1 (function components).** Preact function components are functions. `Card` is keyed by `pid`. The pure render helpers from `grid.js` (`renderCardMeta`, `renderCardActivity`, `renderCardFooter`, `passesFilter`) become either small sub-components or pure functions colocated in the same file — `passesFilter` is reused by `Rail` (`rail.js:11`) and `FilterBar` send-bulk (`app.js:231`), so it moves to `store.js` or a `filter.js` beside it as a shared pure function (three consumers — meets the bar). Drag-reorder state lives in a `dragState` signal (already in the spec's store sketch), not component-local, because the drag must survive a poll-driven re-render (`grid.js:1037` comment is the load-bearing reason).

**`Rail.jsx` — rung 1 (function component) owning a pure derivation.** `mergeLiveAndPrefs` (`rail.js:39-90`) is a pure function of `(windows, prefRepoOrder, prefWtByRepo, prefPanesByWt)` → tree. It ports **verbatim** as a module-level pure function in `Rail.jsx` (or a sibling `railTree.js` if it's unit-tested — see §6, but the spec says frontend is manual-smoke, so colocate). The component calls it inside render with `computed` inputs from `windows` + `prefsSignal`. This is the cleanest fit for rung 2 thinking — frozen-ish value-data (the live windows + pref arrays) through a pure transform — but expressed as a plain function, no dataclass ceremony in JS.

**`Detail.jsx` — rung 1, dispatch component.** The four-state switch (`detail.js:23`'s `show()`) becomes a render-time switch on a `railSelection`-derived state. The critical structural point: the **same-mount no-op** logic (`detail.js:59` `sameMount`, `detail.js:116`) must NOT be reimplemented in `Detail` — it's handled by `<Terminal>` keying (next). `Detail` just renders `<Terminal key={pid} target={...}/>` or the iframe; Preact's keyed reconciliation does the "don't remount same pid" job that `sameMount` did by hand. The LGTM iframe gets the same treatment: `<iframe key={worktreeKey} src={...}/>` — keying prevents the src reassignment that `detail.js:115` guards against manually.

**`Terminal.jsx` — rung 1, the imperative wrapper (the spec's central question, #3).** Structure:
- `terminal.js` and `terminal-mount.js` **stay as imperative modules, ported nearly verbatim.** They are the single-xterm-instance core; that invariant (`terminal-mount.js:6` "One xterm instance lives in the app at a time") is real state ownership and is exactly what should NOT be dissolved into the component tree. Do not absorb them.
- `<Terminal>` is a thin wrapper: `useRef` for the container div, `useEffect(() => { mountTerminal(ref.current, target, opts); return unmountTerminal; }, [])` — empty deps so it mounts/unmounts on the component's lifecycle.
- **Single-instance reuse + reconnect-not-remount (invariant #3)** is achieved by **keying `<Terminal>` on `pid` at the call site** (`<Terminal key={pid}/>`). Same pid across a poll → Preact keeps the same component instance → effect does not re-run → no remount → matches `detail.js`'s `sameMount` exactly. Different pid → Preact unmounts old (cleanup → `unmountTerminal`) and mounts new (`mountTerminal` → `startLiveTerminal` → server re-syncs initial paint per invariant #3). The reconnect+prefix path is preserved because it lives in `terminal.js:startLiveTerminal`, untouched.
- `opts` (onPaste → `handleModalImagePaste`, onMdLink) pass as props. `state.activeTarget` → `activeTarget` signal, set in the same effect.
- This is rung 1 because the component holds no state of its own; the state lives in the imperative module it wraps. A class component would buy nothing.

**`useEscape.js` — rung 1, a hook.** Ports `overlay.js`'s shared stack (the 24-line module). `useEscape(handler, active)` pushes/pops on mount/unmount-or-toggle. One shared module-level stack (same as today); the hook is the registration API. This is the one place a "hook" is the right shape — it's lifecycle-bound cross-cutting behavior.

**Secondary modals — rung 1, one function component each.** Drop `modal-shell.js` (the factory); Preact + `useEscape` replaces it. `Dialog.jsx` keeps the promise-returning API (`confirmDialog`/`promptDialog`/`alertDialog` return promises — `dialog.js:18,68,117`) by rendering a single imperatively-resolved dialog driven by a `dialogState` signal; the three exported functions set the signal and return a promise. That's the one modal where a signal+promise bridge is justified (callers `await` it from non-component code like `app.js:213`'s new-session flow).

### Backend

**`panes.py:list_windows` — change the format string only.** Add `\t#{window_activity}` to the `-F` string (`panes.py:258`) and parse `parts[7]` as an int. ~4 lines. No structural change; rung stays where it is (a function).

**`window_view.py` — the cache fast-path, rung 1 (module-level dict + functions, per §1c).**
```python
# module-level
_view_cache: dict[str, tuple[int, dict]] = {}   # pid -> (last_window_activity, view_dict)
```
`build_window_view(w, now_ts, *, last_activity, prev_activity)` gains a guard at the top: if `pid` has a cached entry AND `last_activity == prev_activity` (pane emitted nothing since last poll) AND a cached view exists → **skip `capture()` + `parse_pane()`**, but still run the cheap stamp/git/pr/lgtm assembly (those are already cached on their own clocks and are correct to refresh). Re-capture path is unchanged. Cache write on every capturing pass.

The function is already documented as deliberately impure (`window_view.py:6-14` — it mutates `_completed_at` / `_prev_state`). Adding `_view_cache` to its side-effect set is consistent with that; no class needed. Keyword-only `last_activity`/`prev_activity` per the multi-arg rule.

**Critical invariant-#1 guard, structurally enforced:** the activity value is a parameter named `last_activity` used *only* in the skip predicate. It is never passed to `record_state_transition`, `note_focus`, `note_action`, or written to `_focused_at`/`_acted_at`. Keep the parse-cached view's `focused_at`/`acted_at` fields refreshed from the live `recency_stamps_for(target)` + persisted stamps on *every* poll (not cached), so a skipped-capture pane still reports correct focus/acted recency. That is the structural line that keeps the capture-skip hint from leaking into focus — make it a one-line comment at the cache-skip branch citing invariant #1.

**`routes/state.py` — `ThreadPoolExecutor` fan-out, rung 1 (function, in place).** Build a `prev_activity` map from `_view_cache` (or pass the cache module a "begin poll" snapshot). Replace the sequential `for w in windows` loop (`state.py:37-41`) with:
```python
with ThreadPoolExecutor(max_workers=min(len(windows), N)) as ex:
    futures = [ex.submit(build_window_view, w, now_ts,
                         last_activity=acts[w_target(w)], prev_activity=...) for w in windows]
    pairs = [f.result() for f in futures]
```
preserving result order (`as futures list`, not `as_completed`, since the response order is the windows order). The handler stays `def` (sync) so it runs in Starlette's threadpool and the subprocess waits release the GIL — spec §Backend point 1, correct. Stamp batching (`state.py:48-51`) is unchanged; it runs after the join.

**Shared-mutable-state caution:** `build_window_view` mutates module dicts (`_completed_at`, `_prev_state`, `_spinner_last_seen`, `_view_cache`) from worker threads now. Each pane touches **its own pid/target keys**, so there's no key contention, but dict writes from multiple threads need the GIL to keep the dict itself consistent (CPython dict ops are atomic under the GIL — safe). Flag in §7; the structurally clean alternative is to have workers return their mutations and apply them single-threaded after the join, but that's a larger refactor than the spec's "trivial, no restructuring" framing wants. Recommend: keep per-key writes in-thread, document the assumption.

---

## 5. Patterns

**Used:**
- **Keyed reconciliation (key-on-pid)** — `<Card>`, rail rows, `<Terminal>`, LGTM iframe. Replaces every manual `sameMount` / `src`-reassignment guard. The single most load-bearing pattern in the port.
- **Signals as the store** — flat module of `signal()`/`computed()`. Direct import, no hook wrapper.
- **Cache-as-signal for prefs** — the §1a fix; makes durable state reactive without moving persistence out of `prefs.js`.
- **Ref + `useEffect` imperative wrapper** — `<Terminal>` over the unchanged xterm core. The blessed way to host an imperative widget in a component tree.
- **Hook for cross-cutting lifecycle** — `useEscape`.
- **`ThreadPoolExecutor` fan-out** — backend, per-request, ordered results.
- **Module-dict + functions cache** — backend view cache (rung 1).

**Considered and rejected:**
- **`createStore()` / store class** — rejected; a signals module is already the store. (rung-1 default)
- **`useStore()` subscription hook** — rejected; direct signal import is idiomatic and avoids indirection.
- **`<TabStrip>`/`<Sidebar>` reusable components** — rejected (§1b); one consumer, speculative seam, YAGNI.
- **`WindowViewCache` class** — rejected (§1c); singleton-as-class smell.
- **Custom store for toasts/alerts as classes** — rejected; signal-backed arrays.
- **`modal-shell.js` factory ported** — rejected; Preact + `useEscape` is the native replacement.
- **Segment/block extension seam in `<Detail>`** — rejected (spec non-goal, hard-YAGNI); detail ports the existing 4-state mount only.
- **Shared module-global thread pool** — rejected in favor of per-request (simpler ownership); revisit if pool churn shows in profiling (§7).

---

## 6. Test strategy

**Backend poll optimization — unit tests, real logic, faked `list_windows`/`capture`.** The spec is right and this is the testable core:
- `window_view.py` cache fast-path: a `test_window_view_cache.py` that fakes `capture()` (monkeypatch `periscope.window_view.capture`) and `recency_stamps_for`, then asserts: (a) unchanged `window_activity` → `capture` NOT called, cached view returned with **refreshed** focus/acted stamps; (b) advanced activity → `capture` called, cache updated; (c) no cached entry → always captures. This is unit-appropriate — `capture()` is a subprocess boundary that adds nothing real to the assertion, and the Q1-2026 mock-incident lesson doesn't bite here because the thing under test *is* the skip decision, not the subprocess. The one mock (`capture`) is the boundary, isolated, exactly as the rules allow.
- **Invariant #1 regression test (new, important):** assert that `_focused_at` / `_acted_at` are byte-for-byte unchanged across a poll where a pane's capture was skipped. This is the structural guard from §4 turned into a test — it's the test that would have caught the original invariant-#1 bug class if applied to this hint.
- `ThreadPoolExecutor` fan-out: a test asserting result **order** matches input window order (regression guard against `as_completed` creeping in) and that all panes' views are present. Fake `capture` with per-target sleeps to prove concurrency isn't reordering.
- `parse_pane` / `build_window_view` keep their existing tests verbatim (invariants #2, #7). `list_windows` format change: extend the existing `list_windows` parse test (if present) or add one asserting the new `window_activity` column parses to int and missing column defaults safely.

**Frontend — manual smoke, per the existing no-frontend-tests convention. Confirmed, with one push:** the convention holds (no framework, single-user tool, git-log-as-audit). But `mergeLiveAndPrefs` (rail tree derivation, `rail.js:39-90`) is pure, gnarly, and the one piece of frontend logic where a regression is both likely and invisible to a quick smoke. **Recommendation:** if it's split into a `railTree.js` pure module, it's trivially unit-testable under Node — but since the spec commits to manual-smoke and Vite gives no test runner today, I do **not** propose adding one. Colocate it in `Rail.jsx` and rely on smoke, matching the convention. Flag for Tom in §7 as the one frontend spot that's a coin-flip.

**Testability smells:** none introduced. The `<Terminal>` wrapper does NOT trap logic behind a hard-to-construct object — all the real terminal logic stays in `terminal.js` (already untested, manual-smoke today, unchanged). The store-as-signals keeps poll-apply logic in plain `poll.js` functions, directly callable.

---

## 7. Decisions to sanity-check

1. **Prefs cache becomes a signal (§1a).** Alternative: leave `prefs.js` as a plain cache and have components call getters, accepting that durable-state changes need a manual nudge to a transient signal to trigger re-render. Close because the spec *explicitly* says "prefs.js remains the persistence boundary" and a purist reading keeps it non-reactive — but I believe a non-reactive prefs cache silently breaks rail re-render on collapse, so I committed to the signal. Worth a yes/no from Tom because it's the one place I'm reshaping a module the spec named as staying put.

2. **`static/src/` vs keeping app files flat in `static/`.** Alternative: bundle in place, set Vite root to `static/`, output to `static/dist/`, and tolerate `.jsx` sources sitting beside served files (the StaticFiles mount would serve them but nobody requests them). Close because the flat layout is a smaller diff from today, but `src/` is cleaner given the StaticFiles mount serves all of `static/` publicly. Committed to `src/`.

3. **Per-request `ThreadPoolExecutor` and in-thread per-key dict mutation.** Alternative A: a shared module-global pool (avoids per-poll pool construction). Alternative B: workers return mutations, apply single-threaded after join (avoids any cross-thread dict writes). Close because the spec frames this as "trivial, no restructuring" and per-key writes are GIL-safe, but a reviewer could reasonably want the return-and-apply purity. Committed to per-request pool + in-thread per-key writes; documented the GIL assumption.

4. **All ~10 secondary modals ported in this milestone (§2 assumption).** Alternative: port only grid/split/modal surfaces named in the spec table and leave the secondary modals on the old vanilla path temporarily. Close because mixed-paradigm is explicitly rejected by the locked decisions — so they must port — but the spec's table under-lists them, so I'm flagging that the port is bigger than the table implies. Committed to porting all.

5. **`mergeLiveAndPrefs` stays colocated in `Rail.jsx`, not extracted + unit-tested.** Alternative: extract to `railTree.js` and add the first frontend unit test. Close because it's the one pure-and-gnarly frontend function where a test pays — but the no-frontend-tests convention is a locked-ish norm and there's no runner. Committed to colocate + smoke; raising it so Tom can overrule.

---

## Key file references

- Backend edit points: `periscope/routes/state.py:37-51`, `periscope/window_view.py:42-49`, `periscope/panes.py:258`.
- Invariant-#1 anchors (do not touch): `periscope/panes.py:27-49`, `periscope/panes.py:113-124`.
- Single-xterm reuse logic being replaced by keying: `static/detail.js:59,116`, `static/terminal-mount.js:27-55`.
- Prefs persistence boundary to preserve: `static/prefs.js:103-123,203-240`.
- Rail derivation to port verbatim: `static/rail.js:39-90`.
- Serving path that drives the `src/`-vs-flat call: `periscope/app.py:110`.

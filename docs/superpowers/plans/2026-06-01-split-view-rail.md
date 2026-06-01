# Split-view rail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `split` top-level view to periscope: a persistent 3-level left rail (Repo → Worktree → Pane children + auto-review row) and a persistent right detail pane. Becomes the new default view. Grid and stream views stay; the modal keeps serving them.

**Architecture:** Frontend-heavy. New modules `rail.js` / `detail.js` / `terminal-mount.js` / `launcher-modal.js` / `open-picker-modal.js` carry the new view. Existing `modal.js` migrates to use `terminal-mount.js` so the xterm lifecycle is shared. Backend touches are tiny: extend `cached_git_state` to expose `repo_key`/`repo_label` per pane, extend `UIPatch` schema to accept `"split"` view + new RailState keys. Status-dot rollup happens frontend-only — `/api/state` already carries all the per-window data needed.

**Tech Stack:** FastAPI (Python 3.13), pytest + pytest-mock, vanilla ES modules, xterm.js (vendored). No bundler. `uv run server.py` for prod, `npm run dev` for HMR.

**Spec:** `docs/superpowers/specs/2026-06-01-split-view-rail-design.md`

---

## File Structure

### New frontend files
- `static/rail.js` — left-rail rendering, drag/drop, selection state, two-level filter predicate, frontend status rollup
- `static/detail.js` — right-pane rendering: pane view, review-live view, review-empty (start CTA), empty state
- `static/terminal-mount.js` — thin wrapper around `terminal.js` that parameterizes the container element + paste handler + link-provider hook
- `static/open-picker-modal.js` — `+ open` picker, lists tmux sessions not already in the rail
- `static/launcher-modal.js` — per-worktree `+ New tab` launcher, reads `prefs.getCommands()` and POSTs to `/api/window/new`

### Modified frontend files
- `static/index.html` — DOM scaffolding (`#rail`, `#detail`, third view-toggle button)
- `static/app.js` — view switching with 3-way toggle; show/hide rail vs. grid vs. stream
- `static/grid.js` — hidden when split is active (purely DOM visibility, no behavior change)
- `static/stream.js` — same
- `static/modal.js` — calls `terminal-mount` instead of mounting xterm directly
- `static/terminal.js` — accept container element per `startLiveTerminal` call; replace hard import of `addLgtmDocFromTerminal` with a registerable hook
- `static/new-project-modal.js` — submit handler appends to rail prefs
- `static/review-pr-modal.js` — same
- `static/prefs.js` — new RailState getters/setters
- `static/state.js` — transient rail state (drag-in-progress, current selection)
- `static/styles.css` — new rail/detail CSS

### Modified backend files
- `periscope/git_pr.py` — `git_state_for()` returns `repo_key` + `repo_label`
- `periscope/routes/prefs.py` — `UIPatch` accepts new keys + `"split"` view
- `tests/test_git_pr.py` — covers new `repo_key`/`repo_label` fields
- `tests/routes/test_prefs.py` (new if missing — check first) — covers new UI patch keys + view validation

---

## Phase 1 — Backend prep

### Task 1.1: `git_state_for` returns `repo_key` and `repo_label`

**Files:**
- Modify: `periscope/git_pr.py` (function `git_state_for` around line 50)
- Modify: `tests/test_git_pr.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_git_pr.py`:

```python
def test_git_state_includes_repo_key_and_label(tmp_path, monkeypatch):
    """git_state_for() returns repo_key (full path) + repo_label (basename)."""
    import periscope.git_pr as gp
    # Stub _run to look like a clean repo at /tmp/foo.
    def fake_run(args, cwd=None, timeout=None):
        if "diff" in args and "--shortstat" in args and "--cached" not in args:
            return (0, "")  # no unstaged changes
        if "--cached" in args:
            return (0, "")  # no staged changes
        if "rev-parse" in args and "--abbrev-ref" in args:
            return (0, "main")
        if "rev-list" in args:
            return (0, "0")
        if "remote" in args and "get-url" in args:
            return (1, "")  # no github slug
        if "rev-parse" in args and "--git-common-dir" in args:
            return (0, "/tmp/foo/.git")
        return (0, "")
    monkeypatch.setattr("periscope.git_pr._run", fake_run)
    monkeypatch.setattr("periscope.gitutil._run", fake_run)
    out = gp.git_state_for("/tmp/foo/branch-a")
    assert out["repo_key"] == "/tmp/foo"
    assert out["repo_label"] == "foo"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_git_pr.py::test_git_state_includes_repo_key_and_label -v`
Expected: FAIL — `KeyError: 'repo_key'` or similar.

- [ ] **Step 3: Update `git_state_for`**

In `periscope/git_pr.py`, modify the `git_state_for` function. Find the return statement (around line 66):

```python
    return {"branch": branch, "git": state, "repo_slug": github_slug(path)}
```

Replace with:

```python
    # repo_key is the full repo path (handles both sibling and inline
    # worktree layouts via gitutil.resolve_repo). repo_label is the
    # basename for human-readable display in the rail.
    from periscope.gitutil import resolve_repo
    repo_key = resolve_repo(path)
    repo_label = os.path.basename(repo_key.rstrip("/")) if repo_key else ""
    return {
        "branch": branch,
        "git": state,
        "repo_slug": github_slug(path),
        "repo_key": repo_key,
        "repo_label": repo_label,
    }
```

Add `import os` near the top of `git_pr.py` if not already present (it is — check `import os` on line 1 or near top).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_git_pr.py::test_git_state_includes_repo_key_and_label -v`
Expected: PASS.

- [ ] **Step 5: Run full git_pr test module**

Run: `uv run pytest tests/test_git_pr.py -q`
Expected: All existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add periscope/git_pr.py tests/test_git_pr.py
git commit -m "git_pr: expose repo_key + repo_label on cached_git_state for rail grouping"
```

---

### Task 1.2: `UIPatch` accepts `"split"` view + new RailState keys

**Files:**
- Modify: `periscope/routes/prefs.py`
- Modify: `tests/routes/test_prefs.py` (check if exists; create otherwise — it doesn't today)

- [ ] **Step 1: Check whether tests/routes/test_prefs.py exists**

Run: `ls tests/routes/test_prefs.py 2>/dev/null || echo "MISSING"`

If MISSING, the file needs to be created with a header matching peer route tests.

- [ ] **Step 2: Write the failing test**

Append to (or create) `tests/routes/test_prefs.py`:

```python
"""Tests for /api/prefs/ui — UI patch validation and round-trip."""

from fastapi.testclient import TestClient

from periscope.app import app


def test_ui_patch_accepts_split_view(clean_state):
    client = TestClient(app)
    r = client.patch("/api/prefs/ui", json={"view": "split"})
    assert r.status_code == 200, r.text
    assert r.json()["ui"]["view"] == "split"


def test_ui_patch_rejects_unknown_view(clean_state):
    client = TestClient(app)
    r = client.patch("/api/prefs/ui", json={"view": "kanban"})
    assert r.status_code == 400


def test_ui_patch_accepts_rail_state_keys(clean_state):
    client = TestClient(app)
    body = {
        "repo_order": ["/home/tom/dev/foo"],
        "worktrees_by_repo": {"/home/tom/dev/foo": ["session-a"]},
        "panes_by_worktree": {"session-a": ["pane:abc", "review"]},
        "rail_collapsed": {"repo:/home/tom/dev/foo": False},
        "last_selected": {"kind": "pane", "pid": "abc"},
    }
    r = client.patch("/api/prefs/ui", json=body)
    assert r.status_code == 200, r.text
    ui = r.json()["ui"]
    for key in body:
        assert ui[key] == body[key]
```

If creating the file, also create an `__init__.py` next to it (the tests/routes/ dir already has one — confirm).

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/routes/test_prefs.py -v`
Expected: All three FAIL — Pydantic rejects the new keys, view validator rejects "split".

- [ ] **Step 4: Extend `UIPatch` and view validator**

In `periscope/routes/prefs.py`, replace the `UIPatch` class (lines 32-36):

```python
class UIPatch(BaseModel):
    session_order: list[str] | None = None
    collapsed_sessions: list[str] | None = None
    view: str | None = None  # "grid" | "stream" | "split"
    alerts_open: bool | None = None  # right-rail alerts feed visibility
    # Rail (split view) state — opaque dicts merged through update_ui's
    # generic merge. The schema is documented in
    # docs/superpowers/specs/2026-06-01-split-view-rail-design.md §Data model.
    repo_order: list[str] | None = None
    worktrees_by_repo: dict[str, list[str]] | None = None
    panes_by_worktree: dict[str, list[str]] | None = None
    rail_collapsed: dict[str, bool] | None = None
    last_selected: dict | None = None
```

Update the view validator (around line 44):

```python
    if "view" in patch and patch["view"] not in ("grid", "stream", "split"):
        raise HTTPException(400, f"invalid view: {patch['view']!r}")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/routes/test_prefs.py -v`
Expected: All three PASS.

- [ ] **Step 6: Run full route test suite as smoke check**

Run: `uv run pytest tests/routes/ -q`
Expected: No regressions.

- [ ] **Step 7: Commit**

```bash
git add periscope/routes/prefs.py tests/routes/test_prefs.py
git commit -m "prefs: accept split view + rail state keys in UIPatch"
```

---

## Phase 2 — Terminal-mount refactor

This phase decouples the xterm from `#modal-xterm` and `modal.js` imports so the same lifecycle works for both modal and split-view-detail mounts. Verify by eye — modal must still work after this phase.

### Task 2.1: Parameterize `terminal.js` container + register link-provider hook

**Files:**
- Modify: `static/terminal.js`
- Modify: `static/modal.js` (call sites — pass container, register hook)

- [ ] **Step 1: Replace the module-level container lookup**

In `static/terminal.js`, remove line 15:

```js
const modalXtermEl = document.getElementById("modal-xterm");
```

Add module-level state for the active container + the registered link callback. Near the other `let` declarations (around lines 17-26), add:

```js
let containerEl = null;          // set by setTerminalContainer() before startLiveTerminal()
let linkClickCallback = null;    // set by setTerminalLinkCallback()
```

Add two new exported setters after the imports:

```js
// Mount target for the live xterm. Must be called before startLiveTerminal().
// Consumers: modal.js (passes #modal-xterm) and detail.js (passes #detail-xterm).
export function setTerminalContainer(el) {
  containerEl = el;
}

// Register a callback invoked when an .md link in the terminal is clicked.
// Replaces the previous hard import of addLgtmDocFromTerminal from modal.js.
// Callback signature: (mdPath: string, lineNumber: number | null) => void
export function setTerminalLinkCallback(cb) {
  linkClickCallback = cb;
}
```

Find every reference to `modalXtermEl` in the file and replace with `containerEl`. Then remove the line `import { addLgtmDocFromTerminal } from './modal.js';` and find every call to `addLgtmDocFromTerminal(...)` — replace with:

```js
if (linkClickCallback) linkClickCallback(/* same args as before */);
```

Check usages by grep before editing — do `grep -n "addLgtmDocFromTerminal\|modalXtermEl" /Users/tom/dev/periscope/static/terminal.js` to enumerate.

- [ ] **Step 2: Update `modal.js` to call the new setters**

In `static/modal.js`, find where the modal opens a pane terminal (search for `startLiveTerminal`). Before calling `startLiveTerminal(target)`, add:

```js
import { setTerminalContainer, setTerminalLinkCallback, startLiveTerminal, stopLiveTerminal } from './terminal.js';

// In the modal's open-terminal code path:
setTerminalContainer(document.getElementById("modal-xterm"));
setTerminalLinkCallback((mdPath, lineNumber) => addLgtmDocFromTerminal(mdPath, lineNumber));
startLiveTerminal(target);
```

Adjust imports at top of `modal.js` to ensure `startLiveTerminal` and `stopLiveTerminal` are imported from `terminal.js` (they likely are already; reuse the existing import).

- [ ] **Step 3: Manual verify**

Run: `npm run dev` (uses port 8766 via vite proxy to a dev periscope).

Open `http://localhost:5174/`, click a Claude pane card to open the modal. Confirm:
- Terminal mirrors the live tmux pane (same content as the tmux window).
- Typing in the terminal sends keys (try a benign `echo hi`).
- Closing + reopening the modal works (no broken state on re-mount).
- An `.md` path link in the terminal still triggers the LGTM doc handler (find a pane that has rendered an md path — Claude often outputs them; or just confirm no JS errors in console on click).

If broken: don't proceed. Roll back, diagnose, repeat.

- [ ] **Step 4: Commit**

```bash
git add static/terminal.js static/modal.js
git commit -m "terminal: parameterize container element + link callback for split-view reuse"
```

---

### Task 2.2: Create `terminal-mount.js`

**Files:**
- Create: `static/terminal-mount.js`
- Modify: `static/modal.js` (replace direct `terminal.js` calls with `terminal-mount.js`)

- [ ] **Step 1: Create `terminal-mount.js`**

```js
// Shared mounting helper for the live xterm. Wraps terminal.js so callers
// (modal.js, detail.js) don't have to repeat the
// setTerminalContainer + setTerminalLinkCallback + paste-handler dance.
//
// One xterm instance lives in the app at a time (terminal.js's invariant).
// mount() retargets it onto a new container; unmount() tears it down.

import {
  setTerminalContainer,
  setTerminalLinkCallback,
  startLiveTerminal,
  stopLiveTerminal,
} from './terminal.js';

let activePasteHandler = null;
let activeContainer = null;

/**
 * Mount the live terminal for `target` into `container`.
 * @param {HTMLElement} container
 * @param {string} target — tmux target spec (e.g. "session:0.0")
 * @param {Object} opts
 * @param {(mdPath: string, lineNumber: number | null) => void} [opts.onMdLink]
 * @param {(event: ClipboardEvent) => void} [opts.onPaste] — capture-phase paste hook
 */
export function mountTerminal(container, target, opts = {}) {
  unmountTerminal();  // tear down any previous mount
  setTerminalContainer(container);
  setTerminalLinkCallback(opts.onMdLink || null);
  if (opts.onPaste) {
    activePasteHandler = opts.onPaste;
    container.addEventListener("paste", activePasteHandler, true);
  }
  activeContainer = container;
  startLiveTerminal(target);
}

export function unmountTerminal() {
  stopLiveTerminal();
  if (activeContainer && activePasteHandler) {
    activeContainer.removeEventListener("paste", activePasteHandler, true);
  }
  activePasteHandler = null;
  activeContainer = null;
  setTerminalContainer(null);
  setTerminalLinkCallback(null);
}
```

- [ ] **Step 2: Update `modal.js` to use `terminal-mount`**

In `modal.js`, replace the direct `setTerminalContainer + setTerminalLinkCallback + startLiveTerminal` sequence introduced in Task 2.1 with a single `mountTerminal` call. Also move any existing paste-handler `addEventListener("paste", ..., true)` line on `#modal-xterm` into the `onPaste` opts (so it follows the lifecycle).

Concretely: find the open-terminal code path, replace with:

```js
import { mountTerminal, unmountTerminal } from './terminal-mount.js';

// open:
mountTerminal(
  document.getElementById("modal-xterm"),
  target,
  {
    onMdLink: (mdPath, lineNumber) => addLgtmDocFromTerminal(mdPath, lineNumber),
    onPaste: handleModalImagePaste,  // existing function in modal.js
  }
);

// close:
unmountTerminal();
```

Look up the existing paste handler — search `modal.js` for `addEventListener("paste"` — and refactor it to a named function `handleModalImagePaste` if it's currently inline. Otherwise pass it by reference.

- [ ] **Step 3: Manual verify (same checks as Task 2.1)**

Run: `npm run dev`. Open modal on a Claude pane.
- Terminal mirrors live.
- Typing sends keys.
- Image paste still works (the existing capture-phase handler in modal.js is now attached via `mountTerminal(..., {onPaste})`).
- Md-link click still triggers LGTM handler.

- [ ] **Step 4: Commit**

```bash
git add static/terminal-mount.js static/modal.js
git commit -m "terminal-mount: shared lifecycle helper, modal.js migrated"
```

---

## Phase 3 — Rail prefs + state plumbing

### Task 3.1: Extend `prefs.js` with RailState getters/setters

**Files:**
- Modify: `static/prefs.js`

- [ ] **Step 1: Inspect the existing prefs.js to find the patch-helper pattern**

Run: `grep -n "patch\|update_ui\|/api/prefs/ui\|export function" /Users/tom/dev/periscope/static/prefs.js | head -30`

Identify the existing PATCH helper (something like `patchUI(body)` or inline `apiCall('/api/prefs/ui', 'PATCH', ...)`).

- [ ] **Step 2: Add rail-state getters and setters**

Append to `static/prefs.js`:

```js
// --- Rail state (split view) -----------------------------------------------
// All five fields default to empty / null when the prefs blob hasn't seen
// them yet. Mutators write through the existing PATCH /api/prefs/ui endpoint.

export function getRepoOrder() {
  return [...(prefs.ui?.repo_order || [])];
}

export function setRepoOrder(order) {
  return patchUI({ repo_order: order });
}

export function getWorktreesByRepo() {
  return { ...(prefs.ui?.worktrees_by_repo || {}) };
}

export function setWorktreesByRepo(map) {
  return patchUI({ worktrees_by_repo: map });
}

export function getPanesByWorktree() {
  return { ...(prefs.ui?.panes_by_worktree || {}) };
}

export function setPanesByWorktree(map) {
  return patchUI({ panes_by_worktree: map });
}

export function getRailCollapsed() {
  return { ...(prefs.ui?.rail_collapsed || {}) };
}

export function setRailCollapsedKey(key, collapsed) {
  const next = getRailCollapsed();
  next[key] = collapsed;
  return patchUI({ rail_collapsed: next });
}

export function getLastSelected() {
  return prefs.ui?.last_selected || null;
}

export function setLastSelected(sel) {
  return patchUI({ last_selected: sel });
}

// Add a worktree to the rail. If its repo isn't railed yet, append to
// repo_order. Idempotent — re-adding the same worktree is a no-op.
//
// Used by + project / + review PR / + open flows.
export async function addWorktreeToRail({ repoKey, worktreeKey, paneIds, hasReview }) {
  const order = getRepoOrder();
  const wts = getWorktreesByRepo();
  const panes = getPanesByWorktree();

  if (!order.includes(repoKey)) order.push(repoKey);
  const wtList = wts[repoKey] || [];
  if (!wtList.includes(worktreeKey)) wtList.push(worktreeKey);
  wts[repoKey] = wtList;

  if (!panes[worktreeKey]) {
    panes[worktreeKey] = [...paneIds];
    if (hasReview) panes[worktreeKey].push("review");
  }

  await patchUI({
    repo_order: order,
    worktrees_by_repo: wts,
    panes_by_worktree: panes,
  });
}

export async function removeWorktreeFromRail({ repoKey, worktreeKey }) {
  const wts = getWorktreesByRepo();
  const panes = getPanesByWorktree();
  const order = getRepoOrder();

  if (wts[repoKey]) {
    wts[repoKey] = wts[repoKey].filter(w => w !== worktreeKey);
    if (wts[repoKey].length === 0) {
      delete wts[repoKey];
      const idx = order.indexOf(repoKey);
      if (idx >= 0) order.splice(idx, 1);
    }
  }
  delete panes[worktreeKey];

  await patchUI({
    repo_order: order,
    worktrees_by_repo: wts,
    panes_by_worktree: panes,
  });
}
```

The reference to `patchUI` must exist (or be added). If `prefs.js` already exposes a private patch helper, use it; otherwise add at the top of the new section:

```js
async function patchUI(body) {
  await apiCall('/api/prefs/ui', { method: 'PATCH', body });
  // Re-fetch full prefs to refresh the in-memory `prefs` cache.
  Object.assign(prefs, await apiCall('/api/prefs'));
}
```

Match whatever helper pattern is already in `prefs.js`. Don't duplicate.

- [ ] **Step 3: Verify no JS errors**

Run: `npm run dev`. Open browser console. Reload `http://localhost:5174/`.
Expected: no console errors. The new getters/setters aren't called yet, just defined.

- [ ] **Step 4: Commit**

```bash
git add static/prefs.js
git commit -m "prefs: rail state getters + addWorktreeToRail / removeWorktreeFromRail"
```

---

### Task 3.2: Add rail transient state to `state.js`

**Files:**
- Modify: `static/state.js`

- [ ] **Step 1: Read state.js to confirm shape**

Run: `cat /Users/tom/dev/periscope/static/state.js`

- [ ] **Step 2: Add rail-specific transient fields**

Append fields to the existing `state` object literal:

```js
export const state = {
  // ... existing fields ...

  // Rail (split-view) transient state — not persisted; lost on reload.
  railDragging: null,             // { kind: "repo"|"worktree"|"child", key: string }
  railSelected: null,             // mirror of prefs.last_selected for fast read
};
```

The exact placement depends on the existing shape — preserve the existing comment style.

- [ ] **Step 3: Commit**

```bash
git add static/state.js
git commit -m "state: rail transient fields (drag, selected)"
```

---

## Phase 4 — DOM scaffolding + view switch

### Task 4.1: `index.html` adds `#rail`, `#detail`, third view-toggle button

**Files:**
- Modify: `static/index.html`
- Modify: `static/styles.css` (minimal layout scaffolding only — polish in Phase 12)

- [ ] **Step 1: Add the split view toggle button**

In `static/index.html`, find the view-switch block:

```html
<div class="view-switch" id="view-switch" role="tablist" aria-label="view">
  <button class="view-switch-btn" data-view="grid" role="tab" aria-selected="true" title="grid view (Tab to toggle)">▦ grid</button>
  <button class="view-switch-btn" data-view="stream" role="tab" aria-selected="false" title="stream view — tabs you've opened, by recency (Tab to toggle)">≡ stream</button>
</div>
```

Insert a new button before the grid button (split is now the default):

```html
<div class="view-switch" id="view-switch" role="tablist" aria-label="view">
  <button class="view-switch-btn" data-view="split" role="tab" aria-selected="true" title="split view — curated rail + persistent detail (Tab to toggle)">▤ split</button>
  <button class="view-switch-btn" data-view="grid" role="tab" aria-selected="false" title="grid view (Tab to toggle)">▦ grid</button>
  <button class="view-switch-btn" data-view="stream" role="tab" aria-selected="false" title="stream view — tabs you've opened, by recency (Tab to toggle)">≡ stream</button>
</div>
```

- [ ] **Step 2: Add the split-view DOM containers**

In `static/index.html`, find `<main id="grid"></main>`. Add a sibling element below it:

```html
<main id="grid"></main>
<div id="split-view" class="hidden">
  <aside id="rail" aria-label="projects rail"></aside>
  <section id="detail">
    <div id="detail-empty" class="detail-empty"></div>
    <div id="detail-pane" class="detail-pane hidden">
      <header id="detail-pane-header" class="detail-pane-header"></header>
      <div class="detail-pane-body">
        <div id="detail-xterm" class="detail-xterm"></div>
        <aside id="detail-side" class="detail-side"></aside>
      </div>
    </div>
    <div id="detail-review" class="detail-review hidden">
      <header id="detail-review-header" class="detail-review-header"></header>
      <iframe id="detail-review-iframe" class="detail-review-iframe"></iframe>
    </div>
    <div id="detail-review-start" class="detail-review-start hidden"></div>
  </section>
</div>
```

- [ ] **Step 3: Add minimal layout CSS to `styles.css`**

Append to `static/styles.css`:

```css
/* --- Split view: structural layout (polish in Phase 12) ----------------- */

#split-view {
  display: grid;
  grid-template-columns: 320px 1fr;
  height: calc(100vh - var(--header-height, 56px));
}

#split-view.hidden { display: none; }

#rail {
  overflow-y: auto;
  border-right: 1px solid var(--border, #333);
  background: var(--bg-1, #1c1c1f);
}

#detail {
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
}

#detail-empty, #detail-pane, #detail-review, #detail-review-start {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.detail-pane { display: flex; flex-direction: column; }
.detail-pane-body { display: grid; grid-template-columns: 1fr 240px; flex: 1; min-height: 0; }
.detail-xterm { background: #0a0a0a; min-height: 0; overflow: hidden; }
.detail-side { border-left: 1px solid var(--border, #333); overflow-y: auto; padding: 8px 10px; font-size: 12px; }
.detail-review { display: flex; flex-direction: column; }
.detail-review-iframe { flex: 1; border: 0; min-height: 0; }
.detail-empty { display: flex; align-items: center; justify-content: center; color: var(--muted, #888); }
.detail-review-start { display: flex; align-items: center; justify-content: center; }

.hidden { display: none !important; }
```

Note `--header-height` may not exist — if not, replace with the actual header height value (inspect existing CSS first; the header is `.periscope-header`).

- [ ] **Step 4: Manual verify**

Run: `npm run dev`. Reload. Confirm:
- A third "▤ split" button appears in the view toggle, left of "▦ grid" and "≡ stream".
- The page still renders the existing grid view (because split-view starts `hidden`).
- No console errors.

- [ ] **Step 5: Commit**

```bash
git add static/index.html static/styles.css
git commit -m "split-view: DOM scaffolding + view toggle button"
```

---

### Task 4.2: `app.js` switches between three views

**Files:**
- Modify: `static/app.js`

- [ ] **Step 1: Inspect existing view-switch wiring**

Run: `grep -n "view-switch\|data-view\|prefs.getView\|setView\|grid\|stream" /Users/tom/dev/periscope/static/app.js | head -25`

Identify the existing view-switch handler (looks like a click delegate on `.view-switch-btn`).

- [ ] **Step 2: Make view switching three-way**

In `static/app.js`, find the view-switch click handler. The existing pattern (paraphrased) probably looks like:

```js
const view = btn.dataset.view;  // "grid" | "stream"
prefs.setView(view);
applyView(view);
```

Extend `applyView()` (or its inline equivalent) to handle `"split"`. Replace whatever shows/hides `#grid` with logic that also shows/hides `#split-view`:

```js
function applyView(view) {
  const grid = document.getElementById("grid");
  const split = document.getElementById("split-view");

  grid.classList.toggle("hidden", view !== "grid" && view !== "stream");
  split.classList.toggle("hidden", view !== "split");

  // Update the aria-selected on toggle buttons.
  document.querySelectorAll(".view-switch-btn").forEach(b => {
    b.setAttribute("aria-selected", String(b.dataset.view === view));
  });

  // Stream view rebrands #grid container — preserve existing stream wiring.
  if (view === "stream") {
    // grid.js's renderStream handler reuses #grid container — let render() pick it up.
  }

  if (view === "split") {
    // Defer to rail/detail modules (added in Phase 5/6).
    import('./rail.js').then(m => m.renderRail());
    import('./detail.js').then(m => m.refreshDetail());
  }
}
```

Adjust the import-strategy if `app.js` already eagerly imports modules — use a static import in that case. Pattern-match the existing style.

- [ ] **Step 3: Update the Tab keybinding to cycle through three views**

Find the existing Tab handler (search `keydown\|Tab`). Update the next-view function:

```js
function nextView(current) {
  const order = ["split", "grid", "stream"];
  const i = order.indexOf(current);
  return order[(i + 1) % order.length];
}
```

- [ ] **Step 4: Set split as default on cold load**

In the bootstrap path of `app.js` (or wherever the initial view is read from prefs), change the default fallback. If today it reads:

```js
const view = prefs.getView() || "grid";
```

Change to:

```js
const view = prefs.getView() || "split";
```

- [ ] **Step 5: Manual verify**

Run: `npm run dev`. Reload `http://localhost:5174/`. Confirm:
- Cold-load shows the split view (empty rail; right pane will be unset — that's fine for now, Phase 5/6 handles it).
- Clicking "▦ grid" switches to grid view; grid renders as before.
- Clicking "≡ stream" switches to stream; stream renders as before.
- Tab key cycles through split → grid → stream → split.
- Reload after switching to grid — grid persists.

The split-view containers will look broken in this phase (empty/black). That's expected; Phase 5 starts filling them.

- [ ] **Step 6: Commit**

```bash
git add static/app.js
git commit -m "app: 3-way view switch with split as default"
```

---

## Phase 5 — Rail read-only rendering

### Task 5.1: Create `rail.js` skeleton with `renderRail()` derived from prefs

**Files:**
- Create: `static/rail.js`
- Verify: imports in `app.js` resolve

- [ ] **Step 1: Create `rail.js` with derivation + rendering**

Create `static/rail.js`:

```js
// Left rail for split view: derives the Repo → Worktree → Pane-children
// tree from prefs (curated membership/order) joined with /api/state
// (live status). Renders into #rail.
//
// rail.js only renders. Interactions (collapse, drag, select) are wired
// in later tasks but live in this file.

import { state } from './state.js';
import * as prefs from './prefs.js';
import { escapeHtml } from './util.js';

const railEl = () => document.getElementById("rail");

// Severity ranking for status rollup: higher index = higher priority.
const SEVERITY = ["shell", "idle", "done", "working", "needs-input"];

function maxSeverity(states) {
  let best = -1;
  for (const s of states) {
    const i = SEVERITY.indexOf(s);
    if (i > best) best = i;
  }
  return best >= 0 ? SEVERITY[best] : "shell";
}

// Build a quick { worktreeKey: [windowObj, ...] } map from /api/state.
// state.lastState is the most recent /api/state poll result, populated
// by grid.js's poll() — we read it here without re-fetching.
function indexWindowsByWorktree(lastState) {
  const out = {};
  for (const w of (lastState?.windows || [])) {
    const key = w.session;  // worktree_key = session name
    (out[key] = out[key] || []).push(w);
  }
  return out;
}

// Look up the human-readable repo label for a repo_key. We pull it off
// any window whose `repo_key` matches; falls back to basename of the
// repo_key path.
function repoLabelFor(repoKey, lastState) {
  for (const w of (lastState?.windows || [])) {
    if (w.repo_key === repoKey && w.repo_label) return w.repo_label;
  }
  const parts = String(repoKey || "").split("/").filter(Boolean);
  return parts[parts.length - 1] || repoKey;
}

function statusDotClass(s) {
  if (s === "needs-input") return "dot dot-alert dot-pulse";
  if (s === "working") return "dot dot-green";
  if (s === "done") return "dot dot-blue";
  if (s === "idle") return "dot dot-grey";
  return "dot dot-none";
}

function paneRow(w, selectedKey) {
  const k = `pane:${w.pid}`;
  const sel = k === selectedKey ? " selected" : "";
  return `
    <div class="rail-row child-row${sel}" data-row="pane" data-pid="${escapeHtml(w.pid)}" data-key="${escapeHtml(k)}">
      <span class="rail-conn">├</span>
      <span class="rail-icon icon-pane">✦</span>
      <span class="rail-label">${escapeHtml(w.name || "claude")}</span>
      <span class="${statusDotClass(w.state)}"></span>
    </div>`;
}

function reviewRow(worktreeKey, lgtmLive, selectedKey) {
  const k = `review:${worktreeKey}`;
  const sel = k === selectedKey ? " selected" : "";
  const empty = lgtmLive ? "" : " review-empty";
  return `
    <div class="rail-row child-row${sel}${empty}" data-row="review" data-worktree="${escapeHtml(worktreeKey)}" data-key="${escapeHtml(k)}">
      <span class="rail-conn">├</span>
      <span class="rail-icon icon-review">👁</span>
      <span class="rail-label">review${lgtmLive ? "" : " <em>start →</em>"}</span>
    </div>`;
}

function worktreeRow(worktreeKey, children, collapsed, rolledUp, label) {
  const chev = collapsed ? "▸" : "▾";
  const childCountChip = collapsed && children.length > 0
    ? `<span class="rail-count">${children.length}</span>`
    : "";
  const body = collapsed ? "" : children.join("");
  return `
    <div class="rail-row wt-row" data-row="worktree" data-key="${escapeHtml(`wt:${worktreeKey}`)}">
      <span class="rail-chev">${chev}</span>
      <span class="rail-icon icon-worktree">⎇</span>
      <span class="rail-label"><b>${escapeHtml(label)}</b></span>
      ${childCountChip}
      <span class="${statusDotClass(rolledUp)}"></span>
    </div>
    ${body}
  `;
}

function repoRow(repoKey, label, worktreeBlocks, collapsed, rolledUp) {
  const chev = collapsed ? "▸" : "▾";
  const body = collapsed ? "" : worktreeBlocks.join("");
  return `
    <div class="rail-row repo-row" data-row="repo" data-key="${escapeHtml(`repo:${repoKey}`)}">
      <span class="rail-chev">${chev}</span>
      <span class="rail-icon icon-repo">📚</span>
      <span class="rail-label"><b>${escapeHtml(label)}</b></span>
      <span class="${statusDotClass(rolledUp)}"></span>
    </div>
    ${body}
  `;
}

export function renderRail() {
  const el = railEl();
  if (!el) return;

  const repoOrder = prefs.getRepoOrder();
  const worktreesByRepo = prefs.getWorktreesByRepo();
  const panesByWorktree = prefs.getPanesByWorktree();
  const collapsed = prefs.getRailCollapsed();
  const selectedKey = (() => {
    const sel = prefs.getLastSelected();
    if (!sel) return null;
    if (sel.kind === "pane") return `pane:${sel.pid}`;
    if (sel.kind === "review") return `review:${sel.worktree}`;
    return null;
  })();
  const byWorktree = indexWindowsByWorktree(state.lastState);

  if (repoOrder.length === 0) {
    el.innerHTML = `
      <div class="rail-head">
        <span>Projects</span>
        <button class="rail-add" id="rail-add">+</button>
      </div>
      <div class="rail-empty">
        Empty. Use <code>+ project</code>, <code>review PR</code>, or <code>+ open</code> to add a worktree.
      </div>`;
    return;
  }

  const blocks = repoOrder.map(repoKey => {
    const repoLabel = repoLabelFor(repoKey, state.lastState);
    const worktrees = worktreesByRepo[repoKey] || [];
    const wtCollapsed = collapsed[`repo:${repoKey}`] === true;
    const wtBlocks = worktrees.map(wtKey => {
      const childOrder = panesByWorktree[wtKey] || [];
      const wtWindows = byWorktree[wtKey] || [];
      // Resolve the live window for each pane child.
      const windowsByPid = Object.fromEntries(wtWindows.map(w => [w.pid, w]));
      const childMarkup = [];
      const childStates = [];
      for (const child of childOrder) {
        if (child === "review") {
          const lgtmLive = wtWindows.some(w => w.lgtm && w.lgtm.session_slug);
          childMarkup.push(reviewRow(wtKey, lgtmLive, selectedKey));
          // Review row doesn't roll up into the worktree dot.
        } else {
          const w = windowsByPid[child];
          if (!w) continue;  // pane gone; skip silently (Phase 11 prunes)
          childMarkup.push(paneRow(w, selectedKey));
          childStates.push(w.state || "shell");
        }
      }
      const wtIsCollapsed = collapsed[`wt:${wtKey}`] === true;
      const rolledUp = maxSeverity(childStates);
      // Label: branch from any window in this worktree.
      const label = (wtWindows[0]?.branch) || wtKey;
      return worktreeRow(wtKey, childMarkup, wtIsCollapsed, rolledUp, label);
    });
    // Repo rollup = max across worktree rollups.
    const allChildStates = (worktrees.flatMap(wt => (byWorktree[wt] || []).map(w => w.state || "shell")));
    const repoRolledUp = maxSeverity(allChildStates);
    return repoRow(repoKey, repoLabel, wtBlocks, wtCollapsed, repoRolledUp);
  });

  el.innerHTML = `
    <div class="rail-head">
      <span>Projects</span>
      <button class="rail-add" id="rail-add">+</button>
    </div>
    ${blocks.join("")}
  `;
}
```

- [ ] **Step 2: Verify `state.lastState` is populated**

`grid.js`'s `poll()` populates state with the last `/api/state` result. Confirm:

Run: `grep -n "lastState\|state\.windows" /Users/tom/dev/periscope/static/grid.js /Users/tom/dev/periscope/static/state.js | head -10`

If `state.lastState` doesn't exist by that exact name, find the field that does (might be `state.windows` directly). Update `rail.js`'s `indexWindowsByWorktree(state.lastState)` to use the right field.

- [ ] **Step 3: Add minimal rail row styles to `styles.css`**

Append:

```css
.rail-head {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 10px;
  font-size: 11px; text-transform: uppercase; letter-spacing: .5px;
  color: var(--muted, #888);
  border-bottom: 1px solid var(--border, #2a2a2a);
}
.rail-head .rail-add {
  margin-left: auto; background: transparent; border: 1px solid var(--border, #333);
  color: var(--muted, #888); padding: 1px 7px; border-radius: 3px; font-size: 13px; cursor: pointer;
}
.rail-empty { padding: 14px; font-size: 12px; color: var(--muted, #888); }

.rail-row {
  display: flex; align-items: center; gap: 7px;
  padding: 7px 10px; font-size: 13px; position: relative;
  border-left: 2px solid transparent;
}
.rail-row.selected { background: rgba(255,255,255,.04); border-left-color: #e89243; }
.rail-row .rail-chev { width: 10px; opacity: .5; font-size: 9px; }
.rail-row .rail-icon { width: 14px; opacity: .85; text-align: center; }
.rail-row .rail-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rail-row .rail-count { font-size: 10px; opacity: .55; padding: 0 4px; background: #333; border-radius: 2px; }
.rail-row .rail-conn { width: 14px; color: #444; font-size: 11px; text-align: center; }
.rail-row.child-row { padding-left: 30px; }
.rail-row.review-empty .rail-label em { font-style: italic; opacity: .55; font-size: 11px; }

.dot { width: 7px; height: 7px; border-radius: 50%; }
.dot-green { background: #3a7; }
.dot-blue { background: #58a; }
.dot-grey { background: #444; }
.dot-alert { background: #c44; }
.dot-none { background: transparent; }
.dot-pulse { animation: rail-pulse 1.6s ease-in-out infinite; }
@keyframes rail-pulse { 50% { box-shadow: 0 0 0 4px rgba(204,68,68,.15); } }
```

- [ ] **Step 4: Manual verify (empty state)**

Run: `npm run dev`. Reload. Switch to split view (should be default). Confirm:
- "Projects" header + `+` button visible.
- Empty state copy below.
- No console errors.

- [ ] **Step 5: Manual verify (populated state)**

In the browser console, inject some rail state directly to test rendering. Run:

```js
await fetch("/api/prefs/ui", {
  method: "PATCH",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({
    repo_order: ["/Users/tom/dev/periscope"],
    worktrees_by_repo: {"/Users/tom/dev/periscope": ["periscope"]},
    panes_by_worktree: {"periscope": ["review"]}
  })
});
location.reload();
```

(Replace "periscope" with whatever tmux session name exists on the test machine.)

Expected: rail renders one repo row "periscope" with one worktree row containing one "review" child row showing `start →` (no LGTM session for that worktree yet).

Clear the test state:

```js
await fetch("/api/prefs/ui", {
  method: "PATCH",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({ repo_order: [], worktrees_by_repo: {}, panes_by_worktree: {} })
});
location.reload();
```

- [ ] **Step 6: Commit**

```bash
git add static/rail.js static/styles.css
git commit -m "rail: read-only rendering of repos / worktrees / children with status dots"
```

---

### Task 5.2: Re-render the rail on `/api/state` poll

**Files:**
- Modify: `static/grid.js` (the poll loop hooks rail re-render)

- [ ] **Step 1: Find the poll loop's render call**

Run: `grep -n "function poll\|setTimeout.*poll\|render()\|renderStream\|state\.lastState\s*=" /Users/tom/dev/periscope/static/grid.js | head -15`

`poll()` should already update `state.lastState` (or its equivalent) and call `render()`. We need it to also trigger rail re-render when the current view is split.

- [ ] **Step 2: Add rail re-render on poll**

In `static/grid.js`, find `render()` (the function that picks between grid and stream rendering based on view). Add a third branch for split:

```js
import { renderRail } from './rail.js';

export function render() {
  const view = prefs.getView() || "split";
  if (view === "split") {
    renderRail();
    return;
  }
  // ... existing grid/stream branches ...
}
```

Watch out for circular import — `grid.js ↔ stream.js` is already noted in the file's comment header as tolerated. Adding `rail.js` to that cycle should be fine since `rail.js` doesn't import `grid.js` (it imports `state`, `prefs`, `util`).

- [ ] **Step 3: Manual verify (live updates)**

Run: `npm run dev`. With the test rail state injected as in Task 5.1 Step 5, watch the rail row over ~10 seconds — confirm:
- Status dot updates when the pane state changes (e.g., type a command in a real tmux pane; rail dot should reflect working → idle).
- Branch label stays accurate (no flicker).
- Branch / worktree / repo rows present.

- [ ] **Step 4: Commit + clear test state**

```bash
git add static/grid.js
git commit -m "rail: re-render on /api/state poll"
```

---

## Phase 6 — Rail selection + detail pane

### Task 6.1: Click handlers for rows + `lastSelected` persistence

**Files:**
- Modify: `static/rail.js`

- [ ] **Step 1: Wire row click delegation**

Append to `static/rail.js` (or place at module init):

```js
// One click delegate on the rail container — re-attaches on each render
// is unnecessary because the listener lives on the static #rail element.
function attachRailListeners() {
  const el = railEl();
  if (!el || el.dataset.listenersAttached === "1") return;
  el.dataset.listenersAttached = "1";

  el.addEventListener("click", async (e) => {
    const row = e.target.closest(".rail-row");
    if (!row) return;
    const kind = row.dataset.row;
    if (kind === "repo" || kind === "worktree") {
      // Toggle collapse — persisted (Task 6.3 wires this).
      const key = row.dataset.key;
      const current = prefs.getRailCollapsed()[key] === true;
      await prefs.setRailCollapsedKey(key, !current);
      renderRail();
      return;
    }
    if (kind === "pane") {
      const pid = row.dataset.pid;
      await prefs.setLastSelected({ kind: "pane", pid });
      state.railSelected = `pane:${pid}`;
      const { selectPane } = await import('./detail.js');
      selectPane(pid);
      renderRail();
      return;
    }
    if (kind === "review") {
      const worktree = row.dataset.worktree;
      await prefs.setLastSelected({ kind: "review", worktree });
      state.railSelected = `review:${worktree}`;
      const { selectReview } = await import('./detail.js');
      selectReview(worktree);
      renderRail();
      return;
    }
  });
}

// Call attachRailListeners() at the start of renderRail():
```

Insert `attachRailListeners();` as the first line of the existing `renderRail()` body.

- [ ] **Step 2: Manual verify (clicks log; no detail.js yet)**

Run: `npm run dev`. With test rail state injected, click rows. Confirm:
- Clicking a repo or worktree row toggles its collapse (chevron flips, children show/hide).
- Clicking a pane or review row throws a temporary console error (because detail.js doesn't export selectPane/selectReview yet) — that's OK, fix in Task 6.2.

- [ ] **Step 3: Commit**

```bash
git add static/rail.js
git commit -m "rail: click handlers for repo/worktree collapse + pane/review selection persistence"
```

---

### Task 6.2: Create `detail.js` — pane + review + empty states

**Files:**
- Create: `static/detail.js`

- [ ] **Step 1: Create `detail.js`**

```js
// Right-pane (#detail) rendering for split view. Four states:
//
//   - "pane"            — terminal + side metadata
//   - "review-live"     — LGTM iframe
//   - "review-empty"    — start CTA
//   - "empty"           — nothing selected
//
// detail.js owns the mount/unmount lifecycle of the xterm + iframe.
// Callers come from rail.js click handlers; the public API is
// selectPane / selectReview / showEmpty / refreshDetail.

import { state } from './state.js';
import * as prefs from './prefs.js';
import { escapeHtml, apiCall } from './util.js';
import { mountTerminal, unmountTerminal } from './terminal-mount.js';

let currentMount = null;  // "pane" | "review" | "empty"

function $(id) { return document.getElementById(id); }

function show(id) {
  for (const which of ["detail-empty", "detail-pane", "detail-review", "detail-review-start"]) {
    $(which).classList.toggle("hidden", which !== id);
  }
}

function lookupWindow(pid) {
  return (state.lastState?.windows || []).find(w => w.pid === pid) || null;
}

function lgtmSessionForWorktree(worktreeKey) {
  const w = (state.lastState?.windows || []).find(w => w.session === worktreeKey);
  return w?.lgtm?.session_slug ? w.lgtm : null;
}

function paneHeader(w) {
  const ctx = (w.is_claude && w.context_pct != null)
    ? `${escapeHtml((w.model || "").replace(/\s*\(.*\)/, ""))} · ${w.context_pct}%`
    : "";
  return `
    <span><b>${escapeHtml(w.session || "")}</b></span>
    <span class="hsep">·</span>
    <span>${escapeHtml(w.branch || "")}</span>
    ${w.pr ? `<span class="hsep">·</span><span>#${escapeHtml(String(w.pr))} ${w.ci || ""}</span>` : ""}
    ${ctx ? `<span class="hsep">·</span><span>${ctx}</span>` : ""}
  `;
}

export function selectPane(pid) {
  const w = lookupWindow(pid);
  if (!w) {
    showEmpty();
    return;
  }
  show("detail-pane");
  $("detail-pane-header").innerHTML = paneHeader(w);
  // Re-mount xterm on the detail container.
  mountTerminal(
    $("detail-xterm"),
    w.target,
    { onPaste: null }  // image paste lives in modal; split view ships without it (future work)
  );
  // Side panel: recap / commits / notes — minimal v1, populated from window fields.
  $("detail-side").innerHTML = renderSidePanel(w);
  currentMount = "pane";
}

function renderSidePanel(w) {
  const recap = w.recap ? `<div class="side-section"><div class="side-label">Recap</div><div>${escapeHtml(w.recap)}</div></div>` : "";
  const last = w.last_line ? `<div class="side-section"><div class="side-label">Last line</div><div class="side-mono">${escapeHtml(w.last_line)}</div></div>` : "";
  return recap + last;
}

export function selectReview(worktreeKey) {
  const session = lgtmSessionForWorktree(worktreeKey);
  if (!session) {
    // No LGTM session — show start CTA.
    show("detail-review-start");
    $("detail-review-start").innerHTML = `
      <div class="review-start-card">
        <div class="review-start-title">No LGTM session for this worktree</div>
        <button class="review-start-btn" data-worktree="${escapeHtml(worktreeKey)}">Start review →</button>
      </div>`;
    $("detail-review-start").querySelector("button").addEventListener("click", async (e) => {
      const wt = e.currentTarget.dataset.worktree;
      const w = (state.lastState?.windows || []).find(x => x.session === wt);
      if (!w) return;
      // POST /api/lgtm/start with the worktree cwd.
      const resp = await apiCall("/api/lgtm/start", { method: "POST", body: { cwd: w.cwd } });
      // After start, switch to the iframe.
      selectReview(worktreeKey);
    });
    currentMount = "review";
    return;
  }
  show("detail-review");
  $("detail-review-header").innerHTML = `<span><b>review</b></span><span class="hsep">·</span><span>${escapeHtml(worktreeKey)}</span>`;
  $("detail-review-iframe").src = `http://localhost:9900/project/${session.session_slug}/`;
  // Tear down xterm if it was mounted.
  if (currentMount === "pane") unmountTerminal();
  currentMount = "review";
}

export function showEmpty() {
  show("detail-empty");
  $("detail-empty").innerHTML = `
    <div class="detail-empty-card">
      <p>Select a tab on the left, or <button id="detail-empty-add">+ open</button> to add one.</p>
    </div>`;
  if (currentMount === "pane") unmountTerminal();
  currentMount = "empty";
}

// Called on view switch into split. Restores last selection.
export function refreshDetail() {
  const sel = prefs.getLastSelected();
  if (!sel) {
    showEmpty();
    return;
  }
  if (sel.kind === "pane") selectPane(sel.pid);
  else if (sel.kind === "review") selectReview(sel.worktree);
  else showEmpty();
}
```

- [ ] **Step 2: Add detail-specific CSS**

Append to `static/styles.css`:

```css
.detail-pane-header, .detail-review-header {
  padding: 8px 14px; border-bottom: 1px solid var(--border, #333);
  display: flex; gap: 8px; align-items: baseline; font-size: 12px;
}
.detail-pane-header .hsep, .detail-review-header .hsep { opacity: .5; }
.side-section { margin-bottom: 10px; }
.side-label { font-size: 10px; text-transform: uppercase; letter-spacing: .5px; opacity: .5; margin-bottom: 3px; }
.side-mono { font-family: monospace; font-size: 11px; opacity: .85; }
.review-start-card, .detail-empty-card { text-align: center; }
.review-start-btn { margin-top: 10px; padding: 6px 14px; cursor: pointer; }
```

- [ ] **Step 3: Manual verify**

Run: `npm run dev`. With a real tmux session that has a Claude pane:
1. Switch to split view.
2. Use the console to inject a railed worktree: `addWorktreeToRail({repoKey: ..., worktreeKey: ..., paneIds: ["<real-pid>"], hasReview: true})` (or via direct fetch).
3. Click the pane row → terminal mirror appears in the right pane.
4. Type into it — confirms keys go through to the live tmux pane.
5. Click the review row → either iframe loads (if LGTM session exists for that cwd) or start CTA shows.
6. Click the start CTA → LGTM session created, iframe replaces the CTA.
7. Reload page → last selection persists.

Console errors block proceeding; debug before commit.

- [ ] **Step 4: Commit**

```bash
git add static/detail.js static/styles.css
git commit -m "detail: pane / review-live / review-empty / empty states for split view"
```

---

### Task 6.3: Update `app.js` to call `refreshDetail()` on split-view enter

**Files:**
- Modify: `static/app.js`

- [ ] **Step 1: Ensure `applyView("split")` calls `refreshDetail()`**

From Task 4.2 Step 2, the `applyView` "split" branch already calls `m.refreshDetail()`. Confirm it works: switch from grid → split → grid → split and verify the rail and detail re-populate (selection restored).

- [ ] **Step 2: No code change expected** — this task is a verification gate. If Task 4.2 was done right, nothing to commit. If not, fix the `applyView` branch.

---

## Phase 7 — Collapse interactions

Already wired in Task 6.1's click delegate. Verify and harden.

### Task 7.1: Verify collapse persistence + restoration

**Files:** none (verification only)

- [ ] **Step 1: Manual verify**

With test rail data injected:
1. Click a worktree row's chevron → collapses, children hide.
2. Click again → expands.
3. Reload page → collapsed state restored.
4. Same for repo rows.
5. Hidden-child count chip shows on collapsed worktree rows (Task 5.1 logic).

If broken, fix in `rail.js`. No commit if nothing changed.

---

## Phase 8 — Drag-and-drop reorder

### Task 8.1: Drag handles + reorder within levels

**Files:**
- Modify: `static/rail.js`
- Modify: `static/styles.css`

- [ ] **Step 1: Add hover-only drag handles to rows**

In `static/rail.js`, update each `*Row` function to include a hover-only drag handle. Modify `paneRow`, `reviewRow`, `worktreeRow`, `repoRow` to insert a draggable wrapper:

For each row's outer `<div>`, add:
- `draggable="true"`
- A `<span class="rail-grip">⋮⋮</span>` inside the row (visible on hover via CSS).

Example for `worktreeRow`:

```js
function worktreeRow(worktreeKey, children, collapsed, rolledUp, label) {
  const chev = collapsed ? "▸" : "▾";
  const childCountChip = collapsed && children.length > 0
    ? `<span class="rail-count">${children.length}</span>`
    : "";
  const body = collapsed ? "" : children.join("");
  return `
    <div class="rail-row wt-row" data-row="worktree" data-key="${escapeHtml(`wt:${worktreeKey}`)}" draggable="true">
      <span class="rail-grip">⋮⋮</span>
      <span class="rail-chev">${chev}</span>
      <span class="rail-icon icon-worktree">⎇</span>
      <span class="rail-label"><b>${escapeHtml(label)}</b></span>
      ${childCountChip}
      <span class="${statusDotClass(rolledUp)}"></span>
    </div>
    ${body}`;
}
```

Apply the same `draggable="true"` + grip to `repoRow`, `paneRow`, `reviewRow`. (Repo rows: grip on left of chevron; pane/review rows: grip replaces or precedes the tree connector.)

- [ ] **Step 2: Add drag event handlers**

Append to `rail.js` (inside `attachRailListeners`):

```js
el.addEventListener("dragstart", (e) => {
  const row = e.target.closest(".rail-row");
  if (!row) return;
  const kind = row.dataset.row;
  const key = row.dataset.key;
  state.railDragging = { kind, key, row };
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", key);  // for cross-window compat — unused
  row.classList.add("dragging");
});

el.addEventListener("dragover", (e) => {
  const drag = state.railDragging;
  if (!drag) return;
  const row = e.target.closest(".rail-row");
  if (!row || row === drag.row) return;
  if (row.dataset.row !== drag.kind && !(drag.kind === "pane" && row.dataset.row === "review")
                                    && !(drag.kind === "review" && row.dataset.row === "pane")) {
    // Cross-level drop rejected.
    return;
  }
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
});

el.addEventListener("drop", async (e) => {
  e.preventDefault();
  const drag = state.railDragging;
  if (!drag) return;
  const targetRow = e.target.closest(".rail-row");
  if (!targetRow) { state.railDragging = null; return; }

  // Reorder within the appropriate prefs key based on level.
  if (drag.kind === "repo") {
    await reorderRepos(drag.key, targetRow.dataset.key);
  } else if (drag.kind === "worktree") {
    await reorderWorktrees(drag.key, targetRow.dataset.key);
  } else {
    // pane / review: reorder within their worktree.
    await reorderChildren(drag.row, targetRow);
  }
  drag.row.classList.remove("dragging");
  state.railDragging = null;
  renderRail();
});

async function reorderRepos(draggedKey, targetKey) {
  const order = prefs.getRepoOrder();
  const dragged = draggedKey.replace(/^repo:/, "");
  const target = targetKey.replace(/^repo:/, "");
  const from = order.indexOf(dragged);
  const to = order.indexOf(target);
  if (from < 0 || to < 0 || from === to) return;
  order.splice(from, 1);
  order.splice(to, 0, dragged);
  await prefs.setRepoOrder(order);
}

async function reorderWorktrees(draggedKey, targetKey) {
  const dragged = draggedKey.replace(/^wt:/, "");
  const target = targetKey.replace(/^wt:/, "");
  const wts = prefs.getWorktreesByRepo();
  // Find which repo each belongs to; must match (cross-repo drag rejected).
  let repoKey = null;
  for (const [r, list] of Object.entries(wts)) {
    if (list.includes(dragged)) { repoKey = r; break; }
  }
  if (!repoKey) return;
  const list = wts[repoKey];
  if (!list.includes(target)) return;  // cross-repo
  const from = list.indexOf(dragged);
  const to = list.indexOf(target);
  if (from < 0 || to < 0 || from === to) return;
  list.splice(from, 1);
  list.splice(to, 0, dragged);
  wts[repoKey] = list;
  await prefs.setWorktreesByRepo(wts);
}

async function reorderChildren(draggedRow, targetRow) {
  // Both rows must be inside the same worktree's child block.
  // Walk up to find the closest ancestor wt-row of each.
  const draggedWt = closestWorktreeKey(draggedRow);
  const targetWt = closestWorktreeKey(targetRow);
  if (!draggedWt || draggedWt !== targetWt) return;

  const dragKey = childPrefKey(draggedRow);
  const targetKey = childPrefKey(targetRow);
  if (!dragKey || !targetKey) return;

  const panes = prefs.getPanesByWorktree();
  const list = panes[draggedWt] || [];
  const from = list.indexOf(dragKey);
  const to = list.indexOf(targetKey);
  if (from < 0 || to < 0 || from === to) return;
  list.splice(from, 1);
  list.splice(to, 0, dragKey);
  panes[draggedWt] = list;
  await prefs.setPanesByWorktree(panes);
}

function closestWorktreeKey(row) {
  // Walk up DOM until we find the prior wt-row; its data-key is "wt:<key>".
  let n = row;
  while (n && n !== railEl()) {
    if (n.classList && n.classList.contains("wt-row")) {
      return n.dataset.key.replace(/^wt:/, "");
    }
    n = n.previousElementSibling || n.parentElement;
  }
  return null;
}

function childPrefKey(row) {
  if (row.dataset.row === "pane") return row.dataset.pid;
  if (row.dataset.row === "review") return "review";
  return null;
}
```

- [ ] **Step 3: Add drag-state CSS**

Append to `static/styles.css`:

```css
.rail-grip { width: 12px; opacity: 0; font-size: 10px; cursor: grab; transition: opacity .15s; flex-shrink: 0; }
.rail-row:hover .rail-grip { opacity: .4; }
.rail-row.dragging { opacity: .5; }
```

- [ ] **Step 4: Manual verify**

With test rail state showing ≥2 repos, ≥2 worktrees in one repo, and ≥2 pane children in one worktree:
1. Drag a repo row to reorder → confirm repo_order persists across reload.
2. Drag a worktree → reorders within its repo.
3. Drag a worktree to another repo's section → rejected (no drop indicator).
4. Drag a pane within a worktree → reorders.
5. Drag the review row up/down within a worktree → reorders.

- [ ] **Step 5: Commit**

```bash
git add static/rail.js static/styles.css
git commit -m "rail: drag-and-drop reorder within repo/worktree/children levels"
```

---

## Phase 9 — Launcher modals

### Task 9.1: `+ open` picker modal

**Files:**
- Create: `static/open-picker-modal.js`
- Modify: `static/index.html` (modal DOM stub)
- Modify: `static/rail.js` (wire `#rail-add` click)

- [ ] **Step 1: Add modal DOM to `index.html`**

Insert near the other modals:

```html
<div id="open-picker-modal" class="hidden open-picker-modal-overlay">
  <div class="open-picker-modal-card">
    <header class="open-picker-modal-head">
      <h2>+ open</h2>
      <button id="open-picker-close" title="close">×</button>
    </header>
    <p class="open-picker-modal-sub">Pick tmux sessions to add to the rail. Already-railed sessions are hidden.</p>
    <div id="open-picker-list"></div>
    <div class="open-picker-modal-actions">
      <button type="button" id="open-picker-cancel">cancel</button>
      <button type="button" id="open-picker-submit" disabled>add (0)</button>
    </div>
  </div>
</div>
```

- [ ] **Step 2: Create `open-picker-modal.js`**

```js
// + open picker for split-view rail. Lists tmux sessions whose worktree
// isn't already in the rail, grouped by repo. Multi-select; submit calls
// prefs.addWorktreeToRail() once per selected session.

import { state } from './state.js';
import * as prefs from './prefs.js';
import { escapeHtml } from './util.js';

const $ = (id) => document.getElementById(id);

let selected = new Set();

export function openPicker() {
  selected.clear();
  $("open-picker-modal").classList.remove("hidden");
  renderList();
}

function closePicker() {
  $("open-picker-modal").classList.add("hidden");
}

function renderList() {
  const railed = new Set(
    Object.values(prefs.getWorktreesByRepo()).flat()
  );
  const grouped = {};  // repo_label → [{session, branch, repo_key}, ...]
  for (const w of (state.lastState?.windows || [])) {
    if (railed.has(w.session)) continue;
    if (!w.repo_key) continue;  // skip non-git sessions for v1
    const k = w.repo_label || w.repo_key;
    (grouped[k] = grouped[k] || []).push({
      session: w.session,
      branch: w.branch,
      repo_key: w.repo_key,
      pid: w.pid,
      has_review: true,  // worktree-backed → review row
    });
  }

  if (Object.keys(grouped).length === 0) {
    $("open-picker-list").innerHTML = `<div class="open-picker-empty">No sessions available to add — every git session is already railed.</div>`;
    updateSubmitButton();
    return;
  }

  $("open-picker-list").innerHTML = Object.entries(grouped).map(([label, sessions]) => `
    <div class="open-picker-repo">
      <div class="open-picker-repo-head">${escapeHtml(label)}</div>
      ${sessions.map(s => `
        <label class="open-picker-row">
          <input type="checkbox" data-session="${escapeHtml(s.session)}" data-repo-key="${escapeHtml(s.repo_key)}" data-pid="${escapeHtml(s.pid)}">
          <span>${escapeHtml(s.session)}</span>
          <span class="open-picker-branch">${escapeHtml(s.branch || "")}</span>
        </label>`).join("")}
    </div>`).join("");

  $("open-picker-list").querySelectorAll("input[type=checkbox]").forEach(cb => {
    cb.addEventListener("change", () => {
      const key = cb.dataset.session;
      if (cb.checked) selected.add(key); else selected.delete(key);
      updateSubmitButton();
    });
  });
  updateSubmitButton();
}

function updateSubmitButton() {
  const btn = $("open-picker-submit");
  btn.textContent = `add (${selected.size})`;
  btn.disabled = selected.size === 0;
}

async function submit() {
  // Re-derive worktree info from the checked rows.
  const checks = Array.from($("open-picker-list").querySelectorAll("input[type=checkbox]:checked"));
  for (const cb of checks) {
    const session = cb.dataset.session;
    const repoKey = cb.dataset.repoKey;
    // Collect ALL panes for this session, not just the pid stored in the checkbox.
    const sessionPanes = (state.lastState?.windows || [])
      .filter(w => w.session === session)
      .map(w => w.pid);
    await prefs.addWorktreeToRail({
      repoKey,
      worktreeKey: session,
      paneIds: sessionPanes,
      hasReview: true,
    });
  }
  closePicker();
  // Trigger immediate render.
  const { renderRail } = await import('./rail.js');
  renderRail();
}

export function initOpenPicker() {
  $("open-picker-close").addEventListener("click", closePicker);
  $("open-picker-cancel").addEventListener("click", closePicker);
  $("open-picker-submit").addEventListener("click", submit);
}
```

- [ ] **Step 3: Wire `+ open` button in rail**

In `static/rail.js`, inside `attachRailListeners`, add another delegate:

```js
el.addEventListener("click", async (e) => {
  if (e.target.id === "rail-add") {
    const { openPicker } = await import('./open-picker-modal.js');
    openPicker();
  }
});
```

(Merge with the existing click delegate — single delegate handles both row clicks and the add button.)

- [ ] **Step 4: Initialize the picker on app boot**

In `static/app.js`, near other modal-init calls (search for `initCommands\|initCleanup\|new-project`), add:

```js
import { initOpenPicker } from './open-picker-modal.js';
// ...
initOpenPicker();
```

- [ ] **Step 5: Add modal CSS**

Append to `static/styles.css`:

```css
.open-picker-modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.5); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.open-picker-modal-overlay.hidden { display: none; }
.open-picker-modal-card { background: var(--bg-1, #1c1c1f); border-radius: 6px; padding: 14px 16px; min-width: 480px; max-width: 720px; max-height: 80vh; overflow: auto; }
.open-picker-modal-head { display: flex; align-items: baseline; gap: 12px; margin-bottom: 6px; }
.open-picker-modal-head h2 { margin: 0; font-size: 16px; }
.open-picker-modal-head button { margin-left: auto; background: transparent; border: 0; font-size: 18px; cursor: pointer; }
.open-picker-modal-sub { font-size: 12px; opacity: .7; margin: 0 0 12px; }
.open-picker-repo-head { font-size: 11px; text-transform: uppercase; letter-spacing: .5px; opacity: .55; margin: 10px 0 4px; }
.open-picker-row { display: flex; gap: 8px; align-items: center; padding: 4px 6px; cursor: pointer; }
.open-picker-row:hover { background: rgba(255,255,255,.04); }
.open-picker-branch { opacity: .6; font-size: 12px; }
.open-picker-empty { padding: 14px; font-size: 13px; opacity: .7; }
.open-picker-modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }
```

- [ ] **Step 6: Manual verify**

1. Start with a tmux session that periscope can see (e.g., one created via the dashboard).
2. Switch to split view.
3. Click `+` in the rail header.
4. Picker shows the unrailed session under its repo.
5. Check it, click "add (1)".
6. Rail repopulates with that worktree visible. Review child is `start →`.

- [ ] **Step 7: Commit**

```bash
git add static/open-picker-modal.js static/index.html static/styles.css static/rail.js static/app.js
git commit -m "rail: + open picker modal for adding worktrees to the rail"
```

---

### Task 9.2: `+ New tab` per-worktree launcher modal

**Files:**
- Create: `static/launcher-modal.js`
- Modify: `static/index.html` (modal DOM)
- Modify: `static/rail.js` (render + New tab row inside worktrees; click handler)

- [ ] **Step 1: Add modal DOM**

Insert into `index.html`:

```html
<div id="launcher-modal" class="hidden launcher-modal-overlay">
  <div class="launcher-modal-card">
    <header class="launcher-modal-head">
      <h2>+ New tab</h2>
      <button id="launcher-close" title="close">×</button>
    </header>
    <p class="launcher-modal-sub" id="launcher-session-name"></p>
    <div id="launcher-list"></div>
  </div>
</div>
```

- [ ] **Step 2: Create `launcher-modal.js`**

```js
// Per-worktree "+ New tab" launcher. Reads prefs.getCommands() and lets
// the user pick one; POSTs to /api/window/new with the worktree's
// session as the target.

import * as prefs from './prefs.js';
import { escapeHtml, apiCall } from './util.js';

const $ = (id) => document.getElementById(id);

export function openLauncher(worktreeKey) {
  $("launcher-session-name").textContent = `Add to session: ${worktreeKey}`;
  const commands = prefs.getCommands();
  $("launcher-list").innerHTML = commands.map(c => `
    <button class="launcher-row" data-label="${escapeHtml(c.label)}">${escapeHtml(c.label)}</button>
  `).join("") || `<div class="launcher-empty">No commands configured. Use Commands settings to add some.</div>`;

  $("launcher-list").querySelectorAll(".launcher-row").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      const label = e.currentTarget.dataset.label;
      await apiCall("/api/window/new", {
        method: "POST",
        body: { session: worktreeKey, label },
      });
      close();
    });
  });
  $("launcher-modal").classList.remove("hidden");
}

function close() {
  $("launcher-modal").classList.add("hidden");
}

export function initLauncher() {
  $("launcher-close").addEventListener("click", close);
}
```

- [ ] **Step 3: Render `+ New tab` row in `rail.js`**

In `rail.js`, modify `worktreeRow`'s rendering — when not collapsed, append a synthetic "+ New tab" row after the children:

```js
function newTabRow(worktreeKey) {
  return `
    <div class="rail-row child-row newtab-row" data-row="newtab" data-worktree="${escapeHtml(worktreeKey)}">
      <span class="rail-conn">└</span>
      <span class="rail-icon">+</span>
      <span class="rail-label">New tab</span>
    </div>`;
}
```

Within `renderRail`, when iterating children, append `newTabRow(wtKey)` if the worktree isn't collapsed.

In `attachRailListeners`'s click delegate, add a branch:

```js
if (kind === "newtab") {
  const wt = row.dataset.worktree;
  const { openLauncher } = await import('./launcher-modal.js');
  openLauncher(wt);
  return;
}
```

- [ ] **Step 4: Initialize in `app.js`**

```js
import { initLauncher } from './launcher-modal.js';
// ...
initLauncher();
```

- [ ] **Step 5: Add launcher CSS**

```css
.launcher-modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.5); display: flex; align-items: center; justify-content: center; z-index: 1000; }
.launcher-modal-overlay.hidden { display: none; }
.launcher-modal-card { background: var(--bg-1, #1c1c1f); border-radius: 6px; padding: 14px 16px; min-width: 360px; }
.launcher-modal-head { display: flex; align-items: baseline; gap: 12px; margin-bottom: 4px; }
.launcher-modal-head h2 { margin: 0; font-size: 16px; }
.launcher-modal-sub { font-size: 12px; opacity: .7; margin: 0 0 12px; }
.launcher-row { display: block; width: 100%; padding: 8px 10px; text-align: left; background: transparent; border: 0; color: inherit; cursor: pointer; }
.launcher-row:hover { background: rgba(255,255,255,.05); }
.launcher-empty { padding: 10px; opacity: .6; font-size: 12px; }
.newtab-row { opacity: .6; }
.newtab-row:hover { opacity: .9; }
```

- [ ] **Step 6: Manual verify**

1. With a railed worktree expanded, the last child is "+ New tab".
2. Click it — launcher modal shows current commands list.
3. Click a command label — POSTs to `/api/window/new` with that session as target. (Confirm by checking that a new tmux window opens in the target session.)
4. Close + re-open the launcher — list still populates.

- [ ] **Step 7: Commit**

```bash
git add static/launcher-modal.js static/index.html static/styles.css static/rail.js static/app.js
git commit -m "rail: + New tab per-worktree launcher modal"
```

---

## Phase 10 — Auto-add hooks for `+ project` and `review PR`

### Task 10.1: Patch `new-project-modal.js` submit handler

**Files:**
- Modify: `static/new-project-modal.js`

- [ ] **Step 1: Inspect existing submit handler**

Run: `cat /Users/tom/dev/periscope/static/new-project-modal.js`

Identify the success-path code after the POST to `/api/project/new` (or wherever it lands).

- [ ] **Step 2: Append rail-add on success**

After the project creation succeeds, derive `repo_key`, `worktree_key`, and the new session's pane id from the response, then call `addWorktreeToRail`. Concretely (paraphrased — adapt to actual response shape):

```js
import { addWorktreeToRail } from './prefs.js';

// After successful create:
const resp = await apiCall("/api/project/new", { method: "POST", body });
// resp shape (verify against periscope/routes/sessions.py or similar):
// { ok: true, session: "name", repo_key: "/path", repo_label: "name", pane_pids: ["abc"] }
if (resp && resp.session) {
  await addWorktreeToRail({
    repoKey: resp.repo_key || body.repo,  // fallback to user input
    worktreeKey: resp.session,
    paneIds: resp.pane_pids || [],
    hasReview: true,  // worktree-backed
  });
}
```

If `/api/project/new`'s response doesn't expose `repo_key` / `pane_pids` yet, two options:
- (a) Extend the response server-side (small change to the route).
- (b) Re-fetch `/api/state` and find the just-created session there.

Pick (b) if it's a small change — simpler:

```js
if (resp && resp.session) {
  // Wait for next /api/state poll to see the new session's pids.
  // The grid.js poll runs every 3s; wait ~3.5s then add to rail.
  setTimeout(async () => {
    const live = await apiCall("/api/state");
    const wins = (live?.windows || []).filter(w => w.session === resp.session);
    if (wins.length === 0) return;  // race; user can + open later
    await addWorktreeToRail({
      repoKey: wins[0].repo_key,
      worktreeKey: resp.session,
      paneIds: wins.map(w => w.pid),
      hasReview: true,
    });
  }, 3500);
}
```

Match whatever async flow the existing handler already uses.

- [ ] **Step 3: Manual verify**

1. Open `+ project`. Create a new project against a real repo.
2. After ~5 seconds, switch to split view → the new project's worktree is in the rail.

- [ ] **Step 4: Commit**

```bash
git add static/new-project-modal.js
git commit -m "new-project: auto-add created worktree to split-view rail"
```

---

### Task 10.2: Patch `review-pr-modal.js` submit handler

**Files:**
- Modify: `static/review-pr-modal.js`

- [ ] **Step 1: Apply the same pattern as Task 10.1**

Run: `cat /Users/tom/dev/periscope/static/review-pr-modal.js`

After the create succeeds and a new worktree session exists, call `addWorktreeToRail({...})` with `hasReview: true`.

- [ ] **Step 2: Manual verify**

1. Open `review PR`, create a review session for a real PR.
2. Confirm the worktree appears in the rail after ~5 seconds.

- [ ] **Step 3: Commit**

```bash
git add static/review-pr-modal.js
git commit -m "review-pr: auto-add reviewed worktree to split-view rail"
```

---

## Phase 11 — Filter + auto-prune dangling rails

### Task 11.1: Two-level filter predicate

**Files:**
- Modify: `static/rail.js`

- [ ] **Step 1: Implement `passesFilterOrParentMatches`**

Append to `rail.js`:

```js
// Two-level filter: a row is shown (full opacity) if it matches the
// filter, OR if any of its descendants does. Non-matching rows that
// have no matching descendants are grayed in place.

function paneMatchesFilter(w, filter) {
  if (!filter || filter === "all") return true;
  // Reuse the same rule as grid.js:passesFilter (state-based filtering).
  if (filter === "needs-input") return w.state === "needs-input";
  if (filter === "working") return w.state === "working";
  if (filter === "done") return w.state === "done";
  if (filter === "idle") return w.state === "idle";
  if (filter === "claude") return !!w.is_claude;
  if (filter === "shell") return !w.is_claude;
  if (filter === "ci-bad") return w.ci === "✗";
  return true;
}

function worktreeMatchesFilter(worktreeKey, filter, byWorktree) {
  if (!filter || filter === "all") return true;
  const windows = byWorktree[worktreeKey] || [];
  return windows.some(w => paneMatchesFilter(w, filter));
}

function repoMatchesFilter(repoKey, filter, byWorktree, worktreesByRepo) {
  if (!filter || filter === "all") return true;
  const wts = worktreesByRepo[repoKey] || [];
  return wts.some(wt => worktreeMatchesFilter(wt, filter, byWorktree));
}
```

- [ ] **Step 2: Apply the predicate at render time**

In `renderRail`, after deriving `byWorktree`, read the current filter:

```js
const filter = prefs.getFilter ? prefs.getFilter() : "all";
```

(If `prefs.getFilter()` doesn't exist, replicate the pattern that grid.js uses to read the current filter — likely `state.filter` or similar.)

For each row, add a `dim` class when it doesn't match. In `paneRow`, replace the `data-row` line with a conditional dim:

```js
const dim = paneMatchesFilter(w, filter) ? "" : " rail-dim";
return `
  <div class="rail-row child-row${sel}${dim}" ...>`;
```

Apply analogously to `worktreeRow` (use `worktreeMatchesFilter`) and `repoRow`.

- [ ] **Step 3: Add `.rail-dim` CSS**

```css
.rail-dim { opacity: .35; }
.rail-row.rail-dim:hover { opacity: .6; }
```

- [ ] **Step 4: Re-render on filter change**

The existing filter dropdown should already trigger `render()` in `app.js` or `grid.js`. Confirm `render()` calls `renderRail()` when view is split (Task 5.2). If not, fix.

- [ ] **Step 5: Manual verify**

1. With several panes of mixed states (one working, one idle), set filter to "working".
2. Idle rows gray out; working rows stay full opacity.
3. Worktree rows containing only idle panes also gray out.
4. Reset filter to "all" — everything restores.

- [ ] **Step 6: Commit**

```bash
git add static/rail.js static/styles.css
git commit -m "rail: two-level filter predicate (gray non-matching, preserve layout)"
```

---

### Task 11.2: Auto-prune dangling rail entries on poll

**Files:**
- Modify: `static/rail.js`

- [ ] **Step 1: Add prune helper**

Append to `rail.js`:

```js
// Remove rail entries for sessions / panes that no longer exist in
// /api/state. Runs at the top of renderRail() with throttling so it
// doesn't write prefs on every poll.

let lastPruneAt = 0;
async function pruneDanglingEntries() {
  if (Date.now() - lastPruneAt < 5000) return;  // throttle to 5s
  lastPruneAt = Date.now();

  const live = state.lastState?.windows || [];
  const liveSessions = new Set(live.map(w => w.session));
  const livePids = new Set(live.map(w => w.pid));

  const wts = prefs.getWorktreesByRepo();
  const panes = prefs.getPanesByWorktree();
  const order = prefs.getRepoOrder();
  let changed = false;

  // Remove worktrees whose session is gone.
  for (const [repo, list] of Object.entries(wts)) {
    const kept = list.filter(wt => liveSessions.has(wt));
    if (kept.length !== list.length) {
      changed = true;
      if (kept.length === 0) {
        delete wts[repo];
        const idx = order.indexOf(repo);
        if (idx >= 0) order.splice(idx, 1);
      } else {
        wts[repo] = kept;
      }
    }
  }

  // Remove pane ids that aren't in livePids (keep "review" sentinels).
  for (const [wt, children] of Object.entries(panes)) {
    if (!liveSessions.has(wt)) { delete panes[wt]; changed = true; continue; }
    const kept = children.filter(c => c === "review" || livePids.has(c));
    if (kept.length !== children.length) { panes[wt] = kept; changed = true; }
  }

  if (changed) {
    await prefs.patchUI({ repo_order: order, worktrees_by_repo: wts, panes_by_worktree: panes });
  }
}
```

(`prefs.patchUI` may need to be exported — adjust `prefs.js` Task 3.1 if it isn't already.)

Insert `await pruneDanglingEntries();` at the very top of `renderRail()`, but make `renderRail` itself synchronous-friendly — read prefs after `await` returns. (Or convert to `async function renderRail()`.)

- [ ] **Step 2: Manual verify**

1. Inject a railed worktree pointing to a session.
2. Kill that tmux session externally: `tmux kill-session -t <name>`.
3. Within ~10 seconds the rail row disappears.
4. Other rail entries persist.

- [ ] **Step 3: Commit**

```bash
git add static/rail.js
git commit -m "rail: prune dangling entries when their tmux session/pane is gone"
```

---

## Phase 12 — Aesthetic polish

### Task 12.1: Tree connectors, type icons, status-dot pulsing

**Files:**
- Modify: `static/styles.css`

- [ ] **Step 1: Improve tree connector visuals**

In `static/styles.css`, refine the connectors and icons:

```css
.rail-conn { color: #3a3a3d; font-size: 12px; }
.icon-repo { color: #7aa6e0; }
.icon-worktree { color: #b07ec5; }
.icon-pane { color: #d59fe0; }
.icon-review { color: #6ec089; }

/* Selected accent stripe on the left edge */
.rail-row.selected { background: rgba(255,255,255,.05); }
.rail-row.selected::before {
  content: ""; position: absolute; left: 0; top: 4px; bottom: 4px;
  width: 2px; background: #e89243; border-radius: 0 2px 2px 0;
}

/* Hover only handles look more like the reference */
.rail-row { position: relative; }
.rail-grip { color: #555; }
.rail-row:hover .rail-grip { opacity: .55; }

/* Truncation in labels keeps row height uniform */
.rail-label b { font-weight: 600; }
```

- [ ] **Step 2: Manual verify**

Open split view with a populated rail and confirm:
- Icons render with the right colors and at consistent size.
- Tree connectors are faint enough not to dominate.
- Selection accent is subtle but unmistakable.
- Hover reveals drag handles cleanly; no layout shift.
- Long branch names truncate with ellipsis without wrapping.

- [ ] **Step 3: Commit**

```bash
git add static/styles.css
git commit -m "rail: aesthetic polish — tree connectors, icon colors, selection accent"
```

---

### Task 12.2: Final smoke test (run the whole pytest suite + manual sweep)

**Files:** none

- [ ] **Step 1: Run pytest**

Run: `uv run pytest -q`
Expected: All 222+ existing tests pass, plus the new ones from Tasks 1.1 and 1.2.

- [ ] **Step 2: Manual smoke**

In `npm run dev`:
1. Cold load → split view default, last selection restored, rail populated.
2. Create a new project via `+ project` → appears in rail.
3. Click a Claude pane in the rail → terminal mirror in right pane, can type.
4. Click the review row of a pane → LGTM iframe loads (or start CTA + start works).
5. Drag-reorder repos, worktrees, children — persists across reload.
6. Toggle collapse on every level — persists.
7. Switch to grid view → grid renders, modal still opens on click.
8. Switch to stream view → stream renders, modal still opens on click.
9. Filter to "needs-input" — non-matching rows gray out in rail.
10. `+ open` picker shows unrailed sessions; adding works.
11. `+ New tab` opens launcher; running a command creates a new tmux window in the target session.
12. Kill an external tmux session — its rail entry disappears within 10s.

- [ ] **Step 3: If anything fails, file follow-up tasks**

Do not commit a "fix" that masks the issue. Triage and either fix the root cause (in this plan or a follow-up) or document the gap before declaring done.

---

## Self-review

**1. Spec coverage**

| Spec section                          | Tasks                                                  |
| ------------------------------------- | ------------------------------------------------------ |
| Architecture overview                 | 4.1 (DOM), 4.2 (view switch)                           |
| Data model — Identity                 | 1.1 (repo_key/repo_label backend), 3.1 (prefs schema)  |
| Data model — Persisted state          | 1.2 (UIPatch), 3.1 (prefs helpers)                     |
| Status rollup                         | 5.1 (maxSeverity in rail.js)                           |
| Entry                                 | 9.1 (+ open), 10.1 (+ project), 10.2 (review PR)       |
| Exit                                  | 11.2 (auto-prune), context menu in 12 (future)         |
| Selection                             | 6.1 (click), 6.2 (detail), 7.1 (collapse)              |
| Drag-and-drop                         | 8.1                                                    |
| Filters                               | 11.1                                                   |
| Right pane behavior                   | 6.2                                                    |
| Chrome (3-way toggle, alerts overlay) | 4.1 / 4.2 (toggle); alerts rail unchanged              |
| Modal coexistence                     | 2.1 / 2.2 (terminal-mount), modal stays                |
| Files added & touched                 | Each task names exact paths                            |
| Open implementation Q: xterm reattach | 6.2 picks mount-on-select (remount on selection swap)  |

All spec sections covered.

**2. Placeholder scan**

Scanning for TBD / TODO / placeholder text — none present.

**3. Type consistency**

- `repo_key` is consistent across backend (`git_pr.py`) and frontend (`prefs.js` field name `repo_order` keyed by full path; `data-repo-key` attribute carrying same value).
- `worktree_key` is the tmux session name everywhere.
- `panes_by_worktree[wtKey]` is an array of strings; `"review"` is a valid string element.
- `setLastSelected({kind, pid|worktree})` matches `getLastSelected()` shape and matches `data-key="pane:<pid>"` / `data-key="review:<wt>"` in rendered rows.
- `mountTerminal(container, target, opts)` signature matches the calls in `modal.js` and `detail.js`.

**4. Open issues**

- **Image paste in split view's terminal:** the spec doesn't explicitly say whether to ship image paste in the split-view xterm. Task 6.2 passes `onPaste: null` — future work to add it via `terminal-mount`'s `onPaste` opt with a split-view-specific handler.
- **LGTM iframe URL hardcodes `localhost:9900`:** matches what `modal.js` does today. If LGTM_URL becomes configurable, update both call sites.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-01-split-view-rail.md`. Going with **Subagent-Driven Development** (per Tom's instructions: "use subagent-driven").

Next step: `superpowers:subagent-driven-development`.

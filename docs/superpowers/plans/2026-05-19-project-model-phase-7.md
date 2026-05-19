# Project Model + Settings (Phase 7) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire up the `settings` block (reserved in phase 1's state v2, never used). Make the worktree layout per-repo (auto-detected, with override) and cleanup idle threshold configurable. Replace phases 1-3's crude `prompt()`/`confirm()` UX on the per-project ⋯ menu with a proper dropdown. Replace the buried "worktree tab" affordance with split-button trigger on each new-tile command. Add a global settings modal.

**Architecture:** Three threads.

1. **Settings backend.** `state["settings"]` schema + `GET /api/settings` + `PATCH /api/settings`. Lazy auto-detect of `worktree_layout` per repo (sibling vs inline) on first `spawn_worktree` invocation for the repo — caches result in `settings.worktree_layout_overrides`. `spawn_worktree` branches its path resolution on the per-repo layout. `cleanup.IDLE_THRESHOLD_DAYS` reads from settings with fallback.

2. **Per-project ⋯ dropdown.** Replace the existing `prompt()` chain (rename, archive, worktree-tab) with a real DOM dropdown. Worktree-tab moves OUT (becomes part of Thread 3). Items: **Rename** (inline edit on the project name), **Archive** (two-click inline confirm). No `prompt()`, no browser `confirm()`. Click-outside + Escape close.

3. **Split-button new-tile.** Each command in the new-tile becomes a pair: `[+ <cmd>] [⌥]`. Main click = today's plain-tab behavior. Variant click = inline branch-name input → POST `/api/window/new-worktree`. Pattern matches GitHub Desktop's split-button.

4. **Global settings modal.** New `🛠 settings` button in the filter bar opens a modal with cleanup_idle_days + worktree_layout_default + per-repo overrides table. Reads via GET, writes via PATCH.

**Tech Stack:** Python 3.11+ / FastAPI / vanilla JS ES modules / tmux / git. pytest at `tests/` covers the new backend surface.

**Spec:** `docs/superpowers/specs/2026-05-15-workflow-management-design.md` §Verb 8 (settings).

**Design calls (confirmed in conversation):**
- Default layout when auto-detect can't decide → silently default to `sibling`, record the override, never re-detect.
- Per-project settings panel (base_branch / pinned_repo edits) **deferred** — out of phase-7 scope.
- Icon for global settings → `🛠 settings` in the filter bar (avoids conflict with the existing `⚙` commands editor).
- Split-button UX: pattern **A** (adjacent icon button), inline `<input>` for branch name.

**What's explicitly NOT in phase 7:**
- **`repos_dir` multi-path config.** `~/dev` works. Defer.
- **`worktrees_dir` config.** `~/dev/worktrees` works. Defer.
- **User initials + branch templates** — never wired.
- **Per-project settings UI** (base_branch / pinned_repo edits). PATCH /api/projects/patch already exists.
- **Polished PR-merged banner** — independently deferred.
- **Per-project cleanup view** (the `?repo=` query param) — its endpoint exists; UI not in phase 7.

---

## File Structure

**Created:**
- `static/settings-modal.js` — global settings modal: open/close/load/save.

**Modified:**
- `periscope/store.py` — adds `Settings` TypedDict + accessor helpers `get_settings()` / `update_settings(patch: dict)`.
- `periscope/routes/prefs.py` (or a new `routes/settings.py` — see Task 1 decision) — adds `GET /api/settings` + `PATCH /api/settings`.
- `periscope/worktree_spawn.py` — `_resolve_layout(repo)` + branches path generation on it; auto-detect runs once per repo and writes to settings.
- `periscope/cleanup.py` — `IDLE_THRESHOLD_DAYS` becomes a fallback; reads `settings.cleanup_idle_days`.
- `static/grid.js` — `renderNewTile` produces split buttons; `handleProjectMenu` replaced with proper dropdown; new `handleWorktreeVariant` inline-input flow; new `handleProjectMenuDropdown` shows the dropdown.
- `static/index.html` — `🛠 settings` button + modal markup.
- `static/styles.css` — split-button styles, dropdown styles, settings modal aliased to existing modal rules.
- `static/app.js` — wire `initSettingsModal()` at boot.
- `tests/routes/test_settings.py` (new) — pytest for the new endpoints + layout auto-detect.

---

## Task 1: settings backend — schema + endpoints + accessors

**Files:**
- Modify: `periscope/store.py` (add `Settings` TypedDict + accessors)
- Create: `periscope/routes/settings.py` (new routes file — distinct from `routes/prefs.py` which handles UI prefs + commands)
- Modify: `periscope/app.py` (wire the new router)
- Create: `tests/routes/test_settings.py`

- [ ] **Step 1: Add `Settings` TypedDict + accessors in `store.py`**

Near the existing `WindowAnnotation` TypedDict (around line 57), add:

```python
class Settings(TypedDict, total=False):
    """Per-host preferences. Persisted under state['settings']."""
    worktree_layout_default: str  # "sibling" | "inline"
    worktree_layout_overrides: dict[str, str]  # realpath -> "sibling" | "inline"
    cleanup_idle_days: int
```

After the existing `update_ui` accessor (around line 234-243), add:

```python
def get_settings() -> Settings:
    """Snapshot of the settings block."""
    with _STATE_LOCK:
        # Deep-ish copy: shallow-copy the top-level + deep-copy the
        # worktree_layout_overrides dict so callers can't mutate state
        # by reference.
        raw = _STATE.get("settings", {})
        return {
            **raw,
            "worktree_layout_overrides": dict(
                raw.get("worktree_layout_overrides", {}) or {}
            ),
        }  # type: ignore[return-value]


def update_settings(patch: dict) -> None:
    """Merge `patch` into settings and persist. `worktree_layout_overrides`
    is replaced wholesale rather than merged (consumers always send the
    full override map). Keys with None value at the top level are removed.
    """
    with _STATE_LOCK:
        cur = _STATE.setdefault("settings", {})
        for k, v in patch.items():
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = v
        _write_state(_STATE)
```

The `worktree_layout_overrides` "replace wholesale" is a deliberate choice — the settings modal sends the whole map every save, so merge semantics for that key would be confusing (how do you delete an override?). Top-level fields use the same null-clears pattern as `update_ui`.

- [ ] **Step 2: Create `periscope/routes/settings.py`**

A new routes file (NOT extending `routes/prefs.py`) so the settings surface stays distinct from the UI-prefs surface:

```python
"""Settings endpoints: GET + PATCH the persisted settings block."""

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope.store import get_settings, update_settings


router = APIRouter()


_VALID_LAYOUTS = ("sibling", "inline")


@router.get("/api/settings")
def settings_get():
    return {"settings": get_settings()}


class SettingsPatch(BaseModel):
    # All fields optional. Pydantic's model_fields_set distinguishes
    # "not sent" from "sent as null"; null at the top level clears.
    worktree_layout_default: str | None = None
    worktree_layout_overrides: dict[str, str] | None = None
    cleanup_idle_days: int | None = None


@router.patch("/api/settings")
def settings_patch(body: SettingsPatch):
    sent = body.model_fields_set
    patch: dict = {}

    if "worktree_layout_default" in sent:
        v = body.worktree_layout_default
        if v is None or v in _VALID_LAYOUTS:
            patch["worktree_layout_default"] = v
        else:
            raise HTTPException(400, f"worktree_layout_default must be one of {_VALID_LAYOUTS}")

    if "worktree_layout_overrides" in sent:
        overrides = body.worktree_layout_overrides
        if overrides is not None:
            # Validate values + normalize keys to realpath. `_resolve_layout`
            # looks up by realpath, so accepting a non-realpath'd key from
            # the modal would mean the override silently never matches.
            # Normalize on the way in.
            normalized: dict[str, str] = {}
            for repo, layout in overrides.items():
                if layout not in _VALID_LAYOUTS:
                    raise HTTPException(
                        400, f"worktree_layout_overrides[{repo!r}] must be one of {_VALID_LAYOUTS}",
                    )
                normalized[os.path.realpath(repo)] = layout
            patch["worktree_layout_overrides"] = normalized
        else:
            patch["worktree_layout_overrides"] = None

    if "cleanup_idle_days" in sent:
        v = body.cleanup_idle_days
        if v is None or (isinstance(v, int) and v > 0):
            patch["cleanup_idle_days"] = v
        else:
            raise HTTPException(400, "cleanup_idle_days must be a positive integer")

    update_settings(patch)
    return {"ok": True, "settings": get_settings()}
```

- [ ] **Step 3: Wire the router in `periscope/app.py`**

`periscope/app.py` does NOT use inline `include_router` per route; it uses a tuple-driven loop. Around line 24-30 you'll find:

```python
from periscope.routes import (
    auto_rename, channel, cleanup as cleanup_routes, healthz,
    history as history_routes, lgtm as lgtm_routes, pane, paste_image,
    prefs, projects as projects_routes, send, sessions, state, ws,
)
```

And around line 84-88 the wiring loop:

```python
for r in (
    auto_rename, channel, cleanup_routes, healthz, history_routes,
    lgtm_routes, pane, paste_image, prefs, projects_routes, send,
    sessions, state, ws,
):
    app.include_router(r.router)
```

Add `settings as settings_routes` to the import tuple AND `settings_routes` to the wiring loop. Position alphabetically (between `sessions` and `state`).

- [ ] **Step 4: Write the tests**

Create `tests/routes/test_settings.py`:

```python
"""Tests for /api/settings."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from periscope.app import app
    return TestClient(app)


def test_get_settings_returns_block(client, mocker):
    mocker.patch(
        "periscope.routes.settings.get_settings",
        return_value={"cleanup_idle_days": 14},
    )
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.json() == {"settings": {"cleanup_idle_days": 14}}


def test_patch_writes_top_level_field(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch(
        "periscope.routes.settings.get_settings",
        return_value={"cleanup_idle_days": 30},
    )
    r = client.patch("/api/settings", json={"cleanup_idle_days": 30})
    assert r.status_code == 200
    update_spy.assert_called_once_with({"cleanup_idle_days": 30})


def test_patch_clears_with_null(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    r = client.patch("/api/settings", json={"cleanup_idle_days": None})
    assert r.status_code == 200
    update_spy.assert_called_once_with({"cleanup_idle_days": None})


def test_patch_rejects_invalid_layout(client, mocker):
    r = client.patch("/api/settings", json={"worktree_layout_default": "bogus"})
    assert r.status_code == 400


def test_patch_rejects_invalid_idle_days(client, mocker):
    r = client.patch("/api/settings", json={"cleanup_idle_days": 0})
    assert r.status_code == 400
    r = client.patch("/api/settings", json={"cleanup_idle_days": -1})
    assert r.status_code == 400


def test_patch_overrides_validates_each(client, mocker):
    r = client.patch("/api/settings", json={
        "worktree_layout_overrides": {"/foo": "sibling", "/bar": "bogus"},
    })
    assert r.status_code == 400
    assert "/bar" in r.json()["detail"]


def test_patch_overrides_replaces_wholesale(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    r = client.patch("/api/settings", json={
        "worktree_layout_overrides": {"/foo": "sibling"},
    })
    assert r.status_code == 200
    update_spy.assert_called_once_with({"worktree_layout_overrides": {"/foo": "sibling"}})
```

- [ ] **Step 5: Verify**

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 && uv run pytest tests/routes/test_settings.py -x -v 2>&1 | tail -15
```

Expected: all 7 tests pass. Then full suite:

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 && uv run pytest tests/ -x -q 2>&1 | tail -5
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 commit -am "settings: schema + GET /api/settings + PATCH /api/settings"
```

Don't forget `git add` for new files if `-am` misses them.

---

## Task 2: layout auto-detect + `spawn_worktree` per-repo path

**Files:**
- Modify: `periscope/worktree_spawn.py`

- [ ] **Step 1: Add `_resolve_layout(repo)` helper**

Add to `periscope/worktree_spawn.py` near the existing helpers (around line 33, after `_slug_for_path`):

```python
def _resolve_layout(repo: str) -> str:
    """Return the worktree-layout string for `repo`. Order:
      1. settings.worktree_layout_overrides[repo] (sticky once set)
      2. Auto-detect from existing worktrees: if all non-main worktrees
         match the sibling pattern → 'sibling'; if all match inline →
         'inline'; mixed or zero → fall back to default.
      3. settings.worktree_layout_default (= 'sibling' if unset).

    Auto-detect runs ONCE per repo per process — once a layout is
    written to overrides, we never re-detect (Tom's design call: the
    first spawn determines the convention).

    Always-writes to `settings.worktree_layout_overrides[realpath(repo)]`
    after deciding, so subsequent spawns are O(1) settings lookups.
    """
    from periscope.store import get_settings, update_settings
    from periscope import worktrees

    repo_real = os.path.realpath(repo)
    s = get_settings()
    overrides = s.get("worktree_layout_overrides") or {}
    if repo_real in overrides:
        return overrides[repo_real]

    default = s.get("worktree_layout_default") or "sibling"

    # Auto-detect.
    detected: set[str] = set()
    for wt_path, _branch in worktrees._cached_worktrees(repo_real):
        wt_real = os.path.realpath(wt_path)
        if wt_real == repo_real:
            continue  # skip main checkout
        if wt_real.startswith(str(WORKTREES_DIR) + "/"):
            detected.add("sibling")
        elif wt_real.startswith(os.path.join(repo_real, ".worktrees") + "/"):
            detected.add("inline")
    if len(detected) == 1:
        layout = next(iter(detected))
    else:
        layout = default

    # Record + persist.
    new_overrides = {**overrides, repo_real: layout}
    update_settings({"worktree_layout_overrides": new_overrides})
    return layout
```

- [ ] **Step 2: Branch `spawn_worktree`'s path resolution on layout**

Currently `spawn_worktree` computes `wt_path = WORKTREES_DIR / repo_name / _slug_for_path(branch)` unconditionally (around line 104). Replace with layout-aware resolution:

```python
    # Resolve layout + worktree path.
    layout = _resolve_layout(repo)
    if layout == "inline":
        # `<repo>/.worktrees/<branch-slugged>` — splash convention.
        wt_path = Path(repo) / ".worktrees" / _slug_for_path(branch)
    else:
        # Default: sibling layout.
        wt_path = WORKTREES_DIR / repo_name / _slug_for_path(branch)
    wt_path_str = str(wt_path)
```

And update the `mkdir(parents=True, exist_ok=True)` calls just below to use the layout-aware parent:

```python
        # Ensure parent dir exists for both layouts. mkdir(parents=True)
        # handles arbitrary depth.
        wt_path.parent.mkdir(parents=True, exist_ok=True)
```

(Drops the prior two separate `mkdir` calls for `WORKTREES_DIR` and `WORKTREES_DIR / repo_name` — `parents=True` on `wt_path.parent` covers both layouts uniformly.)

- [ ] **Step 3: Verify auto-detect against live state**

Tom's splash repo uses inline (`splash/.worktrees/drawings-refactor`). After a fresh import:

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 && uv run python3 -c "
from periscope.worktree_spawn import _resolve_layout
print('fdy:', _resolve_layout('/Users/tom/dev/fdy'))
print('splash:', _resolve_layout('/Users/tom/dev/splash'))
print('periscope:', _resolve_layout('/Users/tom/dev/periscope'))
# Second call hits the override (no re-detect).
print('fdy again:', _resolve_layout('/Users/tom/dev/fdy'))
"
```

Expected:
- `fdy: sibling` (Tom's fdy worktrees live at `~/dev/worktrees/fdy/...`)
- `splash: inline` (splash uses `splash/.worktrees/...`)
- `periscope: sibling` (default, no existing non-main worktrees to detect from — gets the default override)
- Second call returns immediately from the override

After this, inspect state.json to confirm the overrides landed:

```bash
python3 -c "
import json
d = json.load(open('/Users/tom/.config/periscope/state.json'))
print(json.dumps(d.get('settings', {}), indent=2))
"
```

Expected: `worktree_layout_overrides` populated with the 3 repos.

Note: this verify mutates your live state.json's settings block. If you want to keep it pristine for the smoke task, run with `XDG_CONFIG_HOME=/tmp/p7-detect` instead. Either way the autodetect is correct.

- [ ] **Step 4: Run pytest**

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 && uv run pytest tests/ -x -q 2>&1 | tail -5
```

Expected: all green. No new tests yet (Task 1's settings tests + the existing suite stay green).

- [ ] **Step 5: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 commit -am "worktree_spawn: _resolve_layout(repo) auto-detect + per-repo override; spawn path branches on layout"
```

---

## Task 3: cleanup reads idle threshold from settings

**Files:**
- Modify: `periscope/cleanup.py`

- [ ] **Step 1: Replace the constant with a setting-aware lookup**

In `periscope/cleanup.py`, the existing constant lives at module level:

```python
IDLE_THRESHOLD_DAYS = 14
```

Used at line 294:

```python
if not session_alive and idle_days > IDLE_THRESHOLD_DAYS:
```

Replace the usage (NOT the constant — keep it as a fallback default):

```python
from periscope.store import get_settings
idle_threshold = int(get_settings().get("cleanup_idle_days") or IDLE_THRESHOLD_DAYS)
```

And further down:

```python
if not session_alive and idle_days > idle_threshold:
```

The hoisted `idle_threshold` lives at the top of `compute_candidates`, alongside the other hoisted lookups (`windows_snapshot`, `alive_sessions`). One settings-block read per candidates query.

- [ ] **Step 2: Verify**

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 && uv run --with httpx python3 -c "
from fastapi.testclient import TestClient
from periscope.app import app
from periscope.store import update_settings
client = TestClient(app)

# Default (no setting): falls back to 14.
update_settings({'cleanup_idle_days': None})
r1 = client.get('/api/cleanup/candidates')
n1 = len(r1.json()['candidates'])
print(f'default threshold: {n1} candidates')

# Bump threshold to 60 days — fewer candidates should flag idle.
update_settings({'cleanup_idle_days': 60})
r2 = client.get('/api/cleanup/candidates')
n2 = len(r2.json()['candidates'])
print(f'60d threshold: {n2} candidates')

# Tighten to 1 day — more should flag idle.
update_settings({'cleanup_idle_days': 1})
r3 = client.get('/api/cleanup/candidates')
n3 = len(r3.json()['candidates'])
print(f'1d threshold: {n3} candidates')

# Restore.
update_settings({'cleanup_idle_days': None})
print(f'expect n2 <= n1 <= n3: {n2 <= n1 <= n3}')
"
```

Expected: candidates counts trend with the threshold (lower threshold → more idle-flagged candidates).

- [ ] **Step 3: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 commit -am "cleanup: idle threshold reads from settings.cleanup_idle_days (fallback 14)"
```

---

## Task 4: per-project ⋯ dropdown (replaces `prompt()` chain)

**Files:**
- Modify: `static/grid.js` (replace `handleProjectMenu`)
- Modify: `static/styles.css` (dropdown styles + inline confirm styles)

- [ ] **Step 1: Render the dropdown panel**

The existing `⋯` button in `renderSession` is fine as-is — it's the anchor for the dropdown. The change is what `handleProjectMenu` does on click.

Replace the existing `handleProjectMenu` function in `static/grid.js` (around line 644) with:

```javascript
let openProjectMenu = null;  // module-level: {pinnedDir, panelEl, anchorEl}

function closeProjectMenu() {
  if (!openProjectMenu) return;
  openProjectMenu.panelEl.remove();
  document.removeEventListener("click", onDocumentClickForMenu);
  document.removeEventListener("keydown", onKeydownForMenu);
  openProjectMenu = null;
}

function onDocumentClickForMenu(e) {
  if (!openProjectMenu) return;
  if (openProjectMenu.panelEl.contains(e.target)) return;
  if (openProjectMenu.anchorEl.contains(e.target)) return;
  closeProjectMenu();
}

function onKeydownForMenu(e) {
  if (e.key === "Escape") closeProjectMenu();
}

function handleProjectMenu(btn) {
  // If another menu is open, close it first.
  if (openProjectMenu && openProjectMenu.anchorEl !== btn) {
    closeProjectMenu();
  } else if (openProjectMenu) {
    closeProjectMenu();
    return;
  }

  const pinnedDir = btn.dataset.pinnedDir;
  if (!pinnedDir) return;

  // Build the panel. Position absolutely under the anchor.
  const panel = document.createElement("div");
  panel.className = "project-menu-panel";
  panel.innerHTML = `
    <button class="project-menu-item" data-action="rename">Rename</button>
    <button class="project-menu-item" data-action="archive">Archive</button>
  `;

  // Anchor the panel under the ⋯ button. Positioned via fixed + getBoundingClientRect
  // so it works even when the parent is overflow-hidden.
  const rect = btn.getBoundingClientRect();
  panel.style.position = "fixed";
  panel.style.top = `${rect.bottom + 2}px`;
  panel.style.right = `${window.innerWidth - rect.right}px`;
  document.body.appendChild(panel);

  openProjectMenu = { pinnedDir, panelEl: panel, anchorEl: btn };
  // Defer document listeners by one tick so the originating click doesn't
  // immediately fire close.
  setTimeout(() => {
    document.addEventListener("click", onDocumentClickForMenu);
    document.addEventListener("keydown", onKeydownForMenu);
  }, 0);

  panel.addEventListener("click", (e) => {
    const item = e.target.closest(".project-menu-item");
    if (!item) return;
    const action = item.dataset.action;
    if (action === "rename") {
      closeProjectMenu();
      startProjectRename(pinnedDir);
    } else if (action === "archive") {
      // Inline two-click confirm: first click changes the button label.
      if (item.dataset.confirming) {
        closeProjectMenu();
        archiveProject(pinnedDir);
      } else {
        item.dataset.confirming = "1";
        item.textContent = "Click again to confirm";
        item.classList.add("project-menu-item-confirming");
      }
    }
  });
}

function startProjectRename(pinnedDir) {
  // Find the project's session header by walking projectsByTmux back to
  // the data-session attribute. Then make the .session-name h2 editable
  // in place.
  const project = (state.lastProjects || []).find((p) => p.pinned_dir === pinnedDir);
  if (!project) return;
  const session = project.tmux_session;
  const header = grid.querySelector(`.session-header[data-session="${session}"]`);
  if (!header) return;
  const nameEl = header.querySelector(".session-name");
  if (!nameEl) return;

  const currentName = project.name || session;
  const input = document.createElement("input");
  input.type = "text";
  input.value = currentName;
  input.className = "session-name-input";
  nameEl.replaceWith(input);
  input.focus();
  input.select();

  const commit = async () => {
    // Guard against double-fire: `Enter` calls commit synchronously, which
    // removes the input from the DOM. Browser then fires `blur` on the
    // detached input, which our listener would re-invoke. Idempotency flag
    // makes the second invocation a no-op.
    if (input.dataset.committed) return;
    input.dataset.committed = "1";
    const newName = input.value.trim();
    // Restore the heading regardless (re-rendered on poll if rename succeeded).
    const restored = document.createElement("h2");
    restored.className = "session-name";
    restored.textContent = currentName;
    input.replaceWith(restored);
    if (!newName || newName === currentName) return;
    try {
      const res = await fetch("/api/projects/patch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          pinned_dir: pinnedDir,
          name: newName,
          tmux_session: newName,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(`rename failed: ${err.detail || res.status}`);
      }
    } catch (e) {
      alert(`rename request failed: ${e.message}`);
    }
  };
  const cancel = () => {
    const restored = document.createElement("h2");
    restored.className = "session-name";
    restored.textContent = currentName;
    input.replaceWith(restored);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      cancel();
    }
  });
  input.addEventListener("blur", commit);
}

async function archiveProject(pinnedDir) {
  try {
    const res = await fetch("/api/projects/archive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned_dir: pinnedDir }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`archive failed: ${err.detail || res.status}`);
    }
  } catch (e) {
    alert(`archive request failed: ${e.message}`);
  }
}
```

The click cascade in `wireGrid` already dispatches `.project-menu` clicks to `handleProjectMenu(projectMenuBtn)` — no change needed.

- [ ] **Step 2: CSS for the dropdown + rename input**

Add to `static/styles.css`:

```css
.project-menu-panel {
  background: var(--bg-1);
  border: 1px solid color-mix(in oklch, var(--fg-0) 15%, transparent);
  border-radius: 0.5em;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
  padding: 0.25em 0;
  z-index: 100;
  min-width: 160px;
}
.project-menu-item {
  display: block;
  width: 100%;
  text-align: left;
  background: none;
  border: none;
  color: var(--fg-0);
  padding: 0.4em 0.75em;
  cursor: pointer;
  font-size: 0.9em;
}
.project-menu-item:hover {
  background: color-mix(in oklch, var(--fg-0) 8%, transparent);
}
.project-menu-item-confirming {
  background: color-mix(in oklch, var(--warn, #d97706) 18%, transparent);
  color: var(--warn, #d97706);
}
.session-name-input {
  font: inherit;
  font-weight: inherit;
  font-size: inherit;
  background: var(--bg-0);
  color: var(--fg-0);
  border: 1px solid var(--accent, #4a90e2);
  border-radius: 0.3em;
  padding: 0.1em 0.3em;
  min-width: 8em;
}
```

- [ ] **Step 3: Verify in the browser**

Restart periscope on the phase-7 worktree's code (or hard-refresh after kicking launchd). Click `⋯` on any non-main project:
- Menu opens anchored under the ⋯ button.
- Click outside → closes.
- Press Escape → closes.
- Click `Rename` → menu closes, project name becomes editable. Enter commits via PATCH; Escape reverts.
- Click `Archive` → button label becomes "Click again to confirm" + warn-tinted; second click POSTs archive; clicking another item resets the confirm state (or close-and-reopen does).

If no browser available, source-grep + pytest sanity:

```bash
grep -n "project-menu-panel\|startProjectRename\|archiveProject\|prompt(" /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7/static/grid.js | head -15
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 && uv run pytest tests/ -x -q 2>&1 | tail -5
```

Expected: the grep finds the new functions and shows ZERO remaining `prompt(` calls in the project-menu region (worktree-tab prompt() also goes away in Task 5).

- [ ] **Step 4: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 commit -am "grid: replace ⋯ prompt() chain with proper dropdown + inline rename + two-click archive confirm"
```

---

## Task 5: split-button new-tile

**Files:**
- Modify: `static/grid.js` (`renderNewTile` + new handler)
- Modify: `static/styles.css` (split-button styles + worktree-input row)

- [ ] **Step 1: Render the pair**

Replace `renderNewTile` (around line 224) with:

```javascript
function renderNewTile(session) {
  // Read commands from prefs. First entry is the primary (top, larger
  // hit area). Each command renders as a pair: a main button (plain
  // tab) + a ⌥ button (worktree variant — opens an inline branch-name
  // input).
  const s = escapeHtml(session);
  const commands = prefs.getCommands();
  if (!commands.length) {
    return `<div class="card card-new" data-session="${s}"></div>`;
  }

  // Whether this session has a non-main project (worktree-eligible).
  // Worktree tab requires a project with a repo; for unmanaged sessions
  // the ⌥ button is hidden.
  const project = state.projectsByTmux?.[session];
  const worktreeEligible = project
    && project.pinned_dir !== "__main__"
    && !project.archived_at
    && (project.repo || null);  // require resolved repo

  const [primary, ...rest] = commands;
  const pair = (cmd, cls) => {
    const label = escapeHtml(cmd.label);
    const execAttr = escapeHtml(cmd.exec || "");
    const mainBtn = `<button class="new-window${cls}" data-session="${s}" data-exec="${execAttr}">+ ${label}</button>`;
    const variantBtn = worktreeEligible
      ? `<button class="new-window-worktree${cls}" data-session="${s}" data-exec="${execAttr}" data-label="${label}" title="new worktree tab + ${label}">⌥</button>`
      : "";
    return `<span class="new-window-pair">${mainBtn}${variantBtn}</span>`;
  };
  const stack = rest.length
    ? `<div class="new-window-stack">${rest.map((c) => pair(c, "")).join("")}</div>`
    : "";
  return `
    <div class="card card-new" data-session="${s}">
      ${pair(primary, " is-primary")}
      ${stack}
    </div>
  `;
}
```

- [ ] **Step 2: Add the worktree-variant click handler**

Just below `handleNewWindow` (around line 756), add:

```javascript
async function handleWorktreeVariant(btn) {
  const session = btn.dataset.session;
  const exec = btn.dataset.exec || "";
  const label = btn.dataset.label || "command";
  if (!session) return;

  // Swap the new-tile's contents for an inline branch-name form.
  // Closing/cancelling restores the tile via the next /api/state poll's
  // re-render (3s max). Storing a flag on the tile so other handlers
  // don't fight us mid-flow.
  const tile = btn.closest(".card-new");
  if (!tile) return;
  if (tile.dataset.worktreeForm === "1") return;  // already open
  tile.dataset.worktreeForm = "1";
  const prevHtml = tile.innerHTML;

  tile.innerHTML = `
    <div class="new-window-worktree-form">
      <div class="new-window-worktree-label">+ ${escapeHtml(label)} (worktree)</div>
      <input type="text" class="new-window-worktree-input" placeholder="branch name (e.g. tc/sub-feat)" autofocus>
      <div class="new-window-worktree-actions">
        <button class="new-window-worktree-cancel" type="button">cancel</button>
        <button class="new-window-worktree-submit" type="button">create</button>
      </div>
    </div>
  `;
  const input = tile.querySelector(".new-window-worktree-input");
  input.focus();

  const restore = () => {
    tile.removeAttribute("data-worktree-form");
    tile.innerHTML = prevHtml;
  };

  const submit = async () => {
    const branch = input.value.trim();
    if (!branch) {
      input.focus();
      return;
    }
    const params = new URLSearchParams({ session, branch });
    if (exec) params.set("exec", exec);
    try {
      const res = await fetch(`/api/window/new-worktree?${params}`, { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(`new worktree tab failed: ${err.detail || res.status}`);
        restore();
        return;
      }
      const body = await res.json();
      if (body.warning) console.warn("new-worktree warning:", body.warning);
      restore();
    } catch (e) {
      alert(`request failed: ${e.message}`);
      restore();
    }
  };

  tile.querySelector(".new-window-worktree-cancel").addEventListener("click", restore);
  tile.querySelector(".new-window-worktree-submit").addEventListener("click", submit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      submit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      restore();
    }
  });
}
```

- [ ] **Step 3: Add the click cascade entry**

In the existing `wireGrid` cascade (around line 830, where `.new-window` is matched), add the worktree-variant branch BEFORE the plain `.new-window` match (since the variant button is a sibling, but using `.closest` could match either — to be safe, check most-specific first):

```javascript
    const newWorktreeBtn = e.target.closest(".new-window-worktree");
    if (newWorktreeBtn) {
      e.stopPropagation();
      handleWorktreeVariant(newWorktreeBtn);
      return;
    }
    const newWindowBtn = e.target.closest(".new-window");
    if (newWindowBtn) {
      e.stopPropagation();
      handleNewWindow(newWindowBtn);
      return;
    }
```

- [ ] **Step 4: CSS**

Add to `static/styles.css`:

```css
.new-window-pair {
  display: inline-flex;
  align-items: stretch;
  gap: 0;  /* buttons share an edge */
  border-radius: 0.4em;
  overflow: hidden;
}
.new-window-pair .new-window {
  border-radius: 0;
  border-right: 1px solid color-mix(in oklch, var(--fg-0) 15%, transparent);
}
.new-window-pair .new-window-worktree {
  padding: 0 0.5em;
  background: color-mix(in oklch, var(--accent, #4a90e2) 12%, transparent);
  color: var(--fg-0);
  border: none;
  border-radius: 0;
  font-size: 0.9em;
  cursor: pointer;
}
.new-window-pair .new-window-worktree:hover {
  background: color-mix(in oklch, var(--accent, #4a90e2) 25%, transparent);
}
.new-window-pair .new-window-worktree.is-primary {
  font-size: 1em;
}
.new-window-worktree-form {
  padding: 0.5em;
  display: flex;
  flex-direction: column;
  gap: 0.5em;
}
.new-window-worktree-label {
  font-size: 0.85em;
  opacity: 0.8;
}
.new-window-worktree-input {
  font: inherit;
  padding: 0.3em 0.5em;
  border: 1px solid var(--accent, #4a90e2);
  border-radius: 0.3em;
  background: var(--bg-0);
  color: var(--fg-0);
}
.new-window-worktree-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.4em;
}
.new-window-worktree-actions button {
  font: inherit;
  font-size: 0.85em;
  padding: 0.3em 0.6em;
  border: 1px solid color-mix(in oklch, var(--fg-0) 20%, transparent);
  border-radius: 0.3em;
  background: var(--bg-0);
  color: var(--fg-0);
  cursor: pointer;
}
.new-window-worktree-submit {
  background: var(--accent, #4a90e2) !important;
  color: white !important;
  border-color: var(--accent, #4a90e2) !important;
}
```

The `!important`s on submit override the generic button defaults if they conflict.

- [ ] **Step 5: Verify**

Browser: open dashboard, find a worktree-pinned project (e.g. one of the `tc/...` ones), the new-tile should show `[+ claude] [⌥]` pairs. Click `⌥` → inline branch-input form replaces tile contents. Type a branch name, Enter creates a worktree-tab via `/api/window/new-worktree`. Escape cancels.

Headless sanity check:

```bash
grep -n "new-window-worktree\|handleWorktreeVariant\|new-window-pair" /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7/static/grid.js | head -10
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 && uv run pytest tests/ -x -q 2>&1 | tail -5
```

Expected: grep shows the new strings; pytest stays green.

- [ ] **Step 6: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 commit -am "grid: split-button new-tile — ⌥ variant opens inline branch input → /api/window/new-worktree"
```

---

## Task 6: global settings modal

**Files:**
- Create: `static/settings-modal.js`
- Modify: `static/index.html` (filter-bar button + modal markup)
- Modify: `static/app.js` (wire init)
- Modify: `static/styles.css` (alias to existing modal rules)

- [ ] **Step 1: HTML slots**

Add to `static/index.html`. Filter-bar button (after `#cleanup-btn`):

```html
<button id="settings-btn" class="filter-btn is-action" title="settings">🛠 settings</button>
```

Modal markup (after `#cleanup-modal`):

```html
<div id="settings-modal" class="hidden settings-modal-overlay">
  <div class="settings-modal-card">
    <header class="settings-modal-head">
      <h2>🛠 settings</h2>
      <button id="settings-modal-close" title="close">×</button>
    </header>
    <form id="settings-form">
      <label>
        Cleanup idle threshold (days)
        <input id="settings-cleanup-idle-days" type="number" min="1" value="14">
      </label>
      <label>
        Default worktree layout
        <select id="settings-worktree-default">
          <option value="sibling">sibling — ~/dev/worktrees/&lt;repo&gt;/&lt;branch&gt;</option>
          <option value="inline">inline — &lt;repo&gt;/.worktrees/&lt;branch&gt;</option>
        </select>
      </label>
      <fieldset>
        <legend>Per-repo overrides</legend>
        <div id="settings-overrides-list"></div>
        <p class="settings-overrides-hint">Auto-detected on first <code>+ project</code> per repo. Edit or remove rows here.</p>
      </fieldset>
      <div id="settings-modal-error" class="settings-modal-error" hidden></div>
      <div class="settings-modal-actions">
        <button type="button" id="settings-cancel">cancel</button>
        <button type="submit" id="settings-submit">save</button>
      </div>
    </form>
  </div>
</div>
```

- [ ] **Step 2: Create `static/settings-modal.js`**

```javascript
// Settings modal: GET /api/settings on open, PATCH /api/settings on save.

import { pushEscape, popEscape } from './overlay.js';
import { escapeHtml } from './util.js';

const modal = document.getElementById("settings-modal");
const closeBtn = document.getElementById("settings-modal-close");
const cancelBtn = document.getElementById("settings-cancel");
const form = document.getElementById("settings-form");
const idleInput = document.getElementById("settings-cleanup-idle-days");
const defaultSelect = document.getElementById("settings-worktree-default");
const overridesListEl = document.getElementById("settings-overrides-list");
const errorEl = document.getElementById("settings-modal-error");
const submitBtn = document.getElementById("settings-submit");

let currentSettings = {};
let isOpen = false;

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.hidden = false;
}

function clearError() {
  errorEl.hidden = true;
  errorEl.textContent = "";
}

function renderOverrides(overrides) {
  const rows = Object.entries(overrides || {});
  if (rows.length === 0) {
    overridesListEl.innerHTML = `<div class="settings-overrides-empty">(none yet)</div>`;
    return;
  }
  overridesListEl.innerHTML = rows
    .map(([repo, layout]) => `
      <div class="settings-overrides-row" data-repo="${escapeHtml(repo)}">
        <span class="settings-overrides-repo">${escapeHtml(repo)}</span>
        <select class="settings-overrides-layout">
          <option value="sibling"${layout === "sibling" ? " selected" : ""}>sibling</option>
          <option value="inline"${layout === "inline" ? " selected" : ""}>inline</option>
        </select>
        <button type="button" class="settings-overrides-remove" title="remove override">×</button>
      </div>
    `)
    .join("");
}

async function refresh() {
  clearError();
  try {
    const res = await fetch("/api/settings");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    currentSettings = body.settings || {};
    idleInput.value = currentSettings.cleanup_idle_days ?? 14;
    defaultSelect.value = currentSettings.worktree_layout_default ?? "sibling";
    renderOverrides(currentSettings.worktree_layout_overrides || {});
  } catch (e) {
    showError(`failed to load settings: ${e.message}`);
  }
}

export async function openSettingsModal() {
  if (isOpen) return;
  isOpen = true;
  clearError();
  modal.classList.remove("hidden");
  document.body.classList.add("settings-modal-open");
  pushEscape(closeSettingsModal);
  await refresh();
}

export function closeSettingsModal() {
  if (!isOpen) return;
  isOpen = false;
  modal.classList.add("hidden");
  document.body.classList.remove("settings-modal-open");
  popEscape(closeSettingsModal);
}

function collectOverrides() {
  const out = {};
  overridesListEl.querySelectorAll(".settings-overrides-row").forEach((row) => {
    const repo = row.dataset.repo;
    const layout = row.querySelector(".settings-overrides-layout").value;
    if (repo && layout) out[repo] = layout;
  });
  return out;
}

async function handleSubmit(e) {
  e.preventDefault();
  clearError();
  const idle = parseInt(idleInput.value, 10);
  if (!idle || idle < 1) {
    showError("cleanup idle days must be a positive integer");
    return;
  }
  const patch = {
    cleanup_idle_days: idle,
    worktree_layout_default: defaultSelect.value,
    worktree_layout_overrides: collectOverrides(),
  };
  submitBtn.disabled = true;
  try {
    const res = await fetch("/api/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError(err.detail || `HTTP ${res.status}`);
      return;
    }
    closeSettingsModal();
  } catch (e) {
    showError(`save failed: ${e.message}`);
  } finally {
    submitBtn.disabled = false;
  }
}

export function initSettingsModal() {
  const openBtn = document.getElementById("settings-btn");
  if (openBtn) openBtn.addEventListener("click", openSettingsModal);
  closeBtn.addEventListener("click", closeSettingsModal);
  cancelBtn.addEventListener("click", closeSettingsModal);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeSettingsModal();
  });
  overridesListEl.addEventListener("click", (e) => {
    const remove = e.target.closest(".settings-overrides-remove");
    if (remove) {
      const row = remove.closest(".settings-overrides-row");
      if (row) row.remove();
    }
  });
  form.addEventListener("submit", handleSubmit);
}
```

- [ ] **Step 3: Wire init in `static/app.js`**

Alongside the other modal inits:

```javascript
import { initSettingsModal } from './settings-modal.js';
// ...
initSettingsModal();
```

- [ ] **Step 4: CSS**

Alias the existing modal-overlay / modal-card / modal-head etc. rules to also match `.settings-modal-*`. Plus settings-specific rules in `static/styles.css`:

```css
.settings-modal-card {
  width: min(640px, 90vw);
}
#settings-form label {
  display: block;
  margin-bottom: 0.75em;
  font-size: 0.9em;
}
#settings-form input,
#settings-form select {
  display: block;
  width: 100%;
  margin-top: 0.25em;
  padding: 0.4em 0.6em;
  border: 1px solid color-mix(in oklch, var(--fg-0) 20%, transparent);
  border-radius: 0.4em;
  background: var(--bg-0);
  color: var(--fg-0);
  font: inherit;
}
#settings-form fieldset {
  border: 1px solid color-mix(in oklch, var(--fg-0) 15%, transparent);
  border-radius: 0.4em;
  padding: 0.5em 0.75em;
  margin-bottom: 0.75em;
}
.settings-overrides-empty {
  font-size: 0.85em;
  opacity: 0.6;
  padding: 0.5em 0;
}
.settings-overrides-row {
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: 0.5em;
  align-items: center;
  margin-bottom: 0.4em;
}
.settings-overrides-repo {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 0.85em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.settings-overrides-layout {
  width: auto !important;
  padding: 0.2em 0.4em !important;
}
.settings-overrides-remove {
  background: none;
  border: none;
  color: var(--fg-0);
  opacity: 0.6;
  cursor: pointer;
  font-size: 1.2em;
}
.settings-overrides-remove:hover { opacity: 1; }
.settings-overrides-hint {
  font-size: 0.8em;
  opacity: 0.6;
  margin: 0.25em 0 0 0;
}
```

- [ ] **Step 5: Verify**

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 && uv run --with httpx python3 -c "
from fastapi.testclient import TestClient
from periscope.app import app
client = TestClient(app)
r = client.get('/settings-modal.js')
print('JS status:', r.status_code, 'bytes:', len(r.content))
assert r.status_code == 200 and 'initSettingsModal' in r.text
r = client.get('/')
print('HTML has #settings-btn:', 'id=\"settings-btn\"' in r.text)
print('HTML has #settings-modal:', 'id=\"settings-modal\"' in r.text)

# Round-trip a settings patch via the API.
r = client.patch('/api/settings', json={'cleanup_idle_days': 21})
print('patch:', r.status_code, r.json())
r = client.get('/api/settings')
print('get after patch:', r.json())
# Restore to default for cleanliness.
client.patch('/api/settings', json={'cleanup_idle_days': None})
print('PASS')
"
```

Then full pytest:

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 && uv run pytest tests/ -x -q 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git -C /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 commit -am "settings-modal: top-bar 🛠 button + modal with idle-days + worktree-layout-default + per-repo overrides"
```

Don't forget `git add static/settings-modal.js` if `-am` misses it.

---

## Task 7: end-to-end smoke

- [ ] **Step 1: Full pytest suite**

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 && uv run pytest tests/ -x -q 2>&1 | tail -5
```

Expected: all green (existing + new Task 1 tests).

- [ ] **Step 2: TestClient round-trip exercising the wires**

```bash
cd /Users/tom/dev/worktrees/periscope/tc-project-model-phase-7 && XDG_CONFIG_HOME=/tmp/p7-smoke uv run --with httpx python3 << 'EOF'
"""Phase-7 smoke: settings round-trip + auto-detect + cleanup respects override."""
import os, subprocess, tempfile, time
from fastapi.testclient import TestClient
from periscope.app import app
client = TestClient(app)

# 1. Settings GET starts empty / defaulted.
r = client.get("/api/settings")
print(f"[1] initial settings: {r.json()['settings']}")
assert r.status_code == 200

# 2. PATCH idle days.
r = client.patch("/api/settings", json={"cleanup_idle_days": 21})
assert r.status_code == 200
r = client.get("/api/settings")
assert r.json()["settings"]["cleanup_idle_days"] == 21
print(f"[2] PATCH+GET idle days: 21 OK")

# 3. PATCH overrides.
r = client.patch("/api/settings", json={
    "worktree_layout_overrides": {"/Users/tom/dev/splash": "inline"},
})
assert r.status_code == 200
r = client.get("/api/settings")
assert r.json()["settings"]["worktree_layout_overrides"]["/Users/tom/dev/splash"] == "inline"
print(f"[3] override write OK")

# 4. Invalid layout rejected.
r = client.patch("/api/settings", json={"worktree_layout_default": "bogus"})
assert r.status_code == 400
print(f"[4] invalid layout 400 OK")

# 5. Layout auto-detect: spawn against a fresh repo, confirm override is
# written.
with tempfile.TemporaryDirectory(prefix="p7-detect-") as tmpdir:
    repo = os.path.join(tmpdir, "repo")
    bare = os.path.join(tmpdir, "bare.git")
    subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-m", "init", "-q"], check=True)
    subprocess.run(["git", "-C", repo, "remote", "add", "origin", bare], check=True)
    subprocess.run(["git", "-C", repo, "push", "-q", "origin", "main"], check=True)
    subprocess.run(["git", "-C", repo, "fetch", "-q", "origin"], check=True)
    subprocess.run(["git", "-C", repo, "remote", "set-head", "origin", "main"], check=True)

    from periscope.worktree_spawn import _resolve_layout
    layout = _resolve_layout(repo)
    print(f"[5] auto-detect on fresh repo: {layout} (expected: sibling default)")
    assert layout == "sibling"

    # Confirm it was recorded.
    r = client.get("/api/settings")
    repo_real = os.path.realpath(repo)
    assert r.json()["settings"]["worktree_layout_overrides"][repo_real] == "sibling"
    print(f"[5b] override recorded for {repo_real}")

print("PASS")
EOF
rm -rf /tmp/p7-smoke
```

Expected: PASS.

- [ ] **Step 3: Browser-level smoke (optional but recommended)**

Restart periscope. Visit dashboard:
- `🛠 settings` button shows in the filter bar.
- Click → modal opens with current settings populated.
- Modify idle days → save → reopen, value persists.
- `⋯` on any project → dropdown (not browser prompt).
- Click `Rename` → name becomes editable, Enter commits.
- Click `Archive` → button labels change to "Click again to confirm", second click archives.
- New-tile buttons show `[+ claude] [⌥]` pairs on worktree-eligible projects.
- Click `⌥` → inline branch input replaces tile contents.

---

## What's deliberately NOT in phase 7

- **Per-project settings panel** (base_branch / pinned_repo edits) — `PATCH /api/projects/patch` is the API, no UI in this phase.
- **`repos_dir` / `worktrees_dir` config** — defer.
- **Polished PR-merged banner** — independent.
- **Per-project cleanup view** — backend supports the query param, UI deferred.
- **Change-branch + edit-repo-override verbs** — independent.

The phase-7 endpoints (`GET /api/settings` + `PATCH /api/settings`) are stable. The settings shape can grow (new keys, new override types) without schema migrations — additive only.

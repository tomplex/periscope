# Persistent Config Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ad-hoc localStorage + hardcoded constants with a server-managed `state.json`, add periscope-assigned window ids (`@periscope_id`) with a rebind heuristic, surface per-window annotations in the modal sidebar, and make the new-window command list user-editable.

**Architecture:** Single JSON file at `${XDG_CONFIG_HOME:-~/.config}/periscope/state.json`, mutated only by `server.py` behind an `asyncio.Lock` with atomic tempfile-rename writes. Frontend gains a `prefs.js` module that caches the file in memory and exposes scoped mutators. Window ids are stored as tmux user options (`@periscope_id`) for rename/move/reorder survival; a rebind heuristic recovers ids across tmux server restarts. Four mergeable phases: storage + UI prefs migration, periscope ids, annotations UI, configurable commands.

**Tech Stack:** Python 3.11+ / FastAPI / pydantic / asyncio (`server.py`); vanilla JS ES modules (`static/*.js`); tmux user options for the id namespace.

**Spec:** `docs/superpowers/specs/2026-05-13-persistent-config-layer-design.md`

**No test suite exists in this project** — per `CLAUDE.md`, iteration is against the live dashboard via `npm run dev` (HMR) or `uv run server.py`. Each task therefore includes an explicit **Verify** step that exercises the behavior in the running dashboard, rather than an automated assertion. Commits are still frequent — one per task minimum.

---

## File Structure

**Created:**
- `static/prefs.js` — in-memory cache of `/api/prefs`, mutator surface, localStorage migration shim.
- `static/overlay.js` — tiny shared Escape-key handler so the commands modal and pane modal don't fight (added in phase 4).
- `static/commands-modal.js` — open/close + form state for the commands editor modal (phase 4).
- `~/.config/periscope/state.json` — runtime state file; not committed; the server creates it on first save.

**Modified:**
- `server.py` — state.json load/save under `asyncio.Lock`, all `/api/prefs/*` endpoints, `resolve_pids()` helper, last-seen tracking, GC, `/api/window/new` contract change.
- `static/state.js` — drops localStorage helpers (`loadOrder`/`saveOrder`/`loadCollapsed`/`saveCollapsed`/`loadView`/`saveView`); keeps the shared `state` object.
- `static/grid.js` — calls `prefs.set*` instead of localStorage helpers; consumes `w.pid` for annotation indicator; reads `prefs.getCommands()` in `renderNewTile`.
- `static/app.js` — initializes prefs before grid; switches view persistence to prefs; wires the gear icon.
- `static/modal.js` — renders a "Notes" section in `#modal-side`; delegates Escape handling to `overlay.js`.
- `static/index.html` — adds the gear icon, the `#commands-modal` div.
- `static/styles.css` — Notes section, commands modal, card annotation indicator.

---

## Phase 1 — storage + UI prefs migration

### Task 1.1: `state.json` paths and atomic write helper

**Files:**
- Modify: `server.py` (add new section near the top of the file, below the imports and before `_focused_at` initialization)

- [ ] **Step 1: Add path resolution + load/save helpers to `server.py`**

Locate the existing block in `server.py` near line 39 (right after `app = FastAPI(lifespan=lifespan)` and the `STATIC = ...` line) and insert this section immediately after the `STATIC = Path(...)` line:

```python
# --- Persistent state (state.json) ----------------------------------------
#
# Single JSON file mutated only by the server, under an asyncio.Lock, with
# atomic tempfile+rename writes. See
# docs/superpowers/specs/2026-05-13-persistent-config-layer-design.md.

def _state_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "periscope" / "state.json"


_STATE_LOCK = asyncio.Lock()
_STATE_DEFAULTS: dict = {
    "version": 1,
    "ui": {},
    "windows": {},
    "commands": [],
}


def _load_state() -> dict:
    """Read state.json. On parse failure rename to .corrupt-<ts> and return
    defaults — the next save writes a fresh valid file, and the user can
    recover from the renamed file if they care."""
    path = _state_path()
    if not path.exists():
        return json.loads(json.dumps(_STATE_DEFAULTS))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Missing keys default to their empty value — older files written by
        # earlier phases never carry `windows` or `commands`.
        for k, v in _STATE_DEFAULTS.items():
            data.setdefault(k, json.loads(json.dumps(v)))
        return data
    except (json.JSONDecodeError, OSError) as e:
        corrupt = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.rename(corrupt)
            print(f"periscope: state.json unreadable ({e}); renamed to {corrupt}")
        except OSError:
            pass
        return json.loads(json.dumps(_STATE_DEFAULTS))


def _write_state(data: dict) -> None:
    """Atomic write: tempfile + os.replace. Caller must hold _STATE_LOCK."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# In-memory cache — every endpoint reads from this, writes go through
# _write_state under the lock. Loaded once at startup.
_STATE: dict = _load_state()
```

- [ ] **Step 2: Verify the module imports cleanly**

Run:
```
cd /Users/tom/dev/periscope && uv run server.py &
SERVER=$!
sleep 2
curl -s http://127.0.0.1:8765/api/state | head -c 100
kill $SERVER
wait $SERVER 2>/dev/null
```

Expected: the curl returns JSON starting with `{"windows":` (the existing `/api/state` works because we haven't changed any handlers yet).

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "config: state.json load/save helpers with atomic write + corrupt recovery"
```

---

### Task 1.2: `GET /api/prefs` endpoint

**Files:**
- Modify: `server.py` (add endpoint near the other `/api/*` routes — group it with the new prefs endpoints below the `/api/state` handler)

- [ ] **Step 1: Add the read endpoint**

In `server.py`, just below the existing `/api/state` handler (search for `@app.get("/api/state")` — append after the function returns), insert:

```python
# --- /api/prefs endpoints -------------------------------------------------

@app.get("/api/prefs")
def get_prefs():
    """Full state blob, for client boot. Reads from the in-memory cache —
    every mutation refreshes the cache atomically, so this is safe to call
    without the lock."""
    return _STATE
```

- [ ] **Step 2: Verify the endpoint returns defaults on first boot**

Make sure `~/.config/periscope/state.json` does NOT exist before this test:

```bash
rm -f ~/.config/periscope/state.json
cd /Users/tom/dev/periscope && uv run server.py &
SERVER=$!
sleep 2
curl -s http://127.0.0.1:8765/api/prefs
kill $SERVER
wait $SERVER 2>/dev/null
```

Expected output (formatting may vary):
```
{"version":1,"ui":{},"windows":{},"commands":[]}
```

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "config: GET /api/prefs returns full state blob"
```

---

### Task 1.3: `PATCH /api/prefs/ui` endpoint

**Files:**
- Modify: `server.py` (continue in the `/api/prefs` section started in task 1.2)

- [ ] **Step 1: Add the UI patch endpoint and a `UIPatch` pydantic model**

In `server.py`, just below the `@app.get("/api/prefs")` handler from task 1.2, insert:

```python
class UIPatch(BaseModel):
    session_order: list[str] | None = None
    collapsed_sessions: list[str] | None = None
    view: str | None = None  # "grid" or "stream"


@app.patch("/api/prefs/ui")
async def patch_prefs_ui(body: UIPatch):
    """Merge partial UI prefs. Only fields present in the body get written."""
    patch = body.model_dump(exclude_none=True)
    # `view` is validated against a fixed enum to keep junk out of the file.
    if "view" in patch and patch["view"] not in ("grid", "stream"):
        return {"ok": False, "error": f"invalid view: {patch['view']!r}"}
    async with _STATE_LOCK:
        _STATE["ui"].update(patch)
        _write_state(_STATE)
    return {"ok": True, "ui": _STATE["ui"]}
```

- [ ] **Step 2: Verify the patch round-trips**

```bash
rm -f ~/.config/periscope/state.json
cd /Users/tom/dev/periscope && uv run server.py &
SERVER=$!
sleep 2
curl -s -X PATCH -H 'content-type: application/json' \
  -d '{"session_order":["tc/foo","tc/bar"]}' \
  http://127.0.0.1:8765/api/prefs/ui
echo
curl -s -X PATCH -H 'content-type: application/json' \
  -d '{"view":"stream"}' \
  http://127.0.0.1:8765/api/prefs/ui
echo
curl -s http://127.0.0.1:8765/api/prefs
kill $SERVER
wait $SERVER 2>/dev/null
```

Expected: the final GET returns `ui` containing both `session_order` and `view`, and `state.json` exists on disk with the same contents (cat it to confirm).

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "config: PATCH /api/prefs/ui merges session_order/collapsed_sessions/view"
```

---

### Task 1.4: `static/prefs.js` skeleton with `loadPrefs()` and failure mode

**Files:**
- Create: `static/prefs.js`

- [ ] **Step 1: Create the prefs module with the UI-prefs surface**

Create `static/prefs.js`:

```javascript
// Cache of /api/prefs + mutators. Frontend modules call into here instead
// of touching localStorage directly. See
// docs/superpowers/specs/2026-05-13-persistent-config-layer-design.md.

import { apiCall } from './util.js';

// The cache mirrors the server's state.json shape. `loaded` flips to true
// only after a successful loadPrefs(); mutators refuse to write while false.
const cache = {
  loaded: false,
  ui: {},
  windows: {},
  commands: [],
};

let lastError = "";

export function isLoaded() {
  return cache.loaded;
}

export function lastLoadError() {
  return lastError;
}

export async function loadPrefs() {
  try {
    const res = await fetch("/api/prefs");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    cache.ui = data.ui || {};
    cache.windows = data.windows || {};
    cache.commands = data.commands || [];
    cache.loaded = true;
    lastError = "";
    await migrateLocalStorage();
    return cache;
  } catch (err) {
    cache.loaded = false;
    lastError = err.message || String(err);
    return null;
  }
}

// ── UI prefs ────────────────────────────────────────────────────────────

export function getSessionOrder() {
  return cache.ui.session_order || [];
}

export function getCollapsed() {
  // grid.js consumes a Set — keep the existing call sites unchanged.
  return new Set(cache.ui.collapsed_sessions || []);
}

export function getView() {
  return cache.ui.view === "stream" ? "stream" : "grid";
}

async function patchUI(patch) {
  if (!cache.loaded) {
    // Try to load first; refuse the write if that still fails so we don't
    // clobber real server state with empty defaults.
    await loadPrefs();
    if (!cache.loaded) return false;
  }
  const previous = { ...cache.ui };
  cache.ui = { ...cache.ui, ...patch };  // eager local update
  const data = await apiCall("save prefs", "/api/prefs/ui", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!data) {
    cache.ui = previous;  // revert on failure
    return false;
  }
  cache.ui = data.ui;
  return true;
}

export function setSessionOrder(order) {
  return patchUI({ session_order: order });
}

export function setCollapsed(set) {
  return patchUI({ collapsed_sessions: [...set] });
}

export function setView(view) {
  return patchUI({ view });
}

// ── One-time localStorage → server migration ─────────────────────────────

async function migrateLocalStorage() {
  const LEGACY = {
    "periscope:sessionOrder": "session_order",
    "periscope:collapsedSessions": "collapsed_sessions",
    "periscope:view": "view",
  };
  const haveAny = Object.keys(LEGACY).some(
    (k) => localStorage.getItem(k) !== null
  );
  if (!haveAny) return;

  const serverEmpty =
    !cache.ui.session_order &&
    !cache.ui.collapsed_sessions &&
    !cache.ui.view;
  if (serverEmpty) {
    const patch = {};
    for (const [k, field] of Object.entries(LEGACY)) {
      const raw = localStorage.getItem(k);
      if (raw === null) continue;
      try {
        patch[field] = field === "view" ? raw : JSON.parse(raw);
      } catch (_) {
        // unparseable legacy data — skip, the user can re-establish in UI
      }
    }
    if (Object.keys(patch).length) {
      const ok = await patchUI(patch);
      if (!ok) return;  // leave localStorage in place — try again next boot
    }
  }
  // Always delete legacy keys on a successful load. Once the server has
  // authoritative state the client copies are noise.
  for (const k of Object.keys(LEGACY)) localStorage.removeItem(k);
}
```

- [ ] **Step 2: Verify the module loads without runtime errors**

Add a temporary diagnostic call in `static/app.js` (revert in step 4):

```javascript
// Temporary diagnostic — remove before commit
import { loadPrefs } from './prefs.js';
loadPrefs().then((p) => console.log("prefs:", p));
```

Run `npm run dev` (or `uv run server.py` and open the page). Open browser devtools console. Expected: a single log line `prefs: {loaded: true, ui: {...}, windows: {...}, commands: [...]}`.

- [ ] **Step 3: Revert the diagnostic in app.js**

Remove the two diagnostic lines added in step 2.

- [ ] **Step 4: Commit**

```bash
git add static/prefs.js
git commit -m "config: prefs.js with UI surface, failure-mode guard, localStorage migration"
```

---

### Task 1.5: Switch `state.js` and `grid.js` from localStorage to `prefs.js`

**Files:**
- Modify: `static/state.js` (delete `loadOrder`/`saveOrder`/`loadCollapsed`/`saveCollapsed`/`loadView`/`saveView`)
- Modify: `static/grid.js:7,147,232,502` (replace imports + helpers)
- Modify: `static/app.js:9,40-58` (replace view imports + persistence)

- [ ] **Step 1: Strip the localStorage helpers and rename-migration from `state.js`**

Replace the entire content of `static/state.js` with:

```javascript
// Cross-module mutable state. Persistence now lives in prefs.js — this module
// only holds in-flight UI state that doesn't survive a reload.

export const state = {
  // grid
  currentFilter: "all",
  lastWindows: [],
  editingTarget: null,           // pauses polling while a card rename input is open
  collapsedSessions: new Set(),  // hydrated from prefs.getCollapsed() at boot

  // modal
  activeTarget: null,
  modalRenaming: false,          // pauses modal header refresh during inline rename
};
```

- [ ] **Step 2: Update `static/grid.js` imports and call sites**

Open `static/grid.js`. Find the top-of-file import (line 7):

```javascript
import { state, loadOrder, saveOrder, saveCollapsed } from './state.js';
```

Replace with:

```javascript
import { state } from './state.js';
import * as prefs from './prefs.js';
```

Find the `orderedSessions` function and replace its first body line (`const saved = loadOrder();`) with:

```javascript
  const saved = prefs.getSessionOrder();
```

Find the `handleToggleAll` function. Replace `saveCollapsed(state.collapsedSessions);` with:

```javascript
  prefs.setCollapsed(state.collapsedSessions);
```

Find the `reorderSessions` function. Replace `saveOrder(without);` with:

```javascript
  prefs.setSessionOrder(without);
```

Find the grid click handler's session-header branch (search for `// Header click toggles collapse`). Replace `saveCollapsed(state.collapsedSessions);` with:

```javascript
  prefs.setCollapsed(state.collapsedSessions);
```

- [ ] **Step 3: Update `static/app.js` to load prefs first and use them for view**

Open `static/app.js`. Replace its content with:

```javascript
// Entry point. Loads prefs first so render() sees collapsed/order, then wires
// the filter + new-session + view switch handlers and starts the grid loop.

import { state } from './state.js';
import * as prefs from './prefs.js';
import { apiCall } from './util.js';
import { initModal } from './modal.js';
import { initGrid, poll, render } from './grid.js';

// `[data-filter]` scope excludes the action chips (+ session, collapse all)
// that share the .filters parent — those have their own handlers.
const filterButtons = document.querySelectorAll("#filters button[data-filter]");
filterButtons.forEach((b) => {
  b.addEventListener("click", () => {
    filterButtons.forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.currentFilter = b.dataset.filter;
    render(state.lastWindows);
  });
});

document.getElementById("new-session").addEventListener("click", async () => {
  const name = prompt("session name:");
  if (!name) return;
  await apiCall("new session", `/api/session/new`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim() }),
  });
  poll();
});

// View switch (grid ↔ stream). Persisted via prefs.js. Applied via
// body.dataset.view; the renderer dispatches on the attribute, and CSS keys
// off it to hide grid-only chrome (collapse-all toggle) in stream view.
const viewSwitch = document.getElementById("view-switch");
const viewButtons = viewSwitch.querySelectorAll("[data-view]");
function applyView(view) {
  document.body.dataset.view = view;
  viewButtons.forEach((b) => {
    const active = b.dataset.view === view;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
  });
}

async function bootstrap() {
  await prefs.loadPrefs();
  // Seed the in-memory collapsed set from server state. Subsequent toggles
  // mutate `state.collapsedSessions` directly and call prefs.setCollapsed.
  state.collapsedSessions = prefs.getCollapsed();
  applyView(prefs.getView());
  initModal();
  initGrid();
}

viewSwitch.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-view]");
  if (!btn) return;
  const v = btn.dataset.view;
  if (document.body.dataset.view === v) return;  // no-op click on active
  applyView(v);
  prefs.setView(v);
  render(state.lastWindows);  // re-render against cached data, no refetch
});

bootstrap();
```

- [ ] **Step 4: Verify roundtrip in the dashboard**

Run `npm run dev` (or `uv run server.py`), open `http://127.0.0.1:8765/`. With devtools open:

1. Drag a session header to reorder. Reload. Confirm the order persists.
2. Click a session header to collapse it. Reload. Confirm it stays collapsed.
3. Toggle the view-switch to "stream". Reload. Confirm it stays in stream view.
4. In devtools Application → Local Storage, confirm `periscope:sessionOrder`, `periscope:collapsedSessions`, `periscope:view` are **all absent**.
5. Cat `~/.config/periscope/state.json` and confirm it contains the order/collapse/view you set.

- [ ] **Step 5: Verify localStorage migration on a fresh frontend**

In devtools Application → Local Storage, manually set:
- `periscope:sessionOrder` = `["session-a","session-b"]`
- `periscope:view` = `stream`

Stop the dev server. Delete `~/.config/periscope/state.json`. Restart the dev server. Reload the page. Confirm:
- The "stream" view is active.
- `~/.config/periscope/state.json` contains `"session_order":["session-a","session-b"]` and `"view":"stream"`.
- The localStorage keys are gone (devtools).

- [ ] **Step 6: Commit**

```bash
git add static/state.js static/grid.js static/app.js
git commit -m "config: route session order/collapse/view through prefs.js (localStorage → state.json with migration)"
```

---

## Phase 2 — periscope ids + rebind heuristic

### Task 2.1: Extend `list_windows()` to carry `@periscope_id`

**Files:**
- Modify: `server.py:707-731` (`list_windows`)

- [ ] **Step 1: Add the format field**

In `server.py`, locate the `list_windows()` function (~line 707). Replace its body with:

```python
def list_windows() -> list[dict]:
    out = tmux(
        "list-windows",
        "-a",
        "-F",
        "#{session_name}\t#{window_index}\t#{window_name}\t#{window_active}\t#{pane_current_path}\t#{@periscope_id}",
    )
    rows = []
    for line in out.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        # pane_current_path is the active pane's cwd; safe even when missing.
        # @periscope_id is empty for unmanaged windows — `resolve_pids` mints
        # one on first sighting and stamps it onto the window.
        s, idx, name, active = parts[:4]
        cwd = parts[4] if len(parts) > 4 else ""
        pid_raw = parts[5] if len(parts) > 5 else ""
        rows.append(
            {
                "session": s,
                "index": int(idx),
                "name": name,
                "active": active == "1",
                "cwd": cwd,
                "pid_raw": pid_raw,
            }
        )
    return rows
```

- [ ] **Step 2: Verify the field appears (empty) on first run**

```bash
cd /Users/tom/dev/periscope && uv run server.py &
SERVER=$!
sleep 2
curl -s http://127.0.0.1:8765/api/state | python3 -m json.tool | grep -A 1 pid_raw | head -10
kill $SERVER
wait $SERVER 2>/dev/null
```

Note: `pid_raw` will be empty for every window (no `@periscope_id` set yet). That's expected — task 2.2 fills it.

Actually — the `pid_raw` value isn't in the `/api/state` response yet because `parse_pane`'s output doesn't include it, and `/api/state` does `{**w, **parsed, ...}`. So `pid_raw` from `w` does end up in the response. Confirm by searching the JSON for `pid_raw`.

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "config: list_windows() format string carries @periscope_id (always empty until resolve_pids stamps it)"
```

---

### Task 2.2: `resolve_pids()` helper, mint + rebind + last_seen update

**Files:**
- Modify: `server.py` (add a new section between the `list_windows()` function and `capture()`)

- [ ] **Step 1: Add the resolver**

In `server.py`, immediately after `list_windows()` ends and before `capture()`, insert:

```python
# --- Periscope window-ids (@periscope_id) ---------------------------------
#
# Every window we see acquires a periscope-assigned 8-char hex id, stamped
# onto the window as a tmux user option `@periscope_id`. The id survives
# rename / move / reorder. When the tmux server restarts (reboot,
# kill-server, OOM) and the option is gone, `_rebind_pid` recovers it from
# the (session, name) hint in `last_seen` within a 30-day window — see the
# rebind heuristic in the design spec.

_PID_TTL_S = 30 * 86400  # 30 days
_LAST_SEEN_KEYS = ("session", "name", "branch", "cwd", "ts")


def _mint_pid() -> str:
    return uuid.uuid4().hex[:8]


def _stamp_pid(target: str, pid: str) -> None:
    """Fire-and-forget set-option. If it fails (window gone, tmux racy),
    the next poll repeats the attempt."""
    subprocess.run(
        ["tmux", "set-option", "-w", "-t", target, "@periscope_id", pid],
        capture_output=True, check=False, timeout=2,
    )


def _rebind_pid(
    windows_block: dict,
    session: str,
    name: str,
    branch: str | None,
    cwd: str | None,
    taken_pids: set[str],
) -> str | None:
    """Look for an orphan id in state's `windows` block that matches the
    sighted window on (session, name) — or as a softer fallback,
    (branch, cwd). Returns the matched pid, or None if no candidate
    matches."""
    now = time.time()
    # Pass 1: strong match on (session, name).
    # Pass 2: secondary match on (branch, cwd) when both are set.
    for pass_n in (1, 2):
        for pid, entry in windows_block.items():
            if pid in taken_pids:
                continue
            ls = entry.get("last_seen") or {}
            ts = ls.get("ts")
            if not ts or now - ts > _PID_TTL_S:
                continue
            if pass_n == 1:
                if ls.get("session") == session and ls.get("name") == name:
                    return pid
            else:
                if not branch or not cwd:
                    continue
                if ls.get("branch") == branch and ls.get("cwd") == cwd:
                    return pid
    return None


def resolve_pids(windows: list[dict]) -> None:
    """Mutates `windows` in place, adding a `pid` field to every entry.

    For each window:
      1. If @periscope_id is non-empty, use it.
      2. Else attempt rebind from state.json's `windows` block.
      3. Else mint a fresh id.
    In cases 2 and 3, stamp the chosen id onto the tmux window (`set-option
    -w @periscope_id`) so subsequent polls take the fast path.

    Always updates the pid's `last_seen` block with (session, name, branch,
    cwd, now) under the lock so the rebind hint stays fresh.

    Callers MUST have populated each window's `branch` (from
    cached_git_state) before calling, or rebind falls back to the
    session/name-only path.
    """
    if not windows:
        return
    now_ts = int(time.time())
    # _STATE writes need the async lock; resolve_pids runs in sync endpoint
    # context. The lock is asyncio.Lock; we use the sync .locked()-style
    # mutation by reaching directly into _STATE here because every endpoint
    # that calls us is single-threaded relative to other prefs writes.
    # ARG: if we ever introduce a writer that races with this, this section
    # needs to move under the lock.
    wblock = _STATE.setdefault("windows", {})
    taken: set[str] = set()
    dirty = False
    for w in windows:
        target = f"{w['session']}:{w['index']}"
        pid_raw = (w.get("pid_raw") or "").strip()
        pid: str | None = None
        if pid_raw and len(pid_raw) == 8 and all(c in "0123456789abcdef" for c in pid_raw):
            pid = pid_raw
        if pid is None:
            pid = _rebind_pid(
                wblock,
                session=w["session"],
                name=w["name"],
                branch=w.get("branch"),
                cwd=w.get("cwd"),
                taken_pids=taken,
            )
        if pid is None:
            pid = _mint_pid()
        # Stamp + last_seen update for synthesized ids only.
        if pid != pid_raw:
            _stamp_pid(target, pid)
        taken.add(pid)
        w["pid"] = pid
        # `pid_raw` was internal — strip it before emit.
        w.pop("pid_raw", None)
        # Update last_seen for every sighted window (including ones that
        # already had a pid stamped) — keeps the rebind hint fresh.
        entry = wblock.setdefault(pid, {})
        entry["last_seen"] = {
            "session": w["session"],
            "name": w["name"],
            "branch": w.get("branch"),
            "cwd": w.get("cwd"),
            "ts": now_ts,
        }
        dirty = True
    if dirty:
        _write_state(_STATE)
```

- [ ] **Step 2: Verify the helper compiles**

```bash
cd /Users/tom/dev/periscope && uv run python -c "import server"
```

Expected: no output (clean exit). Any traceback means there's a syntax error to fix before continuing.

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "config: resolve_pids() — mint/rebind/stamp + last_seen tracking for window annotations"
```

---

### Task 2.3: Wire `resolve_pids()` into `/api/state` and the auto-rename endpoints

**Files:**
- Modify: `server.py` (`/api/state` handler — search for `@app.get("/api/state")`)
- Modify: `server.py` (`auto_rename_session` and `auto_rename_window` handlers)

- [ ] **Step 1: Add a small helper that decorates windows with branch+cwd before calling `resolve_pids`**

In `server.py`, just below `resolve_pids` (after step 2 of task 2.2 lands), insert:

```python
def _attach_git_then_resolve_pids(windows: list[dict]) -> None:
    """resolve_pids relies on `branch` for its secondary match. Populate it
    via cached_git_state before calling so the rebind heuristic has
    everything it needs."""
    for w in windows:
        git = cached_git_state(w.get("cwd", "")) or {}
        if "branch" in git:
            w["branch"] = git["branch"]
    resolve_pids(windows)
```

- [ ] **Step 2: Call the helper from `/api/state`**

In `server.py`, locate the `/api/state` handler. Find the line:
```python
windows = list_windows()
update_focus_from_windows(windows)
```
Insert immediately after:
```python
_attach_git_then_resolve_pids(windows)
```

Then locate the `result.append(...)` block inside the loop. The final dict-merge needs to keep the `pid` field that `resolve_pids` added to each `w`. Confirm the existing `{**w, **parsed, **git, **pr, ...}` already merges `pid` — it does, since `w` is the dict `resolve_pids` mutated.

- [ ] **Step 3: Call the helper from `auto_rename_session`**

In `server.py`, locate `auto_rename_session`. Find:
```python
all_windows = list_windows()
target_windows = [w for w in all_windows if w["session"] == session]
```
Insert immediately after:
```python
_attach_git_then_resolve_pids(target_windows)
```

(Resolving the full `all_windows` list would update last_seen for sessions we aren't editing, which is fine but wasteful; scoping to `target_windows` is enough — last_seen for other sessions gets refreshed on the next /api/state poll.)

- [ ] **Step 4: Add a per-window pid resolution to `auto_rename_window`**

In `server.py`, locate `auto_rename_window`. Just below the `meta = tmux(...)` block where `current_name` and `cwd` are unpacked, add:

```python
    # Single-window pid resolution: build a one-element list and reuse the
    # batch helper so `last_seen` stays current for this window too.
    one = [{"session": session, "index": index, "name": current_name, "active": False, "cwd": cwd, "pid_raw": ""}]
    _attach_git_then_resolve_pids(one)
    pid = one[0].get("pid")
```

Then find the **no-change return** (currently `return {"ok": True, "applied": False, "old": current_name, "new": current_name}`). Replace with:

```python
        return {"ok": True, "applied": False, "old": current_name, "new": current_name, "pid": pid}
```

And find the **success return** at the end of the function (`return {"ok": True, "applied": True, "old": current_name, "new": new_name}`). Replace with:

```python
    return {"ok": True, "applied": True, "old": current_name, "new": new_name, "pid": pid}
```

- [ ] **Step 5: Verify pid surfaces on the wire**

```bash
cd /Users/tom/dev/periscope && uv run server.py &
SERVER=$!
sleep 2
curl -s http://127.0.0.1:8765/api/state | python3 -m json.tool | grep -E '"pid"|"name"' | head -20
kill $SERVER
wait $SERVER 2>/dev/null
```

Expected: every window object has a `"pid"` field with an 8-char hex string. Then run:

```bash
tmux show-options -wv -t $(tmux list-windows -a -F '#{session_name}:#{window_index}' | head -1) @periscope_id
```

Expected: the same 8-char hex appears (or an empty result if no tmux windows are running — but if you have any window, it should be stamped).

- [ ] **Step 6: Verify rename survival**

In tmux, rename a window (`tmux rename-window -t <session>:<index> new-name`). Run `/api/state` again. Confirm the same `pid` shows up for that window. Cat `~/.config/periscope/state.json` and confirm the pid's `last_seen.name` now reflects `new-name`.

- [ ] **Step 7: Commit**

```bash
git add server.py
git commit -m "config: /api/state and /api/auto-rename-* resolve and emit @periscope_id"
```

---

### Task 2.4: Post-poll GC of stale id-only entries

**Files:**
- Modify: `server.py` (`resolve_pids`)

- [ ] **Step 1: Add GC at the end of `resolve_pids`**

In `server.py`, find the end of `resolve_pids` (the `if dirty: _write_state(_STATE)` line). Replace that line with:

```python
    # GC: drop windows entries that (a) carry no notes and no tags, AND (b)
    # weren't refreshed this pass, AND (c) have a last_seen older than 30
    # days. Annotated entries are immune — losing one would lose notes.
    refreshed = taken
    cutoff = now_ts - _PID_TTL_S
    for pid in list(wblock.keys()):
        if pid in refreshed:
            continue
        entry = wblock[pid]
        if entry.get("notes") or entry.get("tags"):
            continue
        ts = (entry.get("last_seen") or {}).get("ts") or 0
        if ts < cutoff:
            del wblock[pid]
            dirty = True
    if dirty:
        _write_state(_STATE)
```

- [ ] **Step 2: Verify GC fires on a synthetic stale entry**

Stop the dev server. Manually edit `~/.config/periscope/state.json` to add an old entry:

```json
"windows": {
  "deadbeef": {
    "last_seen": {"session": "fake", "name": "fake", "branch": null, "cwd": null, "ts": 1
  }
}
```

(Replace the entire `windows` block; keep the ui/commands blocks intact.)

Restart the dev server and load the dashboard. Confirm the `deadbeef` entry is gone from `state.json`. Confirm any real windows you have still keep their pids.

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "config: GC drops 30d-stale, unannotated last_seen hints after pid resolution"
```

---

## Phase 3 — annotations UI

### Task 3.1: `PUT`/`DELETE /api/prefs/windows/{pid}` endpoints

**Files:**
- Modify: `server.py` (continue in the `/api/prefs` section)

- [ ] **Step 1: Add the annotation endpoints**

In `server.py`, just below the `patch_prefs_ui` endpoint from task 1.3, insert:

```python
class WindowAnnotation(BaseModel):
    notes: str | None = None
    tags: list[str] | None = None


@app.put("/api/prefs/windows/{pid}")
async def put_window_annotation(pid: str, body: WindowAnnotation):
    """Set/replace the annotation fields on a window. `last_seen` is left
    intact — only notes/tags are managed via this endpoint."""
    if not pid or not pid.isalnum():
        return {"ok": False, "error": "invalid pid"}
    patch = body.model_dump(exclude_none=True)
    # Coerce tags to a trimmed unique list, preserving order.
    if "tags" in patch:
        seen: set[str] = set()
        clean: list[str] = []
        for t in patch["tags"]:
            t = (t or "").strip()
            if t and t not in seen:
                seen.add(t)
                clean.append(t)
        patch["tags"] = clean
    async with _STATE_LOCK:
        entry = _STATE["windows"].setdefault(pid, {})
        for k in ("notes", "tags"):
            if k in patch:
                entry[k] = patch[k]
            elif k in entry and patch.get(k) is None and k in patch:
                del entry[k]
        # Drop empty notes / empty tag list to keep the file tidy.
        if entry.get("notes") == "":
            entry.pop("notes", None)
        if entry.get("tags") == []:
            entry.pop("tags", None)
        _write_state(_STATE)
    return {"ok": True, "pid": pid, "annotation": {
        "notes": entry.get("notes"),
        "tags": entry.get("tags") or [],
    }}


@app.delete("/api/prefs/windows/{pid}")
async def delete_window_annotation(pid: str):
    """Remove notes + tags. last_seen is preserved (it's the rebind hint)."""
    if not pid or not pid.isalnum():
        return {"ok": False, "error": "invalid pid"}
    async with _STATE_LOCK:
        entry = _STATE["windows"].get(pid)
        if entry:
            entry.pop("notes", None)
            entry.pop("tags", None)
            _write_state(_STATE)
    return {"ok": True, "pid": pid}
```

- [ ] **Step 2: Verify the endpoints round-trip**

```bash
cd /Users/tom/dev/periscope && uv run server.py &
SERVER=$!
sleep 2

PID=$(curl -s http://127.0.0.1:8765/api/state | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["windows"][0]["pid"])')
echo "Using pid: $PID"

curl -s -X PUT -H 'content-type: application/json' \
  -d '{"notes":"hello world","tags":["a","b","b","c"]}' \
  http://127.0.0.1:8765/api/prefs/windows/$PID
echo

curl -s http://127.0.0.1:8765/api/prefs | python3 -m json.tool | grep -A 5 "\"$PID\""

curl -s -X DELETE http://127.0.0.1:8765/api/prefs/windows/$PID
echo

curl -s http://127.0.0.1:8765/api/prefs | python3 -m json.tool | grep -A 5 "\"$PID\""

kill $SERVER
wait $SERVER 2>/dev/null
```

Expected: after PUT, the entry shows `"notes":"hello world"` and `"tags":["a","b","c"]` (dedup applied). After DELETE, the same entry shows only `last_seen` (no notes/tags).

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "config: PUT/DELETE /api/prefs/windows/{pid} for annotation set/clear"
```

---

### Task 3.2: Extend `prefs.js` with annotation methods

**Files:**
- Modify: `static/prefs.js`

- [ ] **Step 1: Add the annotation surface**

Open `static/prefs.js`. Just below the `// ── UI prefs ──...` section and above the `// ── One-time localStorage → server migration ──` section, insert:

```javascript
// ── Window annotations ──────────────────────────────────────────────────

export function getAnnotation(pid) {
  if (!pid) return null;
  const entry = cache.windows[pid];
  if (!entry) return null;
  const notes = entry.notes || "";
  const tags = entry.tags || [];
  if (!notes && !tags.length) return null;
  return { notes, tags };
}

export function hasAnnotation(pid) {
  return getAnnotation(pid) !== null;
}

export async function setAnnotation(pid, { notes, tags }) {
  if (!cache.loaded) {
    await loadPrefs();
    if (!cache.loaded) return false;
  }
  const previous = cache.windows[pid];
  const entry = cache.windows[pid] || {};
  cache.windows[pid] = {
    ...entry,
    notes: notes ?? entry.notes,
    tags: tags ?? entry.tags,
  };
  const data = await apiCall("save annotation", `/api/prefs/windows/${encodeURIComponent(pid)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ notes, tags }),
  });
  if (!data) {
    if (previous === undefined) delete cache.windows[pid];
    else cache.windows[pid] = previous;
    return false;
  }
  // Server returns the cleaned annotation (deduped tags, trimmed); use it.
  cache.windows[pid] = { ...(cache.windows[pid] || {}), ...data.annotation };
  return true;
}

export async function deleteAnnotation(pid) {
  if (!cache.loaded) return false;
  const previous = cache.windows[pid];
  if (cache.windows[pid]) {
    delete cache.windows[pid].notes;
    delete cache.windows[pid].tags;
  }
  const data = await apiCall("clear annotation", `/api/prefs/windows/${encodeURIComponent(pid)}`, {
    method: "DELETE",
  });
  if (!data) {
    cache.windows[pid] = previous;
    return false;
  }
  return true;
}
```

- [ ] **Step 2: Verify the methods work from devtools**

Run the dev server, open the page, open devtools console:

```javascript
const m = await import('/prefs.js');
const pid = state.lastWindows[0].pid;  // grab the first window's pid
await m.setAnnotation(pid, { notes: "test", tags: ["one"] });
console.log(m.getAnnotation(pid));     // {notes:"test", tags:["one"]}
await m.deleteAnnotation(pid);
console.log(m.getAnnotation(pid));     // null
```

`state` isn't a global — instead grab the pid from `await fetch('/api/state').then(r => r.json())`. Or set a temporary `window.__state = state` in `state.js` for the test. (Revert that temporary if added.)

- [ ] **Step 3: Commit**

```bash
git add static/prefs.js
git commit -m "config: prefs.js gets getAnnotation/setAnnotation/deleteAnnotation"
```

---

### Task 3.3: Card indicator for annotated windows

**Files:**
- Modify: `static/grid.js` (`renderCard` and `renderStreamRow`)
- Modify: `static/styles.css` (add `.card-anno` style)

- [ ] **Step 1: Add the indicator to `renderCard`**

Open `static/grid.js`. Find `renderCard` (~line 47). At the top of the function (right after the `const stateClass = ...` line), add:

```javascript
  const anno = prefs.hasAnnotation(w.pid)
    ? `<span class="card-anno" title="has notes">📝</span>`
    : "";
```

Then in the returned template's `<header class="card-head">` block, insert `${anno}` right after `${statusLabel}` (before the kill button) so the head row reads:

```javascript
      <header class="card-head">
        <span class="card-title">${escapeHtml(w.name)}</span>
        <span class="card-idx">${w.index}</span>
        ${statusLabel}
        ${anno}
        <button class="card-kill" data-target="${w.target}" data-name="${escapeHtml(w.name)}" title="kill this window">✕</button>
      </header>
```

- [ ] **Step 2: Add the indicator to `renderStreamRow`**

In the same file, find `renderStreamRow`. Inside the `<div class="stream-title">` block, after the `<em>${branchPart}</em>` line, add:

```javascript
          ${prefs.hasAnnotation(w.pid) ? `<span class="stream-anno" title="has notes">📝</span>` : ""}
```

- [ ] **Step 3: Add minimal CSS**

In `static/styles.css`, add near the existing `.card-head` / card status rules:

```css
.card-anno,
.stream-anno {
  font-size: 11px;
  opacity: 0.7;
  margin-left: 2px;
  cursor: help;
}
.card-anno:hover,
.stream-anno:hover {
  opacity: 1;
}
```

- [ ] **Step 4: Verify**

Run the dev server. Use devtools to add an annotation via `prefs.setAnnotation(pid, {notes:'x'})` for some visible window. Wait for the next poll (~3s) or trigger a re-render by clicking a filter twice. Confirm the 📝 indicator appears on that card (and on the stream row if the window has been engaged through periscope).

- [ ] **Step 5: Commit**

```bash
git add static/grid.js static/styles.css
git commit -m "config: 📝 indicator on cards and stream rows for annotated windows"
```

---

### Task 3.4: "Notes" section in the modal sidebar

**Files:**
- Modify: `static/modal.js`
- Modify: `static/styles.css`

- [ ] **Step 1: Add the Notes renderer and wire it into `renderModalSidebar`**

Open `static/modal.js`. Add this at the top of the file just after the existing `import { poll } from './grid.js';` line:

```javascript
import * as prefs from './prefs.js';
```

Then locate `renderModalSidebar`. Replace it with:

```javascript
function renderModalSidebar(data) {
  if (!modalSide) return;
  modalSide.innerHTML = `
    <section class="modal-side-section">
      <h4>Linked</h4>
      ${renderPRCard(data)}
      ${renderLinearPlaceholder()}
    </section>
    <section class="modal-side-section modal-side-notes">
      <h4>Notes</h4>
      ${renderNotesEditor(data)}
    </section>
    <section class="modal-side-section modal-side-activity">
      <h4>Activity</h4>
      ${renderActivityTimeline(data.activity)}
    </section>
  `;
  wireNotesEditor(data);
}
```

Just below `renderActivityTimeline`, add the renderer and the wiring:

```javascript
function renderNotesEditor(data) {
  const pid = data.pid || "";
  const ann = pid ? prefs.getAnnotation(pid) : null;
  const notes = ann?.notes || "";
  const tags = ann?.tags || [];
  const chips = tags
    .map(
      (t, i) =>
        `<span class="tag-chip" data-tag-i="${i}">${escapeHtml(t)}<button class="tag-chip-x" data-tag-i="${i}" title="remove">×</button></span>`
    )
    .join("");
  return `
    <textarea id="modal-notes" class="modal-notes" placeholder="${
      pid ? "Notes — saves on blur" : "Notes unavailable (no pid)"
    }" ${pid ? "" : "disabled"}>${escapeHtml(notes)}</textarea>
    <div class="tag-row">
      <div class="tag-chips" id="modal-tags">${chips}</div>
      <input id="modal-tag-input" class="modal-tag-input" type="text"
             placeholder="add tag, Enter or comma" ${pid ? "" : "disabled"}>
    </div>
  `;
}

let _notesTimer = null;

function wireNotesEditor(data) {
  const pid = data.pid;
  if (!pid) return;
  const ta = document.getElementById("modal-notes");
  const ti = document.getElementById("modal-tag-input");
  const tagsHost = document.getElementById("modal-tags");
  if (!ta || !ti || !tagsHost) return;

  // Debounce typing 600ms; flush immediately on blur.
  const flushNotes = () => {
    const ann = prefs.getAnnotation(pid) || { notes: "", tags: [] };
    prefs.setAnnotation(pid, { notes: ta.value, tags: ann.tags });
  };
  ta.addEventListener("input", () => {
    clearTimeout(_notesTimer);
    _notesTimer = setTimeout(flushNotes, 600);
  });
  ta.addEventListener("blur", () => {
    clearTimeout(_notesTimer);
    flushNotes();
  });
  // Stop Escape/Enter from bubbling to the modal handler.
  ta.addEventListener("keydown", (e) => e.stopPropagation());

  const submitTag = () => {
    const raw = ti.value.trim();
    if (!raw) return;
    const ann = prefs.getAnnotation(pid) || { notes: ta.value, tags: [] };
    const parts = raw.split(/[\s,]+/).filter(Boolean);
    const nextTags = [...ann.tags, ...parts];
    prefs.setAnnotation(pid, { notes: ta.value, tags: nextTags });
    ti.value = "";
    refreshModalHeader();  // re-render the sidebar with the new chip
  };
  ti.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      submitTag();
    }
  });

  tagsHost.addEventListener("click", (e) => {
    const btn = e.target.closest(".tag-chip-x");
    if (!btn) return;
    const i = Number(btn.dataset.tagI);
    const ann = prefs.getAnnotation(pid) || { notes: ta.value, tags: [] };
    const nextTags = ann.tags.filter((_, idx) => idx !== i);
    prefs.setAnnotation(pid, { notes: ta.value, tags: nextTags });
    refreshModalHeader();
  });
}
```

- [ ] **Step 2: Add `pid` to `/api/pane`'s output so the modal has it**

Open `server.py`. Locate the `/api/pane` handler. Add this between the `meta = tmux(...)` block and the `git = cached_git_state(...)` line:

```python
    one = [{"session": session, "index": index, "name": window_name, "active": False, "cwd": cwd, "pid_raw": ""}]
    _attach_git_then_resolve_pids(one)
    pid = one[0].get("pid")
```

Then locate the final `return` of the function (a dict that currently includes `"content"`, `"target"`, `"name"`, `"cwd"`, `"session"`, `"activity"`, and the merged `**parsed/**git/**pr`). Add `"pid": pid,` as a new key — e.g., immediately after `"session": session,`:

```python
    return {
        "content": content,
        "target": target,
        "name": window_name,
        "cwd": cwd_display,
        "session": session,
        "pid": pid,
        "activity": activity,
        **parsed,
        **git,
        **pr,
    }
```

- [ ] **Step 3: Add the supporting CSS**

In `static/styles.css`, add somewhere in the modal-side section:

```css
.modal-side-notes .modal-notes {
  width: 100%;
  min-height: 80px;
  resize: vertical;
  font-family: var(--mono);
  font-size: 11.5px;
  background: var(--bg-1);
  color: var(--fg-1);
  border: 1px solid var(--line-soft);
  border-radius: var(--r-sm);
  padding: 6px 8px;
}
.modal-side-notes .modal-notes:focus {
  outline: none;
  border-color: var(--accent);
}
.tag-row {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.tag-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.tag-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 6px 1px 8px;
  font-size: 10.5px;
  background: var(--bg-2);
  border: 1px solid var(--line-soft);
  border-radius: 999px;
  color: var(--fg-2);
}
.tag-chip-x {
  background: none;
  border: 0;
  color: var(--fg-3);
  cursor: pointer;
  padding: 0 2px;
  font-size: 13px;
  line-height: 1;
}
.tag-chip-x:hover { color: var(--s-danger); }
.modal-tag-input {
  font-family: var(--mono);
  font-size: 11px;
  background: var(--bg-1);
  border: 1px solid var(--line-soft);
  border-radius: var(--r-sm);
  padding: 3px 6px;
  color: var(--fg-1);
}
```

- [ ] **Step 4: Verify end-to-end**

Run the dev server, open a window's modal. Confirm the sidebar now has three sections: Linked, Notes, Activity. Type into the notes textarea, blur, reopen — confirm the text persists. Add a tag via the input, hit Enter — confirm the chip appears and is in `state.json`. Click the × on a chip — confirm it's gone in both UI and file. Close and reopen the modal — confirm the annotation indicator (📝) appears on the card.

- [ ] **Step 5: Commit**

```bash
git add server.py static/modal.js static/styles.css
git commit -m "config: modal sidebar 'Notes' section with debounced text + tag chips (annotations bound by pid)"
```

---

## Phase 4 — configurable commands

### Task 4.1: Extract Escape-handler into `static/overlay.js`

**Files:**
- Create: `static/overlay.js`
- Modify: `static/modal.js`

- [ ] **Step 1: Create the helper**

Create `static/overlay.js`:

```javascript
// Lightweight shared overlay primitives. Multiple modals (pane modal,
// commands modal) need an Escape handler — without a shared registry they'd
// fight each other (whoever attached first or last would close every modal).
//
// Each modal registers an `onEscape` callback while open and unregisters on
// close. Only the most-recently-opened modal's callback fires per Escape.

const stack = [];

export function pushEscape(onEscape) {
  stack.push(onEscape);
}

export function popEscape(onEscape) {
  const i = stack.lastIndexOf(onEscape);
  if (i >= 0) stack.splice(i, 1);
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!stack.length) return;
  const top = stack[stack.length - 1];
  top(e);
});
```

- [ ] **Step 2: Replace `modal.js`'s global Escape handler**

Open `static/modal.js`. Add at the top of the file (with the other imports):

```javascript
import { pushEscape, popEscape } from './overlay.js';
```

Locate `openModal` and add to it (right after `state.activeTarget = target;`):

```javascript
  pushEscape(closeModal);
```

Locate `closeModal` and add (right after the `state.activeTarget = null;` line):

```javascript
  popEscape(closeModal);
```

Locate `initModal`. Remove the document-level Escape handler:

```javascript
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
  });
```

- [ ] **Step 3: Verify Escape still closes the pane modal**

Run the dev server. Open any card's modal. Press Escape. Modal closes. Open it again, click outside — still closes.

- [ ] **Step 4: Commit**

```bash
git add static/overlay.js static/modal.js
git commit -m "config: shared Escape stack (overlay.js) so multiple modals don't fight"
```

---

### Task 4.2: `/api/prefs/commands` endpoints + first-boot seed

**Files:**
- Modify: `server.py`

- [ ] **Step 1: Add the seed function and call it once at load**

In `server.py`, just below the `_load_state` function (from task 1.1), insert:

```python
_DEFAULT_COMMANDS = [
    {"label": "claude", "exec": "claude"},
    {"label": "shell", "exec": ""},
    {"label": "vim", "exec": "vim"},
]


def _seed_commands_if_empty() -> None:
    """If `commands` is empty (fresh install or pre-phase-4 state.json),
    seed the three legacy defaults so the new-window tile keeps working
    while phase 4 is in flight."""
    if not _STATE["commands"]:
        _STATE["commands"] = [dict(c) for c in _DEFAULT_COMMANDS]
        _write_state(_STATE)


_seed_commands_if_empty()
```

(The `_seed_commands_if_empty()` call at the bottom runs once at server startup, right after `_STATE = _load_state()`.)

- [ ] **Step 2: Add the commands endpoints**

In `server.py`, just below the `delete_window_annotation` endpoint from task 3.1, insert:

```python
class Command(BaseModel):
    label: str
    exec: str = ""


@app.post("/api/prefs/commands")
async def add_command(body: Command):
    label = body.label.strip()
    if not label:
        return {"ok": False, "error": "empty label"}
    async with _STATE_LOCK:
        if any(c["label"] == label for c in _STATE["commands"]):
            return {"ok": False, "error": f"duplicate label: {label!r}"}
        _STATE["commands"].append({"label": label, "exec": body.exec or ""})
        _write_state(_STATE)
    return {"ok": True, "commands": _STATE["commands"]}


@app.put("/api/prefs/commands/{label}")
async def update_command(label: str, body: Command):
    new_label = body.label.strip()
    if not new_label:
        return {"ok": False, "error": "empty label"}
    async with _STATE_LOCK:
        for c in _STATE["commands"]:
            if c["label"] == label:
                if new_label != label and any(
                    other["label"] == new_label for other in _STATE["commands"] if other is not c
                ):
                    return {"ok": False, "error": f"duplicate label: {new_label!r}"}
                c["label"] = new_label
                c["exec"] = body.exec or ""
                _write_state(_STATE)
                return {"ok": True, "commands": _STATE["commands"]}
    return {"ok": False, "error": f"unknown label: {label!r}"}


@app.delete("/api/prefs/commands/{label}")
async def delete_command(label: str):
    async with _STATE_LOCK:
        before = len(_STATE["commands"])
        _STATE["commands"] = [c for c in _STATE["commands"] if c["label"] != label]
        if len(_STATE["commands"]) == before:
            return {"ok": False, "error": f"unknown label: {label!r}"}
        _write_state(_STATE)
    return {"ok": True, "commands": _STATE["commands"]}


class CommandsReorder(BaseModel):
    labels: list[str]


@app.put("/api/prefs/commands")
async def reorder_commands(body: CommandsReorder):
    """Reorder the commands list to match `labels`. Unknown labels are
    ignored; missing labels stay in place at the end."""
    async with _STATE_LOCK:
        by_label = {c["label"]: c for c in _STATE["commands"]}
        ordered = [by_label[l] for l in body.labels if l in by_label]
        leftover = [c for c in _STATE["commands"] if c["label"] not in {l for l in body.labels if l in by_label}]
        _STATE["commands"] = ordered + leftover
        _write_state(_STATE)
    return {"ok": True, "commands": _STATE["commands"]}
```

- [ ] **Step 3: Verify the endpoints work**

```bash
rm -f ~/.config/periscope/state.json
cd /Users/tom/dev/periscope && uv run server.py &
SERVER=$!
sleep 2

# Seeded defaults
curl -s http://127.0.0.1:8765/api/prefs | python3 -m json.tool | grep -A 10 commands

# Add
curl -s -X POST -H 'content-type: application/json' \
  -d '{"label":"htop","exec":"htop"}' \
  http://127.0.0.1:8765/api/prefs/commands
echo

# Update
curl -s -X PUT -H 'content-type: application/json' \
  -d '{"label":"htop","exec":"htop -d 5"}' \
  http://127.0.0.1:8765/api/prefs/commands/htop
echo

# Reorder
curl -s -X PUT -H 'content-type: application/json' \
  -d '{"labels":["htop","claude","shell","vim"]}' \
  http://127.0.0.1:8765/api/prefs/commands
echo

# Delete
curl -s -X DELETE http://127.0.0.1:8765/api/prefs/commands/htop
echo

kill $SERVER
wait $SERVER 2>/dev/null
```

Expected: seeded list contains claude/shell/vim; add appends htop; update edits its exec; reorder puts htop first; delete removes it.

- [ ] **Step 4: Commit**

```bash
git add server.py
git commit -m "config: /api/prefs/commands CRUD + reorder; seed claude/shell/vim on first boot"
```

---

### Task 4.3: Switch `/api/window/new` to the `exec` contract

**Files:**
- Modify: `server.py` (`window_new`)
- Modify: `static/prefs.js` (add `getCommands`)
- Modify: `static/grid.js` (`renderNewTile`, `handleNewWindow`)

- [ ] **Step 1: Update the server handler**

In `server.py`, locate `window_new` (~line 1054). Replace it with:

```python
@app.post("/api/window/new")
def window_new(session: str, exec: str = "", mode: str | None = None):
    """Spawn a window in `session`. If `exec` is non-empty, type it followed
    by Enter into the new pane (after a 100ms pause so the shell's rc has
    completed loading). Empty `exec` = bare prompt.

    The legacy `mode` query param is supported for one release for the
    benefit of clients still on the old contract; it maps to claude/vim/shell
    -> 'claude'/'vim'/''.
    """
    if mode and not exec:
        exec = {"claude": "claude", "vim": "vim", "shell": ""}.get(mode, "")
    cwd = tmux(
        "display-message", "-t", f"{session}:", "-p", "#{pane_current_path}",
    ).strip() or os.path.expanduser("~")
    ok, msg = _tmux_mutate(
        "new-window", "-t", f"{session}:", "-c", cwd,
        "-P", "-F", "#{window_index}",
    )
    if not ok:
        return {"ok": False, "error": msg}
    try:
        index = int(msg)
    except ValueError:
        return {"ok": False, "error": f"tmux returned unexpected index: {msg!r}"}
    target = f"{session}:{index}"
    cmd = exec.strip()
    if cmd:
        # Let the shell finish its rc before the command line arrives, so the
        # command runs as a real prompt entry rather than mid-rc echoed text.
        time.sleep(0.1)
        tmux("send-keys", "-t", target, cmd, "Enter")
    note_focus(target)
    note_action(target)
    return {"ok": True, "session": session, "index": index, "target": target, "exec": cmd}
```

- [ ] **Step 2: Add `prefs.getCommands()` to the prefs surface**

Open `static/prefs.js`. In the `// ── UI prefs ──...` section's general getters, add (near `getView`):

```javascript
export function getCommands() {
  return cache.commands || [];
}
```

- [ ] **Step 3: Update `renderNewTile` to read from prefs**

Open `static/grid.js`. Locate `renderNewTile` (~line 125) and replace it with:

```javascript
function renderNewTile(session) {
  // Read commands from prefs. First entry is the primary (top, larger hit
  // area); the rest stack below. Falls back to an empty tile if prefs hasn't
  // loaded yet — render() runs again on every poll, so the buttons appear
  // within the polling interval after bootstrap.
  const s = escapeHtml(session);
  const commands = prefs.getCommands();
  if (!commands.length) {
    return `<div class="card card-new" data-session="${s}"></div>`;
  }
  const [primary, ...rest] = commands;
  const btn = (cmd, cls) => {
    const label = escapeHtml(cmd.label);
    const execAttr = escapeHtml(cmd.exec || "");
    return `<button class="new-window${cls}" data-session="${s}" data-exec="${execAttr}">+ ${label}</button>`;
  };
  const stack = rest.length
    ? `<div class="new-window-stack">${rest.map((c) => btn(c, "")).join("")}</div>`
    : "";
  return `
    <div class="card card-new" data-session="${s}">
      ${btn(primary, " is-primary")}
      ${stack}
    </div>
  `;
}
```

- [ ] **Step 4: Update `handleNewWindow` to send `exec` instead of `mode`**

In the same file, locate `handleNewWindow` and replace it with:

```javascript
async function handleNewWindow(btn) {
  const session = btn.dataset.session;
  const exec = btn.dataset.exec || "";
  const tile = btn.closest(".card-new");
  // Disable all buttons in the tile while the request is in flight so a
  // double-click can't spawn two windows.
  tile.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    await apiCall(
      "new window",
      `/api/window/new?session=${encodeURIComponent(session)}&exec=${encodeURIComponent(exec)}`,
      { method: "POST" }
    );
  } finally {
    tile.querySelectorAll("button").forEach((b) => (b.disabled = false));
  }
  poll();
}
```

- [ ] **Step 5: Verify in the dashboard**

Run the dev server. Confirm the three default buttons (`+ claude`, `+ shell`, `+ vim`) still appear on each new-tile. Click each in turn in a test session — confirm:
- `+ claude` opens a window and types `claude`.
- `+ shell` opens a bare shell.
- `+ vim` opens a window and types `vim`.

- [ ] **Step 6: Commit**

```bash
git add server.py static/grid.js static/prefs.js
git commit -m "config: /api/window/new exec contract; new-window tile reads from prefs.getCommands()"
```

---

### Task 4.4: Commands editor modal

**Files:**
- Create: `static/commands-modal.js`
- Modify: `static/index.html` (add gear button + `#commands-modal` div)
- Modify: `static/app.js` (wire the gear button)
- Modify: `static/styles.css` (commands modal styling)

- [ ] **Step 1: Add the modal HTML**

Open `static/index.html`. Inside the filters `<nav>`, just before the `<div class="view-switch" ...>`, insert:

```html
      <button id="open-commands" class="filter-btn is-action" title="edit + claude / + shell / + ... commands">⚙</button>
```

After the closing `</header>` tag (and before `<main id="grid">`), insert the modal markup:

```html
  <div id="commands-modal" class="hidden commands-modal-overlay">
    <div class="commands-modal-card">
      <header class="commands-modal-head">
        <h2>New-window commands</h2>
        <button id="commands-modal-close" title="close">×</button>
      </header>
      <p class="commands-modal-sub">First row is the primary button. Drag rows to reorder. Empty <code>exec</code> = bare shell.</p>
      <div id="commands-modal-list"></div>
      <button id="commands-modal-add" class="commands-modal-add">+ add command</button>
    </div>
  </div>
```

- [ ] **Step 2: Add the command-mutator surface to `prefs.js`**

Open `static/prefs.js`. Just below `getCommands` (added in task 4.3 step 2), add:

```javascript
export async function addCommand({ label, exec }) {
  if (!cache.loaded) return false;
  const data = await apiCall("add command", "/api/prefs/commands", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label, exec: exec || "" }),
  });
  if (!data) return false;
  cache.commands = data.commands;
  return true;
}

export async function updateCommand(oldLabel, { label, exec }) {
  if (!cache.loaded) return false;
  const data = await apiCall("update command", `/api/prefs/commands/${encodeURIComponent(oldLabel)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label, exec: exec || "" }),
  });
  if (!data) return false;
  cache.commands = data.commands;
  return true;
}

export async function deleteCommand(label) {
  if (!cache.loaded) return false;
  const data = await apiCall("delete command", `/api/prefs/commands/${encodeURIComponent(label)}`, {
    method: "DELETE",
  });
  if (!data) return false;
  cache.commands = data.commands;
  return true;
}

export async function reorderCommands(labels) {
  if (!cache.loaded) return false;
  const data = await apiCall("reorder commands", "/api/prefs/commands", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ labels }),
  });
  if (!data) return false;
  cache.commands = data.commands;
  return true;
}
```

- [ ] **Step 3: Create the commands-modal module**

Create `static/commands-modal.js`:

```javascript
// Commands editor modal. Open/close + row state + drag reorder. Persists
// every mutation through prefs.js — no batched save button.

import * as prefs from './prefs.js';
import { escapeHtml } from './util.js';
import { pushEscape, popEscape } from './overlay.js';

const modal = document.getElementById("commands-modal");
const closeBtn = document.getElementById("commands-modal-close");
const addBtn = document.getElementById("commands-modal-add");
const listEl = document.getElementById("commands-modal-list");

let isOpen = false;

function render() {
  const commands = prefs.getCommands();
  listEl.innerHTML = commands
    .map(
      (c, i) => `
        <div class="commands-row" draggable="true" data-label="${escapeHtml(c.label)}" data-i="${i}">
          <span class="commands-grip" title="drag to reorder">⋮⋮</span>
          <input class="commands-label" value="${escapeHtml(c.label)}" placeholder="label">
          <input class="commands-exec" value="${escapeHtml(c.exec || "")}" placeholder="exec (empty = bare shell)">
          <button class="commands-del" title="delete">×</button>
        </div>`
    )
    .join("");
}

async function handleAdd() {
  const base = "command";
  let label = base;
  let n = 1;
  const taken = new Set(prefs.getCommands().map((c) => c.label));
  while (taken.has(label)) label = `${base}-${++n}`;
  const ok = await prefs.addCommand({ label, exec: "" });
  if (ok) render();
}

async function handleUpdateRow(row) {
  const oldLabel = row.dataset.label;
  const newLabel = row.querySelector(".commands-label").value.trim();
  const newExec = row.querySelector(".commands-exec").value;
  if (!newLabel) return;
  const ok = await prefs.updateCommand(oldLabel, { label: newLabel, exec: newExec });
  if (ok) render();
}

async function handleDeleteRow(row) {
  const label = row.dataset.label;
  const ok = await prefs.deleteCommand(label);
  if (ok) render();
}

async function handleReorder(newOrder) {
  const ok = await prefs.reorderCommands(newOrder);
  if (ok) render();
}

// ── Drag/drop reorder ───────────────────────────────────────────────────

let dragLabel = null;

function bindDragHandlers() {
  listEl.addEventListener("dragstart", (e) => {
    const row = e.target.closest(".commands-row");
    if (!row) return;
    dragLabel = row.dataset.label;
    row.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
  });
  listEl.addEventListener("dragend", () => {
    listEl.querySelectorAll(".commands-row").forEach((r) => r.classList.remove("dragging"));
    listEl.querySelectorAll(".commands-row").forEach((r) =>
      r.classList.remove("drag-over-top", "drag-over-bottom")
    );
    dragLabel = null;
  });
  listEl.addEventListener("dragover", (e) => {
    const row = e.target.closest(".commands-row");
    if (!row) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const rect = row.getBoundingClientRect();
    const before = e.clientY < rect.top + rect.height / 2;
    row.classList.toggle("drag-over-top", before);
    row.classList.toggle("drag-over-bottom", !before);
  });
  listEl.addEventListener("dragleave", (e) => {
    const row = e.target.closest(".commands-row");
    if (row) row.classList.remove("drag-over-top", "drag-over-bottom");
  });
  listEl.addEventListener("drop", (e) => {
    const row = e.target.closest(".commands-row");
    if (!row || !dragLabel) return;
    e.preventDefault();
    const targetLabel = row.dataset.label;
    if (targetLabel === dragLabel) return;
    const rect = row.getBoundingClientRect();
    const before = e.clientY < rect.top + rect.height / 2;
    const labels = prefs.getCommands().map((c) => c.label);
    const idxDrag = labels.indexOf(dragLabel);
    if (idxDrag < 0) return;
    labels.splice(idxDrag, 1);
    const idxTarget = labels.indexOf(targetLabel);
    const insertAt = before ? idxTarget : idxTarget + 1;
    labels.splice(insertAt, 0, dragLabel);
    handleReorder(labels);
  });
}

// ── Open / close ────────────────────────────────────────────────────────

export function openCommandsModal() {
  if (isOpen) return;
  isOpen = true;
  render();
  modal.classList.remove("hidden");
  document.body.classList.add("commands-modal-open");
  pushEscape(closeCommandsModal);
}

export function closeCommandsModal() {
  if (!isOpen) return;
  isOpen = false;
  modal.classList.add("hidden");
  document.body.classList.remove("commands-modal-open");
  popEscape(closeCommandsModal);
}

export function initCommandsModal() {
  closeBtn.addEventListener("click", closeCommandsModal);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeCommandsModal();
  });
  addBtn.addEventListener("click", handleAdd);
  listEl.addEventListener("change", (e) => {
    const row = e.target.closest(".commands-row");
    if (!row) return;
    if (e.target.matches(".commands-label, .commands-exec")) handleUpdateRow(row);
  });
  listEl.addEventListener("click", (e) => {
    const delBtn = e.target.closest(".commands-del");
    if (!delBtn) return;
    const row = delBtn.closest(".commands-row");
    handleDeleteRow(row);
  });
  bindDragHandlers();
}
```

- [ ] **Step 4: Wire the gear button in `app.js`**

Open `static/app.js`. Add to the imports:

```javascript
import { initCommandsModal, openCommandsModal } from './commands-modal.js';
```

Add at the bottom of `bootstrap()` (right after `initGrid()`):

```javascript
  initCommandsModal();
  document.getElementById("open-commands").addEventListener("click", openCommandsModal);
```

- [ ] **Step 5: Add the modal styling**

In `static/styles.css`, add:

```css
.commands-modal-overlay {
  position: fixed;
  inset: 0;
  background: oklch(0 0 0 / 0.55);
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: center;
}
.commands-modal-overlay.hidden { display: none; }

.commands-modal-card {
  background: var(--bg-modal);
  border-radius: var(--r-lg);
  padding: 18px 22px 16px;
  width: min(620px, 90vw);
  box-shadow: var(--shadow-modal);
}
.commands-modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
}
.commands-modal-head h2 { margin: 0; font-size: 15px; }
.commands-modal-head button {
  background: none; border: 0; color: var(--fg-2); font-size: 18px;
  cursor: pointer;
}
.commands-modal-sub {
  font-size: 11px; color: var(--fg-3); margin: 0 0 10px;
}
.commands-modal-sub code {
  background: var(--bg-2); padding: 1px 4px; border-radius: 3px;
  font-size: 10.5px;
}
.commands-row {
  display: grid;
  grid-template-columns: 18px 1fr 2fr 24px;
  gap: 8px;
  align-items: center;
  padding: 6px 4px;
  border-radius: var(--r-sm);
  cursor: grab;
}
.commands-row.dragging { opacity: 0.4; }
.commands-row.drag-over-top { box-shadow: inset 0 2px 0 0 var(--accent); }
.commands-row.drag-over-bottom { box-shadow: inset 0 -2px 0 0 var(--accent); }
.commands-grip { color: var(--fg-4); user-select: none; }
.commands-label, .commands-exec {
  font-family: var(--mono); font-size: 12px;
  background: var(--bg-1); color: var(--fg-1);
  border: 1px solid var(--line-soft); border-radius: var(--r-sm);
  padding: 4px 8px;
}
.commands-del {
  background: none; border: 0; color: var(--fg-3); cursor: pointer;
  font-size: 14px;
}
.commands-del:hover { color: var(--s-danger); }
.commands-modal-add {
  margin-top: 8px;
  width: 100%;
  background: transparent;
  border: 1px dashed var(--line);
  color: var(--fg-2);
  padding: 6px;
  font-family: var(--mono);
  font-size: 12px;
  border-radius: var(--r-sm);
  cursor: pointer;
}
.commands-modal-add:hover { color: var(--fg-0); border-color: var(--accent); }
```

- [ ] **Step 6: Verify end-to-end**

Run the dev server. Click the ⚙ button in the filters row. Modal opens with three rows (claude, shell, vim).
1. Add a new command "htop" with exec "htop". Confirm a fourth button appears in every new-tile within the next poll.
2. Reorder: drag "htop" to the top. Confirm "htop" becomes the primary button.
3. Edit "htop" → label "top" / exec "top". Confirm the button label updates and clicking it types "top".
4. Delete "top". Confirm it's gone from new-tiles.
5. Close modal with Escape. Open a card's modal — confirm its Escape closes only the card modal (not the closed commands modal). Open commands modal then a card modal on top — Escape closes the card modal first (most-recent wins).

- [ ] **Step 7: Commit**

```bash
git add server.py static/index.html static/app.js static/prefs.js static/commands-modal.js static/styles.css
git commit -m "config: commands editor modal — add/edit/delete/reorder via gear icon; mutations route through prefs.js"
```

---

## Self-Review

After the last task lands, run this checklist:

- [ ] **Spec coverage check.** Re-read `docs/superpowers/specs/2026-05-13-persistent-config-layer-design.md`. For each section/requirement, confirm a task implemented it. The four phases map to tasks 1.*, 2.*, 3.*, 4.* — every spec claim should have a corresponding step.

- [ ] **Migration shim deletion.** The localStorage migration in `prefs.js` (task 1.4) is intentionally left in place until the operator confirms their data migrated. Once Tom confirms (after running the dashboard once and seeing `state.json` populated), open a follow-up commit that removes:
  - The `migrateLocalStorage` function in `prefs.js`.
  - Its single call site at the end of `loadPrefs`.

- [ ] **Mode-param deletion.** `/api/window/new` still accepts the legacy `mode` query param (task 4.3) for one release. After Tom confirms no clients send `mode` anymore, drop the `mode` parameter and the if-mode-and-not-exec line. Quick `grep -r '?mode=' static/ server.py` check confirms safety.

- [ ] **Sanity dump.** Cat `~/.config/periscope/state.json` end-to-end and confirm:
  - `ui` has session_order, collapsed_sessions, view (whatever you've set in the UI).
  - `windows` has one entry per live window, each with `last_seen` and possibly `notes`/`tags`.
  - `commands` has at least claude/shell/vim plus anything you added.
  - `version` is `1`.

---

## Phase boundary commits

If you want clean phase boundaries for review/revert purposes, after each phase's last task lands, tag the commit:

```bash
git tag config-phase-1  # after task 1.5
git tag config-phase-2  # after task 2.4
git tag config-phase-3  # after task 3.4
git tag config-phase-4  # after task 4.4
```

(Tagging is optional; phases are already cleanly separable by commit-message prefix `config: ...`.)

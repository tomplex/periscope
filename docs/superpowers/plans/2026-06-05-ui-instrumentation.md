# UI Instrumentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Log which periscope dashboard actions Tom performs most often into a `ui_events` table in `periscope.db`, queryable by hand to drive UX work.

**Architecture:** A client-side `track(name, detail)` emitter buffers events and batch-POSTs them to a new `/api/events` endpoint (via `navigator.sendBeacon` + a `fetch` keepalive fallback). The endpoint writes rows through `periscope/activity.py` — the sole owner of `periscope.db` — into a new `ui_events` table. Server mutations are auto-tracked at the `apiCall()` chokepoint; ~15 pure-frontend gestures get hand-placed `track()` calls. Readout is SQL by hand; no UI.

**Tech Stack:** FastAPI (Python, `uv`), SQLite (WAL), Preact + `@preact/signals` (Vite bundle).

**Spec:** `docs/superpowers/specs/2026-06-05-ui-instrumentation-design.md`

---

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `periscope/activity.py` | Modify | Add `ui_events` to `_SCHEMA`; add `record_ui_events()` + `prune_ui_events()` |
| `tests/test_activity.py` | Modify | New cases for the two functions |
| `periscope/routes/events.py` | Create | `POST /api/events` ingest router |
| `tests/routes/test_events.py` | Create | Endpoint behavior tests |
| `periscope/app.py` | Modify | Register `events` router; wire `prune_ui_events` into lifespan |
| `static/src/track.js` | Create | Client emitter: buffer + flush + beacon |
| `static/src/main.jsx` | Modify | Import `track`; emit `app.open` at boot |
| `static/src/util.js` | Modify | Auto-track every `apiCall()` |
| `static/src/modal/Modal.jsx` | Modify | `modal.open` / `modal.close` / `modal.tab` |
| `static/src/split/Detail.jsx` | Modify | `view.switch` |
| `static/src/split/Rail.jsx` | Modify | `pane.focus` |
| `static/src/chrome/FilterBar.jsx` | Modify | `filter.use` |
| `static/src/chrome/Header.jsx` | Modify | `key.shortcut` (Cmd/Ctrl+/) |
| `static/src/terminal/terminalCore.js` | Modify | `terminal.open` |
| `static/src/overlays/{Cleanup,Launcher,Commands,OpenPicker,NewProject,ReviewPr,Settings}Modal.jsx` | Modify | `overlay.open` (7 openers) |
| `docs/instrumentation-queries.sql` | Create | Canned readout queries |

Tasks 1–2 are backend (TDD, fully unit-tested). Tasks 3–7 are frontend (browser-verified per project convention — `track.js` timer/beacon plumbing is a poor unit-test target). Task 8 builds the bundle, verifies in the browser, and ships the query doc.

---

## Task 1: `ui_events` table + record/prune in activity.py

**Files:**
- Modify: `periscope/activity.py` (extend `_SCHEMA`; add two functions after the `pane_sessions` section, before `_row_to_event`)
- Test: `tests/test_activity.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_activity.py`:

```python
# --- UI instrumentation ------------------------------------------------

def test_record_ui_events_inserts_and_stamps_dev():
    n = activity.record_ui_events(
        [{"name": "modal.open", "detail": {"tab": "terminal"}, "t": 100}],
        dev=True,
    )
    assert n == 1
    c = activity._conn()
    row = c.execute("SELECT at, name, dev, detail FROM ui_events").fetchone()
    assert row[0] == 100
    assert row[1] == "modal.open"
    assert row[2] == 1
    assert row[3] == '{"tab": "terminal"}'


def test_record_ui_events_dev_false_stamps_zero():
    activity.record_ui_events([{"name": "app.open", "t": 5}], dev=False)
    c = activity._conn()
    assert c.execute("SELECT dev FROM ui_events").fetchone()[0] == 0


def test_record_ui_events_skips_rows_missing_name():
    n = activity.record_ui_events(
        [{"detail": {"x": 1}, "t": 1}, {"name": "", "t": 1}, {"name": "ok", "t": 1}],
        dev=False,
    )
    assert n == 1
    c = activity._conn()
    assert c.execute("SELECT name FROM ui_events").fetchone()[0] == "ok"


def test_record_ui_events_skips_non_dict_elements():
    n = activity.record_ui_events(["nope", 42, {"name": "ok", "t": 1}], dev=False)
    assert n == 1


def test_record_ui_events_invalid_t_falls_back_to_now():
    activity.record_ui_events([{"name": "x"}, {"name": "y", "t": "bad"}], dev=False)
    c = activity._conn()
    ats = [r[0] for r in c.execute("SELECT at FROM ui_events")]
    assert all(a > 1_000_000_000 for a in ats)  # real unix timestamps


def test_record_ui_events_detail_none_and_empty_become_null():
    activity.record_ui_events(
        [{"name": "a", "t": 1}, {"name": "b", "detail": {}, "t": 1}],
        dev=False,
    )
    c = activity._conn()
    details = [r[0] for r in c.execute("SELECT detail FROM ui_events ORDER BY name")]
    assert details == [None, None]


def test_record_ui_events_empty_batch_returns_zero():
    assert activity.record_ui_events([], dev=False) == 0


def test_prune_ui_events_drops_old_keeps_recent():
    import time
    now = int(time.time())
    activity.record_ui_events([{"name": "old", "t": now - 200 * 86400}], dev=False)
    activity.record_ui_events([{"name": "new", "t": now}], dev=False)
    activity.prune_ui_events(max_age_days=90)
    c = activity._conn()
    names = [r[0] for r in c.execute("SELECT name FROM ui_events")]
    assert names == ["new"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_activity.py -k ui_events -q`
Expected: FAIL — `AttributeError: module 'periscope.activity' has no attribute 'record_ui_events'`

- [ ] **Step 3: Add the table to `_SCHEMA`**

In `periscope/activity.py`, inside the `_SCHEMA` string, after the `pane_sessions` table definition (before the closing `"""`), add:

```sql
CREATE TABLE IF NOT EXISTS ui_events (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  at     INTEGER NOT NULL,
  name   TEXT NOT NULL,
  dev    INTEGER NOT NULL,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_ui_events_name ON ui_events (name, at);
```

- [ ] **Step 4: Add the two functions**

In `periscope/activity.py`, after the `migrate_legacy_pane_sessions()` function and before `_row_to_event`, add:

```python
# --- UI instrumentation ------------------------------------------------
#
# Lightweight usage telemetry: which dashboard actions get used most, so
# UX work is data-driven. The client (static/src/track.js) batches events
# to POST /api/events (routes/events.py), which calls record_ui_events.
# Single-user, low volume; SQLite-by-hand is the readout (no UI). ui_events
# is a separate tenant in periscope.db, like pane_sessions above.

def record_ui_events(events: list, dev: bool) -> int:
    """Bulk-insert UI instrumentation rows. Each event is a dict with keys
    name (str), detail (dict|None), t (int unix seconds, client clock).
    Non-dict elements and rows with no non-empty `name` are skipped.
    `detail` is JSON-serialized (None / empty / non-dict -> NULL). `t` is
    coerced to int, falling back to time.time() when missing/invalid. `dev`
    stamps every row in the batch. Returns the number of rows inserted."""
    now = int(time.time())
    dev_flag = 1 if dev else 0
    rows: list[tuple] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        if not isinstance(name, str) or not name:
            continue
        try:
            at = int(e.get("t"))
        except (TypeError, ValueError):
            at = now
        detail = e.get("detail")
        detail_json = json.dumps(detail) if isinstance(detail, dict) and detail else None
        rows.append((at, name, dev_flag, detail_json))
    if not rows:
        return 0
    with _LOCK:
        c = _conn()
        c.executemany(
            "INSERT INTO ui_events (at, name, dev, detail) VALUES (?,?,?,?)",
            rows,
        )
        c.commit()
    return len(rows)


def prune_ui_events(max_age_days: int = 90) -> None:
    """Drop ui_events older than max_age_days. Called once at startup."""
    cutoff = int(time.time()) - max_age_days * 86400
    with _LOCK:
        c = _conn()
        c.execute("DELETE FROM ui_events WHERE at < ?", (cutoff,))
        c.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_activity.py -k ui_events -q`
Expected: PASS (8 passed)

- [ ] **Step 6: Run the full activity suite (no regressions)**

Run: `uv run pytest tests/test_activity.py -q`
Expected: PASS (all)

- [ ] **Step 7: Commit**

```bash
git add periscope/activity.py tests/test_activity.py
git commit -m "instrument: ui_events table + record_ui_events/prune_ui_events in activity.py"
```

---

## Task 2: `POST /api/events` route + lifespan wiring

**Files:**
- Create: `periscope/routes/events.py`
- Modify: `periscope/app.py` (import + register router; wire prune into lifespan)
- Test: `tests/routes/test_events.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/routes/test_events.py`:

```python
"""Tests for /api/events (UI instrumentation ingest)."""
import json

import pytest

from periscope import activity, config


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Isolate periscope.db and force prod port (dev=0) by default."""
    monkeypatch.setattr(config, "ACTIVITY_DB", tmp_path / "t.db")
    monkeypatch.setattr(config, "PORT", 8765)
    activity._CONN = None
    yield
    if activity._CONN is not None:
        activity._CONN.close()
        activity._CONN = None


def _count(name=None):
    c = activity._conn()
    if name:
        return c.execute("SELECT COUNT(*) FROM ui_events WHERE name=?", (name,)).fetchone()[0]
    return c.execute("SELECT COUNT(*) FROM ui_events").fetchone()[0]


def test_post_events_inserts_batch(client):
    r = client.post("/api/events", json={"events": [
        {"name": "modal.open", "detail": {"tab": "terminal"}, "t": 100},
        {"name": "app.open", "t": 101},
    ]})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "n": 2}
    assert _count() == 2


def test_post_events_empty_body_is_noop(client):
    r = client.post("/api/events", content=b"")
    assert r.status_code == 200
    assert r.json()["n"] == 0


def test_post_events_malformed_json_is_noop(client):
    r = client.post("/api/events", content=b"{not json")
    assert r.status_code == 200
    assert r.json()["n"] == 0


def test_post_events_missing_events_key_is_noop(client):
    r = client.post("/api/events", json={"nope": 1})
    assert r.status_code == 200
    assert r.json()["n"] == 0


def test_post_events_caps_batch_at_1000(client):
    big = [{"name": "x", "t": 1} for _ in range(1500)]
    r = client.post("/api/events", json={"events": big})
    assert r.json()["n"] == 1000


def test_post_events_dev_flag_from_port(client, monkeypatch):
    monkeypatch.setattr(config, "PORT", 8766)
    client.post("/api/events", json={"events": [{"name": "x", "t": 1}]})
    c = activity._conn()
    assert c.execute("SELECT dev FROM ui_events").fetchone()[0] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/routes/test_events.py -q`
Expected: FAIL — 404 (route not registered) / import error.

- [ ] **Step 3: Create the route**

Create `periscope/routes/events.py`:

```python
"""POST /api/events — UI instrumentation ingest.

The frontend (static/src/track.js) batches usage events and ships them
here via navigator.sendBeacon (plus a fetch keepalive fallback). This
endpoint is fire-and-forget: it NEVER raises on a malformed batch. It is
not called through apiCall, so a 4xx/5xx would be silently swallowed by
sendBeacon / fetch().catch() anyway, and instrumentation must never
surface an error to the user. A bad body is logged and returns 200 n=0.

The raw request body is parsed by hand rather than via a Pydantic body
model: a model would raise 422 BEFORE the handler runs, so the handler
could not swallow it. dev is derived from config.PORT != 8765 (the
prod/dev discriminator the package already trusts), not PERISCOPE_DEV
(read only in server.py's __main__). Real-usage queries filter dev=0.
"""

import json

from fastapi import APIRouter, Request

from periscope import activity, config
from periscope.log import log

router = APIRouter()

_MAX_BATCH = 1000


@router.post("/api/events")
async def post_events(request: Request):
    try:
        raw = await request.body()
        payload = json.loads(raw) if raw else {}
        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list):
            events = []
    except (ValueError, TypeError):
        log.warning("POST /api/events: undecodable body, dropping batch")
        return {"ok": True, "n": 0}
    if len(events) > _MAX_BATCH:
        events = events[:_MAX_BATCH]
    n = activity.record_ui_events(events, dev=config.PORT != 8765)
    return {"ok": True, "n": n}
```

- [ ] **Step 4: Register the router in app.py**

In `periscope/app.py`, add `events` to the route imports (line ~24-27 block):

```python
from periscope.routes import (
    alerts, auto_rename, channel, events, fs, healthz, history, pane, paste_image, prefs,
    send, sessions, state, ws,
)
```

And add `events` to the `include_router` tuple (line ~116-120):

```python
for r in (
    alerts, auto_rename, channel, cleanup_routes, events, fs, healthz, history, lgtm_route,
    pane, paste_image, prefs, projects_routes, send, sessions, settings_routes,
    state, ws,
):
    app.include_router(r.router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/routes/test_events.py -q`
Expected: PASS (6 passed)

- [ ] **Step 6: Wire prune into the lifespan**

In `periscope/app.py`, in `lifespan`, immediately after the existing
`_bg("activity-prune", activity.prune)` line (line 46), add:

```python
    _bg("ui-events-prune", activity.prune_ui_events)
```

(Runs on both prod and dev — a DELETE-by-age is idempotent and harmless to double-run, unlike the prod-gated activity worker below it.)

- [ ] **Step 7: Run the full route suite + commit**

Run: `uv run pytest tests/routes/ -q`
Expected: PASS (all)

```bash
git add periscope/routes/events.py periscope/app.py tests/routes/test_events.py
git commit -m "instrument: POST /api/events ingest + prune_ui_events in lifespan"
```

---

## Task 3: Client emitter `track.js` + `app.open` heartbeat

**Files:**
- Create: `static/src/track.js`
- Modify: `static/src/main.jsx`

This task is browser-verified (Task 8). No unit test.

- [ ] **Step 1: Create `static/src/track.js`**

```js
// UI instrumentation emitter. track(name, detail) buffers a usage event;
// a 5s interval and the unload listeners flush the buffer to POST /api/events
// (navigator.sendBeacon, with a fetch keepalive fallback). Fire-and-forget:
// every failure path is swallowed — instrumentation must never disrupt the UI.
// See docs/superpowers/specs/2026-06-05-ui-instrumentation-design.md.

let buf = [];

export function track(name, detail) {
  buf.push({ name, detail: detail || null, t: Math.floor(Date.now() / 1000) });
  if (buf.length > 500) buf = buf.slice(-500); // cap if the server is down
}

function flush(beacon) {
  if (!buf.length) return;
  const body = JSON.stringify({ events: buf });
  buf = [];
  if (beacon && navigator.sendBeacon) {
    navigator.sendBeacon("/api/events", new Blob([body], { type: "application/json" }));
  } else {
    fetch("/api/events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      keepalive: true,
    }).catch(() => {});
  }
}

setInterval(() => flush(false), 5000);
addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") flush(true);
});
addEventListener("pagehide", () => flush(true));
```

- [ ] **Step 2: Emit `app.open` at boot**

In `static/src/main.jsx`, add the import (near the other imports):

```js
import { track } from "./track.js";
```

And inside `boot()`, after `await loadPrefs();`, add:

```js
  track("app.open");
```

- [ ] **Step 3: Commit**

```bash
git add static/src/track.js static/src/main.jsx
git commit -m "instrument: client track() emitter + app.open heartbeat"
```

---

## Task 4: Auto-track every `apiCall()`

**Files:**
- Modify: `static/src/util.js`

Captures every server mutation as `api:<label>` with `{path, method, ok}` — one emit per call at each return.

- [ ] **Step 1: Import track**

At the top of `static/src/util.js`, add:

```js
import { track } from "./track.js";
```

(No circular import: `track.js` imports nothing from `util.js`.)

- [ ] **Step 2: Emit at all three return points**

Replace the body of `apiCall` (lines 98-114) with:

```js
export async function apiCall(label, path, opts = {}) {
  const method = opts.method || "GET";
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    track("api:" + label, { path, method, ok: false });
    showToast(`${label} failed: ${err.message}`, "bad", 6000);
    return null;
  }
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok || data.ok === false) {
    track("api:" + label, { path, method, ok: false });
    const err = data.error || data.detail || `HTTP ${res.status}`;
    showToast(`${label} failed: ${err}`, "bad", 6000);
    return null;
  }
  track("api:" + label, { path, method, ok: true });
  return data;
}
```

- [ ] **Step 3: Commit**

```bash
git add static/src/util.js
git commit -m "instrument: auto-track every apiCall as api:<label>"
```

---

## Task 5: Modal, view, focus, filter, shortcut, terminal seams

**Files:**
- Modify: `static/src/modal/Modal.jsx`, `static/src/split/Detail.jsx`, `static/src/split/Rail.jsx`, `static/src/chrome/FilterBar.jsx`, `static/src/chrome/Header.jsx`, `static/src/terminal/terminalCore.js`

Browser-verified (Task 8). Each seam is one `track()` call.

- [ ] **Step 1: `modal.open` + `modal.close` (Modal.jsx)**

Add the import at the top of `static/src/modal/Modal.jsx`:

```js
import { track } from "../track.js";
```

In `openModal` (line 47-50), after `modalTarget.value = target;` add:

```js
  track("modal.open", { tab: opts.tab || "terminal" });
```

In `closeModal` (line 640-642), inside the function, before/after `modalTarget.value = null;` add:

```js
  track("modal.close");
```

- [ ] **Step 2: `modal.tab` (Modal.jsx)**

There are **two distinct handlers** that make a tab active — `switchTab(id)` (~line 545) and `mountDoc(id)` (~line 553, which calls `setActiveTab(id)` after mounting a doc tab). Add the emit to **each** (one emit per entry point; this does not double-count a single gesture). Alongside the `setActiveTab(id);` in both, add:

```js
    track("modal.tab", { tab: id });
```

- [ ] **Step 3: `view.switch` (Detail.jsx)**

Add the import at the top of `static/src/split/Detail.jsx`:

```js
import { track } from "../track.js";
```

At line ~393, the `PaneHeader` `onMode` prop is `onMode={(next) => setDetailMode(w.pid, next)}`. Change it to:

```jsx
        <PaneHeader w={w} mode={mode} onMode={(next) => { track("view.switch", { view: next }); setDetailMode(w.pid, next); }} />
```

- [ ] **Step 4: `pane.focus` (Rail.jsx)**

Add the import at the top of `static/src/split/Rail.jsx`:

```js
import { track } from "../track.js";
```

In the `selectKey` handler at line ~198, after `railSelection.value = key;` add:

```js
    track("pane.focus", { key });
```

- [ ] **Step 5: `filter.use` (FilterBar.jsx)**

Add the import at the top of `static/src/chrome/FilterBar.jsx`:

```js
import { track } from "../track.js";
```

In `pick(key)` at line ~47-48, after `currentFilter.value = key;` add:

```js
    track("filter.use");
```

(The filter is a discrete picker, not a text field — no string captured, no debounce needed.)

- [ ] **Step 6: `key.shortcut` (Header.jsx)**

Add the import at the top of `static/src/chrome/Header.jsx`:

```js
import { track } from "../track.js";
```

In the `onKey` handler at line ~93, inside the `if ((e.metaKey || e.ctrlKey) && e.key === "/")` block, add this as the **first statement** in the block — before `e.preventDefault()` and the `window.location.href` navigation, so the event is buffered before the synchronous page change:

```js
        track("key.shortcut", { key: "cmd+/" });
```

- [ ] **Step 7: `terminal.open` (terminalCore.js)**

Add the import at the top of `static/src/terminal/terminalCore.js` — note it's a **subdirectory**, so the path is `../track.js` (one level up to `static/src/`), NOT `./track.js`:

```js
import { track } from "../track.js";
```

In `startLiveTerminal(target)` at line ~256, as the first statement of the function body, add:

```js
  track("terminal.open", { target });
```

- [ ] **Step 8: Commit**

```bash
git add static/src/modal/Modal.jsx static/src/split/Detail.jsx static/src/split/Rail.jsx static/src/chrome/FilterBar.jsx static/src/chrome/Header.jsx static/src/terminal/terminalCore.js
git commit -m "instrument: modal/view/focus/filter/shortcut/terminal gesture seams"
```

---

## Task 6: Overlay-open seams (7 openers)

**Files:**
- Modify: `static/src/overlays/CleanupModal.jsx`, `LauncherModal.jsx`, `CommandsModal.jsx`, `OpenPickerModal.jsx`, `NewProjectModal.jsx`, `ReviewPrModal.jsx`, `SettingsModal.jsx`

Six of the overlays have an exported opener that flips `open.value = true`; **`LauncherModal.jsx` is the exception** — its `openLauncher(worktreeKey)` sets `target.value = worktreeKey;` instead (no `open` signal). Add the import `import { track } from "../track.js";` to each file, then add one `track()` inside each opener (after the signal it sets — see the per-file anchor column).

- [ ] **Step 1: Add track to each opener**

| File | Opener (line) | Anchor (line to add after) | Add inside opener |
|---|---|---|---|
| `CleanupModal.jsx` | `openCleanupModal` (20) | `open.value = true;` | `track("overlay.open", { which: "cleanup" });` |
| `LauncherModal.jsx` | `openLauncher` (28) | `target.value = worktreeKey;` | `track("overlay.open", { which: "launcher" });` |
| `CommandsModal.jsx` | `openCommandsModal` (22) | `open.value = true;` | `track("overlay.open", { which: "commands" });` |
| `OpenPickerModal.jsx` | `openPicker` (25) | `open.value = true;` | `track("overlay.open", { which: "openpicker" });` |
| `NewProjectModal.jsx` | `openNewProjectModal` (23) | `open.value = true;` | `track("overlay.open", { which: "newproject" });` |
| `ReviewPrModal.jsx` | `openReviewPRModal` (20) | `open.value = true;` | `track("overlay.open", { which: "reviewpr" });` |
| `SettingsModal.jsx` | `openSettingsModal` (17) | `open.value = true;` | `track("overlay.open", { which: "settings" });` |

For each file: add `import { track } from "../track.js";` at the top, and add the `track(...)` line right after the **Anchor** statement in that opener (all are `open.value = true;` except `LauncherModal.jsx`, which is `target.value = worktreeKey;`).

- [ ] **Step 2: Commit**

```bash
git add static/src/overlays/CleanupModal.jsx static/src/overlays/LauncherModal.jsx static/src/overlays/CommandsModal.jsx static/src/overlays/OpenPickerModal.jsx static/src/overlays/NewProjectModal.jsx static/src/overlays/ReviewPrModal.jsx static/src/overlays/SettingsModal.jsx
git commit -m "instrument: overlay.open seams across the 7 overlay openers"
```

---

## Task 7: Canned readout queries

**Files:**
- Create: `docs/instrumentation-queries.sql`

- [ ] **Step 1: Create the query doc**

```sql
-- UI instrumentation readout. DB: ~/.config/periscope/periscope.db, table ui_events.
-- WHERE dev=0 excludes events logged by a dev instance (PORT != 8765).
-- NEVER COUNT/SUM across the api:<label> namespace and the dotted gesture
-- namespace as one total — group within a namespace, or compare deliberately.

-- Top actions, all time (real usage only)
SELECT name, COUNT(*) n FROM ui_events WHERE dev=0
GROUP BY name ORDER BY n DESC;

-- Last 7 days
SELECT name, COUNT(*) n FROM ui_events
WHERE dev=0 AND at > strftime('%s','now','-7 days')
GROUP BY name ORDER BY n DESC;

-- Where do I rename from? (gesture vs effect — both namespaces on purpose)
SELECT name, COUNT(*) FROM ui_events
WHERE dev=0 AND name LIKE '%rename%' GROUP BY name;

-- Daily volume
SELECT date(at,'unixepoch','localtime') d, COUNT(*) n
FROM ui_events WHERE dev=0 GROUP BY d ORDER BY d DESC;

-- Sessions per day (app.open heartbeat)
SELECT date(at,'unixepoch','localtime') d, COUNT(*) sessions
FROM ui_events WHERE dev=0 AND name='app.open' GROUP BY d ORDER BY d DESC;
```

- [ ] **Step 2: Commit**

```bash
git add docs/instrumentation-queries.sql
git commit -m "instrument: canned ui_events readout queries"
```

---

## Task 8: Build bundle + browser verification

**Files:**
- Modify: `static/dist/app.js` (build artifact — committed)

- [ ] **Step 1: Build the bundle**

Run: `npm run build`
Expected: Vite writes `static/dist/app.js` with no errors.

- [ ] **Step 2: Run dev periscope and exercise the UI**

Run (in a worktree or with `PERISCOPE_NO_RECLAIM=1`):
`PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py`

In the browser at `http://localhost:8766/`: open the app (→ `app.open`), open a pane modal (→ `modal.open`), switch a modal tab (→ `modal.tab`), close it (→ `modal.close`), switch a detail view Transcript/Terminal (→ `view.switch`), click a rail row (→ `pane.focus`), change the filter (→ `filter.use`), press Cmd+/ (→ `key.shortcut`), open a terminal (→ `terminal.open`), open an overlay (→ `overlay.open`), rename a tab (→ `api:rename tab`).

- [ ] **Step 3: Verify rows landed, stamped dev=1**

Run:
```bash
sqlite3 ~/.config/periscope/periscope.db \
  "SELECT name, dev, detail FROM ui_events ORDER BY id DESC LIMIT 30;"
```
Expected: rows for the gestures above, `dev=1` (dev instance on 8766), `detail` populated where applicable. Confirm no error toasts appeared in the UI during the session.

- [ ] **Step 4: Commit the bundle**

```bash
git add static/dist/app.js
git commit -m "instrument: rebuild bundle with track() instrumentation"
```

---

## Self-Review notes

- **Spec coverage:** §1 table → Task 1; §2 functions → Task 1; §3 route → Task 2; §4 track.js → Task 3; §5 apiCall → Task 4; §6 seams (`app.open` Task 3; modal/view/focus/filter/shortcut/terminal Task 5; overlays Task 6); §7 retention → Task 2 Step 6; Readout → Task 7; Testing → Tasks 1–2 (unit) + Task 8 (browser).
- **dev flag** derived from `config.PORT != 8765` everywhere (route + test), never `PERISCOPE_DEV`.
- **Import path** for `track`: `./track.js` from importers in `static/src/` root (`main.jsx`, `util.js`); `../track.js` from every subdirectory importer (`modal/`, `split/`, `chrome/`, `overlays/`, **and `terminal/`** — `terminalCore.js` is a subdir, so `../track.js`, not `./track.js`).
- **`api:rename tab` label** (Task 8 verification) is confirmed real: `Rail.jsx:232` calls `apiCall("rename tab", …)`, so the auto-track emits `api:rename tab`.
- **Contract alignment with spec §6:** `pane.focus` detail is `{key}` (the value at `Rail.jsx:198` is a highlight-key like `pane:<pid>`, not a `session:index` target — spec updated to match); `modal.close` carries no detail (closing doesn't need the tab — spec updated). `app.open` passes no detail → stored NULL (harmless).
- **No double-namespace summing** — warned in Task 7 and the spec.

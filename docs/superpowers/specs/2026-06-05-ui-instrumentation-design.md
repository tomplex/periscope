# UI instrumentation — design

**Date:** 2026-06-05
**Status:** spec-reviewed (findings addressed), pending plan

## Goal

Capture which UI actions Tom performs most often in periscope's dashboard,
so the data can drive future UX improvements. Single-user tool; the bar is
"answer questions like *do I rename via the modal or the rail?* from a SQL
query," not a polished analytics product.

## Scope

All meaningful UI interactions:

- **Server mutations** — captured for free by auto-tracking the `apiCall()`
  chokepoint in `static/src/util.js`. Every call already carries a
  human-readable label.
- **Pure-frontend gestures** — modal opens, split view-mode switches,
  overlay opens, pane focus, filter use, keyboard shortcuts, terminal
  mounts. Hand-placed `track()` calls at ~15 curated seams.

Out of scope: any content (message text, search queries, filter strings,
transcript bodies). Light structural context only.

## Architecture & data flow

```
UI gesture / apiCall  →  track(name, detail)  →  in-memory buffer (track.js)
                                                      │ flush every 5s, or on
                                                      │ pagehide/visibilitychange (hidden)
                                                      │ via navigator.sendBeacon
                                                      ▼
                                          POST /api/events  (routes/events.py)
                                                      ▼
                                  activity.record_ui_events(batch, dev)
                                                      ▼
                                  ui_events table in periscope.db
```

One client emitter, one batched endpoint, one new table.

`activity.py` remains the **sole owner of `periscope.db`** (existing
documented invariant). It gains a small `# --- UI instrumentation ---`
section: the `ui_events` table added to `_SCHEMA`, plus `record_ui_events()`
and `prune_ui_events()`. No second SQLite connection; no new DB-owning
module. The instrumentation functions reuse the module's existing `_conn()`
/ `_LOCK`.

## Components

### 1. `ui_events` table (in `periscope/activity.py` `_SCHEMA`)

```sql
CREATE TABLE IF NOT EXISTS ui_events (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  at     INTEGER NOT NULL,   -- unix seconds, client clock (same machine)
  name   TEXT NOT NULL,      -- 'modal.open', 'view.switch', 'api:rename tab', ...
  dev    INTEGER NOT NULL,   -- 1 if logged by the dev instance, else 0
  detail TEXT                -- JSON: light context, no content. NULL if empty.
);
CREATE INDEX IF NOT EXISTS idx_ui_events_name ON ui_events (name, at);
```

- `at` is the **client** timestamp (unix seconds). Same machine, so clock
  skew is a non-issue; the client value is more accurate than server-receipt
  time given 5 s batching + unload beacons. On the wire the client field is
  named `t` (see §4); `record_ui_events` writes it to the `at` column.
- `dev` is stamped **server-side** from `config.PORT != 8765` — the same
  prod/dev discriminator the rest of the package already trusts (`app.py`
  gates the MCP listener and activity worker on `config.PORT == 8765`). The
  client can't know which instance it's talking to, and `PERISCOPE_DEV` is
  only read in `server.py`'s `__main__` block (uvicorn `--reload` gating),
  never in the package — and nothing enforces it on a dev launch. Port is the
  load-bearing signal. Real-usage queries filter `WHERE dev=0`.
- `detail` is a JSON string or `NULL`. Light context only:
  `{pane, session, target, tab, view, which, key}` as relevant per event.
  Never message text, search/filter strings, or transcript content.

### 2. `periscope/activity.py` additions

```python
def record_ui_events(events: list[dict], dev: bool) -> int:
    """Bulk-insert UI instrumentation rows. Each event is {name, detail, t}.
    Rows missing a non-empty `name` are skipped. `detail` is serialized to
    JSON (None -> NULL). Returns the number of rows inserted."""

def prune_ui_events(max_age_days: int = 90) -> None:
    """Drop ui_events older than max_age_days. Called once at startup."""
```

- `record_ui_events` holds `_LOCK`, uses `c.executemany(...)`, single commit.
- The client wire field `t` is coerced to `int` and written to the `at`
  column; a missing/invalid `t` falls back to `time.time()`.
- `detail` accepts a dict (serialized) or None; anything else -> None.
- Both functions follow the existing `record()` / `prune()` patterns in the
  module (lazy `_conn()`, WAL, `_LOCK`).

### 3. `periscope/routes/events.py` (new APIRouter)

`POST /api/events`:

- Body shape: `{"events": [{"name": str, "detail": object|null, "t": int}, ...]}`.
- Parses the **raw request body** as JSON (not a Pydantic body model). A
  Pydantic body model would raise `422` on malformed input *before the
  handler runs*, so the handler couldn't swallow it — and a single raw-parse
  path also covers both transports (`sendBeacon` Blob and the `fetch`
  keepalive fallback) identically. Empty/garbage body -> treated as zero
  events.
- Derives `dev: bool` from `config.PORT != 8765` (not from an env var — see
  §1).
- Caps the batch at 1000 events (drops the overflow; this is a backstop, the
  client caps its buffer at 500).
- Calls `activity.record_ui_events(events, dev)`.
- Returns `{"ok": True, "n": <inserted>}`. Never raises on a malformed batch
  — instrumentation must never surface an error toast to the user. A
  malformed/undecodable body is `log.warning`'d server-side (so silently
  broken instrumentation is noticeable) and still returns `200` with `n=0`.
  (This is the one deliberate exception to the project's `raise HTTPException`
  route convention: the route is not called through `apiCall`, so a 4xx/5xx
  would be silently swallowed by `sendBeacon`/`fetch().catch()` anyway —
  raising buys nothing and risks a future caller wiring it through `apiCall`.)

Registered in `periscope/app.py`'s `include_router` loop, following the
existing one-router-per-file pattern.

### 4. `static/src/track.js` (new frontend module)

```js
let buf = [];

export function track(name, detail) {
  buf.push({ name, detail: detail || null, t: Math.floor(Date.now() / 1000) });
  if (buf.length > 500) buf = buf.slice(-500);   // cap if the server is down
}

function flush(beacon) {
  if (!buf.length) return;
  const body = JSON.stringify({ events: buf });
  buf = [];
  if (beacon && navigator.sendBeacon) {
    navigator.sendBeacon("/api/events",
      new Blob([body], { type: "application/json" }));
  } else {
    fetch("/api/events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body, keepalive: true,
    }).catch(() => {});   // fire-and-forget; never disrupt the UI
  }
}

setInterval(() => flush(false), 5000);
addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") flush(true);
});
addEventListener("pagehide", () => flush(true));
```

- The interval + unload listeners are installed once when the module is first
  imported (it's a singleton ES module). Imported from `src/main.jsx` so the
  flush loop starts at boot; `main.jsx` also calls `track("app.open")` once at
  boot (the session heartbeat, §6).
- `flush()` swallows all errors. Instrumentation failure is silent by design.

### 5. `apiCall` auto-track (one edit to `static/src/util.js`)

`apiCall(label, path, opts)` emits exactly one event per call, naming it
`api:<label>` with `{path, method, ok}`:

- On the network-error early return: `track("api:" + label, {path, method, ok: false})`.
- On the `!res.ok || data.ok === false` early return: `ok: false`.
- On success: `ok: true`.

`method` is `opts.method || "GET"`. `util.js` imports `track` from
`./track.js`. No circular import: `track.js` imports nothing from `util.js`.

### 6. Curated frontend gesture seams (~15 `track()` placements)

| Name | Fires when | detail |
|---|---|---|
| `app.open` | the app boots (one `track()` in `main.jsx`) | `{}` — session heartbeat, lets queries normalize per-session / per-active-day |
| `modal.open` | the pane modal opens | `{tab}` |
| `modal.close` | the pane modal closes | `{tab}` |
| `modal.tab` | switching tabs within the modal | `{tab}` |
| `view.switch` | split detail view-mode change | `{view}` (`terminal`\|`transcript`\|`review`) |
| `pane.focus` | clicking a rail row to focus a pane | `{target}` |
| `overlay.open` | any overlay opens | `{which}` (`commands`\|`launcher`\|`cleanup`\|`settings`\|`newproject`\|`openpicker`\|`reviewpr`) |
| `filter.use` | the filter bar is edited (debounced ~500 ms) | `{}` — **no string** |
| `key.shortcut` | a keyboard shortcut fires | `{key}` |
| `terminal.open` | a `/ws/pane` terminal mounts | `{target}` |
| `api:<label>` | every `apiCall()` (see §5) | `{path, method, ok}` |

The exact source files for each seam are resolved during planning (Modal.jsx,
Detail.jsx / Split.jsx, Rail.jsx, the `overlays/*` modals, FilterBar.jsx, the
`useEscape`/shortcut handler, terminalCore.js). The taxonomy above is the
contract; the plan maps each row to its call site.

### 7. Retention

`prune_ui_events(max_age_days=90)` is called once at startup via
`_bg("ui-events-prune", activity.prune_ui_events)`, next to the existing
`_bg("activity-prune", activity.prune)` in the lifespan at `app.py:46`. Like
the existing prune, it runs unconditionally on **both** prod and dev
instances — that's fine, a DELETE-by-age is idempotent and harmless to
double-run (unlike the activity *worker* beside it, which is prod-gated to
avoid two writers racing). Low event volume; 90 days gives trend room.

WAL growth is bounded by `activity.checkpoint()` (TRUNCATE) in the existing
worker tick, which runs every ~30 s on the **prod** instance and truncates
the shared `periscope.db` WAL file regardless of which process wrote the
frames. `ui_events` writes go through the same `_conn()`/`_LOCK` and need no
new checkpoint handling. The dev instance never checkpoints (worker is
prod-only), but it shares the one DB file, so prod's checkpoint bounds the
WAL for both — same as today's `pane_sessions` writes.

## Readout — canned queries

Shipped as a short reference (committed under `docs/` or handed over). No UI.

```sql
-- Top actions, all time (real usage only)
SELECT name, COUNT(*) n FROM ui_events WHERE dev=0
GROUP BY name ORDER BY n DESC;

-- Last 7 days
SELECT name, COUNT(*) n FROM ui_events
WHERE dev=0 AND at > strftime('%s','now','-7 days')
GROUP BY name ORDER BY n DESC;

-- Where do I rename from? (gesture vs effect)
SELECT name, COUNT(*) FROM ui_events
WHERE dev=0 AND name LIKE '%rename%' GROUP BY name;

-- Daily volume
SELECT date(at,'unixepoch','localtime') d, COUNT(*) n
FROM ui_events WHERE dev=0 GROUP BY d ORDER BY d DESC;

-- Sessions per day (app.open heartbeat)
SELECT date(at,'unixepoch','localtime') d, COUNT(*) sessions
FROM ui_events WHERE dev=0 AND name='app.open' GROUP BY d ORDER BY d DESC;
```

**Namespacing caveat:** `api:<label>` events (the *effect* of a mutation) and
dotted gesture events (the *entry point*) coexist. A single rename produces
both an `api:rename tab` row and a gesture row. Never `COUNT(*)`/`SUM` across
the two namespaces as if they were one total — group within a namespace, or
deliberately compare across them (as the rename query above does).

## Testing

- `tests/test_activity.py` (extend — the functions live in `activity.py`, so
  they test there, mirroring the one-test-per-module convention) — new cases:
  `record_ui_events` inserts a batch; `dev` flag persists; rows missing
  `name` are skipped; invalid/missing `t` falls back to `time.time()`;
  `detail` dict serializes and `None` -> NULL; `prune_ui_events` drops old
  rows and keeps recent ones. Reuses the existing `fresh_db` fixture
  (monkeypatches `config.ACTIVITY_DB`, resets `activity._CONN`).
- `tests/routes/test_events.py` — `POST /api/events` inserts a batch and
  returns `{ok, n}`; empty body -> `n=0`, no error; malformed JSON body ->
  `n=0`, no error (no raised 4xx) and a `log.warning`; batch over 1000 is
  capped; `dev` reflects `config.PORT` (monkeypatch to 8766 -> rows stamped
  `dev=1`; default 8765 -> `dev=0`).
- `static/src/track.js` — verified in the browser per project convention
  (timer + `sendBeacon` plumbing is a poor unit-test target). Manual check:
  perform actions, confirm rows land in `ui_events` with correct names.

## Non-goals / YAGNI

- No analytics dashboard page or `/api/instrumentation/stats` endpoint —
  SQLite by hand was the chosen readout.
- No per-event content capture, no PII, no session-replay.
- No client-side sampling or rate limiting beyond the 500-event buffer cap —
  single user, low volume.
- No retry/queue durability if a flush fails — instrumentation is
  best-effort; a dropped batch is acceptable.

## Open questions

None blocking. The per-seam call-site mapping (§6) is resolved during
planning, not in this spec.

# UI instrumentation — design

**Date:** 2026-06-05
**Status:** approved (brainstorming), pending spec-review

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
  time given 5 s batching + unload beacons.
- `dev` is stamped **server-side** from the `PERISCOPE_DEV` env var — the
  client can't know which instance it's talking to. Real-usage queries filter
  `WHERE dev=0`.
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
- `t` is coerced to `int`; a missing/invalid `t` falls back to `time.time()`.
- `detail` accepts a dict (serialized) or None; anything else -> None.
- Both functions follow the existing `record()` / `prune()` patterns in the
  module (lazy `_conn()`, WAL, `_LOCK`).

### 3. `periscope/routes/events.py` (new APIRouter)

`POST /api/events`:

- Body shape: `{"events": [{"name": str, "detail": object|null, "t": int}, ...]}`.
- Parses the **raw request body** as JSON (not a Pydantic body param) so
  `navigator.sendBeacon`'s `Blob` payload — which may arrive without a clean
  `application/json` content-type negotiation — is accepted. Empty/garbage
  body -> treated as zero events.
- Reads `PERISCOPE_DEV` from the environment to derive `dev: bool`.
- Caps the batch at 1000 events (drops the overflow; this is a backstop, the
  client caps its buffer at 500).
- Calls `activity.record_ui_events(events, dev)`.
- Returns `{"ok": True, "n": <inserted>}`. Never raises on a malformed batch
  — instrumentation must never surface an error toast to the user. (This is
  the one deliberate exception to the project's `raise HTTPException` route
  convention: a 4xx/5xx here would trigger `apiCall`'s error toast for a
  fire-and-forget beacon. The route is not called through `apiCall`.)

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
  imported (it's a singleton ES module). Imported for side effect from
  `src/main.jsx` so the flush loop starts at boot even before the first
  `track()` call.
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

`prune_ui_events(max_age_days=90)` is called once at startup, alongside the
existing `activity.prune()` call (locate that call site during planning —
likely the lifespan in `app.py`). Low event volume; 90 days gives trend room.

WAL growth is already bounded by `activity.checkpoint()` in the existing
worker tick; `ui_events` writes share that connection and need no new
checkpoint handling.

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
```

## Testing

- `tests/test_instrument.py` — `record_ui_events` inserts a batch; `dev`
  flag persists; rows missing `name` are skipped; invalid `t` falls back;
  `detail` dict serializes and `None` -> NULL; `prune_ui_events` drops old
  rows and keeps recent ones. Uses a temp DB via the existing test fixture
  pattern for `activity`.
- `tests/routes/test_events.py` — `POST /api/events` inserts a batch and
  returns `{ok, n}`; empty body -> `n=0`, no error; malformed JSON body ->
  `n=0`, no error (no raised 4xx); batch over 1000 is capped; `dev` reflects
  the `PERISCOPE_DEV` env at request time.
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

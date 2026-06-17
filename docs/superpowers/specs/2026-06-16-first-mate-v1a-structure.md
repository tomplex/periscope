# First Mate — v1a (Substrate) — code-structure proposal

**Spec:** `docs/superpowers/specs/2026-06-16-first-mate-design.md`
**Scope:** v1a only — the pure, inert substrate. No live pane, no prod behavior
change, nothing in this PR is *called* by running code until v1b wires it.
**Date:** 2026-06-16

The four v1a pieces: `build_fleet_digest(...)` (pure), `fleet_diverged(prev,
cur)` (pure), captain's-log + `first_mate` marker tables (storage), and
first-mate-only MCP tools with a caller guard.

---

## 1. Spec pushback

Nothing in the v1a spec violates the taste rules — it was written to mirror
existing patterns (narrator pure-core, `pane_status` table, `_CHANNEL_TOOLS`
registry) and the spec-review already split out the risky live integration into
v1b. Two structural decisions the spec left to the plan, which I'm resolving
here rather than leaving open:

- **The spec says the tables live in `activity.py` "the documented DB owner."**
  I agree for the *storage primitives* (table DDL + row dataclass + get/upsert/
  prune), and disagree about putting the *digest/divergence logic* there.
  `activity.py` already imports `git_pr`, `panes`, `rename_ai` and is the worker
  host; `build_fleet_digest` and `fleet_diverged` are pure and belong in a new
  `periscope/first_mate.py` (mirroring narrator's pure-core split), which
  imports nothing heavy. The boundary: **`activity.py` owns the bytes on disk,
  `first_mate.py` owns the pure logic.** Stated in detail in §3 and §4.

- **The marker is described as "pane id + session id" only.** That is a thin row
  and fine, but the conn-state (held-by / scope / expiry) is explicitly slated
  to share the same table tenancy (spec, "Resolved in spec-review"). I am
  **not** building conn-state columns in v1a (it's a v2 concern — see §6 YAGNI),
  but I'm noting that the `first_mate` row should be a single-row "current
  instance" table keyed so v2 can add conn columns with a guarded `ALTER`, the
  same way `pane_status` grew its `rail` column. No speculative columns now.

---

## 2. Assumptions

- **`first_mate` marker is a singleton row.** The spec calls the first mate a
  "Periscope-supervised singleton." I model the marker as a one-row table
  (`CREATE TABLE ... first_mate (id INTEGER PRIMARY KEY CHECK (id = 1), ...)`)
  rather than a keyed multi-row table. One first mate, one row, `get`/`set`/
  `clear` instead of `get(key)`. If the design ever wants multiple first mates,
  this becomes keyed — but that is not foreseeable (spec: "singleton identity").

- **`build_fleet_digest` takes assembled inputs, never assembles them.** The spec
  is explicit ("pass the assembled view in, don't import it") to avoid an
  `activity → window_view` cycle. I take the per-pane read-model dicts
  (`build_window_view` output) plus `cached_plan_usage()` output as parameters.
  v1a does not decide *how* the worker assembles them — that's a v1b wiring
  concern. v1a ships the pure function + fixtures.

- **The digest's per-pane fields are a curated subset, not the whole
  `window_view` dict.** `build_window_view` returns ~40 keys; the digest needs a
  handful (who/status/blocked/PR-CI/idle). I define that subset as a frozen
  dataclass (§3) so the digest is a typed value, not a passthrough of the giant
  view dict. This is the "Periscope does the aggregation pass" curation the spec
  describes.

- **"Blocked" for v1a = the pane has a `need_human` alert outstanding.** The
  read-model surfaces `channel_alerts` (list) and `channel_unread` per window;
  `need_human` is an alert `kind`. The digest's `blocked` flag is derived from
  "most recent alert kind is `need_human` and unacked." Exact materiality
  thresholds (idle minutes, what counts as a status change) are tunable
  constants in `first_mate.py`, mirroring narrator's `MIN_INTERVAL_S` — the spec
  lists threshold-tuning as a non-blocking open question.

- **The caller guard reads the `first_mate` marker by `pane_id`.** A tool handler
  receives the calling `pane` (a tmux `%N`, same value `_MCP_SESSIONS` is keyed
  on). The guard compares `pane` against the marker's stored `pane_id`. v1a
  ships the guard returning a refusal `_tool_result` for non-first-mate panes;
  in v1a the marker is never *set* (no supervisor yet), so the guard refuses
  everyone — which is correct and testable (set the marker in a test, assert
  pass/refuse).

---

## 3. File layout

```
periscope/
  first_mate.py            NEW  pure core: FleetDigest/PaneDigest dataclasses,
                                build_fleet_digest(), fleet_diverged(),
                                materiality constants. No DB, no tmux, no MCP.
                                Mirrors narrator.py's pure-core half.
  activity.py              EDIT add captain_log + first_mate marker tables to
                                _SCHEMA; CaptainLogRow + FirstMateMarker frozen
                                dataclasses; get/append/recent + get/set/clear
                                functions. Pure storage, same template as
                                pane_status.
  channels.py              EDIT add 3 first-mate tools to _CHANNEL_TOOLS
                                (_do_captains_log_read_tool,
                                _do_captains_log_append_tool,
                                _do_fleet_digest_tool) + _require_first_mate()
                                guard helper.

tests/
  test_first_mate.py       NEW  build_fleet_digest shape/materiality +
                                fleet_diverged truth table (fixtures + zero-
                                fixture pure tests). Mirrors test_narrator.py.
  test_activity.py         EDIT captain-log + marker round-trip tests.
  test_channels.py         EDIT first-mate tool registration + caller-guard
                                pass/refuse tests.
```

No new route file: v1a adds no HTTP surface. The first-mate tools are MCP
tools, registered in `_CHANNEL_TOOLS`, reached over the existing socket. The
fleet-digest *pull* tool is a tool, not a `GET /api/...`.

---

## 4. Per-module structure

### `periscope/first_mate.py` — rung 2 (frozen data + pure functions)

The digest is immutable value-data and the logic over it is naturally pure — the
exact case for frozen-dataclass-plus-pure-functions. No class owns mutable
state here; this is not narrator's `tick` shell, it's only the pure half.

**Row/value types (frozen dataclasses):**

```python
@dataclass(frozen=True)
class PaneDigest:
    handle: str                 # @periscope_id (pid) — stable cross-tick key
    name: str
    session: str
    status_line: str | None     # from pane_status_lines / window_view
    blocked: bool               # need_human outstanding
    pr: int | None
    ci: str | None              # "✗" | "⟳" | "✓" | None  (git_pr glyph)
    idle_s: int                 # now - focused_at/acted_at
    # deliberately small — the curation pass, not the whole window_view dict

@dataclass(frozen=True)
class FleetDigest:
    panes: tuple[PaneDigest, ...]      # tuple, not list — keeps it hashable/frozen
    budget_pct: int | None            # meters.session.percent
    budget_resets_at: int | None      # meters.session.resets_at
    at: int                           # unix seconds, when computed
```

**Key signatures:**

```python
def build_fleet_digest(
    *, window_views: list[dict], usage: dict | None, now: int,
) -> FleetDigest: ...
# window_views: the assembled build_window_view dicts (Claude panes only —
# caller filters, or build_fleet_digest filters on view["is_claude"]).
# usage: cached_plan_usage() output (or None when unavailable). PURE: reads
# dict keys, constructs the dataclass, never imports store/window_view/usage.

def fleet_diverged(
    prev: FleetDigest | None, cur: FleetDigest,
) -> tuple[bool, str]: ...
# Mirrors narrator.should_regenerate. prev is None (first sight) -> (True,
# "first_sight"). Returns (diverged, human-readable reason for the delta push
# / log). Materiality lives in module constants:
#   IDLE_MATERIAL_S, BUDGET_MATERIAL_PCT (a $0.01 tick is not material; a
#   blocked-pane status change is). Tunable like narrator's MIN_INTERVAL_S.
```

**Rationale:** pure, zero-IO, unit-testable with literal dataclasses — exactly
the `should_regenerate` shape the spec asks the divergence check to mirror.
`build_fleet_digest` takes assembled inputs so the worker's import graph (and
the `activity → window_view` cycle) is sidestepped: `first_mate.py` imports
**nothing** from periscope's heavy modules. Keyword-only args per the
multi-arg rule.

**Reuse:** consumes `build_window_view` output and `cached_plan_usage()` output
by *shape*, not by import. Reuses the `git_pr` CI glyph vocabulary (`✗ ⟳ ✓`)
already in the view dict — no new CI parsing.

### `periscope/activity.py` (additions) — rung 1/2 (functions over frozen rows)

Follows the `pane_status` template exactly: DDL in `_SCHEMA`, a frozen row
dataclass, and module-level get/upsert/prune functions guarded by `_LOCK`. No
class — `activity.py` is a function module over one connection.

**Schema additions to `_SCHEMA`:**

```sql
CREATE TABLE IF NOT EXISTS captain_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  at         INTEGER NOT NULL,
  kind       TEXT NOT NULL,        -- 'standing_order' | 'watch' | 'narrative'
  text       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_captain_log_at ON captain_log (at);

CREATE TABLE IF NOT EXISTS first_mate (
  id          INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton row
  pane_id     TEXT NOT NULL,
  session_id  TEXT,
  updated_at  INTEGER NOT NULL
);
```

**Row types + functions:**

```python
@dataclass(frozen=True)
class CaptainLogRow:
    id: int
    at: int
    kind: str          # standing_order | watch | narrative
    text: str

@dataclass(frozen=True)
class FirstMateMarker:
    pane_id: str
    session_id: str | None
    updated_at: int

def append_captain_log(*, kind: str, text: str, at: int | None = None) -> None
def recent_captain_log(*, limit: int = 50) -> list[CaptainLogRow]   # newest-first
def get_first_mate() -> FirstMateMarker | None
def set_first_mate(*, pane_id: str, session_id: str | None, at: int) -> None  # upsert id=1
def clear_first_mate() -> None
```

**Rationale:** these are storage primitives, and `activity.py` is the
documented DB owner (one connection, one `_LOCK`, one `_SCHEMA`). Splitting the
SQLite into a second module would mean a second connection to the same file or
a cross-module `_conn()` reach — both worse than co-tenancy, which is the
established pattern (`pane_sessions`, `usage_samples`, `ui_events` all co-tenant
here). `captain_log` is append-only narrative, so an autoincrement log table
(like `events`) rather than upsert-by-key (like `pane_status`). `first_mate` is
a singleton upsert via the `id=1` check. **No guarded `ALTER` needed in v1a**
(fresh tables), but the `first_mate` table is shaped so v2 conn columns add via
the same guarded-`ALTER` idiom `pane_status.rail` used — noted, not built.

### `periscope/channels.py` (additions) — rung 1 (functions + registry records)

The registry is already a functional registry (one record + one `_do_*`
handler). I follow it exactly: three new records, three handlers, one guard
helper. No class.

**Guard helper:**

```python
def _require_first_mate(pane: str) -> bool:
    """True iff `pane` is the registered first_mate marker. Tools that mutate
    or expose fleet-wide first-mate state refuse non-first-mate callers — the
    registry is flat (every pane sees every tool), so the tool guards itself."""
    from periscope import activity
    marker = activity.get_first_mate()
    return marker is not None and marker.pane_id == pane
```

Lazy-import `activity` inside the helper (matching `_do_notify_tool`'s
`from periscope import activity` pattern — channels.py never top-imports
activity, to keep MCP startup independent of the DB).

**Three handlers** (`_do_captains_log_read_tool`, `_do_captains_log_append_tool`,
`_do_fleet_digest_tool`), each opening with:

```python
if not _require_first_mate(pane):
    return _tool_result({"ok": False, "error": "first-mate-only tool"})
```

- `_do_captains_log_read_tool` → `activity.recent_captain_log()` → serialized rows.
- `_do_captains_log_append_tool` → validates `kind` ∈ {standing_order, watch,
  narrative} + non-empty `text`, calls `activity.append_captain_log(...)`.
- `_do_fleet_digest_tool` → the on-demand pull. **v1a open question (flag,
  §7):** the digest is computed in the worker tick (v1b) and isn't stored
  anywhere in v1a. For v1a this tool can only return "no digest available yet"
  or re-assemble on demand — re-assembling pulls in `window_view`, which
  channels.py already imports transitively via `list_claudes`. I propose v1a
  ships this handler as a **thin stub that returns `{"ok": False, "error": "no
  cached digest"}`** until v1b adds digest caching, OR computes a fresh digest
  from `list_windows()`-assembled views. See §7 — this is the one genuinely
  underspecified piece.

**Rationale:** the registry's whole design goal is "adding a tool is one record
plus one handler." First-mate tools are tools; they ride the existing socket
and registry (spec-review resolved: one socket, one registry). The guard is the
spec's named v1 mechanism for "registry-wide tools, refuse non-first-mate
callers" — defense the prompt also states, code also enforces.

---

## 5. Patterns

**Used:**
- **Pure decision core + frozen value-data** (`first_mate.py`) — mirrors
  `narrator.should_regenerate` / `NarratorResult`. The spec names this mirror
  explicitly.
- **Functional registry record + handler** (`channels._CHANNEL_TOOLS`) — the
  established tool-add pattern.
- **Frozen-dataclass storage rows + function CRUD over one connection**
  (`activity.py`) — the `PaneStatusRow` template.
- **Caller guard as a module-level predicate** (`_require_first_mate`) — a
  function, not a decorator. The spec wants three guarded tools; an explicit
  `if not _require_first_mate(pane): return refusal` at the top of each reads
  clearer than a decorator wrapping the dispatch, and the registry dispatch
  (`_call_tool`) stays unchanged.

**Considered and rejected:**
- **A `FirstMate` class holding digest + marker + log** — rejected. No coupled
  mutable state to encapsulate in v1a; the digest is a value, storage is
  functions over a connection, the guard is a predicate. A class would be
  grouping-by-noun, not earning its keep (rung-3 trigger not met).
- **A new `first_mate.py`-owned SQLite store** — rejected. Second connection to
  `periscope.db` or a cross-module `_conn()`; co-tenancy in `activity.py` is the
  documented pattern.
- **A decorator/registry-flag for first-mate-only tools** (e.g. a
  `"first_mate_only": True` key on the record, checked in `_call_tool`) —
  *tempting* and arguably cleaner long-term, but rejected for v1a: it changes
  the shared dispatch path (a v1b/v2 surface that every tool flows through) for
  3 tools. An explicit guard line per handler is lower-blast-radius and matches
  how existing tools self-validate (`_do_link_pr_tool` validates its own args
  inline). Revisit when first-mate-only tools outnumber ~5. Flagged in §7.
- **Custom exception types** (e.g. `NotFirstMateError`) — rejected per the rule.
  The guard returns a refusal `_tool_result`, matching every other tool's
  error convention (`{"ok": False, "error": ...}` in the tool-result body —
  note this is the *MCP tool* convention, distinct from the route
  `HTTPException` convention; tools don't raise `HTTPException`).

---

## 6. YAGNI check

Things in the broader spec that v1a must **not** build (and the proposal
doesn't):

- **Conn-state columns** (held-by / scope / expiry) on the `first_mate` table —
  v2. v1a ships `pane_id / session_id / updated_at` only. The table is *shaped*
  to grow them via guarded `ALTER`; no speculative columns.
- **`emit_channel_event` consumers / heartbeat push** — v1b. v1a is inert.
- **`need_human` interrupt hook** at `_do_notify_tool` — v1b. Don't touch
  `_do_notify_tool` in v1a.
- **Materiality *tuning*** — ship sane constants, don't over-engineer a
  configurable materiality policy. The spec marks thresholds as a tune-later
  open question.
- **Delta-digest rendering** ("auth pane went blocked; budget 62%→71%") — the
  *push payload* formatting is a v1b concern (it's what gets sent). v1a's
  `fleet_diverged` returns a `reason` string, which is enough seed; full delta
  prose isn't needed until there's a pane to push to.

One thing to **flag as possibly over-built for v1a** (§7): the
`_do_fleet_digest_tool` pull tool. It has no cached digest to read until v1b
computes one. Shipping it as a stub in v1a is defensible (the registry entry +
guard are testable) but it does nothing useful until v1b — consider deferring
the *handler body* to v1b while keeping it out of v1a's "demoable-green"
surface.

---

## 7. Decisions to sanity-check

1. **Logic in `first_mate.py`, storage in `activity.py` (split), vs. all in
   `activity.py`.** I split: pure logic in a new `first_mate.py`, bytes in
   `activity.py`. *Alternative:* put `build_fleet_digest`/`fleet_diverged` in
   `activity.py` too (one module, the spec's literal "via activity.py"
   phrasing). *Close because:* `activity.py` is already the worker host and DB
   owner, so co-locating isn't crazy — but it imports `git_pr`/`panes` and would
   make the pure functions live next to heavy IO, hurting the "pure, zero-import,
   unit-testable" property the spec prizes. I weighted the narrator precedent
   (narrator is its own file, imports activity for storage) decisively.

2. **`_do_fleet_digest_tool` in v1a: stub vs. compute-on-demand vs. defer to
   v1b.** I proposed shipping it as a guarded stub. *Alternative:* defer the
   handler entirely to v1b and ship only the two captain's-log tools in v1a.
   *Close because:* v1a's stated deliverable lists "fleet-digest pull" as a v1a
   tool, but there's no digest to pull until v1b — so the v1a version is
   necessarily inert. Recommend: ship the registry entry + guard (testable now),
   keep the body a clean stub, fill it in v1b. Tom to confirm he wants the
   entry in the v1a PR vs. all three tools landing in v1b.

3. **Per-handler guard line vs. a `first_mate_only` registry flag.** I chose the
   per-handler line. *Alternative:* a declarative `"first_mate_only": True` on
   the record, enforced once in `_call_tool`. *Close because:* the flag is
   cleaner if first-mate-only tools proliferate, but it edits the shared
   dispatch path for 3 tools and v1a wants minimal blast radius. Worth a glance:
   if v2/v3 add many gated tools, migrate to the flag then.

4. **`PaneDigest.handle` keyed on pid (`@periscope_id`) vs. tmux `pane_id`
   (`%N`).** I chose pid. *Alternative:* key on `%N`. *Close because:* `%N` is
   what `_MCP_SESSIONS` and the caller guard use, but pid is the *stable*
   cross-tick identity (`%N` recycles), and `fleet_diverged` compares panes
   across ticks — a recycled `%N` would read as a spurious divergence. pid is the
   right cross-tick key; the guard separately uses `%N` because that's what the
   socket gives it. Two identities, used correctly per their lifetime — worth a
   sanity glance since it's the one place the two id systems both appear.

---

## Test strategy (per module)

All v1a tests are **unit tests, no live tmux/Claude, no real Haiku** — that's
the whole point of the v1a/v1b split. Mirrors `test_narrator.py` (pure, fixture-
light) and `test_activity.py` (real SQLite via the `fresh_activity_db`
fixture — a *real* dependency, in-process, not a mock).

**`tests/test_first_mate.py`** — unit, zero external deps.
- `fleet_diverged` truth table (like `should_regenerate`'s): `prev=None` →
  first_sight; identical digests → not diverged; a pane goes blocked → diverged
  with reason; budget tick under `BUDGET_MATERIAL_PCT` → not diverged; budget
  jump over it → diverged; pane appears/disappears → diverged. Built from
  literal `FleetDigest`/`PaneDigest` instances — zero fixtures.
- `build_fleet_digest` shape: feed hand-built `window_views` dicts (the
  curated subset of real `build_window_view` keys) + a fake `cached_plan_usage`
  dict → assert the `FleetDigest`/`PaneDigest` fields, the `is_claude` filter,
  `blocked` derivation from a `need_human` alert, budget extraction, `usage=None`
  → `budget_pct=None`. Fixtures are literal dicts, not captured tmux.
- *Testability note:* because `build_fleet_digest` takes assembled dicts, it's
  directly callable with hand-written inputs — **no mocking of store/usage/git
  needed.** This is the structure that *prevents* the mock-heavy smell; if it
  imported and assembled internally, these tests would need to mock four
  modules.

**`tests/test_activity.py`** (additions) — integration against **real SQLite**
(the `fresh_activity_db` fixture opens a real temp DB; no mock). Mirrors the
existing `test_record_then_events_for_roundtrips` style.
- captain-log append → `recent_captain_log` round-trip; newest-first ordering;
  `limit` honored; `kind` stored verbatim.
- `set_first_mate` → `get_first_mate` round-trip; second `set` replaces (id=1
  singleton, not a second row); `clear_first_mate` → `get` returns None.
- *Real dependency on purpose* — the Q1-2026 mocked-migration incident is the
  exact reason: a mocked SQLite would pass while a real DDL/upsert bug shipped.
  The `_SCHEMA` DDL and the upsert SQL are exercised against real SQLite here.

**`tests/test_channels.py`** (additions) — unit, real `activity` DB via fixture
for the marker, in-memory channel dicts reset by the existing
`reset_channel_state` fixture.
- registration: the three tool names are present in `_CHANNEL_TOOLS`, each has
  an `inputSchema` and a `handler`.
- caller guard: with **no** marker set, each first-mate tool returns the
  refusal body (`ok: False`, "first-mate-only"). With `set_first_mate(pane_id=
  "%9")`, calling the handler with `pane="%9"` passes the guard (and a different
  pane still refuses). Asserts the spec's "refuse unless the calling pane is the
  first_mate marker" directly.
- captain's-log append tool validates `kind` and non-empty `text` (bad kind →
  refusal); read tool returns appended rows.
- *No live MCP listener* — these test the handler functions directly, exactly
  as the existing `test_channels.py` calls `_do_notify_tool("%5", {...})`.

No flagged testability smells: every v1a unit is reachable and assertable by
constructing plain inputs. The one structural choice that *guarantees* this is
keeping `build_fleet_digest` pure-with-assembled-inputs (§4) — the deliberate
move away from a structure that would force four mocks per digest test.

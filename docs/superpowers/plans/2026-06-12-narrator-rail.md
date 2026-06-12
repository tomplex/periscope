# Narrator Rail UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The narrator's Haiku call returns a third field — a ≤28-char `rail` fragment written for the rail's width — and the rail row restructures so the status is its own full-width line under the name row.

**Architecture:** A nullable `rail` TEXT column rides through the existing pane_status pipeline (activity.py schema/CRUD → narrator.py parse/prompt/generate → routes/state.py merge → RailRows.jsx render), appended LAST at every site. The prod DB already has pane_status rows, so `activity._conn()` gains a probe-then-ALTER migration copied from `history/db.py`. The frontend change is markup+CSS only: a nested flex wrapper stacks name row over status row, and the tree-guide connectors get pinned to the name line's center via fixed offsets instead of `top:50%`.

**Tech Stack:** Python 3 / FastAPI / SQLite (stdlib sqlite3), pytest via `uv run`, Preact + Vite (`npm run build` → committed `static/dist/app.js`).

**Spec:** `docs/superpowers/specs/2026-06-12-narrator-rail-design.md` (authoritative — the lockstep set and tree-guide geometry are decided there).

**House rules (project conventions, do not deviate):**
- Comments explain WHY, not what. Defensive parsing exists ONLY at the model-output boundary (`parse_response`) — everywhere else, trust the types.
- Single-line commit messages, commit straight to the feature branch after every green step pair.
- Lifespan tests mock `activity.run_worker` — do not touch them; never let a real worker tick fire from pytest.
- Route errors raise `HTTPException`; success payloads omit absent keys rather than sending nulls (`status_rail` follows `status_line`'s absent-key contract).

---

### Task 0: Worktree + green baseline

**Files:** none (environment setup)

- [x] **Step 1: Create the worktree and branch**

```bash
git worktree add ~/dev/periscope-rail -b feature/narrator-rail
cd ~/dev/periscope-rail
```

All subsequent task commands run with `~/dev/periscope-rail` as cwd unless a step says otherwise.

- [x] **Step 2: Install frontend deps in the worktree** (Task 4 needs Vite; node_modules doesn't travel with worktrees)

```bash
npm install
```

Expected: completes without errors (warnings fine).

- [x] **Step 3: Confirm green baseline and record the count**

```bash
uv run pytest -q
```

Expected: `611 passed` (count as of plan-writing; if main moved, note the new number — every later "expected count" in this plan is baseline + new tests).

```bash
uv run pytest --co -q | tail -1
```

Expected: `611 tests collected in ...`

No commit — nothing changed.

---

### Task 1: Schema + migration + lockstep CRUD (`activity.py`)

The lockstep set: every site that touches the pane_status column list must change together, all appending `rail` LAST. Sites: `_SCHEMA` CREATE TABLE, the new migration in `_conn()`, `_PANE_STATUS_COLS`, `PaneStatusRow`, `upsert_pane_status`, `stamp_pane_rename` (hardcodes a 7-slot VALUES literal — growing the column list without the 8th slot makes every rename throw `OperationalError`), `pane_status_lines`.

**Files:**
- Modify: `periscope/activity.py` (`_SCHEMA` ~line 66, `_conn()` ~line 82, pane_status block ~lines 245-336)
- Test: `tests/test_activity.py` (pane_status section, ~lines 391-468)

- [x] **Step 1: Write the failing tests**

Add to `tests/test_activity.py` after `test_pane_status_upsert_then_get_roundtrips` (the `_status_row` helper at line 393 needs NO change — `rail` defaults to `None` in the dataclass, and `_status_row(rail=...)` works via `**over`):

```python
def test_pane_status_rail_roundtrips():
    activity.upsert_pane_status(_status_row(rail="comparing lookup hit rates"))
    assert activity.get_pane_status("%1").rail == "comparing lookup hit rates"


def test_pane_status_rail_defaults_to_none():
    # Existing keyword constructions never pass rail — the default must hold
    # through a full write/read cycle.
    activity.upsert_pane_status(_status_row())
    assert activity.get_pane_status("%1").rail is None


def test_pane_status_migration_adds_rail_to_old_db():
    # The prod DB has pane_status rows that predate the rail column;
    # CREATE TABLE IF NOT EXISTS won't add it. Fabricate the old shape at
    # the (fixture-redirected) DB path BEFORE activity opens it, then let
    # _conn()'s probe-then-ALTER run on first use.
    import sqlite3
    from periscope import config
    db = sqlite3.connect(str(config.ACTIVITY_DB))
    db.execute(
        "CREATE TABLE pane_status ("
        "  pane_id TEXT PRIMARY KEY, session_id TEXT, status TEXT NOT NULL,"
        "  generated_at INTEGER NOT NULL, jsonl_size INTEGER NOT NULL,"
        "  seen_name TEXT, renamed_at INTEGER)"
    )
    db.execute("INSERT INTO pane_status VALUES "
               "('%1', 'sid-a', 'old status', 1000, 10, 'claude', NULL)")
    db.commit()
    db.close()
    got = activity.get_pane_status("%1")   # first _conn() → migration runs
    assert got.status == "old status"      # rows survive
    assert got.rail is None                # column added, backfilled NULL
    # Idempotent: a reconnect on the now-current shape must not raise.
    activity._CONN.close()
    activity._CONN = None
    assert activity.get_pane_status("%1").rail is None


def test_pane_status_lines_carries_rail():
    activity.upsert_pane_status(_status_row(
        "%1", status="doing a thing", generated_at=42, rail="short rail"))
    assert activity.pane_status_lines() == {"%1": ("doing a thing", 42, "short rail")}
```

Update the two existing stamp tests and the bulk-read test in place:

In `test_stamp_pane_rename_inserts_placeholder_row` (after `assert got.session_id is None`), add:

```python
    assert got.rail is None                # 8th VALUES slot must be NULL
```

In `test_stamp_pane_rename_updates_existing_row_only_in_place`, change the setup line to seed a rail and assert the stamp leaves it alone:

```python
    activity.upsert_pane_status(_status_row(status="working on x", generated_at=900,
                                            rail="short rail"))
    activity.stamp_pane_rename("%1", name="human-name", at=6000)
    got = activity.get_pane_status("%1")
    assert got.status == "working on x"   # status untouched
    assert got.generated_at == 900        # generation clock untouched
    assert got.rail == "short rail"       # rail untouched
    assert got.seen_name == "human-name"
    assert got.renamed_at == 6000
```

In `test_pane_status_lines_bulk_read_skips_placeholders`, update the exact-dict assertion to the 3-tuple:

```python
    assert activity.pane_status_lines() == {"%1": ("doing a thing", 42, None)}
```

- [x] **Step 2: Run the new/changed tests to verify they fail**

```bash
uv run pytest tests/test_activity.py -q -k "rail or stamp_pane_rename or pane_status_lines"
```

Expected: FAIL — `TypeError: ... unexpected keyword argument 'rail'` where tests construct rows with `rail=`, `AttributeError: ... no attribute 'rail'` where they read `got.rail`, and the `pane_status_lines` 3-tuple assertions fail against the current 2-tuple.

- [x] **Step 3: Implement the lockstep change in `periscope/activity.py`**

(a) `_SCHEMA` — append `rail` last in the pane_status CREATE TABLE:

```python
CREATE TABLE IF NOT EXISTS pane_status (
  pane_id      TEXT PRIMARY KEY,   -- tmux %id
  session_id   TEXT,               -- Claude JSONL stem at generation time
  status       TEXT NOT NULL,
  generated_at INTEGER NOT NULL,   -- unix seconds
  jsonl_size   INTEGER NOT NULL,   -- size at generation (change check)
  seen_name    TEXT,               -- window name at last generation
  renamed_at   INTEGER,            -- rename-cooldown stamp (narrator,
                                   -- manual routes, or detected external)
  rail         TEXT                -- <=28-char rail fragment (nullable)
);
```

(b) `_conn()` — probe-then-ALTER right after `executescript(_SCHEMA)` (the `history/db.py` pattern):

```python
def _conn() -> sqlite3.Connection:
    """Lazily open the SQLite connection. Caller must hold _LOCK."""
    global _CONN
    if _CONN is None:
        config.ACTIVITY_DB.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(config.ACTIVITY_DB), check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(_SCHEMA)
        # pane_status predates the rail column in live DBs, and CREATE TABLE
        # IF NOT EXISTS won't add it. Guarded ALTER (history/db.py pattern)
        # is provably idempotent, so dev/prod schema skew is harmless.
        have = {r[1] for r in c.execute("PRAGMA table_info(pane_status)")}
        if "rail" not in have:
            c.execute("ALTER TABLE pane_status ADD COLUMN rail TEXT")
        c.commit()
        _CONN = c
    return _CONN
```

(c) `_PANE_STATUS_COLS` — append last:

```python
_PANE_STATUS_COLS = ("pane_id, session_id, status, generated_at, "
                     "jsonl_size, seen_name, renamed_at, rail")
```

(d) `PaneStatusRow` — new field LAST, defaulted so every existing keyword construction and equality test stays valid:

```python
@dataclass(frozen=True)
class PaneStatusRow:
    pane_id: str
    session_id: str | None
    status: str
    generated_at: int
    jsonl_size: int
    seen_name: str | None
    renamed_at: int | None
    rail: str | None = None
```

(e) `upsert_pane_status` — 8 placeholders, rail in DO UPDATE SET and the values tuple:

```python
def upsert_pane_status(row: PaneStatusRow) -> None:
    with _LOCK:
        c = _conn()
        c.execute(
            f"INSERT INTO pane_status ({_PANE_STATUS_COLS}) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(pane_id) DO UPDATE SET "
            "  session_id=excluded.session_id, status=excluded.status, "
            "  generated_at=excluded.generated_at, jsonl_size=excluded.jsonl_size, "
            "  seen_name=excluded.seen_name, renamed_at=excluded.renamed_at, "
            "  rail=excluded.rail",
            (row.pane_id, row.session_id, row.status, row.generated_at,
             row.jsonl_size, row.seen_name, row.renamed_at, row.rail),
        )
        c.commit()
```

(f) `stamp_pane_rename` — it interpolates `_PANE_STATUS_COLS` but hardcodes the VALUES literal; add the 8th slot as NULL (only the docstring's last sentence and the VALUES line change):

```python
def stamp_pane_rename(pane_id: str, *, name: str, at: int) -> None:
    """Start the narrator's rename cooldown for this pane. Called from the
    manual/auto rename routes. The pane may have no row yet (rename before
    first generation) — insert a placeholder (status='', jsonl_size=0,
    rail=NULL) that the read paths skip and that regenerates promptly
    (size differs)."""
    with _LOCK:
        c = _conn()
        c.execute(
            f"INSERT INTO pane_status ({_PANE_STATUS_COLS}) "
            "VALUES (?, NULL, '', 0, 0, ?, ?, NULL) "
            "ON CONFLICT(pane_id) DO UPDATE SET "
            "  seen_name=excluded.seen_name, renamed_at=excluded.renamed_at",
            (pane_id, name, at),
        )
        c.commit()
```

(g) `pane_status_lines` — 3-tuple:

```python
def pane_status_lines() -> dict[str, tuple[str, int, str | None]]:
    """Bulk read for routes/state.py: pane_id -> (status, generated_at,
    rail). One SELECT per poll — never a per-pane query inside the
    32-thread fan-out (it would serialize on _LOCK). Skips placeholder
    rows."""
    with _LOCK:
        c = _conn()
        rows = c.execute(
            "SELECT pane_id, status, generated_at, rail FROM pane_status "
            "WHERE status != ''"
        ).fetchall()
    return {p: (s, int(g), r) for p, s, g, r in rows}
```

`get_pane_status` and `all_pane_statuses` need NO code change — they SELECT `_PANE_STATUS_COLS` and splat into `PaneStatusRow(*row)`, which now carries 8 values.

- [x] **Step 4: Run the module's tests**

```bash
uv run pytest tests/test_activity.py -q
```

Expected: all pass (46 = 42 existing + 4 new).

Note: `tests/routes/test_state.py::test_state_merges_narrator_status_lines` is now BROKEN (route still 2-unpacks the 3-tuple) — that's expected and is Task 3's job. Confirm it's the only collateral failure:

```bash
uv run pytest -q
```

Expected: `1 failed, 614 passed` — the one failure is `tests/routes/test_state.py::test_state_merges_narrator_status_lines` with `ValueError: too many values to unpack`.

Side effect worth knowing: route tests that DON'T use `fresh_activity_db` (`test_state_empty` and siblings) open the real `~/.config/periscope/periscope.db`, so this first full-suite run also runs the ALTER migration on the prod DB. Benign — the prod code currently running SELECTs the explicit 7-column list, so the extra column is invisible to it — but it means the column will already exist before Task 5's deploy. Don't be confused by that at verification time: the deploy-time signal is new rows with non-null `rail`, not the column's existence.

- [x] **Step 5: Commit**

```bash
git add periscope/activity.py tests/test_activity.py
git commit -m "feat(narrator): pane_status rail column — schema, probe-then-ALTER migration, lockstep CRUD"
```

(The transient route-test failure is closed two commits later in Task 3; acceptable on a feature branch.)

---

### Task 2: Narrator model contract (`narrator.py`)

`rail` is optional model output: validated with the same strip-then-length treatment as `status`, but a bad rail must NOT discard a good status/rename — drop just the rail.

**Files:**
- Modify: `periscope/narrator.py` (constants ~line 40, `NarratorResult` ~line 62, `parse_response` ~line 96, `build_narrator_prompt` ~line 143, `_generate` upsert ~line 301)
- Test: `tests/test_narrator.py`

- [x] **Step 1: Write the failing tests**

Add to `tests/test_narrator.py` in the `---- parse_response ----` section:

```python
def test_parse_response_rail_accepted_at_limit():
    rail = "x" * narrator.RAIL_MAX_LEN
    out = narrator.parse_response(
        f'{{"status": "s", "rail": "{rail}", "rename": null}}')
    assert out.rail == rail


def test_parse_response_rail_over_limit_dropped_status_and_rename_kept():
    # A bad rail must never discard a good status or rename — the rail is
    # the optional garnish, not the meal.
    rail = "x" * (narrator.RAIL_MAX_LEN + 1)
    out = narrator.parse_response(
        f'{{"status": "s", "rail": "{rail}", "rename": "fs-liveness"}}')
    assert out is not None
    assert out.rail is None
    assert out.status == "s"
    assert out.rename == "fs-liveness"


def test_parse_response_rail_empty_or_nonstring_dropped():
    assert narrator.parse_response(
        '{"status": "s", "rail": "  ", "rename": null}').rail is None
    assert narrator.parse_response(
        '{"status": "s", "rail": 42, "rename": null}').rail is None


def test_parse_response_rail_missing_is_none():
    assert narrator.parse_response('{"status": "s", "rename": null}').rail is None


def test_parse_response_rail_strips_whitespace():
    out = narrator.parse_response(
        '{"status": "s", "rail": "  comparing rates  ", "rename": null}')
    assert out.rail == "comparing rates"
```

Add to the `---- build_narrator_prompt ----` section:

```python
def test_build_narrator_prompt_includes_rail_rules():
    p = narrator.build_narrator_prompt(
        window_name="f2-post-deploy", branch=None, pr=None, cwd="/repo",
        signals={})
    assert '"rail"' in p                          # in the return-shape line
    assert str(narrator.RAIL_MAX_LEN) in p        # length rule in-prompt
    # The no-overlap rule must reference the CURRENT name inline (in the
    # rules block, i.e. before the `current_name:` data line), not speak
    # abstractly about "the window name".
    rules_block = p.split("current_name:")[0]
    assert "f2-post-deploy" in rules_block
```

Add to the tick section (after `test_tick_generates_first_status`):

```python
def test_tick_persists_rail(tick_env):
    tick_env["response"] = ('{"status": "fixing flaky reconcile test", '
                            '"rail": "comparing hit rates", "rename": null}')
    narrator.tick([_pane()])
    assert activity.get_pane_status("%1").rail == "comparing hit rates"
```

- [x] **Step 2: Run them to verify they fail**

```bash
uv run pytest tests/test_narrator.py -q -k rail
```

Expected: FAIL in two shapes — the tests that reference the constant (`rail_accepted_at_limit`, `rail_over_limit...`, `build_narrator_prompt_includes_rail_rules`) fail with `AttributeError: module 'periscope.narrator' has no attribute 'RAIL_MAX_LEN'`; the three that never touch it (`rail_empty_or_nonstring_dropped`, `rail_missing_is_none`, `rail_strips_whitespace`) fail with `AttributeError: 'NarratorResult' object has no attribute 'rail'`. (`test_tick_persists_rail` fails with a plain `AssertionError` — `PaneStatusRow.rail` exists since Task 1 but is still `None`.) All mean the same thing: not implemented yet.

- [x] **Step 3: Implement in `periscope/narrator.py`**

(a) Constant, next to `STATUS_MAX_LEN = 72`:

```python
RAIL_MAX_LEN = 28
```

(b) `NarratorResult` — new field LAST with a default (existing two-arg constructions in tests stay valid):

```python
@dataclass(frozen=True)
class NarratorResult:
    status: str
    rename: str | None
    rail: str | None = None
```

(c) `parse_response` — full replacement (rail gets the strip-then-length treatment; only the rail is dropped on failure):

```python
def parse_response(raw: str) -> NarratorResult | None:
    """Model output is an external boundary — the ONLY defensive parsing
    in this module. None means: keep the previous status, retry next tick
    naturally. A non-string rename drops just the rename, and a bad rail
    drops just the rail — never the status."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned,
                         flags=re.MULTILINE)
    try:
        d = json.loads(cleaned)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    status = d.get("status")
    if not isinstance(status, str):
        return None
    status = status.strip()
    if not status or len(status) > STATUS_MAX_LEN:
        return None
    rename = d.get("rename")
    rename = rename.strip() or None if isinstance(rename, str) else None
    rail = d.get("rail")
    if isinstance(rail, str):
        rail = rail.strip()
        if not rail or len(rail) > RAIL_MAX_LEN:
            rail = None
    else:
        rail = None
    return NarratorResult(status=status, rename=rename, rail=rail)
```

(d) `build_narrator_prompt` — insert a rail-rules block between the status rules and the rename block, and extend the return-shape line. The full new function:

```python
def build_narrator_prompt(*, window_name: str, branch: str | None,
                          pr: int | None, cwd: str, signals: dict) -> str:
    """One pane's status+rename prompt. The rename half splices
    rename_ai.RENAME_RULES so taste can't drift from the manual surface."""
    lines = [
        "You watch one developer terminal pane running Claude Code and keep",
        "a one-line status for a dashboard of many such panes.",
        "",
        "Write the status under these rules:",
        f"  - Max {STATUS_MAX_LEN} characters.",
        "  - Present-progressive, e.g. 'fixing flaky reconcile test in tmux_mirror'.",
        "  - Concept-level: what is being accomplished, not which file is open.",
        "    e.g. 'migrating usage scrape to OAuth endpoint', not 'editing usage.py'.",
        "  - No terminal/pane/window/tmux jargon.",
        "  - Describe the most recent WORK even if the pane has since gone quiet —",
        "    the dashboard already shows busy/idle; never mention busy/idle state.",
        "",
        "Also write `rail`: an ultra-short cut of the status for a narrow",
        "sidebar row, rendered directly under the window name. Rules:",
        f"  - Max {RAIL_MAX_LEN} characters.",
        "  - Lead with the current action, e.g. 'comparing lookup hit rates'.",
        f"  - The name '{window_name}' sits right above it — never repeat that",
        "    name's concept; give the differentiating detail instead.",
        "  - No trailing punctuation or ellipsis. All lowercase.",
        "",
        "Also decide whether the window deserves a NEW NAME. Suggest one ONLY",
        "when the work has meaningfully diverged from the current name. Rules:",
        *[f"  {r}" for r in RENAME_RULES],
        "  - Most calls should return null for rename — name churn is worse than",
        "    a slightly stale name. Example: current_name='fs-liveness', recent",
        "    work is still feature-store liveness checks → return",
        '    {"status": "...", "rail": "...", "rename": null}.',
        "",
        f"current_name: {window_name}",
        f"cwd: {cwd}",
    ]
    if branch:
        lines.append(f"branch: {branch}" + (f", PR #{pr}" if pr else ""))
    prompts = signals.get("recent_user_prompts") or []
    if prompts:
        lines.append("recent user prompts (oldest→newest):")
        lines += [f"  {i}. {p}" for i, p in enumerate(prompts, 1)]
    tool_calls = signals.get("recent_tool_calls") or []
    if tool_calls:
        lines.append("recent tool calls (oldest→newest):")
        lines += [f"  - {tc}" for tc in tool_calls]
    files = signals.get("files_touched") or []
    if files:
        lines.append(f"files touched: {', '.join(files)}")
    lines += [
        "",
        'Return ONLY a JSON object: {"status": "<status line>",'
        ' "rail": "<short fragment>", "rename": null | "<new-name>"}.',
        "No markdown fences, no commentary, just the JSON object.",
    ]
    return "\n".join(lines)
```

(e) `_generate` — pass the rail through (final upsert only; without this the column exists but is never written):

```python
    activity.upsert_pane_status(PaneStatusRow(
        pane_id=pane_id, session_id=sid, status=result.status,
        generated_at=now, jsonl_size=size, seen_name=seen_name,
        renamed_at=renamed_at, rail=result.rail))
```

- [x] **Step 4: Run the narrator tests**

```bash
uv run pytest tests/test_narrator.py -q
```

Expected: all pass (56 = 49 existing + 7 new).

- [x] **Step 5: Commit**

```bash
git add periscope/narrator.py tests/test_narrator.py
git commit -m "feat(narrator): rail fragment in model contract — parse/validate, prompt rules, persisted via _generate"
```

---

### Task 3: `/api/state` merge (`routes/state.py`)

**Files:**
- Modify: `periscope/routes/state.py` (merge block, lines 72-81)
- Test: `tests/routes/test_state.py`

- [x] **Step 1: Write the failing tests**

In `tests/routes/test_state.py`, update `test_state_merges_narrator_status_lines` (the seeded row has `rail` unset → defaults None; assert the key is ABSENT — same contract as `status_line` for panes with no status). Add after the existing assertions:

```python
    assert "status_rail" not in views[0]   # row exists but rail is NULL
```

Then add a new test directly below it:

```python
def test_state_merges_status_rail_when_present(client, mocker, clean_state,
                                               fresh_activity_db):
    activity = fresh_activity_db
    activity.upsert_pane_status(activity.PaneStatusRow(
        pane_id="%7", session_id="sid", status="comparing figv2 lookup hit rates",
        generated_at=1234, jsonl_size=10, seen_name="claude", renamed_at=None,
        rail="comparing hit rates"))
    windows = [
        {"session": "s", "index": 0, "active": True, "activity": 0,
         "pane_id": "%7", "cwd": ""},
    ]
    _patch(mocker, "list_windows", return_value=windows)
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "all_projects", return_value={})
    # Hermeticity: same usage patches as test_state_merges_narrator_status_lines.
    _patch(mocker, "cached_claude_usage", return_value={})
    _patch(mocker, "cached_plan_usage", return_value=None)
    _patch(mocker, "build_window_view",
           side_effect=lambda w, now_ts: (
               {"index": w["index"], "pane_id": w["pane_id"]}, None))

    body = client.get("/api/state").json()
    w = body["windows"][0]
    assert w["status_line"] == "comparing figv2 lookup hit rates"
    assert w["status_at"] == 1234
    assert w["status_rail"] == "comparing hit rates"
```

- [x] **Step 2: Run to verify they fail**

```bash
uv run pytest tests/routes/test_state.py -q
```

Expected: 2 FAIL — both merge tests hit `ValueError: too many values to unpack (expected 2)` in the route (the Task 1 collateral failure plus the new test).

- [x] **Step 3: Implement the merge in `periscope/routes/state.py`**

Replace the merge block (lines 75-80):

```python
    statuses = pane_status_lines()
    if statuses:
        for view in result:
            s = statuses.get(view.get("pane_id") or "")
            if s:
                view["status_line"], view["status_at"], rail = s
                # Absent-key contract (same as status_line): rows generated
                # before the rail column, or with model-rejected rails, send
                # nothing and the UI falls back to status_line.
                if rail:
                    view["status_rail"] = rail
```

- [x] **Step 4: Run the route tests, then the full suite**

```bash
uv run pytest tests/routes/test_state.py -q
```

Expected: all pass (8 = 7 existing + 1 new).

```bash
uv run pytest -q
```

Expected: `623 passed` (611 baseline + 12 new), 0 failed — the Task 1 collateral failure is closed.

- [x] **Step 5: Commit**

```bash
git add periscope/routes/state.py tests/routes/test_state.py
git commit -m "feat(narrator): /api/state sends status_rail when a non-empty rail exists"
```

---

### Task 4: Frontend — full-width status row + tree-guide pinning

NO component tests (project convention: frontend is browser-verified; bundle-grep is the headless stand-in). Geometry decisions come from the spec — don't re-derive them:
- The status is its own line BELOW the name row, spanning the full label-column width **past the dot gutter** (the dot/pin/close live only on the name row) but **inside the tree-guide indent** (left edge = row content box, which already starts right of the vertical guides).
- Tree-guide connector stub (`::after`) and last-row terminator (`last-in-worktree::before`) currently assume single-line rows (`top:50%` / `bottom:50%`); pin them to the name line's center via a fixed offset (padding-top + half the ~18px name-row height) so two-line rows don't skew the tree. The compact `#rail` override (3px padding vs the base 7px) needs its own offset value.
- Hover-reveal (`.rail-row:hover .rail-close`, `#rail .child-row:hover .rail-pin`) keeps working untouched — `:hover` is on the row and those are descendant selectors, one nesting level deeper now.
- Row-level `draggable` + `onClick` stay on the row root. Stale-dim (`STATUS_STALE_S`) and the hover `title` (full `status_line`) unchanged. `Detail.jsx` untouched (keeps full `status_line`).

**Files:**
- Modify: `static/src/split/RailRows.jsx` (PaneRow, lines 109-156)
- Modify: `static/styles.css` (rail-status block ~lines 1866-1875, tree guides ~lines 1914-1942, compact overrides ~line 2104)
- Build artifact: `static/dist/app.js`

- [x] **Step 1: Restructure `PaneRow` in `static/src/split/RailRows.jsx`**

Full replacement of the component (the `.rail-label-col` wrapper goes away; a `pane-row-main` nested flex holds the name row in its existing order — icon, label, burn, pin, dot, close — and the status becomes a sibling line; text renders `status_rail || status_line` with the full status as hover title):

```jsx
export function PaneRow({ w, selectedKey, onSelect, onClose, onRename, dim, dragProps, dropPos, pinned, onTogglePin }) {
  const k = `pane:${w.pid}`;
  const sel = k === selectedKey ? " selected" : "";
  const dimCls = dim ? "" : " rail-dim";
  const drop = dropPos ? " drop-target" : "";
  const label = w.name || (w.is_claude ? "claude" : "shell");
  const statusStale =
    w.status_at && Math.floor(Date.now() / 1000) - w.status_at > STATUS_STALE_S;
  return (
    <div
      class={`rail-row child-row pane-row${sel}${dimCls}${drop}`}
      data-drop-pos={dropPos || undefined}
      draggable
      onClick={() => onSelect(k)}
      {...dragProps}
    >
      <div class="pane-row-main">
        {w.is_claude
          ? <span class="rail-icon icon-claude">✻</span>
          : <span class="rail-icon icon-shell">$</span>}
        <RailLabel label={label} kind="pane" renameable onCommit={onRename} />
        {w.burn_hot && (
          <span
            class="rail-burn"
            title={`eating the session quota — ~${w.burn_wtpm || "?"} weighted tok/min over the last 30m`}
          >🔥</span>
        )}
        <button
          class={`rail-pin${pinned ? " pinned" : ""}`}
          title={pinned ? "unpin" : "pin"}
          onClick={(e) => { e.stopPropagation(); onTogglePin && onTogglePin(); }}
        >{pinned ? "★" : "☆"}</button>
        <span class={statusDotClass(w.state)}></span>
        <button
          class="rail-close"
          title="kill this tab"
          onClick={(e) => { e.stopPropagation(); onClose(); }}
        >×</button>
      </div>
      {w.status_line && (
        <span
          class={`rail-status${statusStale ? " stale" : ""}`}
          title={w.status_line}
        >{w.status_rail || w.status_line}</span>
      )}
    </div>
  );
}
```

- [x] **Step 2: Update `static/styles.css`**

(a) Replace the narrator-status block (currently `.rail-label-col` + `.rail-status`, lines ~1866-1875) with:

```css
/* Narrator status — full-width second line under the name row. The pane
   row stacks vertically: a nested .pane-row-main keeps the old single-line
   flex (icon/label/burn/pin/dot/close), and the status spans the whole row
   width past the dot gutter — but stays inside the tree-guide indent (the
   row's content box already starts right of the vertical guides).
   .stale = generated >15 min ago (poll keeps it; the work likely moved on). */
.rail-row.pane-row { flex-direction: column; align-items: stretch; gap: 0; }
.pane-row-main { display: flex; align-items: center; gap: 7px; min-width: 0; }
.pane-row .rail-status { margin-left: 21px; }  /* icon 14px + 7px gap — align with the label */
.rail-status {
  font-size: 11px; color: var(--fg-3);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.rail-status.stale { opacity: .5; }
```

(b) In the tree-guide block (lines ~1914-1942): the connector stub and the last-row terminator assumed single-line rows. Edit the existing rules — `::before` for inner children stays full-height (the vertical line SHOULD run through both lines); the two percentage anchors become fixed offsets pinned to the name line's center:

```css
/* Continuous tree lines drawn via CSS so adjacent child rows' connectors
   actually touch. Replaces the per-row ├/└ glyphs, which broke at every
   row boundary because of vertical padding.
     ::before  — vertical line in the gutter (full row height for inner
                 children; top-to-name-center for the last-in-worktree row)
     ::after   — horizontal stub from the vertical line to the icon
   Positioned at the center of the worktree-level indent column.
   Two-line pane rows make percentage anchors (top/bottom:50%) land below
   the name line, skewing the tree — so the stub and the terminator pin to
   the name line's center instead: padding-top + half the ~18px name row
   (the 18px close/pin buttons set the line height). */
.rail-row.child-row { --rail-name-center: 16px; }   /* base: 7px pad + 9px */
.rail-row.child-row::before,
.rail-row.child-row::after {
  content: "";
  position: absolute;
  background: var(--fg-4);
  opacity: .35;
}
.rail-row.child-row::before {
  left: calc(10px + var(--rail-indent) + 7px);   /* center of worktree column */
  top: 0;
  bottom: 0;
  width: 1px;
}
.rail-row.child-row.last-in-worktree::before {
  bottom: auto;
  height: var(--rail-name-center);                /* line stops at the stub */
}
.rail-row.child-row::after {
  left: calc(10px + var(--rail-indent) + 7px);
  top: var(--rail-name-center);
  width: calc(var(--rail-indent) - 5px);
  height: 1px;
}
```

(c) In the compact-restyle section (~line 2104, `#rail .child-row { padding-left: ... }`): the `#rail` rows run 3px vertical padding instead of 7px, so the name-line center moves up. Add one line next to the existing `#rail .child-row` rule:

```css
#rail .child-row { --rail-name-center: 12px; }   /* compact: 3px pad + 9px */
```

- [x] **Step 3: Build the bundle**

```bash
npm run build
```

Expected: Vite completes, writes `static/dist/app.js`.

- [x] **Step 4: Bundle grep (headless stand-in for browser verification)**

```bash
grep -c "pane-row-main" static/dist/app.js && grep -c "status_rail" static/dist/app.js
```

Expected: both print `1` or more (non-zero exit means the new markup/fallback didn't make the bundle — investigate before committing).

- [x] **Step 5: Browser-verify the geometry (if convenient)**

```bash
PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py
```

Open http://localhost:8766/ — check: status renders as a second line under pane names; the tree's horizontal stubs meet the NAME line (not the row middle) on two-line rows; the last child's vertical guide terminates at the stub; hover still reveals × and ☆; drag-reorder and click-select still work. Kill the server after. (Status text may be the full `status_line` until prod generates rails — fallback chain working as designed.) If the 16px/12px offsets visibly miss the name-line center, adjust `--rail-name-center` by ±1-2px and rebuild.

- [x] **Step 6: Commit (source + CSS + bundle together — the dist is the one committed build artifact)**

```bash
git add static/src/split/RailRows.jsx static/styles.css static/dist/app.js
git commit -m "feat(rail): full-width status row under pane name, rail-fragment fallback chain, tree guides pinned to name-line center"
```

---

### Task 5: Full suite, merge, deploy, prod verification, cleanup

**Files:** none new (merge + ops)

- [x] **Step 1: Full suite in the worktree**

```bash
uv run pytest -q
```

Expected: `623 passed` (or baseline+12 if main's count moved), 0 failed.

- [x] **Step 2: Merge to main** (run from `~/dev/periscope`, the main checkout — plain merge commit, never rebase)

```bash
git -C ~/dev/periscope merge feature/narrator-rail
```

Expected: clean merge. **If `static/dist/app.js` conflicts** (main moved with its own frontend change since branching): the bundle is a build artifact — never hand-merge it. Resolve by rebuilding on the merged tree:

```bash
git -C ~/dev/periscope checkout --theirs static/dist/app.js   # any content; about to overwrite
cd ~/dev/periscope && npm run build
git add static/dist/app.js
git commit -m "merge feature/narrator-rail (bundle rebuilt on merged tree)"
```

If *source* files conflict, resolve those first, then rebuild the bundle the same way before committing the merge.

- [x] **Step 3: Restart prod**

```bash
~/dev/periscope/bin/periscope restart
```

- [x] **Step 4: Prod verification** (the migration is exercised on this deploy — the prod DB has live pane_status rows)

Wait ~2-3 minutes (worker ticks every 30s; per-pane regeneration is gated at 90s and only fires when a transcript changes), then:

```bash
grep "narrator:" ~/.config/periscope/periscope-8765.log | tail -5
```

Expected: recent `narrator: %N status '...'` lines, no `OperationalError` / `no such column` anywhere in the tail of the log.

```bash
sqlite3 ~/.config/periscope/periscope.db \
  "SELECT pane_id, rail FROM pane_status WHERE rail IS NOT NULL LIMIT 5"
```

Expected: at least one row with a short lowercase rail fragment once a generation has fired post-deploy (old rows keep NULL — that's the designed fallback). If empty after several minutes, check that some Claude pane actually produced transcript activity since the restart.

Note: the `rail` COLUMN already exists on the prod DB at this point — Task 1's first full-suite run migrated it (route tests without `fresh_activity_db` open the real `~/.config/periscope/periscope.db`). That's benign and expected; don't treat a pre-existing column as evidence the deploy worked. The deploy-time signal is rows with non-null `rail` appearing after the restart.

```bash
curl -s http://127.0.0.1:8765/api/state | \
  python3 -c "import json,sys; ws=json.load(sys.stdin)['windows']; print([(w.get('name'), w.get('status_rail')) for w in ws if w.get('status_rail')])"
```

Expected: a non-empty list once rails exist in the DB. Also eyeball the dashboard at http://127.0.0.1:8765/ — pane rows show the short fragment under the name; pre-deploy panes show the truncated full status (fallback).

- [x] **Step 5: Worktree cleanup**

```bash
git worktree remove ~/dev/periscope-rail
git -C ~/dev/periscope branch -d feature/narrator-rail
```

Expected: both succeed (branch is fully merged).

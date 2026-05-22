# Faceted History Search — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every real Claude Code session four AI-derived facets (outcome, category, notable, topics) in `history.db`, filter out periscope's `/usage` scrape transcripts, install the SessionEnd hook so the index stays fresh, and run the one-shot classification before the May 23 credit expiry.

**Architecture:** Extend the existing per-session summarizer (`history/summarize.py`) — the forced `save_session_summary` tool gains four fields, so the same one Haiku/Sonnet call that already produces a summary now also classifies. New `sessions` columns hold the facets; the indexer skips scrape transcripts (`asst_msg_count == 0`); the burn runs via the existing `backfill`/`resummarize` verbs on Sonnet.

**Tech Stack:** Python 3 + stdlib `sqlite3`, the Anthropic SDK (already wired in `history/`), `uv run pytest -q`.

**Spec:** `docs/superpowers/specs/2026-05-21-faceted-history-search-design.md`

**Scope:** This plan is **Phase 1 only** — the deadline-critical classification layer. After it lands, `history.db` carries the facets and stays fresh. **Phase 2** (the `/history` filter UI + search-API facet params) is not deadline-bound and gets its own plan, written against the Phase-1-complete code.

---

## File Structure

- `history/schema.sql` — four new `sessions` columns (fresh-DB path).
- `history/db.py` — `SCHEMA_VERSION` bump + an idempotent column-add migration.
- `history/summarize.py` — the four facet fields on the tool, prompt, `SummaryResult`, and parsing.
- `history/indexer.py` — skip scrape transcripts; persist + reuse the facets; drop the model-mismatch resummary trigger.
- `history/backfill.py` + `history/cli.py` — thread a per-run `--model` override.
- `~/.claude/settings.json` — add the `history` SessionEnd hook (user config, not a repo file).
- Tests: `history/tests/{test_db,test_summarize,test_indexer,test_cli}.py`.

---

## Task 1: Schema — four facet columns + migration

**Files:**
- Modify: `history/schema.sql`
- Modify: `history/db.py`
- Test: `history/tests/test_db.py`

- [ ] **Step 1: Write the failing test**

Append to `history/tests/test_db.py`:

```python
def test_migration_adds_facet_columns_to_v1_db(tmp_path):
    """A pre-facets `sessions` table gains the four columns on connect()."""
    import sqlite3
    from history import db
    p = tmp_path / "old.db"
    # Minimal v1-shaped sessions table (no facet columns).
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, summary TEXT)")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    conn.close()
    # connect() runs apply_schema → the migration.
    conn = db.connect(p)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert {"outcome", "category", "notable", "topics"} <= cols
    conn.close()
    # Idempotent: a second connect() does not raise.
    db.connect(p).close()


def test_fresh_db_has_facet_columns(tmp_path):
    from history import db
    conn = db.connect(tmp_path / "fresh.db")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert {"outcome", "category", "notable", "topics"} <= cols
    conn.close()
```

- [ ] **Step 2: Run the test — verify it fails**

Run: `uv run pytest -q history/tests/test_db.py -k facet`
Expected: FAIL — the migration does not exist; `outcome`/etc. not in `cols`.

- [ ] **Step 3: Add the columns to `schema.sql`**

In `history/schema.sql`, inside the `CREATE TABLE IF NOT EXISTS sessions (...)`, immediately after the `summary_model` line, add:

```sql
  outcome              TEXT,
  category             TEXT,
  notable              INTEGER,
  topics               TEXT,
```

(`CREATE TABLE IF NOT EXISTS` is a no-op on an existing DB — this only covers fresh DBs; the migration in Step 4 covers existing ones.)

- [ ] **Step 4: Add the migration to `db.py`**

In `history/db.py`, bump the version and add the migration. Change `SCHEMA_VERSION = 1` to:

```python
SCHEMA_VERSION = 2
```

Add this function above `apply_schema`:

```python
# Columns added after schema_version 1. SQLite has no ADD COLUMN IF NOT
# EXISTS, so the migration is a guarded PRAGMA-checked ALTER — idempotent.
_V2_COLUMNS = (("outcome", "TEXT"), ("category", "TEXT"),
               ("notable", "INTEGER"), ("topics", "TEXT"))


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent in-place column adds for existing DBs."""
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
    if cur.fetchone() is None:
        return  # fresh DB — schema.sql already has the columns
    have = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    for name, decl in _V2_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {decl}")
```

In `apply_schema`, call `_migrate` right after `conn.executescript(_SCHEMA_SQL)` and make the `schema_version` meta reflect the constant. Replace the body of `apply_schema` with:

```python
def apply_schema(conn: sqlite3.Connection) -> None:
    """Run the schema DDL, migrate existing tables, seed meta keys. Idempotent."""
    conn.executescript(_SCHEMA_SQL)
    _migrate(conn)
    cur = conn.execute("SELECT key FROM meta")
    existing = {row[0] for row in cur}
    seed = {
        "schema_version":     str(SCHEMA_VERSION),
        "mechanical_version": str(MECHANICAL_VERSION),
        "haiku_model":        DEFAULT_HAIKU_MODEL,
    }
    for key, value in seed.items():
        if key not in existing:
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (key, value))
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
```

- [ ] **Step 5: Run the test — verify it passes**

Run: `uv run pytest -q history/tests/test_db.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add history/schema.sql history/db.py history/tests/test_db.py
git commit -m "history: schema v2 — outcome/category/notable/topics columns + migration"
```

---

## Task 2: Extend the summarizer with the four facets

**Files:**
- Modify: `history/summarize.py`
- Test: `history/tests/test_summarize.py`

- [ ] **Step 1: Write the failing test**

Append to `history/tests/test_summarize.py` (it already exercises `call_summarizer` with a fake client — match that file's existing fake-client pattern; the snippet below constructs the tool-use response inline):

```python
def test_call_summarizer_parses_facets():
    from history.summarize import call_summarizer, SUMMARIZE_TOOL
    from history.extract import SessionRecord

    # The tool advertises the four facet fields.
    props = SUMMARIZE_TOOL["input_schema"]["properties"]
    assert {"outcome", "category", "notable", "topics"} <= set(props)

    class _Block:
        type = "tool_use"
        name = "save_session_summary"
        input = {
            "summary": "did the thing", "tags": ["a", "b", "c"],
            "outcome": "shipped", "category": "feature",
            "notable": True, "topics": ["periscope", "Rust"],
        }

    class _Msg:
        content = [_Block()]
        stop_reason = "tool_use"

    class _Client:
        class messages:
            @staticmethod
            def create(**_kw):
                return _Msg()

    rec = SessionRecord(
        session_id="s1", jsonl_path="/x", project_path="/p", branch=None,
        started_at=0, ended_at=0, duration_s=0,
        user_msg_count=3, asst_msg_count=3, tool_use_count=0,
        was_interrupted=0, ended_cleanly=1,
        first_user_msg="hi", last_user_msg="bye", final_assistant_msg="done",
        files_touched="[]", notable_cmds="[]", tool_use_counts="{}",
    )
    res = call_summarizer(_Client(), rec, model="claude-haiku-4-5")
    assert res.outcome == "shipped"
    assert res.category == "feature"
    assert res.notable is True
    assert res.topics == ["periscope", "rust"]   # normalized lowercase


def test_call_summarizer_rejects_unknown_enum():
    from history.summarize import call_summarizer
    from history.extract import SessionRecord

    class _Block:
        type = "tool_use"
        name = "save_session_summary"
        input = {"summary": "x", "tags": ["a", "b", "c"],
                 "outcome": "banana", "category": "nonsense",
                 "notable": False, "topics": []}

    class _Msg:
        content = [_Block()]
        stop_reason = "tool_use"

    class _Client:
        class messages:
            @staticmethod
            def create(**_kw):
                return _Msg()

    rec = SessionRecord(
        session_id="s2", jsonl_path="/x", project_path="/p", branch=None,
        started_at=0, ended_at=0, duration_s=0,
        user_msg_count=3, asst_msg_count=3, tool_use_count=0,
        was_interrupted=0, ended_cleanly=1,
        first_user_msg="hi", last_user_msg="bye", final_assistant_msg="done",
        files_touched="[]", notable_cmds="[]", tool_use_counts="{}",
    )
    res = call_summarizer(_Client(), rec, model="claude-haiku-4-5")
    assert res.outcome is None      # unknown enum → None
    assert res.category is None
```

- [ ] **Step 2: Run the test — verify it fails**

Run: `uv run pytest -q history/tests/test_summarize.py -k facet`
Expected: FAIL — `SummaryResult` has no `outcome`; the tool lacks the fields.

- [ ] **Step 3: Extend the tool, prompt, dataclass, and parsing**

In `history/summarize.py`:

Change the import line `from dataclasses import dataclass` to:

```python
from dataclasses import dataclass, field
```

Add module constants below the imports:

```python
_OUTCOMES = {"shipped", "partial", "abandoned", "explored", "blocked"}
_CATEGORIES = {"feature", "bugfix", "refactor", "debugging",
               "research", "ops", "docs", "review"}
```

In `SUMMARIZE_TOOL["input_schema"]["properties"]`, after the `tags` entry, add:

```python
            "outcome": {
                "type": "string",
                "enum": ["shipped", "partial", "abandoned", "explored", "blocked"],
                "description": (
                    "How the session ended. shipped: work landed/committed. "
                    "partial: real progress, left unfinished. abandoned: started "
                    "then dropped. explored: investigation or Q&A, no code change "
                    "intended. blocked: stuck on an external problem."
                ),
            },
            "category": {
                "type": "string",
                "enum": ["feature", "bugfix", "refactor", "debugging",
                         "research", "ops", "docs", "review"],
                "description": "The primary kind of work in the session.",
            },
            "notable": {
                "type": "boolean",
                "description": (
                    "true if the session is substantial or worth revisiting; "
                    "false for routine, trivial, or false-start work."
                ),
            },
            "topics": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 4,
                "description": (
                    "1-4 canonical lowercase topic tags — prefer the project "
                    "name or a broad tech/area term; singular, deduplicated."
                ),
            },
```

In the same `input_schema`, change `"required": ["summary", "tags"]` to:

```python
        "required": ["summary", "tags", "outcome", "category", "notable", "topics"],
```

Change `SUMMARIZE_SYSTEM_PROMPT` to append one sentence — replace it with:

```python
SUMMARIZE_SYSTEM_PROMPT = (
    "You summarize and classify Claude Code coding sessions for a search "
    "index. Output is consumed by a developer searching their own history "
    "later. Bias toward concrete specifics (file names, error messages, "
    "decisions) over generic descriptions. Also classify the session's "
    "outcome, category, notability, and topics per the tool schema — the "
    "final assistant message is the strongest outcome signal. Always call "
    "save_session_summary."
)
```

Replace the `SummaryResult` dataclass with:

```python
@dataclass
class SummaryResult:
    summary: str
    tags: list[str]
    model: str
    outcome: str | None = None
    category: str | None = None
    notable: bool = False
    topics: list[str] = field(default_factory=list)
```

In `call_summarizer`, the success branch currently ends:

```python
            # Normalize + filter empty tags
            norm_tags = [s for s in (str(t).lower().strip() for t in tags) if s]
            return SummaryResult(summary=summary.strip(),
                                  tags=norm_tags,
                                  model=model)
```

Replace that with:

```python
            # Normalize + filter empty tags
            norm_tags = [s for s in (str(t).lower().strip() for t in tags) if s]
            # Facets — unknown enum values degrade to None, not a crash.
            outcome = data.get("outcome")
            if outcome not in _OUTCOMES:
                outcome = None
            category = data.get("category")
            if category not in _CATEGORIES:
                category = None
            notable = bool(data.get("notable"))
            raw_topics = data.get("topics")
            topics = ([s for s in (str(t).lower().strip() for t in raw_topics) if s]
                      if isinstance(raw_topics, list) else [])
            return SummaryResult(summary=summary.strip(),
                                  tags=norm_tags,
                                  model=model,
                                  outcome=outcome,
                                  category=category,
                                  notable=notable,
                                  topics=topics)
```

- [ ] **Step 4: Run the test — verify it passes**

Run: `uv run pytest -q history/tests/test_summarize.py`
Expected: PASS — including the file's pre-existing tests.

- [ ] **Step 5: Commit**

```bash
git add history/summarize.py history/tests/test_summarize.py
git commit -m "history: summarizer also classifies outcome/category/notable/topics"
```

---

## Task 3: Skip `/usage` scrape transcripts

**Files:**
- Modify: `history/indexer.py`
- Test: `history/tests/test_indexer.py`

- [ ] **Step 1: Write the failing test**

Append to `history/tests/test_indexer.py` (match the file's existing pattern for writing a transcript fixture + calling `index_one`; the snippet shows the assertions). A `/usage` scrape transcript is one whose extracted `asst_msg_count` is 0 — Claude launched but never produced an assistant turn:

```python
def test_index_one_skips_scrape_session(tmp_path, monkeypatch):
    """A transcript with no assistant turn (a /usage scrape) is not indexed."""
    from history import indexer
    # A minimal transcript: one user line, zero assistant lines.
    jp = tmp_path / "scrape.jsonl"
    jp.write_text(
        '{"type":"user","cwd":"/repo","sessionId":"sc1",'
        '"timestamp":"2026-05-20T10:00:00.000Z",'
        '"message":{"role":"user","content":"hi"}}\n'
    )
    db = tmp_path / "h.db"
    res = indexer.index_one(str(jp), db_path=db, force=True)
    assert res["status"] == "skipped-scrape"
    from history.db import connect
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    conn.close()
```

If `history/tests/test_indexer.py` already has a transcript-fixture helper, use it instead of the inline JSONL; the assertion (`status == "skipped-scrape"`, zero rows) is the point.

- [ ] **Step 2: Run the test — verify it fails**

Run: `uv run pytest -q history/tests/test_indexer.py -k scrape`
Expected: FAIL — the session is indexed (status is `trivial` or similar), row count is 1.

- [ ] **Step 3: Add the scrape filter to `index_one`**

In `history/indexer.py`, in `index_one`, immediately after the `rec = extract_record(...)` call and before the `if not force and _is_live(...)` check, add:

```python
    # periscope's /usage scraper launches a throwaway `claude` that is
    # screen-scraped and killed — it never produces an assistant turn.
    # asst_msg_count == 0 means no real work; skip without a DB write.
    # (The SessionEnd hook fires for these too, so the filter must live
    # here, not just in backfill.)
    if rec.asst_msg_count == 0:
        return {"status": "skipped-scrape", "session_id": rec.session_id}
```

- [ ] **Step 4: Run the test — verify it passes**

Run: `uv run pytest -q history/tests/test_indexer.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add history/indexer.py history/tests/test_indexer.py
git commit -m "history: skip /usage scrape transcripts (no assistant turn) at index time"
```

---

## Task 4: Persist the facets through the indexer

**Files:**
- Modify: `history/indexer.py`
- Test: `history/tests/test_indexer.py`

- [ ] **Step 1: Write the failing test**

Append to `history/tests/test_indexer.py`. This drives `index_one` with a stubbed summarizer so the facets flow end-to-end into the columns:

```python
def test_index_one_persists_facets(tmp_path, monkeypatch):
    from history import indexer
    from history.summarize import SummaryResult

    # Stub the Anthropic client + the summarizer call.
    monkeypatch.setattr(indexer, "get_anthropic_client", lambda: object())
    monkeypatch.setattr(indexer, "call_summarizer", lambda client, rec, model=None:
        SummaryResult(summary="s", tags=["t1", "t2", "t3"], model="m",
                      outcome="shipped", category="bugfix",
                      notable=True, topics=["periscope"]))

    # A non-trivial transcript: >=2 user msgs, an assistant turn, >60s span.
    jp = tmp_path / "real.jsonl"
    jp.write_text("\n".join([
        '{"type":"user","cwd":"/repo","sessionId":"r1","timestamp":"2026-05-20T10:00:00.000Z","message":{"role":"user","content":"first request please"}}',
        '{"type":"assistant","sessionId":"r1","timestamp":"2026-05-20T10:01:00.000Z","message":{"role":"assistant","content":"working on it"}}',
        '{"type":"user","cwd":"/repo","sessionId":"r1","timestamp":"2026-05-20T10:02:00.000Z","message":{"role":"user","content":"second request please"}}',
        '{"type":"assistant","sessionId":"r1","timestamp":"2026-05-20T10:03:00.000Z","message":{"role":"assistant","content":"done"}}',
    ]) + "\n")
    db = tmp_path / "h.db"
    res = indexer.index_one(str(jp), db_path=db, force=True)
    assert res["status"] == "summarized"

    from history.db import connect
    conn = connect(db)
    row = conn.execute(
        "SELECT outcome, category, notable, topics FROM sessions WHERE session_id='r1'"
    ).fetchone()
    conn.close()
    assert row["outcome"] == "shipped"
    assert row["category"] == "bugfix"
    assert row["notable"] == 1
    import json as _j
    assert _j.loads(row["topics"]) == ["periscope"]
```

If the transcript shape above does not parse as non-trivial in this codebase's `jsonl.py`/`extract.py`, adjust the fixture to whatever the existing `test_indexer.py` uses for a "real, summarized" session — the assertion (facets land in the four columns) is the point.

- [ ] **Step 2: Run the test — verify it fails**

Run: `uv run pytest -q history/tests/test_indexer.py -k persists_facets`
Expected: FAIL — `_upsert` does not write the facet columns; `outcome` is `None`.

- [ ] **Step 3: Extend `_upsert` to write the facet columns**

In `history/indexer.py`, replace the `_upsert` function with this version — it adds four parameters and the four columns to both the INSERT and the ON CONFLICT UPDATE (the `sessions_fts` insert is unchanged — facets are not full-text):

```python
def _upsert(conn: sqlite3.Connection, rec: SessionRecord, *,
             summary: str | None, tags_csv: str | None,
             summary_input_hash: str | None, summary_model: str | None,
             outcome: str | None = None, category: str | None = None,
             notable: int | None = None, topics_json: str | None = None) -> None:
    """One transaction: UPSERT sessions + refresh sessions_fts.

    `tags_csv` is the canonical stored form (comma-separated). The sessions
    table stores it as NULL when absent; the sessions_fts virtual table
    cannot hold NULL in indexed columns, so we substitute "" there. This
    asymmetry is intentional — don't change one side without the other.

    The four facet columns (outcome/category/notable/topics) are AI-derived
    and stay out of sessions_fts — they are filtered structurally."""
    conn.execute(
        """
        INSERT INTO sessions (
          session_id, jsonl_path, project_path, branch,
          started_at, ended_at, duration_s,
          user_msg_count, asst_msg_count, tool_use_count,
          was_interrupted, ended_cleanly,
          summary, tags, summary_input_hash, summary_model,
          outcome, category, notable, topics,
          first_user_msg, last_user_msg, final_assistant_msg,
          files_touched, notable_cmds, tool_use_counts,
          indexed_at, mechanical_version, source_mtime, source_size
        )
        VALUES (
          ?, ?, ?, ?,
          ?, ?, ?,
          ?, ?, ?,
          ?, ?,
          ?, ?, ?, ?,
          ?, ?, ?, ?,
          ?, ?, ?,
          ?, ?, ?,
          ?, ?, ?, ?
        )
        ON CONFLICT(session_id) DO UPDATE SET
          jsonl_path = excluded.jsonl_path,
          project_path = excluded.project_path,
          branch = excluded.branch,
          started_at = excluded.started_at,
          ended_at = excluded.ended_at,
          duration_s = excluded.duration_s,
          user_msg_count = excluded.user_msg_count,
          asst_msg_count = excluded.asst_msg_count,
          tool_use_count = excluded.tool_use_count,
          was_interrupted = excluded.was_interrupted,
          ended_cleanly = excluded.ended_cleanly,
          summary = excluded.summary,
          tags = excluded.tags,
          summary_input_hash = excluded.summary_input_hash,
          summary_model = excluded.summary_model,
          outcome = excluded.outcome,
          category = excluded.category,
          notable = excluded.notable,
          topics = excluded.topics,
          first_user_msg = excluded.first_user_msg,
          last_user_msg = excluded.last_user_msg,
          final_assistant_msg = excluded.final_assistant_msg,
          files_touched = excluded.files_touched,
          notable_cmds = excluded.notable_cmds,
          tool_use_counts = excluded.tool_use_counts,
          indexed_at = excluded.indexed_at,
          mechanical_version = excluded.mechanical_version,
          source_mtime = excluded.source_mtime,
          source_size = excluded.source_size
        """,
        (
            rec.session_id, rec.jsonl_path, rec.project_path, rec.branch,
            rec.started_at, rec.ended_at, rec.duration_s,
            rec.user_msg_count, rec.asst_msg_count, rec.tool_use_count,
            rec.was_interrupted, rec.ended_cleanly,
            summary, tags_csv,
            summary_input_hash, summary_model,
            outcome, category, notable, topics_json,
            rec.first_user_msg, rec.last_user_msg, rec.final_assistant_msg,
            rec.files_touched, rec.notable_cmds, rec.tool_use_counts,
            int(time.time()), MECHANICAL_VERSION, rec.source_mtime, rec.source_size,
        ),
    )
    conn.execute("DELETE FROM sessions_fts WHERE session_id = ?", (rec.session_id,))
    conn.execute(
        """
        INSERT INTO sessions_fts (
          session_id, summary, tags,
          first_user_msg, last_user_msg, final_assistant_msg,
          user_messages, assistant_text,
          files_touched, notable_cmds
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rec.session_id, summary or "", tags_csv or "",
            rec.first_user_msg or "", rec.last_user_msg or "",
            rec.final_assistant_msg or "",
            rec.user_messages_blob, rec.assistant_text_blob,
            rec.files_touched, rec.notable_cmds,
        ),
    )
```

- [ ] **Step 4: Reuse facets on a cache hit; wire the summarized path**

Still in `history/indexer.py`, in `index_one`:

The cache-lookup `SELECT` currently reads four columns. Replace:

```python
        row = conn.execute(
            "SELECT summary, tags, summary_input_hash, summary_model FROM sessions WHERE session_id = ?",
            (rec.session_id,),
        ).fetchone()
```

with:

```python
        row = conn.execute(
            "SELECT summary, tags, summary_input_hash, summary_model, "
            "outcome, category, notable, topics FROM sessions WHERE session_id = ?",
            (rec.session_id,),
        ).fetchone()
```

The **hash-cache-hit** `_upsert` call currently is:

```python
            _upsert(conn, rec,
                    summary=row["summary"], tags_csv=row["tags"],
                    summary_input_hash=new_hash, summary_model=row["summary_model"])
```

Replace it with (carry the stored facets forward):

```python
            _upsert(conn, rec,
                    summary=row["summary"], tags_csv=row["tags"],
                    summary_input_hash=new_hash, summary_model=row["summary_model"],
                    outcome=row["outcome"], category=row["category"],
                    notable=row["notable"], topics_json=row["topics"])
```

The **summarized** `_upsert` call currently is:

```python
        _upsert(conn, rec,
                summary=result.summary,
                tags_csv=",".join(result.tags) if result.tags else None,
                summary_input_hash=new_hash, summary_model=result.model)
```

Replace it with:

```python
        _upsert(conn, rec,
                summary=result.summary,
                tags_csv=",".join(result.tags) if result.tags else None,
                summary_input_hash=new_hash, summary_model=result.model,
                outcome=result.outcome, category=result.category,
                notable=1 if result.notable else 0,
                topics_json=json.dumps(result.topics) if result.topics else None)
```

`json` is already imported in `indexer.py`. The remaining `_upsert` calls
(the `is_trivial`, `no-api-key`, and `summary-failed` paths) pass no facet
arguments — the defaults leave the columns `NULL`, which is correct
(those sessions are unclassified). Leave them as they are.

- [ ] **Step 5: Drop the model-mismatch resummary trigger**

A Sonnet-classified row must not be silently re-done on Haiku by a later
`backfill`. In `history/indexer.py`, `_row_needs_resummary` currently has:

```python
    if row["summary_model"] != target_model:
        return True
```

Delete those two lines. Re-summary is then driven only by a NULL summary
or a content-hash change; a deliberate model change still goes through
`resummarize --all` (which NULLs the hash — see Task 5).

- [ ] **Step 6: Run the tests — verify they pass**

Run: `uv run pytest -q history/tests/test_indexer.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add history/indexer.py history/tests/test_indexer.py
git commit -m "history: persist + reuse the facet columns through the indexer"
```

---

## Task 5: Per-run `--model` override

**Files:**
- Modify: `history/indexer.py`, `history/backfill.py`, `history/cli.py`
- Test: `history/tests/test_indexer.py`

The burn runs on Sonnet without changing the `haiku_model` meta default
(which the ongoing hook keeps using). A per-call `model` override threads
through `index_one` → `backfill` → the `backfill`/`resummarize` verbs.

- [ ] **Step 1: Write the failing test**

Append to `history/tests/test_indexer.py`:

```python
def test_index_one_model_override_beats_meta_default(tmp_path, monkeypatch):
    from history import indexer
    from history.summarize import SummaryResult

    seen = {}
    monkeypatch.setattr(indexer, "get_anthropic_client", lambda: object())
    def _fake(client, rec, model=None):
        seen["model"] = model
        return SummaryResult(summary="s", tags=["a", "b", "c"], model=model or "?",
                             outcome="explored", category="research",
                             notable=False, topics=["x"])
    monkeypatch.setattr(indexer, "call_summarizer", _fake)

    jp = tmp_path / "real.jsonl"
    jp.write_text("\n".join([
        '{"type":"user","cwd":"/r","sessionId":"m1","timestamp":"2026-05-20T10:00:00.000Z","message":{"role":"user","content":"first request please"}}',
        '{"type":"assistant","sessionId":"m1","timestamp":"2026-05-20T10:01:00.000Z","message":{"role":"assistant","content":"ok"}}',
        '{"type":"user","cwd":"/r","sessionId":"m1","timestamp":"2026-05-20T10:02:00.000Z","message":{"role":"user","content":"second request please"}}',
        '{"type":"assistant","sessionId":"m1","timestamp":"2026-05-20T10:03:00.000Z","message":{"role":"assistant","content":"done"}}',
    ]) + "\n")
    indexer.index_one(str(jp), db_path=tmp_path / "h.db", force=True,
                      model="claude-sonnet-test")
    assert seen["model"] == "claude-sonnet-test"
```

- [ ] **Step 2: Run the test — verify it fails**

Run: `uv run pytest -q history/tests/test_indexer.py -k model_override`
Expected: FAIL — `index_one` has no `model` parameter (`TypeError`).

- [ ] **Step 3: Thread `model` through `index_one`**

In `history/indexer.py`, change the `index_one` signature:

```python
def index_one(jsonl_path: str, *, db_path: Path | str | None = None,
              force: bool = False, model: str | None = None) -> dict[str, Any]:
```

Inside `index_one`, the line that resolves the model currently is:

```python
        target_model = get_meta(conn, "haiku_model") or "claude-haiku-4-5"
```

Replace it with:

```python
        target_model = model or get_meta(conn, "haiku_model") or "claude-haiku-4-5"
```

- [ ] **Step 4: Thread `model` through `backfill`**

In `history/backfill.py`, change the `backfill` signature to add `model`:

```python
def backfill(*, projects_dir: Path | None = None,
             db_path: Path | str | None = None,
             workers: int = 2,
             since: int | None = None,
             model: str | None = None) -> dict:
```

In `backfill`, the `pool.submit` line currently is:

```python
        futures = {pool.submit(index_one, str(p), db_path=db_path): p for p in paths}
```

Replace it with:

```python
        futures = {pool.submit(index_one, str(p), db_path=db_path, model=model): p
                   for p in paths}
```

- [ ] **Step 5: Add `--model` to the `backfill` and `resummarize` verbs**

In `history/cli.py`, `_cmd_backfill`: add an argument and pass it through.
After the `parser.add_argument("--dry-run", ...)` line add:

```python
    parser.add_argument("--model", default=None,
                        help="override the summarizer model for this run")
```

In the same function, the `kwargs` dict currently is:

```python
    kwargs = {"workers": args.workers, "db_path": args.db_path, "since": since_ts}
```

Replace it with:

```python
    kwargs = {"workers": args.workers, "db_path": args.db_path,
              "since": since_ts, "model": args.model}
```

In `_cmd_resummarize`: add the same argument after `parser.add_argument("--workers", ...)`:

```python
    parser.add_argument("--model", default=None,
                        help="override the summarizer model for this run")
```

In `_cmd_resummarize`, the `pool.submit` line currently is:

```python
        futures = [pool.submit(index_one, p, db_path=args.db_path) for p in paths]
```

Replace it with:

```python
        futures = [pool.submit(index_one, p, db_path=args.db_path, model=args.model)
                   for p in paths]
```

- [ ] **Step 6: Run the tests — verify they pass**

Run: `uv run pytest -q history/tests/`
Expected: PASS — the full `history` suite, including `test_cli.py` and `test_backfill.py`.

- [ ] **Step 7: Commit**

```bash
git add history/indexer.py history/backfill.py history/cli.py history/tests/test_indexer.py
git commit -m "history: per-run --model override for backfill and resummarize"
```

---

## Task 6: Install the SessionEnd hook

**Files:**
- Modify: `~/.claude/settings.json` (user config — not a repo file)

`history/cli.py` already `load_dotenv`s the periscope repo `.env`, so the
hook self-sources `ANTHROPIC_API_KEY` as long as it runs from the repo via
`uv run`. No code change is needed — only the settings entry.

- [ ] **Step 1: Inspect the current SessionEnd hook**

Run: `python3 -c "import json; print(json.dumps(json.load(open('/Users/tom/.claude/settings.json'))['hooks']['SessionEnd'], indent=2))"`
Expected: one entry — a `{matcher, hooks:[...]}` group whose single hook runs `index-conversation.py`.

- [ ] **Step 2: Add the `history` hook to that group**

Edit `~/.claude/settings.json`. Into the **existing** SessionEnd group's
`hooks` array (alongside the `index-conversation.py` entry — do not remove
it), add a second hook object:

```json
{
  "type": "command",
  "command": "cd /Users/tom/dev/periscope && uv run python -m history hook",
  "timeout": 60
}
```

The resulting `SessionEnd` is one `{matcher:"", hooks:[...]}` group with
two command hooks. Keep the file valid JSON.

- [ ] **Step 3: Verify the hook runs and indexes**

Pick a real, finished transcript and feed it the way Claude Code would:

```bash
TP=$(ls -t ~/.claude/projects/*/*.jsonl | sed -n '2p')
echo "{\"transcript_path\": \"$TP\"}" | (cd /Users/tom/dev/periscope && uv run python -m history hook)
echo "exit: $?"
```

Expected: exit `0`. Then confirm it landed (or was correctly skipped):

```bash
cd /Users/tom/dev/periscope && uv run python -m history stats
```

Expected: `stats` runs; if `$TP` was a real session its row is present;
if it was a `/usage` scrape it is correctly absent (skipped-scrape).

- [ ] **Step 4: Verify the JSON is well-formed**

Run: `python3 -c "import json; json.load(open('/Users/tom/.claude/settings.json')); print('settings.json OK')"`
Expected: `settings.json OK`.

(No commit — `~/.claude/settings.json` is not in the repo.)

---

## Task 7: Run the classification — the credit burn

**Files:** none — this task runs commands. It spends the credits, so it is
deliberately gated. Run from `/Users/tom/dev/periscope`.

- [ ] **Step 1: Resolve the Sonnet model id**

The codebase's Haiku id is `claude-haiku-4-5`. Determine the **current
Sonnet model id** (use the `claude-api` skill, or the Anthropic model
docs). Call it `<SONNET>` below. Do not guess — a wrong id fails every call.

- [ ] **Step 2: One-time cleanup of existing scrape rows**

The pre-existing 359 rows may include scrape sessions indexed before the
Task 3 filter existed. Remove them (the `sessions_fts_after_delete`
trigger cleans the FTS side):

```bash
sqlite3 ~/.claude/history.db "DELETE FROM sessions WHERE asst_msg_count = 0;"
sqlite3 ~/.claude/history.db "SELECT COUNT(*) FROM sessions;"
```

Note the remaining count.

- [ ] **Step 3: Cost dry-run**

Index + classify a small slice on Sonnet and read the actual spend before
committing to the full run. Pick a `--since` ~3-day window (≈50-80 real
sessions):

```bash
uv run python -m history backfill --workers 5 --model <SONNET> --since 2026-05-18
uv run python -m history stats
```

Check the Anthropic console for the spend of that run, divide by the
number of `summarized` sessions it reported, multiply by ~280. **Gate:**
if the projection exceeds ~$18, switch `<SONNET>` to `claude-haiku-4-5`
for the full run (Haiku is ~⅓ the cost and adequate for label
classification).

- [ ] **Step 4: Full backfill — index + classify every real session**

```bash
uv run python -m history backfill --workers 5 --model <SONNET>
```

This indexes every transcript not already current and classifies the
non-trivial ones; scrape transcripts are skipped by the Task 3 filter.
Expect ~13-20 min. Inspect the printed `statuses` — `summarized` should be
in the low hundreds, `skipped-scrape` ~1300.

- [ ] **Step 5: Re-classify the rows that were already indexed**

`backfill` reuses an existing valid summary (cache hit) and so will not
add facets to the original ~359 rows. Force them:

```bash
uv run python -m history resummarize --all --workers 5 --model <SONNET>
```

- [ ] **Step 6: Verify the result**

```bash
uv run python -m history stats
sqlite3 ~/.claude/history.db "SELECT COUNT(*) FROM sessions WHERE outcome IS NOT NULL;"
sqlite3 ~/.claude/history.db "SELECT outcome, COUNT(*) FROM sessions GROUP BY outcome;"
sqlite3 ~/.claude/history.db "SELECT category, COUNT(*) FROM sessions GROUP BY category;"
```

Expected: most non-trivial sessions have a non-NULL `outcome`; the
outcome/category distributions look sane (not all one value). Spot-check
3-4 rows: `sqlite3 ~/.claude/history.db "SELECT session_id, outcome, category, notable, topics FROM sessions WHERE outcome IS NOT NULL LIMIT 4;"`.

After this step the credits are spent and `history.db` is complete,
faceted, and kept fresh by the Task 6 hook. **Phase 1 is done.**

---

## Self-Review

**Spec coverage:**
- Schema — four columns + migration → Task 1.
- Classifier extension (tool, prompt, `SummaryResult`, parsing) → Task 2.
- `/usage` scrape filter (`asst_msg_count == 0`) → Task 3; one-time cleanup of existing junk → Task 7 Step 2.
- Persist facets; `NULL` for unclassified (trivial/failed) → Task 4.
- Sonnet for the burn without raising the ongoing hook's cost → Task 5 (`--model` override) + Task 7.
- Install the hook → Task 6.
- Dry-run cost gate + full run → Task 7.
- Ongoing cost ≈ $0 (hook reuses the existing call) → Task 6 + the `--model` default leaving the hook on the meta `haiku_model`.

**Out of scope (Phase 2, separate plan):** the `/history` filter UI and the search-API facet params — `history/search.py`, `periscope/routes/history.py`, `static/history.*`. The spec's testing bullet for `search` belongs to that plan.

**Placeholder scan:** none — every code step shows the exact code. Two
intentional run-time resolutions: `<SONNET>` (Task 7 Step 1) and the
fixture-shape fallback notes in Tasks 3/4 (use the existing test helpers
if the inline JSONL does not match this codebase's parser).

**Type consistency:** `SummaryResult` gains `outcome: str|None`,
`category: str|None`, `notable: bool`, `topics: list[str]` in Task 2 and
is consumed with those exact names/types in Task 4. `_upsert`'s new
params — `outcome`, `category`, `notable` (int 0/1), `topics_json` (str) —
are used identically across Task 4's call sites. `index_one(model=...)`
(Task 5) matches `call_summarizer(..., model=...)` and `backfill(model=...)`.

---

## Execution Handoff

Phase 1 plan complete. Phase 2 (the filter UI) is a separate, non-deadline
plan to write once this lands. Two execution options for Phase 1:

1. **Subagent-Driven (recommended)** — a fresh subagent per task, two-stage review between tasks.
2. **Inline Execution** — execute here via executing-plans, batched with checkpoints.

Which approach?

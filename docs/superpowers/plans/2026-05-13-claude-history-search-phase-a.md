# Claude History Search — Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `history/` subpackage that indexes every Claude Code JSONL transcript into a searchable SQLite DB with Haiku-generated summaries, plus a `python -m history` CLI. Dogfoodable end-to-end without any periscope-side changes.

**Architecture:** Self-contained Python package at `periscope/history/`. JSONL files in `~/.claude/projects/` are the source of truth; SQLite at `~/.claude/history.db` is a rebuildable derived index. One Haiku call per session at index time (forced tool-use, prompt-cached); rerank at search time is opt-in. Two version counters (`schema`, `mechanical`) plus per-row `summary_input_hash` + `summary_model` drive cache-aware re-indexing.

**Tech Stack:** Python 3.11+, SQLite (stdlib + FTS5), Anthropic Python SDK, `pytest` + `pytest-mock` for tests. No async — `concurrent.futures.ThreadPoolExecutor` for backfill concurrency. No new top-level deps beyond Anthropic (already pinned in periscope).

**Scope boundary:**
- Phase 0 (prelude): migrate `auto_rename_*` in `server.py` to forced tool-use. ~½ day. Punch-list at top of this plan.
- **Phase A (this plan): everything else listed below.**
- Phase B (separate plan after A dogfoods): `/api/history/*` routes, `/history` web page, resume flow.

**Reference docs:**
- Design spec: `docs/superpowers/specs/2026-05-13-claude-history-search-design.md`
- Periscope conventions: `CLAUDE.md`

---

## File structure

New files in this plan:

```
periscope/
├── pyproject.toml                          # NEW — pytest config + dev deps
├── history/
│   ├── __init__.py                         # exposes top-level API
│   ├── __main__.py                         # `python -m history` entry
│   ├── schema.sql                          # DB shape
│   ├── db.py                               # conn mgmt, init, lock
│   ├── jsonl.py                            # stream-parse JSONL → events
│   ├── extract.py                          # events → mechanical record
│   ├── summarize.py                        # Haiku call (forced tool-use)
│   ├── indexer.py                          # one-session pipeline
│   ├── backfill.py                         # multi-session orchestration
│   ├── search.py                           # FTS5 + optional rerank
│   ├── hook.py                             # SessionEnd entry (stdin/JSON)
│   ├── cli.py                              # verb dispatch
│   ├── README.md                           # user-facing docs
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py                     # shared fixtures
│       ├── test_jsonl.py
│       ├── test_extract.py
│       ├── test_summarize.py
│       ├── test_indexer.py
│       ├── test_backfill.py
│       ├── test_search.py
│       ├── test_hook.py
│       ├── test_cli.py
│       └── fixtures/
│           ├── short_session.jsonl
│           ├── normal_session.jsonl
│           ├── interrupted_session.jsonl
│           └── corrupted_session.jsonl
```

Phase 0 modifies (no new files): `periscope/server.py` — specifically the
functions `claude_complete`, `build_rename_prompt`, `auto_rename_session`,
and `auto_rename_window` (cluster near the bottom of the file).

---

## Phase 0 prelude — `auto_rename_*` migration to forced tool-use

Quick punch-list. Periscope has no test suite by convention (`CLAUDE.md`: "iterate against the live dashboard"), so this is verified by clicking the ✨ button in the running app, not pytest.

- [ ] **0.1: Read the existing implementation.** Open `server.py`, locate `claude_complete()`, `build_rename_prompt()`, `auto_rename_session()`, `auto_rename_window()`. Confirm the current pattern: free-form text response → strip code fences → `json.loads()` → `{index_str: name}` dict.

- [ ] **0.2: Add a `claude_tool_call` helper next to `claude_complete`.** Insert after `claude_complete` definition:

```python
def claude_tool_call(
    prompt: str,
    tool: dict,
    *,
    system: str | None = None,
    model: str = "claude-haiku-4-5",
    max_tokens: int = 1024,
) -> dict:
    """Single-shot forced tool-use. Returns the tool's `input` dict, schema-validated by the API.
    `tool` is a full tool definition (name + description + input_schema)."""
    client = get_anthropic()
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": tool["name"]},
        "messages": [{"role": "user", "content": prompt}],
    }
    if system is not None:
        kwargs["system"] = system
    msg = client.messages.create(**kwargs)
    for block in msg.content:
        if block.type == "tool_use" and block.name == tool["name"]:
            return block.input  # already a dict
    raise RuntimeError(f"no tool_use block for {tool['name']!r} in response")
```

- [ ] **0.3: Define `RENAME_TOOL` near the top of the auto-rename section** (just above `build_rename_prompt`):

```python
RENAME_TOOL = {
    "name": "rename_windows",
    "description": "Rename one or more tmux windows with concise, concept-focused labels.",
    "input_schema": {
        "type": "object",
        "properties": {
            "renames": {
                "type": "array",
                "description": "One entry per window that should be renamed. Omit windows whose existing name is already accurate.",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "tmux window index"},
                        "name":  {"type": "string",  "description": "new window name, 1-3 lowercase-dashed words, max 25 chars"},
                    },
                    "required": ["index", "name"],
                },
            },
        },
        "required": ["renames"],
    },
}
```

- [ ] **0.4: Drop the JSON-shape instructions from `build_rename_prompt`.** Replace the final paragraph (the `'Return ONLY a JSON object...'` `lines.append` near the end) with:

```python
    lines.append("")
    lines.append("Call rename_windows with the windows that should change. Omit windows whose existing name is already accurate.")
```

- [ ] **0.5: Migrate `auto_rename_session`.** Replace the block from `prompt = build_rename_prompt(context)` through the `json.loads(cleaned)` `JSONDecodeError` handler with:

```python
    prompt = build_rename_prompt(context)
    try:
        result = claude_tool_call(prompt, RENAME_TOOL)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    applied = []
    for entry in result.get("renames", []):
        index = entry.get("index")
        new_name = (entry.get("name") or "").strip()
        if not isinstance(index, int) or not new_name:
            continue
        old = next((w["name"] for w in target_windows if w["index"] == index), None)
        if old is None or new_name == old:
            continue
        target = f"{session}:{index}"
        tmux("rename-window", "-t", target, new_name)
        applied.append({"index": index, "old": old, "new": new_name})

    return {"ok": True, "applied": applied, "session": session}
```

- [ ] **0.6: Migrate `auto_rename_window` symmetrically.** Replace its `claude_complete` + parse block with:

```python
    prompt = build_rename_prompt(ctx)
    try:
        result = claude_tool_call(prompt, RENAME_TOOL)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    new_name = ""
    for entry in result.get("renames", []):
        if entry.get("index") == index:
            new_name = (entry.get("name") or "").strip()
            break
    if not new_name:
        return {"ok": False, "error": "claude returned empty name"}
    if new_name == current_name:
        return {"ok": True, "applied": False, "old": current_name, "new": current_name}
    tmux("rename-window", "-t", target, new_name)
    return {"ok": True, "applied": True, "old": current_name, "new": new_name}
```

- [ ] **0.7: Verify in the live dashboard.** Run `uv run server.py`, open `http://127.0.0.1:8765/`, click the ✨ button on a session header. Observe that windows get renamed and the response in DevTools Network panel has `applied: [...]`. Repeat for a single window. If anything errors, the stack trace appears in the uvicorn console — common failure mode is the SDK version not supporting `tool_choice={"type": "tool", "name": ...}` (then `pip install -U anthropic` in periscope's env).

- [ ] **0.8: Commit.**

```bash
git add server.py
git commit -m "auto_rename: forced tool-use replaces free-form JSON + fence-stripping"
```

---

## Phase A — `history/` indexer + CLI

### Task 1: Bootstrap the package + test infra

**Files:**
- Create: `pyproject.toml`
- Create: `history/__init__.py`
- Create: `history/__main__.py`
- Create: `history/tests/__init__.py`
- Create: `history/tests/conftest.py`
- Create: `history/tests/test_smoke.py`

- [ ] **Step 1: Create `pyproject.toml`** at the periscope repo root. (Doesn't conflict with `server.py`'s PEP-723 inline metadata — uv resolves both.)

```toml
[project]
name = "periscope"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "anthropic",
    "fastapi",
    "uvicorn[standard]",
    "python-dotenv",
]

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-mock>=3",
]

[tool.pytest.ini_options]
testpaths = ["history/tests"]
python_files = ["test_*.py"]
addopts = "-ra --strict-markers --strict-config"
```

- [ ] **Step 2: Create `history/__init__.py`** — package marker; expose the top-level surface lazily to avoid import-time side effects:

```python
"""Claude Code conversation history indexer + search."""

__all__ = ["index_one", "search"]


def index_one(jsonl_path: str) -> dict:
    """Index (or re-index) one session. Returns a status dict."""
    from .indexer import index_one as _impl
    return _impl(jsonl_path)


def search(query: str, **kwargs) -> list[dict]:
    """FTS5 search across indexed sessions. See history.search.search for kwargs."""
    from .search import search as _impl
    return _impl(query, **kwargs)
```

- [ ] **Step 3: Create `history/__main__.py`** — dispatch to the CLI:

```python
"""Entry point for `python -m history <verb> [args]`."""
from .cli import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Create `history/cli.py` stub** so `__main__.py` imports cleanly:

```python
"""CLI verb dispatch. Fleshed out in later tasks."""
import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m history <verb> [args]")
        print("verbs: backfill, hook, search, reindex, resummarize, stats, clean")
        return 2
    verb = argv.pop(0)
    print(f"verb {verb!r} not yet implemented")
    return 1
```

- [ ] **Step 5: Create `history/tests/conftest.py`** with shared fixtures (extended later):

```python
"""Shared pytest fixtures for history tests."""
import os
import sqlite3
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# Old timestamp used to back-date fixture JSONLs so the indexer's live-session
# heuristic (mtime < 60s ago) doesn't skip them. 2023-11-14, comfortably old.
OLD_MTIME = 1700000000


@pytest.fixture
def fixture_dir() -> Path:
    """Path to the JSONL fixtures directory. Back-dates every fixture's
    mtime so the indexer doesn't classify them as live sessions."""
    for p in FIXTURE_DIR.glob("*.jsonl"):
        os.utime(p, (OLD_MTIME, OLD_MTIME))
    return FIXTURE_DIR


@pytest.fixture
def in_memory_db() -> sqlite3.Connection:
    """A fresh in-memory SQLite with the history schema applied."""
    from history.db import apply_schema
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    yield conn
    conn.close()
```

- [ ] **Step 6: Create `history/tests/test_smoke.py`** to validate the test infra:

```python
def test_package_importable():
    import history
    assert hasattr(history, "index_one")
    assert hasattr(history, "search")


def test_cli_help(capsys):
    from history.cli import main
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 2
    assert "verbs:" in captured.out
```

- [ ] **Step 7: Install dev deps and run the smoke test.**

Run: `cd ~/dev/periscope && uv sync --dev && uv run pytest history/tests/test_smoke.py -v`
Expected: 2 passed (the `in_memory_db` fixture will fail when used because `history.db` doesn't exist yet — that's why test_smoke avoids it). The first run on a fresh checkout creates the venv; subsequent runs are fast.

- [ ] **Step 8: Commit.**

```bash
git add pyproject.toml history/
git commit -m "history: package skeleton + pytest config"
```

---

### Task 2: Schema + db.py

**Files:**
- Create: `history/schema.sql`
- Create: `history/db.py`
- Create: `history/tests/test_db.py`

- [ ] **Step 1: Write the failing test** at `history/tests/test_db.py`:

```python
import sqlite3
from history.db import apply_schema, SCHEMA_VERSION, MECHANICAL_VERSION


def test_apply_schema_creates_expected_tables():
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','virtual','view')"
    )}
    assert "sessions" in tables
    assert "sessions_fts" in tables
    assert "meta" in tables


def test_apply_schema_seeds_versions():
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    rows = {k: v for k, v in conn.execute("SELECT key, value FROM meta")}
    assert rows["schema_version"] == str(SCHEMA_VERSION)
    assert rows["mechanical_version"] == str(MECHANICAL_VERSION)
    assert "haiku_model" in rows


def test_apply_schema_idempotent():
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    apply_schema(conn)  # second call must not error or change meta
    rows = {k: v for k, v in conn.execute("SELECT key, value FROM meta")}
    assert rows["schema_version"] == str(SCHEMA_VERSION)


def test_fts_delete_trigger_fires():
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    conn.execute(
        "INSERT INTO sessions(session_id, jsonl_path, project_path, started_at, ended_at, "
        "duration_s, user_msg_count, asst_msg_count, tool_use_count, indexed_at, "
        "mechanical_version, source_mtime, source_size) "
        "VALUES ('s1', '/p.jsonl', '/p', 0, 0, 0, 0, 0, 0, 0, 1, 0, 0)"
    )
    conn.execute("INSERT INTO sessions_fts(session_id, summary) VALUES ('s1', 'foo')")
    conn.execute("DELETE FROM sessions WHERE session_id = 's1'")
    assert conn.execute("SELECT COUNT(*) FROM sessions_fts WHERE session_id='s1'").fetchone()[0] == 0
```

- [ ] **Step 2: Run the test to confirm it fails.**

Run: `uv run pytest history/tests/test_db.py -v`
Expected: ImportError on `history.db`.

- [ ] **Step 3: Write `history/schema.sql`:**

```sql
-- Two version counters; bump in db.py constants to trigger reindex.
-- - schema_version: tables/columns change. Migration may run.
-- - mechanical_version: extraction logic change. Re-extract rows; reuse summary if input-hash matches.
-- Re-summarization is driven by summary_input_hash + summary_model, not a version counter.

CREATE TABLE IF NOT EXISTS sessions (
  session_id           TEXT PRIMARY KEY,
  jsonl_path           TEXT NOT NULL UNIQUE,
  project_path         TEXT NOT NULL,
  branch               TEXT,

  started_at           INTEGER NOT NULL,
  ended_at             INTEGER NOT NULL,
  duration_s           INTEGER NOT NULL,

  user_msg_count       INTEGER NOT NULL,
  asst_msg_count       INTEGER NOT NULL,
  tool_use_count       INTEGER NOT NULL,
  was_interrupted      INTEGER NOT NULL DEFAULT 0,
  ended_cleanly        INTEGER NOT NULL DEFAULT 0,

  summary              TEXT,
  tags                 TEXT,
  summary_input_hash   TEXT,
  summary_model        TEXT,

  first_user_msg       TEXT,
  last_user_msg        TEXT,
  final_assistant_msg  TEXT,
  files_touched        TEXT,
  notable_cmds         TEXT,
  tool_use_counts      TEXT,

  indexed_at           INTEGER NOT NULL,
  mechanical_version   INTEGER NOT NULL,
  source_mtime         INTEGER NOT NULL,
  source_size          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_path);
CREATE INDEX IF NOT EXISTS idx_sessions_branch  ON sessions(branch);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
  session_id     UNINDEXED,
  summary,
  tags,
  first_user_msg,
  last_user_msg,
  final_assistant_msg,
  user_messages,
  assistant_text,
  files_touched,
  notable_cmds,
  tokenize = "porter unicode61"
);

-- Indexer is the sole writer. UPSERTs to `sessions` are paired with an
-- explicit DELETE + INSERT to `sessions_fts` inside the same transaction.
-- This trigger is a safety net for raw DELETEs (e.g. `clean` verb) so FTS
-- rows don't leak. Don't remove the explicit DELETE in the indexer thinking
-- the trigger covers it — FTS5 has no uniqueness constraint.
CREATE TRIGGER IF NOT EXISTS sessions_fts_after_delete
AFTER DELETE ON sessions BEGIN
  DELETE FROM sessions_fts WHERE session_id = old.session_id;
END;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
```

- [ ] **Step 4: Write `history/db.py`:**

```python
"""SQLite connection management + schema migration."""
import os
import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 1
MECHANICAL_VERSION = 1
DEFAULT_HAIKU_MODEL = "claude-haiku-4-5"

DEFAULT_DB_PATH = Path(os.environ.get("CLAUDE_HISTORY_DB") or
                       Path.home() / ".claude" / "history.db")

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()
_LOCK = threading.Lock()


def apply_schema(conn: sqlite3.Connection) -> None:
    """Run the schema DDL and seed meta keys. Idempotent."""
    conn.executescript(_SCHEMA_SQL)
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
    conn.commit()


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open the history DB in WAL mode with the schema applied."""
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    apply_schema(conn)
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
```

- [ ] **Step 5: Run the tests; verify they pass.**

Run: `uv run pytest history/tests/test_db.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit.**

```bash
git add history/schema.sql history/db.py history/tests/test_db.py
git commit -m "history: schema + db helpers (WAL, idempotent apply_schema, FTS delete trigger)"
```

---

### Task 3: JSONL parsing — event classification

**Files:**
- Create: `history/jsonl.py`
- Create: `history/tests/test_jsonl.py`
- Create: `history/tests/fixtures/short_session.jsonl`
- Create: `history/tests/fixtures/corrupted_session.jsonl`

- [ ] **Step 1: Create `history/tests/fixtures/short_session.jsonl`** — minimal realistic JSONL (one user message, one assistant reply with a tool_use block, one tool_result, one final assistant text reply). Each line is a single-line JSON object:

```jsonl
{"type":"permission-mode","permissionMode":"default","sessionId":"abc-001"}
{"type":"user","sessionId":"abc-001","cwd":"/Users/tom/dev/foo","gitBranch":"main","timestamp":"2026-04-13T10:00:00.000Z","uuid":"u-1","parentUuid":null,"message":{"role":"user","content":[{"type":"text","text":"hi, run ls"}]}}
{"type":"assistant","sessionId":"abc-001","cwd":"/Users/tom/dev/foo","gitBranch":"main","timestamp":"2026-04-13T10:00:05.000Z","uuid":"a-1","parentUuid":"u-1","message":{"role":"assistant","content":[{"type":"text","text":"on it"},{"type":"tool_use","id":"tu-1","name":"Bash","input":{"command":"ls -la /tmp/foo","description":"List foo"}}]}}
{"type":"user","sessionId":"abc-001","cwd":"/Users/tom/dev/foo","gitBranch":"main","timestamp":"2026-04-13T10:00:07.000Z","uuid":"u-2","parentUuid":"a-1","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tu-1","content":"total 0\ndrwx 3 tom staff 96 Apr 13 10:00 ."}]}}
{"type":"assistant","sessionId":"abc-001","cwd":"/Users/tom/dev/foo","gitBranch":"main","timestamp":"2026-04-13T10:00:09.000Z","uuid":"a-2","parentUuid":"u-2","message":{"role":"assistant","content":[{"type":"text","text":"directory is empty"}]}}
```

- [ ] **Step 2: Create `history/tests/fixtures/corrupted_session.jsonl`** — three good lines, one truncated, one malformed:

```jsonl
{"type":"permission-mode","permissionMode":"default","sessionId":"abc-002"}
{"type":"user","sessionId":"abc-002","cwd":"/Users/tom/dev/foo","timestamp":"2026-04-13T10:00:00.000Z","uuid":"u-1","parentUuid":null,"message":{"role":"user","content":[{"type":"text","text":"hi"}]}}
{"type":"assistant","sessionId":"abc-002","cwd":"/Users/tom/dev/foo","timestamp":"2026-04-13T10:00:05.000Z","uuid":"a-1","parentUuid":"u-1","message":{"role":"assistant","content":[{"type":"text","text":"hello!  this line is truncated mid-json
not even valid json at all }
{"type":"assistant","sessionId":"abc-002","cwd":"/Users/tom/dev/foo","timestamp":"2026-04-13T10:00:10.000Z","uuid":"a-2","parentUuid":"u-1","message":{"role":"assistant","content":[{"type":"text","text":"recovered"}]}}
```

- [ ] **Step 3: Write `history/tests/test_jsonl.py`:**

```python
from history.jsonl import parse_jsonl, Event


def test_parses_short_session(fixture_dir):
    path = fixture_dir / "short_session.jsonl"
    events = list(parse_jsonl(str(path)))
    types = [e.type for e in events]
    assert types == ["permission-mode", "user", "assistant", "user", "assistant"]


def test_event_carries_metadata(fixture_dir):
    events = list(parse_jsonl(str(fixture_dir / "short_session.jsonl")))
    first_user = next(e for e in events if e.type == "user")
    assert first_user.session_id == "abc-001"
    assert first_user.cwd == "/Users/tom/dev/foo"
    assert first_user.git_branch == "main"
    assert first_user.ts_ms is not None and first_user.ts_ms > 0


def test_user_text_extraction(fixture_dir):
    events = list(parse_jsonl(str(fixture_dir / "short_session.jsonl")))
    user_events = [e for e in events if e.type == "user"]
    # Second user event is a tool_result wrapper, not real user text
    assert user_events[0].user_text == "hi, run ls"
    assert user_events[1].user_text is None
    assert user_events[1].tool_results == [{"tool_use_id": "tu-1", "content": "total 0\ndrwx 3 tom staff 96 Apr 13 10:00 ."}]


def test_assistant_blocks_classified(fixture_dir):
    events = list(parse_jsonl(str(fixture_dir / "short_session.jsonl")))
    assistants = [e for e in events if e.type == "assistant"]
    assert assistants[0].assistant_text == "on it"
    assert assistants[0].tool_uses == [{"id": "tu-1", "name": "Bash", "input": {"command": "ls -la /tmp/foo", "description": "List foo"}}]
    assert assistants[1].assistant_text == "directory is empty"
    assert assistants[1].tool_uses == []


def test_unknown_types_pass_through(fixture_dir, tmp_path):
    p = tmp_path / "novel.jsonl"
    p.write_text('{"type":"future-feature","x":1}\n{"type":"user","sessionId":"s","message":{"role":"user","content":[{"type":"text","text":"hi"}]}}\n')
    events = list(parse_jsonl(str(p)))
    assert [e.type for e in events] == ["future-feature", "user"]


def test_corrupted_file_skips_bad_lines(fixture_dir):
    events = list(parse_jsonl(str(fixture_dir / "corrupted_session.jsonl")))
    # Good lines: permission-mode, user, last assistant ("recovered")
    types = [e.type for e in events]
    assert types == ["permission-mode", "user", "assistant"]
    assert events[-1].assistant_text == "recovered"


def test_session_id_inferred_from_filename(tmp_path):
    p = tmp_path / "abc-999.jsonl"
    p.write_text('{"type":"permission-mode","permissionMode":"default","sessionId":"abc-999"}\n')
    events = list(parse_jsonl(str(p)))
    assert events[0].session_id == "abc-999"


def test_user_text_from_string_content(tmp_path):
    """Many real Claude JSONLs use message.content as a plain string, not a
    list of blocks. Dropping these silently corrupted ~85% of user prompts in
    an early version of the parser — kept as a regression test."""
    p = tmp_path / "str.jsonl"
    p.write_text(
        '{"type":"user","sessionId":"s","cwd":"/p","timestamp":"2026-01-01T00:00:00Z","uuid":"u1","parentUuid":null,"message":{"role":"user","content":"plain string prompt"}}\n'
        '{"type":"assistant","sessionId":"s","cwd":"/p","timestamp":"2026-01-01T00:00:01Z","uuid":"a1","parentUuid":"u1","message":{"role":"assistant","content":"plain string reply"}}\n'
    )
    events = list(parse_jsonl(str(p)))
    user_ev = next(e for e in events if e.type == "user")
    asst_ev = next(e for e in events if e.type == "assistant")
    assert user_ev.user_text == "plain string prompt"
    assert asst_ev.assistant_text == "plain string reply"
```

- [ ] **Step 4: Run tests to confirm they fail.**

Run: `uv run pytest history/tests/test_jsonl.py -v`
Expected: ImportError on `history.jsonl`.

- [ ] **Step 5: Write `history/jsonl.py`:**

```python
"""Stream-parse Claude Code JSONL transcripts into classified Event records."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)


@dataclass
class Event:
    """One JSONL event, normalized. Fields default to None when absent."""
    type: str
    raw: dict
    session_id: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    ts_ms: int | None = None
    uuid: str | None = None
    parent_uuid: str | None = None
    # Populated for user events
    user_text: str | None = None
    tool_results: list[dict] = field(default_factory=list)
    # Populated for assistant events
    assistant_text: str | None = None
    tool_uses: list[dict] = field(default_factory=list)


def _parse_ts(s: str | None) -> int | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _classify(raw: dict) -> Event:
    ev = Event(
        type=raw.get("type", "<missing>"),
        raw=raw,
        session_id=raw.get("sessionId"),
        cwd=raw.get("cwd"),
        git_branch=raw.get("gitBranch"),
        ts_ms=_parse_ts(raw.get("timestamp")),
        uuid=raw.get("uuid"),
        parent_uuid=raw.get("parentUuid"),
    )
    msg = raw.get("message")
    if not isinstance(msg, dict):
        return ev
    role = msg.get("role")
    content = msg.get("content")
    # Claude Code JSONLs put user prompts under message.content either as a
    # plain string OR as a list of content blocks (text/tool_use/tool_result).
    # Real data is overwhelmingly mixed — handle both shapes or we silently
    # drop the majority of human prompts.
    if isinstance(content, str):
        if role == "user":
            ev.user_text = content
        elif role == "assistant":
            ev.assistant_text = content
        return ev
    if not isinstance(content, list):
        return ev
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text")
            if isinstance(t, str):
                texts.append(t)
        elif btype == "tool_use" and role == "assistant":
            ev.tool_uses.append({
                "id": block.get("id"),
                "name": block.get("name"),
                "input": block.get("input") or {},
            })
        elif btype == "tool_result" and role == "user":
            content_val = block.get("content")
            if isinstance(content_val, list):
                # Filter empty/missing-text blocks (image blocks contribute "")
                # so we don't pad with blank lines.
                content_val = "\n".join(
                    t for c in content_val
                    if isinstance(c, dict) and (t := c.get("text"))
                )
            ev.tool_results.append({
                "tool_use_id": block.get("tool_use_id"),
                "content": content_val if isinstance(content_val, str) else "",
            })
    joined = "\n".join(t for t in texts if t.strip())
    if role == "user" and joined:
        ev.user_text = joined
    elif role == "assistant" and joined:
        ev.assistant_text = joined
    return ev


def parse_jsonl(path: str | Path) -> Iterator[Event]:
    """Stream events from a JSONL file. Skips malformed lines and logs them.
    The session_id falls back to the filename stem if no event carries it."""
    p = Path(path)
    fallback_sid = p.stem
    bad_lines = 0
    total_lines = 0
    with p.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            total_lines += 1
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            if not isinstance(raw, dict):
                bad_lines += 1
                continue
            ev = _classify(raw)
            if ev.session_id is None:
                ev.session_id = fallback_sid
            yield ev
    if total_lines and bad_lines / total_lines > 0.5:
        log.warning("history.jsonl: %s — %d/%d lines malformed, results may be incomplete",
                    p, bad_lines, total_lines)
```

- [ ] **Step 6: Run tests to verify they pass.**

Run: `uv run pytest history/tests/test_jsonl.py -v`
Expected: 8 passed.

- [ ] **Step 7: Commit.**

```bash
git add history/jsonl.py history/tests/test_jsonl.py history/tests/fixtures/short_session.jsonl history/tests/fixtures/corrupted_session.jsonl
git commit -m "history: jsonl event parser (tolerant of malformed/unknown events)"
```

---

### Task 4: Field extraction — counts, timestamps, key messages

**Files:**
- Create: `history/extract.py`
- Create: `history/tests/test_extract.py`
- Create: `history/tests/fixtures/normal_session.jsonl`
- Create: `history/tests/fixtures/interrupted_session.jsonl`

- [ ] **Step 1: Create `history/tests/fixtures/normal_session.jsonl`** — ~20 events with 3 user messages, 3 assistant messages, Bash + Edit tool uses, file paths in tool inputs:

```jsonl
{"type":"permission-mode","permissionMode":"default","sessionId":"normal-001"}
{"type":"user","sessionId":"normal-001","cwd":"/Users/tom/dev/foo","gitBranch":"feat/bar","timestamp":"2026-04-13T10:00:00.000Z","uuid":"u-1","parentUuid":null,"message":{"role":"user","content":[{"type":"text","text":"investigate the slow query in resolve_cohort.py"}]}}
{"type":"assistant","sessionId":"normal-001","cwd":"/Users/tom/dev/foo","gitBranch":"feat/bar","timestamp":"2026-04-13T10:00:30.000Z","uuid":"a-1","parentUuid":"u-1","message":{"role":"assistant","content":[{"type":"text","text":"reading the file"},{"type":"tool_use","id":"tu-1","name":"Read","input":{"file_path":"/Users/tom/dev/foo/resolve_cohort.py"}}]}}
{"type":"user","sessionId":"normal-001","cwd":"/Users/tom/dev/foo","gitBranch":"feat/bar","timestamp":"2026-04-13T10:00:31.000Z","uuid":"u-2","parentUuid":"a-1","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tu-1","content":"def resolve_cohort(...): ..."}]}}
{"type":"assistant","sessionId":"normal-001","cwd":"/Users/tom/dev/foo","gitBranch":"feat/bar","timestamp":"2026-04-13T10:01:00.000Z","uuid":"a-2","parentUuid":"u-2","message":{"role":"assistant","content":[{"type":"tool_use","id":"tu-2","name":"Bash","input":{"command":"psql -c 'EXPLAIN ANALYZE SELECT * FROM events WHERE user_id = 1' --quiet","description":"Run EXPLAIN"}}]}}
{"type":"user","sessionId":"normal-001","cwd":"/Users/tom/dev/foo","gitBranch":"feat/bar","timestamp":"2026-04-13T10:01:30.000Z","uuid":"u-3","parentUuid":"a-2","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tu-2","content":"Seq Scan on events  (cost=0..1234)"}]}}
{"type":"user","sessionId":"normal-001","cwd":"/Users/tom/dev/foo","gitBranch":"feat/bar","timestamp":"2026-04-13T10:02:00.000Z","uuid":"u-4","parentUuid":"u-3","message":{"role":"user","content":[{"type":"text","text":"the query plan shows a seq scan — add an index"}]}}
{"type":"assistant","sessionId":"normal-001","cwd":"/Users/tom/dev/foo","gitBranch":"feat/bar","timestamp":"2026-04-13T10:02:30.000Z","uuid":"a-3","parentUuid":"u-4","message":{"role":"assistant","content":[{"type":"text","text":"writing migration"},{"type":"tool_use","id":"tu-3","name":"Write","input":{"file_path":"/Users/tom/dev/foo/migrations/0042.sql","content":"CREATE INDEX..."}}]}}
{"type":"user","sessionId":"normal-001","cwd":"/Users/tom/dev/foo","gitBranch":"feat/bar","timestamp":"2026-04-13T10:02:31.000Z","uuid":"u-5","parentUuid":"a-3","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tu-3","content":"File created."}]}}
{"type":"user","sessionId":"normal-001","cwd":"/Users/tom/dev/foo","gitBranch":"feat/bar","timestamp":"2026-04-13T10:03:00.000Z","uuid":"u-6","parentUuid":"u-5","message":{"role":"user","content":[{"type":"text","text":"now verify with EXPLAIN"}]}}
{"type":"assistant","sessionId":"normal-001","cwd":"/Users/tom/dev/foo","gitBranch":"feat/bar","timestamp":"2026-04-13T10:03:30.000Z","uuid":"a-4","parentUuid":"u-6","message":{"role":"assistant","content":[{"type":"text","text":"index works — P99 down to 200ms. Migration in migrations/0042.sql."}]}}
```

- [ ] **Step 2: Create `history/tests/fixtures/interrupted_session.jsonl`** — has "Request interrupted by user" text in a user message:

```jsonl
{"type":"permission-mode","permissionMode":"default","sessionId":"interrupted-001"}
{"type":"user","sessionId":"interrupted-001","cwd":"/Users/tom/dev/foo","timestamp":"2026-04-13T11:00:00.000Z","uuid":"u-1","parentUuid":null,"message":{"role":"user","content":[{"type":"text","text":"do a big refactor"}]}}
{"type":"assistant","sessionId":"interrupted-001","cwd":"/Users/tom/dev/foo","timestamp":"2026-04-13T11:00:10.000Z","uuid":"a-1","parentUuid":"u-1","message":{"role":"assistant","content":[{"type":"text","text":"starting"},{"type":"tool_use","id":"tu-1","name":"Read","input":{"file_path":"/Users/tom/dev/foo/big.py"}}]}}
{"type":"user","sessionId":"interrupted-001","cwd":"/Users/tom/dev/foo","timestamp":"2026-04-13T11:00:15.000Z","uuid":"u-2","parentUuid":"a-1","message":{"role":"user","content":[{"type":"text","text":"[Request interrupted by user]"}]}}
```

- [ ] **Step 3: Write `history/tests/test_extract.py`:**

```python
import json
from history.extract import extract_record
from history.jsonl import parse_jsonl


def _extract_fixture(fixture_dir, name):
    path = fixture_dir / name
    events = list(parse_jsonl(str(path)))
    return extract_record(str(path), events, source_mtime=1000000, source_size=path.stat().st_size)


def test_counts_and_timestamps(fixture_dir):
    rec = _extract_fixture(fixture_dir, "normal_session.jsonl")
    assert rec.session_id == "normal-001"
    assert rec.project_path == "/Users/tom/dev/foo"
    assert rec.branch == "feat/bar"
    assert rec.user_msg_count == 3   # excludes tool_result wrappers
    assert rec.asst_msg_count == 4
    assert rec.tool_use_count == 3
    assert rec.duration_s == 3 * 60 + 30  # 10:00:00 → 10:03:30
    assert rec.was_interrupted == 0
    assert rec.ended_cleanly == 1  # last event is assistant text


def test_first_last_final_messages(fixture_dir):
    rec = _extract_fixture(fixture_dir, "normal_session.jsonl")
    assert rec.first_user_msg.startswith("investigate the slow query")
    assert rec.last_user_msg.startswith("now verify with EXPLAIN")
    assert rec.final_assistant_msg.startswith("index works")


def test_files_touched_dedup_and_order(fixture_dir):
    rec = _extract_fixture(fixture_dir, "normal_session.jsonl")
    files = json.loads(rec.files_touched)
    # First-touched ordering: resolve_cohort.py, then migrations/0042.sql
    assert files == [
        "/Users/tom/dev/foo/resolve_cohort.py",
        "/Users/tom/dev/foo/migrations/0042.sql",
    ]


def test_notable_cmds_filters_trivial(fixture_dir, tmp_path):
    # Build a session with both notable and trivial Bash commands
    p = tmp_path / "cmds.jsonl"
    p.write_text(
        '{"type":"user","sessionId":"x","cwd":"/p","timestamp":"2026-01-01T00:00:00Z","uuid":"u1","parentUuid":null,"message":{"role":"user","content":[{"type":"text","text":"go"}]}}\n'
        '{"type":"assistant","sessionId":"x","cwd":"/p","timestamp":"2026-01-01T00:00:01Z","uuid":"a1","parentUuid":"u1","message":{"role":"assistant","content":['
        '{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"ls"}},'
        '{"type":"tool_use","id":"t2","name":"Bash","input":{"command":"pwd"}},'
        '{"type":"tool_use","id":"t3","name":"Bash","input":{"command":"pytest tests/foo_test.py -k slow"}},'
        '{"type":"tool_use","id":"t4","name":"Bash","input":{"command":"grep -r needle ./src"}}'
        ']}}\n'
    )
    events = list(parse_jsonl(str(p)))
    rec = extract_record(str(p), events, source_mtime=0, source_size=p.stat().st_size)
    cmds = json.loads(rec.notable_cmds)
    assert "ls" not in cmds
    assert "pwd" not in cmds
    assert "pytest tests/foo_test.py -k slow" in cmds
    assert "grep -r needle ./src" in cmds


def test_tool_use_counts(fixture_dir):
    rec = _extract_fixture(fixture_dir, "normal_session.jsonl")
    counts = json.loads(rec.tool_use_counts)
    assert counts == {"Read": 1, "Bash": 1, "Write": 1}


def test_interrupted_detection(fixture_dir):
    rec = _extract_fixture(fixture_dir, "interrupted_session.jsonl")
    assert rec.was_interrupted == 1
    assert rec.ended_cleanly == 0  # last event is a user "Request interrupted" message


def test_short_session(fixture_dir):
    rec = _extract_fixture(fixture_dir, "short_session.jsonl")
    assert rec.user_msg_count == 1   # only one real user message; the tool_result is excluded
    assert rec.asst_msg_count == 2
    assert rec.tool_use_count == 1
```

- [ ] **Step 4: Run tests; confirm they fail.**

Run: `uv run pytest history/tests/test_extract.py -v`
Expected: ImportError on `history.extract`.

- [ ] **Step 5: Write `history/extract.py`:**

```python
"""Aggregate parsed JSONL events into a SessionRecord ready for DB upsert."""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .jsonl import Event

# Notable-command filter: keep commands that are non-trivial.
_TRIVIAL_CMDS = {"ls", "pwd", "cat", "echo", "cd", "clear", "exit", "whoami", "date"}
_TRIVIAL_CMD_LENGTH = 20
_NON_TRIVIAL_CHARS = {"|", ">", "<", "/"}

# Path-bearing tool input keys we recognize as file touches.
_FILE_KEYS = ("file_path", "path", "notebook_path", "old_path", "new_path")

# Truncation lengths.
MAX_FIRST_LAST_USER = 500
MAX_FINAL_ASSISTANT = 1000


@dataclass
class SessionRecord:
    """One row's worth of mechanical fields. Haiku fields filled later."""
    session_id: str
    jsonl_path: str
    project_path: str
    branch: str | None
    started_at: int
    ended_at: int
    duration_s: int
    user_msg_count: int
    asst_msg_count: int
    tool_use_count: int
    was_interrupted: int
    ended_cleanly: int

    first_user_msg: str | None
    last_user_msg: str | None
    final_assistant_msg: str | None
    files_touched: str           # JSON array string
    notable_cmds: str            # JSON array string
    tool_use_counts: str         # JSON dict string
    user_messages_blob: str = ""     # joined for FTS + summary input
    assistant_text_blob: str = ""    # joined for FTS

    source_mtime: int = 0
    source_size: int = 0


def _decode_project_path(jsonl_path: str, events: list[Event]) -> str:
    """Project path = first cwd seen in events.

    Falls back to the encoded directory name unchanged when no event carries a
    cwd. Claude Code's encoding (`/Users/tom/dev/foo` → `-Users-tom-dev-foo`,
    plus `--` for dot-prefixed dirs like `~/.claude`) is ambiguous to invert
    (a dash-in-path-segment vs a dot-marker are indistinguishable), and real
    transcripts always carry cwd somewhere — this branch is a near-zero path."""
    for ev in events:
        if ev.cwd:
            return ev.cwd
    import logging
    logging.getLogger(__name__).warning(
        "extract: no cwd in any event for %s — using encoded dir name as-is", jsonl_path,
    )
    return Path(jsonl_path).parent.name


def _is_notable_cmd(cmd: str) -> bool:
    if not cmd:
        return False
    stripped = cmd.strip()
    first_word = stripped.split(None, 1)[0] if stripped else ""
    if first_word in _TRIVIAL_CMDS:
        return False
    if " " not in stripped and len(stripped) < _TRIVIAL_CMD_LENGTH:
        return False
    if len(stripped) >= _TRIVIAL_CMD_LENGTH:
        return True
    return any(c in stripped for c in _NON_TRIVIAL_CHARS)


def extract_record(jsonl_path: str, events: list[Event], *,
                   source_mtime: int, source_size: int) -> SessionRecord:
    """Walk events once; emit a SessionRecord."""
    session_id: str | None = None
    branch: str | None = None
    timestamps: list[int] = []

    user_msg_count = 0
    asst_msg_count = 0
    tool_use_count = 0
    was_interrupted = False

    first_user_msg: str | None = None
    last_user_msg: str | None = None
    final_assistant_msg: str | None = None

    files_seen: list[str] = []
    files_set: set[str] = set()
    notable_cmds: list[str] = []
    notable_cmds_set: set[str] = set()
    tool_counts: Counter[str] = Counter()

    user_chunks: list[str] = []
    assistant_chunks: list[str] = []

    last_event_is_assistant_text = False

    for ev in events:
        if ev.session_id and not session_id:
            session_id = ev.session_id
        if ev.git_branch:
            branch = ev.git_branch
        if ev.ts_ms is not None:
            timestamps.append(ev.ts_ms)

        if ev.type == "user":
            if ev.user_text:
                user_msg_count += 1
                user_chunks.append(ev.user_text)
                if "[Request interrupted by user]" in ev.user_text or \
                   "Request interrupted by user" in ev.user_text:
                    was_interrupted = True
                if first_user_msg is None:
                    first_user_msg = ev.user_text[:MAX_FIRST_LAST_USER]
                last_user_msg = ev.user_text[:MAX_FIRST_LAST_USER]
                last_event_is_assistant_text = False
            # tool_result wrappers don't count as user messages

        elif ev.type == "assistant":
            asst_msg_count += 1
            if ev.assistant_text:
                assistant_chunks.append(ev.assistant_text)
                final_assistant_msg = ev.assistant_text[:MAX_FINAL_ASSISTANT]
                last_event_is_assistant_text = True
            else:
                last_event_is_assistant_text = False
            for tu in ev.tool_uses:
                tool_use_count += 1
                name = tu.get("name") or "?"
                tool_counts[name] += 1
                inp = tu.get("input") or {}
                # File touches
                for key in _FILE_KEYS:
                    fp = inp.get(key)
                    if isinstance(fp, str) and fp and fp not in files_set:
                        files_set.add(fp)
                        files_seen.append(fp)
                # Notable commands
                if name == "Bash":
                    cmd = inp.get("command")
                    if isinstance(cmd, str) and _is_notable_cmd(cmd):
                        if cmd not in notable_cmds_set:
                            notable_cmds_set.add(cmd)
                            notable_cmds.append(cmd)

    started_at = (min(timestamps) // 1000) if timestamps else 0
    ended_at = (max(timestamps) // 1000) if timestamps else 0
    duration_s = max(0, ended_at - started_at)

    project_path = _decode_project_path(jsonl_path, events)

    return SessionRecord(
        session_id=session_id or Path(jsonl_path).stem,
        jsonl_path=jsonl_path,
        project_path=project_path,
        branch=branch,
        started_at=started_at,
        ended_at=ended_at,
        duration_s=duration_s,
        user_msg_count=user_msg_count,
        asst_msg_count=asst_msg_count,
        tool_use_count=tool_use_count,
        was_interrupted=1 if was_interrupted else 0,
        ended_cleanly=1 if last_event_is_assistant_text else 0,
        first_user_msg=first_user_msg,
        last_user_msg=last_user_msg,
        final_assistant_msg=final_assistant_msg,
        files_touched=json.dumps(files_seen),
        notable_cmds=json.dumps(notable_cmds),
        tool_use_counts=json.dumps(dict(tool_counts)),
        user_messages_blob="\n".join(user_chunks),
        assistant_text_blob="\n".join(assistant_chunks),
        source_mtime=source_mtime,
        source_size=source_size,
    )
```

- [ ] **Step 6: Run tests to verify they pass.**

Run: `uv run pytest history/tests/test_extract.py -v`
Expected: 7 passed.

- [ ] **Step 7: Commit.**

```bash
git add history/extract.py history/tests/test_extract.py history/tests/fixtures/normal_session.jsonl history/tests/fixtures/interrupted_session.jsonl
git commit -m "history: extract mechanical fields from JSONL events"
```

---

### Task 5: Summary input hash + triviality filter

**Files:**
- Modify: `history/extract.py` (add `compute_summary_input_hash`, `is_trivial`)
- Modify: `history/tests/test_extract.py`

- [ ] **Step 1: Add the failing tests to `history/tests/test_extract.py`:**

```python
from history.extract import compute_summary_input_hash, is_trivial, heuristic_summary


def test_summary_input_hash_stable_for_same_inputs(fixture_dir):
    rec = _extract_fixture(fixture_dir, "normal_session.jsonl")
    h1 = compute_summary_input_hash(rec)
    h2 = compute_summary_input_hash(rec)
    assert h1 == h2
    assert isinstance(h1, str) and len(h1) == 64  # sha256 hex


def test_summary_input_hash_changes_with_user_msgs(fixture_dir):
    rec = _extract_fixture(fixture_dir, "normal_session.jsonl")
    h1 = compute_summary_input_hash(rec)
    rec.user_messages_blob = rec.user_messages_blob + "\nextra user message"
    h2 = compute_summary_input_hash(rec)
    assert h1 != h2


def test_summary_input_hash_ignores_counts(fixture_dir):
    rec = _extract_fixture(fixture_dir, "normal_session.jsonl")
    h1 = compute_summary_input_hash(rec)
    rec.tool_use_count = rec.tool_use_count + 99
    rec.duration_s = rec.duration_s + 9999
    h2 = compute_summary_input_hash(rec)
    assert h1 == h2  # only summary-input-relevant fields contribute


def test_trivial_session_short_duration(fixture_dir):
    rec = _extract_fixture(fixture_dir, "short_session.jsonl")
    # short_session.jsonl: 1 user msg, ~9s duration → trivial
    assert is_trivial(rec) is True


def test_trivial_session_low_msg_count(fixture_dir):
    rec = _extract_fixture(fixture_dir, "normal_session.jsonl")
    # normal_session: 3 user msgs, 3.5 min duration → not trivial
    assert is_trivial(rec) is False


def test_heuristic_summary_mentions_msg_count(fixture_dir):
    rec = _extract_fixture(fixture_dir, "short_session.jsonl")
    text = heuristic_summary(rec)
    assert "1 messages" in text or "1 message" in text
    # Should include first user message snippet
    assert "hi, run ls" in text
```

- [ ] **Step 2: Run tests; confirm they fail.**

Run: `uv run pytest history/tests/test_extract.py -k 'hash or trivial or heuristic' -v`
Expected: ImportError on the new symbols.

- [ ] **Step 3: Extend `history/extract.py`** by appending:

```python
import hashlib

# Triviality thresholds.
TRIVIAL_USER_MSG_THRESHOLD = 2
TRIVIAL_DURATION_S = 60


def compute_summary_input_hash(rec: SessionRecord) -> str:
    """SHA256 over a canonical representation of the fields that drive the
    Haiku summary. If any of these change, the summary should be re-derived;
    if none change, we can reuse a stored summary."""
    notable_cmds_first_20 = json.loads(rec.notable_cmds)[:20]
    canonical = json.dumps([
        rec.first_user_msg or "",
        rec.user_messages_blob,
        rec.final_assistant_msg or "",
        rec.files_touched,
        rec.branch or "",
        notable_cmds_first_20,
    ], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_trivial(rec: SessionRecord) -> bool:
    """Trivial sessions skip the Haiku call and get a heuristic summary."""
    return (rec.user_msg_count < TRIVIAL_USER_MSG_THRESHOLD or
            rec.duration_s < TRIVIAL_DURATION_S)


def heuristic_summary(rec: SessionRecord) -> str:
    """Concrete placeholder for trivial sessions; surfaces in search results."""
    head = (rec.first_user_msg or "(no user message)")[:120]
    return (f"Short session ({rec.user_msg_count} messages, "
            f"{rec.duration_s}s) — first user message: {head}")
```

- [ ] **Step 4: Run tests to verify they pass.**

Run: `uv run pytest history/tests/test_extract.py -v`
Expected: 13 passed.

- [ ] **Step 5: Commit.**

```bash
git add history/extract.py history/tests/test_extract.py
git commit -m "history: summary input hash + triviality filter"
```

---

### Task 6: Summarizer — Haiku call with forced tool-use + prompt caching

**Files:**
- Create: `history/summarize.py`
- Create: `history/tests/test_summarize.py`

- [ ] **Step 1: Write `history/tests/test_summarize.py`:**

```python
from unittest.mock import MagicMock

import pytest

from history.summarize import (
    SUMMARIZE_TOOL, SUMMARIZE_SYSTEM_PROMPT,
    build_summary_prompt, call_summarizer, SummaryResult,
)
from history.extract import SessionRecord


def _sample_record() -> SessionRecord:
    return SessionRecord(
        session_id="s1", jsonl_path="/p.jsonl", project_path="/Users/tom/dev/foo",
        branch="feat/cohort", started_at=0, ended_at=120, duration_s=120,
        user_msg_count=2, asst_msg_count=3, tool_use_count=2,
        was_interrupted=0, ended_cleanly=1,
        first_user_msg="investigate slow query",
        last_user_msg="verify with explain",
        final_assistant_msg="P99 down to 200ms after adding index",
        files_touched='["resolve_cohort.py", "migrations/0042.sql"]',
        notable_cmds='["psql -c EXPLAIN", "pytest -k slow"]',
        tool_use_counts='{"Read": 1, "Bash": 1, "Write": 1}',
        user_messages_blob="investigate slow query\nverify with explain",
        assistant_text_blob="reading the file\nP99 down to 200ms after adding index",
    )


def test_build_prompt_includes_key_fields():
    rec = _sample_record()
    prompt = build_summary_prompt(rec)
    assert "/Users/tom/dev/foo" in prompt
    assert "feat/cohort" in prompt
    assert "resolve_cohort.py" in prompt
    assert "investigate slow query" in prompt
    assert "P99 down to 200ms" in prompt  # final_assistant_msg
    # Assistant prose mid-conversation NOT included
    assert "reading the file" not in prompt


def test_call_summarizer_uses_forced_tool_use():
    mock_client = MagicMock()
    mock_block = MagicMock()
    mock_block.type = "tool_use"
    mock_block.name = SUMMARIZE_TOOL["name"]
    mock_block.input = {
        "summary": "Fixed slow cohort query by adding events.user_id index.",
        "tags": ["performance", "postgres", "cohorts"],
    }
    mock_msg = MagicMock()
    mock_msg.content = [mock_block]
    mock_client.messages.create.return_value = mock_msg

    result = call_summarizer(mock_client, _sample_record(), model="claude-haiku-4-5")
    assert isinstance(result, SummaryResult)
    assert result.summary.startswith("Fixed slow cohort query")
    assert result.tags == ["performance", "postgres", "cohorts"]
    assert result.model == "claude-haiku-4-5"

    # Verify the call shape
    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["tool_choice"] == {"type": "tool", "name": "save_session_summary"}
    assert call_kwargs["tools"] == [SUMMARIZE_TOOL]
    # Prompt caching set on the system block
    assert call_kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call_kwargs["system"][0]["text"] == SUMMARIZE_SYSTEM_PROMPT
    assert call_kwargs["model"] == "claude-haiku-4-5"


def test_call_summarizer_retries_on_missing_tool_use():
    mock_client = MagicMock()
    # First call: no tool_use block (model misbehaved)
    bad_block = MagicMock(); bad_block.type = "text"; bad_block.text = "I refuse"
    bad_msg = MagicMock(); bad_msg.content = [bad_block]
    # Second call: succeeds
    ok_block = MagicMock(); ok_block.type = "tool_use"; ok_block.name = SUMMARIZE_TOOL["name"]
    ok_block.input = {"summary": "ok", "tags": ["a", "b", "c"]}
    ok_msg = MagicMock(); ok_msg.content = [ok_block]
    mock_client.messages.create.side_effect = [bad_msg, ok_msg]

    result = call_summarizer(mock_client, _sample_record())
    assert result.summary == "ok"
    assert mock_client.messages.create.call_count == 2


def test_call_summarizer_returns_none_after_two_failures():
    mock_client = MagicMock()
    bad_block = MagicMock(); bad_block.type = "text"; bad_block.text = "no"
    bad_msg = MagicMock(); bad_msg.content = [bad_block]
    mock_client.messages.create.return_value = bad_msg

    result = call_summarizer(mock_client, _sample_record())
    assert result is None
    assert mock_client.messages.create.call_count == 2
```

- [ ] **Step 2: Run tests; confirm they fail.**

Run: `uv run pytest history/tests/test_summarize.py -v`
Expected: ImportError on `history.summarize`.

- [ ] **Step 3: Write `history/summarize.py`:**

```python
"""Generate session summaries via the Anthropic SDK with forced tool-use."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from .extract import SessionRecord

log = logging.getLogger(__name__)

SUMMARIZE_TOOL = {
    "name": "save_session_summary",
    "description": "Persist a summary and topic tags for an indexed Claude Code session.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "2-3 sentences in past tense. Concrete: file names, error messages, "
                    "decisions made, what was actually fixed/built. Describe the work done, "
                    "not what the user asked for."
                ),
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 5,
                "description": "3-5 lowercase tags: project, technology, area, action.",
            },
        },
        "required": ["summary", "tags"],
    },
}

SUMMARIZE_SYSTEM_PROMPT = (
    "You summarize Claude Code coding sessions for a search index. "
    "Output is consumed by a developer searching their own history later. "
    "Bias toward concrete specifics (file names, error messages, decisions) "
    "over generic descriptions. Always call save_session_summary."
)

# Token budget for user messages in the prompt body. Crude char proxy: ~4 chars/token.
MAX_USER_MSGS_CHARS = 6000 * 4


@dataclass
class SummaryResult:
    summary: str
    tags: list[str]
    model: str


def build_summary_prompt(rec: SessionRecord) -> str:
    """Construct the per-session prompt body. The stable framing is in
    SUMMARIZE_SYSTEM_PROMPT (cached); only this varies per call."""
    files = json.loads(rec.files_touched)[:15]
    cmds = json.loads(rec.notable_cmds)[:10]
    user_msgs = rec.user_messages_blob
    if len(user_msgs) > MAX_USER_MSGS_CHARS:
        # Keep head and tail; drop the middle.
        head = user_msgs[: MAX_USER_MSGS_CHARS // 2]
        tail = user_msgs[-MAX_USER_MSGS_CHARS // 2:]
        user_msgs = f"{head}\n\n[... truncated ...]\n\n{tail}"
    final_msg = (rec.final_assistant_msg or "").strip()
    return "\n".join([
        "SESSION:",
        f"  project: {rec.project_path}",
        f"  branch: {rec.branch or '(none)'}",
        f"  duration: {rec.duration_s // 60} min",
        f"  files touched: {files}",
        f"  notable commands: {cmds}",
        "",
        "USER MESSAGES (concatenated, may be truncated):",
        user_msgs,
        "",
        "FINAL ASSISTANT MESSAGE (outcome signal, truncated):",
        final_msg,
        "",
        "Call save_session_summary with concrete details from this session.",
    ])


def call_summarizer(client, rec: SessionRecord, *,
                    model: str = "claude-haiku-4-5",
                    max_retries: int = 1) -> SummaryResult | None:
    """Single Haiku call with forced tool-use. Returns None on persistent failure
    (caller stores summary=NULL and logs)."""
    body = build_summary_prompt(rec)
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=512,
                system=[{
                    "type": "text",
                    "text": SUMMARIZE_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=[SUMMARIZE_TOOL],
                tool_choice={"type": "tool", "name": SUMMARIZE_TOOL["name"]},
                messages=[{"role": "user", "content": body}],
            )
        except Exception as e:
            last_err = e
            log.warning("summarize: API error on attempt %d for %s: %s",
                        attempt + 1, rec.session_id, e)
            continue
        for block in getattr(msg, "content", []):
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == SUMMARIZE_TOOL["name"]:
                data = getattr(block, "input", None) or {}
                summary = data.get("summary")
                tags = data.get("tags")
                if isinstance(summary, str) and isinstance(tags, list):
                    return SummaryResult(summary=summary.strip(),
                                          tags=[str(t).lower().strip() for t in tags],
                                          model=model)
        log.warning("summarize: no valid tool_use block on attempt %d for %s",
                    attempt + 1, rec.session_id)
    if last_err is not None:
        log.error("summarize: gave up after %d attempts for %s: %s",
                  max_retries + 1, rec.session_id, last_err)
    return None
```

- [ ] **Step 4: Run tests to verify they pass.**

Run: `uv run pytest history/tests/test_summarize.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit.**

```bash
git add history/summarize.py history/tests/test_summarize.py
git commit -m "history: Haiku summarizer (forced tool-use + ephemeral cache_control)"
```

---

### Task 7: Indexer orchestration — one session, end-to-end

**Files:**
- Create: `history/indexer.py`
- Create: `history/tests/test_indexer.py`

- [ ] **Step 1: Write `history/tests/test_indexer.py`:**

```python
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from history.indexer import index_one, _row_needs_resummary
from history.db import connect, get_meta


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "history.db"
    conn = connect(db_path)
    yield conn, db_path
    conn.close()


def _fake_anthropic(summary="A test summary about X.", tags=("test", "fixture", "stub")):
    client = MagicMock()
    ok_block = MagicMock()
    ok_block.type = "tool_use"
    ok_block.name = "save_session_summary"
    ok_block.input = {"summary": summary, "tags": list(tags)}
    msg = MagicMock()
    msg.content = [ok_block]
    client.messages.create.return_value = msg
    return client


def test_index_one_creates_row_and_fts(temp_db, fixture_dir):
    conn, db_path = temp_db
    client = _fake_anthropic()
    with patch("history.indexer.get_anthropic_client", return_value=client):
        result = index_one(str(fixture_dir / "normal_session.jsonl"), db_path=db_path)
    assert result["status"] == "summarized"
    row = conn.execute("SELECT * FROM sessions WHERE session_id = 'normal-001'").fetchone()
    assert row is not None
    assert row["summary"].startswith("A test summary")
    assert row["tags"] == "test,fixture,stub"
    assert row["summary_model"] == "claude-haiku-4-5"
    assert row["summary_input_hash"] is not None
    fts = conn.execute("SELECT * FROM sessions_fts WHERE session_id = 'normal-001'").fetchone()
    assert fts is not None
    assert "test summary" in fts["summary"]


def test_index_one_is_idempotent_when_content_unchanged(temp_db, fixture_dir):
    conn, db_path = temp_db
    client = _fake_anthropic()
    fixture = fixture_dir / "normal_session.jsonl"
    with patch("history.indexer.get_anthropic_client", return_value=client):
        result1 = index_one(str(fixture), db_path=db_path)
        # Bump mtime but keep it well in the past so live-skip doesn't fire.
        import os
        new_mtime = result1["source_mtime"] + 100
        os.utime(str(fixture), (new_mtime, new_mtime))
        result2 = index_one(str(fixture), db_path=db_path)
    assert result1["status"] == "summarized"
    assert result2["status"] == "hash-cache-hit"
    assert client.messages.create.call_count == 1   # only the first call hit Haiku


def test_index_one_skips_live_session(temp_db, fixture_dir, tmp_path):
    # Copy the fixture into a fresh tmp path; bump mtime to now → looks live.
    import shutil
    src = fixture_dir / "normal_session.jsonl"
    dst = tmp_path / "live.jsonl"
    shutil.copy(src, dst)
    now = time.time()
    import os
    os.utime(dst, (now, now))
    conn, db_path = temp_db
    client = _fake_anthropic()
    with patch("history.indexer.get_anthropic_client", return_value=client):
        result = index_one(str(dst), db_path=db_path)
    assert result["status"] == "skipped-live"
    row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert row == 0  # nothing indexed
    assert client.messages.create.call_count == 0


def test_index_one_trivial_session_no_haiku(temp_db, fixture_dir):
    conn, db_path = temp_db
    client = _fake_anthropic()
    with patch("history.indexer.get_anthropic_client", return_value=client):
        result = index_one(str(fixture_dir / "short_session.jsonl"), db_path=db_path)
    assert result["status"] == "trivial"
    row = conn.execute("SELECT * FROM sessions WHERE session_id = 'abc-001'").fetchone()
    assert row is not None
    assert "Short session" in row["summary"]
    assert client.messages.create.call_count == 0


def test_index_one_handles_summarizer_failure(temp_db, fixture_dir):
    conn, db_path = temp_db
    # Client that never returns a valid tool_use block
    client = MagicMock()
    bad_block = MagicMock(); bad_block.type = "text"; bad_block.text = "no"
    bad_msg = MagicMock(); bad_msg.content = [bad_block]
    client.messages.create.return_value = bad_msg
    with patch("history.indexer.get_anthropic_client", return_value=client):
        result = index_one(str(fixture_dir / "normal_session.jsonl"), db_path=db_path)
    assert result["status"] == "summary-failed"
    row = conn.execute("SELECT * FROM sessions WHERE session_id = 'normal-001'").fetchone()
    assert row is not None
    assert row["summary"] is None
    # Mechanical fields still landed
    assert json.loads(row["files_touched"]) != []


def test_row_needs_resummary_logic():
    row = {"summary_input_hash": "h1", "summary_model": "claude-haiku-4-5", "summary": "x"}
    assert _row_needs_resummary(row, new_hash="h1", target_model="claude-haiku-4-5") is False
    assert _row_needs_resummary(row, new_hash="h2", target_model="claude-haiku-4-5") is True
    assert _row_needs_resummary(row, new_hash="h1", target_model="claude-haiku-5") is True
    # NULL summary → always needs resummary
    row_null = {"summary_input_hash": "h1", "summary_model": "claude-haiku-4-5", "summary": None}
    assert _row_needs_resummary(row_null, new_hash="h1", target_model="claude-haiku-4-5") is True
```

- [ ] **Step 2: Run tests; confirm they fail.**

Run: `uv run pytest history/tests/test_indexer.py -v`
Expected: ImportError on `history.indexer`.

- [ ] **Step 3: Write `history/indexer.py`:**

```python
"""End-to-end indexing of one JSONL session.

Pipeline:
  parse JSONL → extract mechanical record → compute summary_input_hash
  → decide whether to call Haiku → UPSERT sessions + sessions_fts in one txn.

Liveness guard: skip if last event < 5 min ago OR mtime < 60 s ago.
Idempotency: if a row already exists with matching hash + model, reuse summary.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .db import DEFAULT_DB_PATH, MECHANICAL_VERSION, connect, get_meta
from .extract import (
    SessionRecord, compute_summary_input_hash, extract_record,
    heuristic_summary, is_trivial,
)
from .jsonl import parse_jsonl
from .summarize import SummaryResult, call_summarizer

log = logging.getLogger(__name__)

LIVE_LAST_EVENT_S = 5 * 60      # skip if last event newer than 5 min ago
LIVE_MTIME_S = 60               # skip if jsonl mtime newer than 60 s ago

_anthropic_client = None
_anthropic_lock = threading.Lock()


def get_anthropic_client():
    """Lazy-init the Anthropic client. Raises RuntimeError if API key missing."""
    global _anthropic_client
    with _anthropic_lock:
        if _anthropic_client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            from anthropic import Anthropic
            _anthropic_client = Anthropic()
    return _anthropic_client


def _row_needs_resummary(row: dict | sqlite3.Row | None, *,
                          new_hash: str, target_model: str) -> bool:
    if row is None:
        return True
    if row["summary"] is None:
        return True
    if row["summary_input_hash"] != new_hash:
        return True
    if row["summary_model"] != target_model:
        return True
    return False


def _is_live(jsonl_path: Path, source_mtime: int, last_event_ts: int) -> bool:
    now = time.time()
    if last_event_ts and now - last_event_ts < LIVE_LAST_EVENT_S:
        return True
    if source_mtime and now - source_mtime < LIVE_MTIME_S:
        return True
    return False


def _upsert(conn: sqlite3.Connection, rec: SessionRecord, *,
             summary: str | None, tags: list[str] | None,
             summary_input_hash: str | None, summary_model: str | None) -> None:
    """One transaction: UPSERT sessions + refresh sessions_fts."""
    conn.execute(
        """
        INSERT INTO sessions (
          session_id, jsonl_path, project_path, branch,
          started_at, ended_at, duration_s,
          user_msg_count, asst_msg_count, tool_use_count,
          was_interrupted, ended_cleanly,
          summary, tags, summary_input_hash, summary_model,
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
            summary, ",".join(tags) if tags else None,
            summary_input_hash, summary_model,
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
            rec.session_id, summary or "", ",".join(tags) if tags else "",
            rec.first_user_msg or "", rec.last_user_msg or "",
            rec.final_assistant_msg or "",
            rec.user_messages_blob, rec.assistant_text_blob,
            rec.files_touched, rec.notable_cmds,
        ),
    )


def index_one(jsonl_path: str, *, db_path: Path | str | None = None,
              force_summary: bool = False) -> dict[str, Any]:
    """Index (or re-index) one session. Returns a status dict."""
    p = Path(jsonl_path)
    if not p.is_file():
        return {"status": "missing", "jsonl_path": str(p)}
    stat = p.stat()

    # Parse + extract first; if we end up skipping, no DB write.
    events = list(parse_jsonl(str(p)))
    rec = extract_record(str(p), events,
                         source_mtime=int(stat.st_mtime),
                         source_size=stat.st_size)

    if _is_live(p, rec.source_mtime, rec.ended_at):
        return {"status": "skipped-live", "session_id": rec.session_id}

    new_hash = compute_summary_input_hash(rec)

    conn = connect(db_path)
    try:
        target_model = get_meta(conn, "haiku_model") or "claude-haiku-4-5"
        row = conn.execute(
            "SELECT summary, tags, summary_input_hash, summary_model FROM sessions WHERE session_id = ?",
            (rec.session_id,),
        ).fetchone()

        # Decide summary path
        if is_trivial(rec):
            _upsert(conn, rec,
                    summary=heuristic_summary(rec), tags=None,
                    summary_input_hash=new_hash, summary_model=None)
            conn.commit()
            return {"status": "trivial", "session_id": rec.session_id,
                    "source_mtime": rec.source_mtime}

        if not force_summary and not _row_needs_resummary(
                row, new_hash=new_hash, target_model=target_model):
            # Re-extract but reuse existing summary.
            _upsert(conn, rec,
                    summary=row["summary"], tags=(row["tags"] or "").split(",") if row["tags"] else None,
                    summary_input_hash=new_hash, summary_model=row["summary_model"])
            conn.commit()
            return {"status": "hash-cache-hit", "session_id": rec.session_id,
                    "source_mtime": rec.source_mtime}

        # Need to call Haiku
        try:
            client = get_anthropic_client()
        except RuntimeError as e:
            log.warning("summarize: %s — storing mechanical fields only", e)
            _upsert(conn, rec,
                    summary=None, tags=None,
                    summary_input_hash=new_hash, summary_model=None)
            conn.commit()
            return {"status": "no-api-key", "session_id": rec.session_id,
                    "source_mtime": rec.source_mtime}

        result: SummaryResult | None = call_summarizer(client, rec, model=target_model)
        if result is None:
            _upsert(conn, rec,
                    summary=None, tags=None,
                    summary_input_hash=new_hash, summary_model=None)
            conn.commit()
            return {"status": "summary-failed", "session_id": rec.session_id,
                    "source_mtime": rec.source_mtime}

        _upsert(conn, rec,
                summary=result.summary, tags=result.tags,
                summary_input_hash=new_hash, summary_model=result.model)
        conn.commit()
        return {"status": "summarized", "session_id": rec.session_id,
                "source_mtime": rec.source_mtime}
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests to verify they pass.**

Run: `uv run pytest history/tests/test_indexer.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit.**

```bash
git add history/indexer.py history/tests/test_indexer.py
git commit -m "history: end-to-end indexer (live-skip, hash cache, triviality, UPSERT)"
```

---

### Task 8: SessionEnd hook entry point

**Files:**
- Create: `history/hook.py`
- Create: `history/tests/test_hook.py`

- [ ] **Step 1: Write `history/tests/test_hook.py`:**

```python
import io
import json
from unittest.mock import patch

from history.hook import run_hook


def test_hook_indexes_session_from_stdin(monkeypatch, tmp_path):
    jsonl = tmp_path / "x.jsonl"
    jsonl.write_text('{"type":"permission-mode","permissionMode":"default","sessionId":"x"}\n')
    payload = json.dumps({"transcript_path": str(jsonl), "session_id": "x"})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    calls = []

    def fake_index_one(path: str, **kw):
        calls.append((path, kw))
        return {"status": "trivial", "session_id": "x"}

    with patch("history.hook.index_one", side_effect=fake_index_one):
        rc = run_hook()
    assert rc == 0
    assert calls == [(str(jsonl), {})]


def test_hook_returns_0_on_missing_payload(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with patch("history.hook.index_one") as m:
        rc = run_hook()
    assert rc == 0  # silent no-op — never block Claude Code shutdown
    m.assert_not_called()


def test_hook_returns_0_on_missing_file(monkeypatch):
    payload = json.dumps({"transcript_path": "/nope/missing.jsonl"})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with patch("history.hook.index_one") as m:
        rc = run_hook()
    assert rc == 0
    m.assert_not_called()


def test_hook_swallows_indexer_exception(monkeypatch, tmp_path):
    jsonl = tmp_path / "x.jsonl"
    jsonl.write_text("")
    payload = json.dumps({"transcript_path": str(jsonl)})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with patch("history.hook.index_one", side_effect=RuntimeError("kaboom")):
        rc = run_hook()
    assert rc == 0  # never propagate errors out of a hook
```

- [ ] **Step 2: Run tests; confirm they fail.**

Run: `uv run pytest history/tests/test_hook.py -v`
Expected: ImportError on `history.hook`.

- [ ] **Step 3: Write `history/hook.py`:**

```python
"""SessionEnd hook entry point.

Claude Code SessionEnd hooks pipe a JSON event over stdin to the configured
command. We extract `transcript_path` and index that one session. Errors
are swallowed and logged — a hook must never block Claude Code shutdown.

Install via ~/.claude/settings.json:

  {
    "hooks": {
      "SessionEnd": [
        { "command": "python -m history hook" }
      ]
    }
  }
"""
from __future__ import annotations

import json
import logging
import os
import sys

from .indexer import index_one

log = logging.getLogger(__name__)


def run_hook() -> int:
    """Read a SessionEnd JSON payload from stdin and index the named transcript.
    Always returns 0 — hooks must not block Claude Code."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
    except Exception as e:
        log.warning("history.hook: failed to read/parse stdin: %s", e)
        return 0
    if not isinstance(payload, dict):
        return 0
    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not os.path.isfile(transcript_path):
        return 0
    try:
        index_one(transcript_path)
    except Exception as e:
        log.warning("history.hook: index_one failed for %s: %s", transcript_path, e)
    return 0
```

- [ ] **Step 4: Run tests to verify they pass.**

Run: `uv run pytest history/tests/test_hook.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit.**

```bash
git add history/hook.py history/tests/test_hook.py
git commit -m "history: SessionEnd hook (stdin/JSON, idempotent, never blocks)"
```

---

### Task 9: Backfill orchestration

**Files:**
- Create: `history/backfill.py`
- Create: `history/tests/test_backfill.py`

- [ ] **Step 1: Write `history/tests/test_backfill.py`:**

```python
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from history.backfill import backfill, find_jsonl_files


def _fake_anthropic():
    client = MagicMock()
    ok_block = MagicMock()
    ok_block.type = "tool_use"
    ok_block.name = "save_session_summary"
    ok_block.input = {"summary": "fake", "tags": ["a", "b", "c"]}
    msg = MagicMock(); msg.content = [ok_block]
    client.messages.create.return_value = msg
    return client


def test_find_jsonl_files(tmp_path):
    # Create a fake ~/.claude/projects layout
    proj_dir = tmp_path / "projects"
    (proj_dir / "-Users-tom-foo").mkdir(parents=True)
    (proj_dir / "-Users-tom-foo" / "abc.jsonl").write_text("")
    (proj_dir / "-Users-tom-foo" / "def.jsonl").write_text("")
    (proj_dir / "-Users-tom-bar").mkdir(parents=True)
    (proj_dir / "-Users-tom-bar" / "ghi.jsonl").write_text("")
    # Ignore non-jsonl files
    (proj_dir / "-Users-tom-foo" / "notes.md").write_text("")
    found = sorted(find_jsonl_files(projects_dir=proj_dir))
    assert len(found) == 3
    assert all(str(p).endswith(".jsonl") for p in found)


def test_backfill_indexes_each_jsonl(tmp_path, fixture_dir):
    proj_dir = tmp_path / "projects"
    cwd_dir = proj_dir / "-Users-tom-dev-foo"
    cwd_dir.mkdir(parents=True)
    shutil.copy(fixture_dir / "normal_session.jsonl", cwd_dir / "n1.jsonl")
    shutil.copy(fixture_dir / "short_session.jsonl", cwd_dir / "s1.jsonl")
    db_path = tmp_path / "h.db"
    client = _fake_anthropic()
    with patch("history.indexer.get_anthropic_client", return_value=client):
        result = backfill(projects_dir=proj_dir, db_path=db_path, workers=2)
    assert result["scanned"] == 2
    # short_session is trivial → no Haiku call. normal_session → 1 Haiku call.
    assert client.messages.create.call_count == 1
    from history.db import connect
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT session_id FROM sessions ORDER BY session_id").fetchall()
        ids = [r["session_id"] for r in rows]
        assert "abc-001" in ids       # from short_session
        assert "normal-001" in ids
    finally:
        conn.close()


def test_backfill_resumable(tmp_path, fixture_dir):
    proj_dir = tmp_path / "projects"
    cwd_dir = proj_dir / "-Users-tom-dev-foo"
    cwd_dir.mkdir(parents=True)
    shutil.copy(fixture_dir / "normal_session.jsonl", cwd_dir / "n1.jsonl")
    db_path = tmp_path / "h.db"
    client = _fake_anthropic()
    with patch("history.indexer.get_anthropic_client", return_value=client):
        backfill(projects_dir=proj_dir, db_path=db_path, workers=2)
        result2 = backfill(projects_dir=proj_dir, db_path=db_path, workers=2)
    # Second run: hash-cache-hit, no Haiku call
    assert result2["scanned"] == 1
    assert result2["statuses"].get("hash-cache-hit", 0) == 1
    assert client.messages.create.call_count == 1  # only the first backfill called
```

- [ ] **Step 2: Run tests; confirm they fail.**

Run: `uv run pytest history/tests/test_backfill.py -v`
Expected: ImportError on `history.backfill`.

- [ ] **Step 3: Write `history/backfill.py`:**

```python
"""Multi-session orchestration for indexing ~/.claude/projects/*.jsonl.

Bounded concurrency via ThreadPoolExecutor (sync Anthropic client is thread-safe).
SQLite writes are serialized inside each index_one's own connection — WAL +
short transactions keep contention low at this scale.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .indexer import index_one

log = logging.getLogger(__name__)

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def find_jsonl_files(projects_dir: Path = DEFAULT_PROJECTS_DIR) -> list[Path]:
    if not projects_dir.is_dir():
        return []
    return sorted(projects_dir.glob("*/*.jsonl"))


def backfill(*, projects_dir: Path = DEFAULT_PROJECTS_DIR,
             db_path: Path | str | None = None,
             workers: int = 5,
             since: int | None = None) -> dict:
    """Index every JSONL under projects_dir. Idempotent."""
    paths = find_jsonl_files(projects_dir)
    if since is not None:
        paths = [p for p in paths if p.stat().st_mtime >= since]
    statuses: dict[str, int] = {}
    errors: list[tuple[str, str]] = []
    log.info("history.backfill: scanning %d jsonl files (workers=%d)", len(paths), workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(index_one, str(p), db_path=db_path): p for p in paths}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                result = fut.result()
                status = result.get("status", "?")
                statuses[status] = statuses.get(status, 0) + 1
            except Exception as e:
                log.warning("history.backfill: %s failed: %s", p, e)
                errors.append((str(p), str(e)))
                statuses["error"] = statuses.get("error", 0) + 1
    log.info("history.backfill: done — %s", statuses)
    return {"scanned": len(paths), "statuses": statuses, "errors": errors}
```

- [ ] **Step 4: Run tests to verify they pass.**

Run: `uv run pytest history/tests/test_backfill.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit.**

```bash
git add history/backfill.py history/tests/test_backfill.py
git commit -m "history: backfill orchestration (ThreadPoolExecutor, resumable, idempotent)"
```

---

### Task 10: Search backend — FTS5 query

**Files:**
- Create: `history/search.py`
- Create: `history/tests/test_search.py`

- [ ] **Step 1: Write `history/tests/test_search.py`:**

```python
from unittest.mock import MagicMock, patch

import pytest

from history.db import connect
from history.search import search, _build_fts_query


def _seed_session(conn, session_id, summary, tags, project, branch="main",
                   first_user="hello", final_asst="done", started_at=1000,
                   summary_model="claude-haiku-4-5"):
    """Insert a row representing a Haiku-summarized (non-trivial) session.
    Pass summary_model=None for a row that should be filtered by the default
    `include_trivial=False`."""
    conn.execute(
        """
        INSERT INTO sessions(
          session_id, jsonl_path, project_path, branch,
          started_at, ended_at, duration_s,
          user_msg_count, asst_msg_count, tool_use_count,
          summary, tags, summary_model, first_user_msg, last_user_msg, final_assistant_msg,
          files_touched, notable_cmds, tool_use_counts,
          indexed_at, mechanical_version, source_mtime, source_size
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', '[]', '{}', 0, 1, 0, 0)
        """,
        (session_id, f"/p/{session_id}.jsonl", project, branch,
         started_at, started_at + 60, 60, 3, 3, 2,
         summary, tags, summary_model, first_user, first_user, final_asst),
    )
    conn.execute(
        """
        INSERT INTO sessions_fts(
          session_id, summary, tags, first_user_msg, last_user_msg,
          final_assistant_msg, user_messages, assistant_text,
          files_touched, notable_cmds
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '')
        """,
        (session_id, summary, tags, first_user, first_user, final_asst,
         first_user, final_asst),
    )
    conn.commit()


@pytest.fixture
def seeded_db(tmp_path):
    db_path = tmp_path / "h.db"
    conn = connect(db_path)
    _seed_session(conn, "s1", "Fixed slow cohort query by adding events.user_id index",
                  "performance,postgres,cohorts", "/Users/tom/dev/fdy", started_at=2000)
    _seed_session(conn, "s2", "Added OAuth login flow with Google provider",
                  "auth,oauth,frontend", "/Users/tom/dev/web", started_at=1000)
    _seed_session(conn, "s3", "Fixed timezone bug in date_parser.py",
                  "bug,timezones,dates", "/Users/tom/dev/fdy", started_at=3000)
    yield conn, db_path
    conn.close()


def test_search_returns_fts_matches(seeded_db):
    _, db_path = seeded_db
    results = search("cohort", db_path=db_path, rerank=False)
    assert len(results) == 1
    assert results[0]["session_id"] == "s1"


def test_search_filters_by_project(seeded_db):
    _, db_path = seeded_db
    results = search("fixed", db_path=db_path, project="/Users/tom/dev/fdy", rerank=False)
    sids = {r["session_id"] for r in results}
    assert sids == {"s1", "s3"}


def test_search_orders_by_relevance(seeded_db):
    _, db_path = seeded_db
    results = search("timezone", db_path=db_path, rerank=False)
    assert results[0]["session_id"] == "s3"


def test_search_filters_by_since(seeded_db):
    _, db_path = seeded_db
    results = search("fixed", db_path=db_path, since=2500, rerank=False)
    sids = {r["session_id"] for r in results}
    assert sids == {"s3"}


def test_search_excludes_trivial_by_default(tmp_path):
    db_path = tmp_path / "h.db"
    conn = connect(db_path)
    # Trivial sessions have summary_model IS NULL (heuristic-only summary).
    _seed_session(conn, "trivial-1",
                  "Short session (1 messages, 5s) — first user message: hi",
                  None, "/Users/tom/dev/foo", summary_model=None)
    conn.execute("UPDATE sessions SET user_msg_count = 1, duration_s = 5 WHERE session_id = 'trivial-1'")
    conn.commit()
    results = search("session", db_path=db_path, rerank=False)
    assert results == []
    results = search("session", db_path=db_path, rerank=False, include_trivial=True)
    assert len(results) == 1
    conn.close()


def test_build_fts_query_escapes_quotes():
    # Defensive: user-typed quotes shouldn't break FTS5
    assert _build_fts_query('"foo bar"') is not None
    assert _build_fts_query("foo's") is not None


def test_rerank_reorders_and_annotates(seeded_db):
    """Rerank path: mocked Haiku reorders results and adds rerank_reason."""
    _, db_path = seeded_db
    # Mock the Anthropic client so search.rerank picks it up via indexer.get_anthropic_client
    mock_client = MagicMock()
    blk = MagicMock(); blk.type = "tool_use"; blk.name = "rank_search_results"
    blk.input = {
        "results": [
            # Reverse the FTS order: s3 first, then s1
            {"session_id": "s3", "reason": "exact match for timezone", "score": 0.95},
            {"session_id": "s1", "reason": "weakly related", "score": 0.3},
        ],
    }
    msg = MagicMock(); msg.content = [blk]
    mock_client.messages.create.return_value = msg
    with patch("history.indexer.get_anthropic_client", return_value=mock_client):
        # Query that matches both "fixed" sessions
        results = search("fixed", db_path=db_path, rerank=True, limit=10)
    # Rerank should have reordered s3 ahead of s1
    sids = [r["session_id"] for r in results]
    assert sids[:2] == ["s3", "s1"]
    assert results[0]["rerank_reason"] == "exact match for timezone"
    # Verify call shape
    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["tool_choice"] == {"type": "tool", "name": "rank_search_results"}
    assert call_kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_rerank_falls_back_to_fts_order_on_client_failure(seeded_db):
    _, db_path = seeded_db
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = RuntimeError("API down")
    with patch("history.indexer.get_anthropic_client", return_value=mock_client):
        results = search("fixed", db_path=db_path, rerank=True, limit=10)
    # FTS order preserved; no rerank_reason annotations
    assert len(results) >= 1
    for r in results:
        assert r["rerank_reason"] is None
```

- [ ] **Step 2: Run tests; confirm they fail.**

Run: `uv run pytest history/tests/test_search.py -v`
Expected: ImportError on `history.search`.

- [ ] **Step 3: Write `history/search.py`:**

```python
"""FTS5 search over the indexed history."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from .db import connect

log = logging.getLogger(__name__)


def _build_fts_query(query: str) -> str:
    """Convert a user query into an FTS5 MATCH expression.

    FTS5's default tokenizer handles bare words fine. We escape double quotes
    to avoid syntax errors, and split on whitespace into terms ANDed together.
    """
    cleaned = query.replace('"', '""').strip()
    if not cleaned:
        return ""
    # Wrap each whitespace-separated term as a quoted phrase so apostrophes/etc.
    # don't blow up FTS5's tokenizer. AND-join.
    terms = [f'"{t}"' for t in re.split(r"\s+", cleaned) if t]
    return " AND ".join(terms) if terms else ""


def search(query: str, *,
           db_path: Path | str | None = None,
           project: str | None = None,
           branch: str | None = None,
           since: int | None = None,
           until: int | None = None,
           include_trivial: bool = False,
           rerank: bool = False,
           limit: int = 50) -> list[dict]:
    """FTS5-ranked search across indexed sessions.

    Returns a list of result dicts ordered by relevance, optionally re-ranked
    via Haiku when `rerank=True` and the API key is available.
    """
    fts_q = _build_fts_query(query)
    if not fts_q:
        return []

    conn = connect(db_path)
    try:
        sql = """
            SELECT s.*, bm25(sessions_fts) AS fts_rank
            FROM sessions_fts
            JOIN sessions s ON s.session_id = sessions_fts.session_id
            WHERE sessions_fts MATCH :q
        """
        params: dict = {"q": fts_q}
        if project:
            sql += " AND s.project_path = :project"
            params["project"] = project
        if branch:
            sql += " AND s.branch = :branch"
            params["branch"] = branch
        if since is not None:
            sql += " AND s.started_at >= :since"
            params["since"] = since
        if until is not None:
            sql += " AND s.started_at <= :until"
            params["until"] = until
        if not include_trivial:
            # Trivial heuristic-summary rows have summary_model IS NULL.
            sql += " AND s.summary_model IS NOT NULL"
        # FTS rerank request bounds candidates higher, then we trim.
        sql += " ORDER BY fts_rank ASC LIMIT :limit"
        params["limit"] = min(max(limit, 1), 20 if rerank else 200)
        rows = [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()

    if rerank and len(rows) > 1:
        rows = _rerank(rows, query)

    # Normalize for the API surface: parse JSON columns, drop verbose blobs.
    out = []
    for i, r in enumerate(rows[:limit]):
        out.append({
            "session_id": r["session_id"],
            "jsonl_path": r["jsonl_path"],
            "project_path": r["project_path"],
            "branch": r["branch"],
            "started_at": r["started_at"],
            "duration_s": r["duration_s"],
            "user_msg_count": r["user_msg_count"],
            "summary": r["summary"],
            "tags": (r["tags"] or "").split(",") if r["tags"] else [],
            "first_user_msg": r["first_user_msg"],
            "files_touched": json.loads(r["files_touched"]) if r["files_touched"] else [],
            "rank": i + 1,
            "rerank_reason": r.get("rerank_reason"),
        })
    return out


def _rerank(rows: list[dict], query: str) -> list[dict]:
    """Send candidate rows + the query to Haiku for semantic re-ranking.
    Returns rows reordered, each annotated with `rerank_reason`. If the
    Haiku call fails for any reason, the original FTS order is preserved
    (and a one-line log warning is emitted)."""
    try:
        from .indexer import get_anthropic_client
        client = get_anthropic_client()
    except Exception as e:
        log.warning("rerank: skipping (no client): %s", e)
        return rows
    candidates = [{
        "session_id": r["session_id"],
        "summary": r.get("summary") or r.get("first_user_msg") or "",
        "tags": r.get("tags") or "",
        "project": r.get("project_path") or "",
        "first_user_msg": r.get("first_user_msg") or "",
    } for r in rows]
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=[{
                "type": "text",
                "text": (
                    "You re-rank candidate Claude Code session summaries by their relevance "
                    "to the user's query. Always call rank_search_results."
                ),
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[RERANK_TOOL],
            tool_choice={"type": "tool", "name": RERANK_TOOL["name"]},
            messages=[{
                "role": "user",
                "content": (
                    f"QUERY: {query}\n\n"
                    f"CANDIDATES:\n{json.dumps(candidates, indent=2)}\n\n"
                    "Call rank_search_results."
                ),
            }],
        )
        for block in getattr(msg, "content", []):
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == RERANK_TOOL["name"]:
                data = getattr(block, "input", None) or {}
                ranked = data.get("results") or []
                by_id = {r["session_id"]: r for r in rows}
                reordered = []
                for entry in ranked:
                    sid = entry.get("session_id")
                    if sid in by_id:
                        row = dict(by_id[sid])
                        row["rerank_reason"] = entry.get("reason")
                        reordered.append(row)
                # Append any candidates the model omitted at the end
                seen = {r["session_id"] for r in reordered}
                for r in rows:
                    if r["session_id"] not in seen:
                        reordered.append(r)
                return reordered
    except Exception as e:
        log.warning("rerank: model call failed (%s) — preserving FTS order", e)
    return rows


RERANK_TOOL = {
    "name": "rank_search_results",
    "description": "Re-rank candidate Claude Code session summaries by relevance to a query.",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "reason": {"type": "string",
                                    "description": "One sentence explaining the match."},
                        "score": {"type": "number",
                                   "description": "0.0 (irrelevant) to 1.0 (perfect match)."},
                    },
                    "required": ["session_id", "reason", "score"],
                },
            },
        },
        "required": ["results"],
    },
}
```

- [ ] **Step 4: Run tests to verify they pass.**

Run: `uv run pytest history/tests/test_search.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit.**

```bash
git add history/search.py history/tests/test_search.py
git commit -m "history: FTS5 search + optional Haiku rerank"
```

---

### Task 11: CLI verb dispatch — backfill / search / stats / clean / reindex / resummarize / hook

**Files:**
- Modify: `history/cli.py`
- Create: `history/tests/test_cli.py`

- [ ] **Step 1: Write `history/tests/test_cli.py`:**

```python
import json
import shutil
import sys
from unittest.mock import MagicMock, patch

import pytest

from history.cli import main


def _fake_anthropic():
    client = MagicMock()
    blk = MagicMock(); blk.type = "tool_use"; blk.name = "save_session_summary"
    blk.input = {"summary": "x", "tags": ["a", "b", "c"]}
    m = MagicMock(); m.content = [blk]
    client.messages.create.return_value = m
    return client


def test_cli_help_no_verb(capsys):
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 2
    assert "verbs:" in out


def test_cli_unknown_verb(capsys):
    rc = main(["whoami"])
    out = capsys.readouterr().out
    assert rc != 0
    assert "unknown verb" in out.lower()


def test_cli_backfill(tmp_path, fixture_dir, capsys):
    proj_dir = tmp_path / "projects"
    cwd_dir = proj_dir / "-Users-tom-dev-foo"; cwd_dir.mkdir(parents=True)
    shutil.copy(fixture_dir / "short_session.jsonl", cwd_dir / "s1.jsonl")
    db_path = tmp_path / "h.db"
    with patch("history.indexer.get_anthropic_client", return_value=_fake_anthropic()):
        rc = main(["backfill",
                   "--projects-dir", str(proj_dir),
                   "--db-path", str(db_path),
                   "--workers", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "scanned 1" in out.lower() or '"scanned": 1' in out


def test_cli_search(tmp_path, fixture_dir, capsys):
    proj_dir = tmp_path / "projects"
    cwd_dir = proj_dir / "-Users-tom-dev-foo"; cwd_dir.mkdir(parents=True)
    shutil.copy(fixture_dir / "normal_session.jsonl", cwd_dir / "n1.jsonl")
    db_path = tmp_path / "h.db"
    with patch("history.indexer.get_anthropic_client", return_value=_fake_anthropic()):
        main(["backfill",
              "--projects-dir", str(proj_dir),
              "--db-path", str(db_path),
              "--workers", "1"])
    capsys.readouterr()  # discard backfill output
    rc = main(["search", "investigate", "--db-path", str(db_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "normal-001" in out


def test_cli_stats(tmp_path, capsys):
    db_path = tmp_path / "h.db"
    rc = main(["stats", "--db-path", str(db_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sessions" in out.lower()


def test_cli_hook_delegates_to_run_hook(monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = main(["hook"])
    assert rc == 0


def test_cli_clean_removes_missing_files(tmp_path, capsys):
    from history.db import connect
    db_path = tmp_path / "h.db"
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO sessions(session_id, jsonl_path, project_path, started_at, ended_at, "
        "duration_s, user_msg_count, asst_msg_count, tool_use_count, indexed_at, "
        "mechanical_version, source_mtime, source_size, files_touched, notable_cmds, "
        "tool_use_counts) "
        "VALUES ('orphan', '/nope/missing.jsonl', '/p', 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, '[]', '[]', '{}')"
    )
    conn.commit()
    conn.close()
    rc = main(["clean", "--db-path", str(db_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 row" in out.lower() or "removed 1" in out.lower()
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    conn.close()
```

- [ ] **Step 2: Run tests; confirm they fail.**

Run: `uv run pytest history/tests/test_cli.py -v`
Expected: tests fail because cli.py is still the stub from Task 1.

- [ ] **Step 3: Replace `history/cli.py` with the full dispatcher:**

```python
"""CLI verb dispatch: `python -m history <verb> [args]`."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .db import connect, get_meta, set_meta

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        _print_help()
        return 2
    verb = argv.pop(0)
    handler = _VERBS.get(verb)
    if handler is None:
        print(f"unknown verb: {verb!r}", file=sys.stderr)
        _print_help(file=sys.stderr)
        return 2
    return handler(argv)


def _print_help(*, file=sys.stdout) -> None:
    print("usage: python -m history <verb> [args]", file=file)
    print("verbs: " + ", ".join(_VERBS), file=file)


# --- verb: hook ---------------------------------------------------------

def _cmd_hook(argv: list[str]) -> int:
    from .hook import run_hook
    return run_hook()


# --- verb: backfill -----------------------------------------------------

def _cmd_backfill(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="history backfill")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--projects-dir", default=None)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--since", default=None, help="YYYY-MM-DD or unix ts")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    from .backfill import backfill, find_jsonl_files
    from pathlib import Path
    projects = Path(args.projects_dir) if args.projects_dir else None
    since_ts = None
    if args.since:
        try:
            since_ts = int(args.since)
        except ValueError:
            from datetime import datetime
            since_ts = int(datetime.fromisoformat(args.since).timestamp())
    if args.dry_run:
        paths = find_jsonl_files(projects or None)
        print(f"would scan {len(paths)} jsonl files")
        return 0
    kwargs = {"workers": args.workers, "db_path": args.db_path, "since": since_ts}
    if projects is not None:
        kwargs["projects_dir"] = projects
    result = backfill(**kwargs)
    # Record when the last full scan happened for `stats`.
    conn = connect(args.db_path)
    try:
        import time as _t
        set_meta(conn, "last_full_scan_at", str(int(_t.time())))
    finally:
        conn.close()
    print(json.dumps(result, indent=2))
    return 0


# --- verb: search -------------------------------------------------------

def _cmd_search(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="history search")
    parser.add_argument("query", nargs="+")
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--project", default=None)
    parser.add_argument("--branch", default=None)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--include-trivial", action="store_true")
    args = parser.parse_args(argv)
    from .search import search
    results = search(
        " ".join(args.query),
        db_path=args.db_path,
        project=args.project,
        branch=args.branch,
        include_trivial=args.include_trivial,
        rerank=args.rerank,
        limit=args.limit,
    )
    for r in results:
        when = _fmt_ts(r["started_at"])
        print("\t".join([
            when,
            r["session_id"],
            r["project_path"],
            (r["summary"] or r["first_user_msg"] or "")[:200].replace("\n", " "),
        ]))
    return 0


# --- verb: stats --------------------------------------------------------

def _cmd_stats(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="history stats")
    parser.add_argument("--db-path", default=None)
    args = parser.parse_args(argv)
    conn = connect(args.db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        summarized = conn.execute("SELECT COUNT(*) FROM sessions WHERE summary IS NOT NULL AND summary_model IS NOT NULL").fetchone()[0]
        heuristic = conn.execute("SELECT COUNT(*) FROM sessions WHERE summary IS NOT NULL AND summary_model IS NULL").fetchone()[0]
        null_summary = conn.execute("SELECT COUNT(*) FROM sessions WHERE summary IS NULL").fetchone()[0]
        last_scan = get_meta(conn, "last_full_scan_at")
        model = get_meta(conn, "haiku_model")
        mech_ver = get_meta(conn, "mechanical_version")
    finally:
        conn.close()
    print(f"sessions: {total}")
    print(f"  Haiku-summarized: {summarized}")
    print(f"  heuristic (trivial): {heuristic}")
    print(f"  no summary: {null_summary}")
    print(f"  mechanical_version: {mech_ver}")
    print(f"  haiku_model: {model}")
    if last_scan:
        print(f"  last full scan: {_fmt_ts(int(last_scan))}")
    return 0


# --- verb: clean --------------------------------------------------------

def _cmd_clean(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="history clean")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    conn = connect(args.db_path)
    try:
        rows = conn.execute("SELECT session_id, jsonl_path FROM sessions").fetchall()
        orphans = [r["session_id"] for r in rows if not os.path.isfile(r["jsonl_path"])]
        if args.dry_run:
            print(f"would remove {len(orphans)} orphan rows")
            for sid in orphans:
                print(f"  - {sid}")
            return 0
        for sid in orphans:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
        conn.commit()
        print(f"removed {len(orphans)} rows")
    finally:
        conn.close()
    return 0


# --- verb: reindex ------------------------------------------------------

def _cmd_reindex(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="history reindex")
    parser.add_argument("--all", action="store_true", required=True,
                        help="re-extract every row (mechanical_version bump effect)")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--projects-dir", default=None)
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args(argv)
    # Bumping local MECHANICAL_VERSION constant happens at code-edit time;
    # this verb forces re-extraction in case rows were inserted under an older
    # version. It walks every JSONL again — Haiku is reused via hash if the
    # underlying content is stable, so this is a free pass in the common case.
    from .backfill import backfill
    from pathlib import Path
    projects = Path(args.projects_dir) if args.projects_dir else None
    kwargs = {"workers": args.workers, "db_path": args.db_path}
    if projects is not None:
        kwargs["projects_dir"] = projects
    result = backfill(**kwargs)
    print(json.dumps(result, indent=2))
    return 0


# --- verb: resummarize --------------------------------------------------

def _cmd_resummarize(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="history resummarize")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--missing", action="store_true",
                     help="only re-summarize rows where summary IS NULL")
    grp.add_argument("--all", action="store_true",
                     help="force re-summary of every row (clears summary_input_hash)")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args(argv)
    conn = connect(args.db_path)
    try:
        if args.all:
            conn.execute("UPDATE sessions SET summary_input_hash = NULL")
            conn.commit()
            print("cleared summary_input_hash for all rows")
            paths = [r["jsonl_path"] for r in conn.execute("SELECT jsonl_path FROM sessions")]
        else:
            paths = [r["jsonl_path"] for r in conn.execute(
                "SELECT jsonl_path FROM sessions WHERE summary IS NULL"
            )]
    finally:
        conn.close()
    if not paths:
        print("nothing to do")
        return 0
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .indexer import index_one
    statuses: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(index_one, p, db_path=args.db_path) for p in paths]
        for fut in as_completed(futures):
            try:
                status = fut.result().get("status", "?")
            except Exception:
                status = "error"
            statuses[status] = statuses.get(status, 0) + 1
    print(json.dumps({"scanned": len(paths), "statuses": statuses}, indent=2))
    return 0


# --- helpers ------------------------------------------------------------

def _fmt_ts(ts: int) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


_VERBS = {
    "hook":         _cmd_hook,
    "backfill":     _cmd_backfill,
    "search":       _cmd_search,
    "stats":        _cmd_stats,
    "clean":        _cmd_clean,
    "reindex":      _cmd_reindex,
    "resummarize":  _cmd_resummarize,
}
```

- [ ] **Step 4: Run tests to verify they pass.**

Run: `uv run pytest history/tests/test_cli.py -v`
Expected: 7 passed.

- [ ] **Step 5: Run the full suite as a sanity check.**

Run: `uv run pytest history/tests/ -v`
Expected: all tests pass (smoke + db + jsonl + extract + summarize + indexer + hook + backfill + search + cli).

- [ ] **Step 6: Commit.**

```bash
git add history/cli.py history/tests/test_cli.py
git commit -m "history: CLI verb dispatch (backfill / search / stats / clean / reindex / resummarize / hook)"
```

---

### Task 12: README + hook installation snippet

**Files:**
- Create: `history/README.md`

- [ ] **Step 1: Write `history/README.md`:**

```markdown
# history

A searchable index over every Claude Code conversation transcript in
`~/.claude/projects/`. Standalone Python package; periscope mounts a web
UI over it in a separate phase.

## Quick start

```sh
# from the periscope repo root
uv sync --dev                               # one-time
export ANTHROPIC_API_KEY=...                # optional; falls back to mechanical
python -m history backfill --workers 5      # one-shot index, ~13 min, ~$4-6
python -m history search "the timezone bug"
python -m history stats
```

## Verbs

| Verb | Purpose |
|---|---|
| `backfill` | One-shot index of `~/.claude/projects/`. Idempotent. |
| `hook` | SessionEnd hook entry point. Reads JSON from stdin. |
| `search <q>` | FTS5 search. `--rerank` adds Haiku re-rank (~1s, ~$0.001). |
| `stats` | Row counts, summarization coverage, model. |
| `clean` | Remove rows whose JSONL has been deleted from disk. |
| `reindex --all` | Re-extract every row (reuses summary via hash, free). |
| `resummarize --missing\|--all` | Re-run Haiku for missing rows, or force a full re-summarize. |

## Hook installation

Append to `~/.claude/settings.json`:

```jsonc
{
  "hooks": {
    "SessionEnd": [
      { "command": "python -m history hook" }
    ]
  }
}
```

The hook reads a JSON event from stdin with a `transcript_path` field.
Errors are swallowed — the hook never blocks Claude Code shutdown.

## Storage

- DB: `~/.claude/history.db` (override with `$CLAUDE_HISTORY_DB`).
- Source-of-truth = JSONL files in `~/.claude/projects/`. The DB is a
  derived index, rebuildable at any time with `backfill`.

## Design

See `/Users/tom/dev/periscope/docs/superpowers/specs/2026-05-13-claude-history-search-design.md`.
```

- [ ] **Step 2: Commit.**

```bash
git add history/README.md
git commit -m "history: README + hook install snippet"
```

---

### Task 13: Install the hook and do a backfill dry run

This task is operational, not code. Verify the package works end-to-end on your real data before declaring Phase A done.

- [ ] **Step 1: Confirm `ANTHROPIC_API_KEY` is exported.**

```bash
echo "key set? $([ -n "$ANTHROPIC_API_KEY" ] && echo yes || echo NO)"
```

- [ ] **Step 2: Dry-run the backfill to see the file count.**

```bash
cd ~/dev/periscope
uv run python -m history backfill --dry-run
```

Expected: a single line like `would scan 1984 jsonl files`.

- [ ] **Step 3: Run a real backfill against a SMALL subset first.** Pick one project directory with ~10 JSONLs:

```bash
ls ~/.claude/projects/ | head -5
# Pick one with a small number of jsonl files; create a temp projects dir
mkdir -p /tmp/history-test-projects
cp -r ~/.claude/projects/<one-project>/ /tmp/history-test-projects/
uv run python -m history backfill --projects-dir /tmp/history-test-projects \
    --db-path /tmp/history-test.db --workers 2
```

Expected: JSON output with `scanned: N`, `statuses: {summarized: M, trivial: K, ...}`. No tracebacks.

- [ ] **Step 4: Smoke-test search against the test DB.**

```bash
uv run python -m history search "claude" --db-path /tmp/history-test.db --limit 5
uv run python -m history stats --db-path /tmp/history-test.db
```

Expected: results print as TSV. Stats prints row counts.

- [ ] **Step 5: Run the full backfill.**

```bash
uv run python -m history backfill --workers 5
```

Expected: ~13 minutes (with prompt cache hits, possibly less). Final JSON shows ~2,000 scanned, mostly `summarized` + `trivial`.

- [ ] **Step 6: Install the SessionEnd hook.** Edit `~/.claude/settings.json` and add:

```jsonc
{
  "hooks": {
    "SessionEnd": [
      { "command": "python -m history hook" }
    ]
  }
}
```

(The existing `index-conversation.py` hook stays alongside — they don't interact. After ~2 weeks of trusting the new system, remove the old hook line.)

- [ ] **Step 7: Verify the hook is wired up.** Start a Claude Code session in a throwaway directory, run a `/exit`. Check the DB row count:

```bash
uv run python -m history stats
```

Expected: row count increased by 1.

- [ ] **Step 8: Commit operational notes if any (e.g. lessons learned in README).** Otherwise, no commit; Phase A is complete.

---

## Phase A complete — what's shippable

After this plan you have:

- A working CLI: `python -m history backfill/search/stats/hook/...`
- A SessionEnd hook keeping the DB fresh as you use Claude Code
- A SQLite DB at `~/.claude/history.db` with ~2,000 indexed sessions
- A full pytest suite with ~40 tests, no live-network requirements
- Zero periscope-side code changes (clean separation)

Phase B (separate plan) adds the `/history` web UI + resume flow on top of this.

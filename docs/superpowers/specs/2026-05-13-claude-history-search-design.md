# Claude History Search — Design Spec

**Date:** 2026-05-13
**Status:** approved, ready for implementation plan
**Author:** Tom + Claude (brainstorm session)

---

## Summary

Add a searchable index over every Claude Code conversation in `~/.claude/projects/`,
queryable from a new `/history` route in periscope and from a standalone CLI.
Indexes mechanical metadata + a Haiku-generated 2-3 sentence summary per session.
Search uses SQLite FTS5 with optional Haiku-powered semantic rerank. Results can
be resumed in a new tmux window via `claude --resume <id>`.

The indexer lives as a self-contained `history/` subpackage inside the periscope
repo. It has zero periscope-specific imports — periscope mounts a thin
`/api/history/*` route layer over it. Standalone use is via `python -m history`.

## Goals

- **Find any past Claude conversation semantically**, not by exact keyword. ("The
  session where I figured out the timezone bug" should work.)
- **One-click resume** into a fresh tmux window running `claude --resume <id>`.
- **No third-party dependencies** beyond Anthropic SDK + SQLite (already present).
- **Source-of-truth = JSONL files.** The DB is a derived index; rebuildable at any
  time.
- **Bounded LLM cost.** Index-time summary is the only required call (~$0.005 per
  session). Rerank is opt-in per query (~$0.001).
- **Graceful degradation** when `ANTHROPIC_API_KEY` is missing: mechanical
  fields still indexed and FTS-searchable.

## Non-goals

- Search across sub-agent transcripts (Task-tool dispatches). These live in
  `~/.claude/tasks/<task-uuid>/N.json` (numbered JSON files, not JSONL) —
  a separate directory tree the indexer simply doesn't walk in v1. The
  non-goal is automatic.
- Cross-session entity extraction (e.g. "everyone who worked on the cohort
  table"). FTS + summary should cover the realistic queries.
- Embedding-based retrieval. Anthropic doesn't ship embeddings; FTS + Haiku
  rerank is sufficient and avoids a new provider dependency.
- Multi-user / remote access. Periscope is single-user, 127.0.0.1.

## Phases

This work ships in three phases. Each is a self-contained PR.

### Phase 0 — `auto_rename_*` forced-tool-use migration (prerequisite, ~½ day)

Migrate the existing `auto_rename_session` and `auto_rename_window` in
`server.py` from free-form JSON parsing (`json.loads(cleaned)` +
code-fence stripping + `JSONDecodeError` handling) to forced tool-use
via the Anthropic SDK's `tool_choice={"type": "tool", "name": ...}`
mechanism. Same model (`claude-haiku-4-5`), same prompt body, schema
captures the existing `{index: name}` output shape.

Why first: smallest possible end-to-end exercise of the structured-output
pattern, with a known-working fallback (the current code) one revert away.
Validates the SDK pin, the call shape, and the response-parsing code path
before Phase A depends on it for the larger feature.

### Phase A — `history/` indexer + CLI (~3–4 days, dogfoodable)

The `history/` subpackage end-to-end: schema, indexer, JSONL parsing, Haiku
summarization with forced tool-use, backfill, SessionEnd hook, `python -m
history` CLI with `search` / `backfill` / `hook` / `reindex` /
`resummarize` / `stats` / `clean` verbs.

Ship-criteria: backfill 1,984 sessions cleanly; `python -m history search
"foo"` returns ranked results; SessionEnd hook updates the DB on session
exit. Zero periscope code changes in this phase. Tom dogfoods the CLI for
~1 week before Phase B.

### Phase B — periscope web UI + resume (~2–3 days)

Add `/history` static page, `/api/history/search`, `/api/history/session/{id}`
routes, extend `/api/window/new` with `mode="resume"`. Builds on the
validated Phase A indexing — if Phase A surfaces "summaries are too short"
or "FTS columns are wrong" feedback, the schema is still cheap to change
before web UI work hardens around it.

## Architecture

```
                                   ┌─────────────────────────────┐
                                   │  ~/.claude/projects/        │
                                   │    -Users-tom-dev-foo/      │
                                   │      <session-uuid>.jsonl   │  ← source of truth (never written by us)
                                   └────────────┬────────────────┘
                                                │
                ┌───────────────────────────────┼───────────────────────────────┐
                │                               │                               │
   SessionEnd hook                         backfill CLI                  on-demand re-index
   (python -m history hook <path>)         (python -m history backfill)  (CLI / API)
                │                               │                               │
                └───────────────────┬───────────┘                               │
                                    ▼                                           │
                          ┌───────────────────┐                                 │
                          │  history.indexer  │                                 │
                          │   • parse JSONL   │                                 │
                          │   • extract fields│ ◀───────────────────────────────┘
                          │   • Haiku summary │
                          │     (forced tool) │
                          │   • upsert SQLite │
                          └─────────┬─────────┘
                                    │
                                    ▼
                          ┌──────────────────────────────┐
                          │  ~/.claude/history.db        │
                          │   • sessions (metadata)      │
                          │   • sessions_fts (FTS5)      │
                          └─────────┬────────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
        history.search       periscope /history    python -m history search "..."
        (Python module)      (web UI, FTS+rerank)  (CLI, FTS5 ranked stdout)
              │                     │                     │
              └─────────────────────┼─────────────────────┘
                                    ▼
                        results → optional Haiku rerank
                                    │
                                    ▼
                        detail view ← reads JSONL on demand
                                    │
                                    ▼
                        "Resume" button → spawns tmux window
                        running `claude --resume <id>` in original cwd
```

### Key design choices

1. **JSONL is canonical.** SQLite is a derived index. Blow it away and rebuild
   from `~/.claude/projects/` at any time. Costs: one JSONL read per detail-view
   click and per resume.
2. **`history/` package has zero periscope-specific imports.** Inputs: JSONL
   paths. Outputs: SQLite rows + Haiku calls. Standalone-usable via CLI; the
   future "extract into its own repo" path is `mv history/ ../claude-history/`.
3. **Forced-tool-call for structured Haiku output.** Eliminates free-form JSON
   parsing failure modes. (Existing `auto_rename_*` paths in `server.py` migrate
   to the same pattern as a stretch cleanup.)
4. **No periscope-side state.** Web UI is stateless: `/api/history/search?q=...`
   → SQLite → JSON. The DB owns everything; periscope is a view layer.
5. **Re-indexing is content-aware.** Two version counters (`schema`,
   `mechanical`) plus a per-row `summary_input_hash` + `summary_model`
   mean that adding a new mechanical field doesn't trigger
   re-summarization, and a deliberate prompt/model change is a manual
   one-liner (`resummarize --all`).

## Repo layout

```
~/dev/periscope/
├── server.py                       # tmux dashboard — stays ~single-file
├── static/
│   ├── index.html                  # live dashboard
│   ├── history.html                # NEW — search page
│   ├── history.js                  # NEW
│   ├── history.css                 # NEW (or extend styles.css)
│   ├── app.js
│   ├── styles.css
│   └── vendor/
└── history/                        # NEW — the indexer + search engine
    ├── __init__.py
    ├── schema.sql                  # tables + FTS5 virtual table + triggers
    ├── db.py                       # connection + schema migrations
    ├── jsonl.py                    # JSONL parsing, event classification
    ├── extract.py                  # mechanical field extraction
    ├── summarize.py                # Haiku call with forced tool-use
    ├── indexer.py                  # orchestrates one-session pipeline
    ├── backfill.py                 # multi-session parallel index
    ├── search.py                   # FTS5 query + optional rerank
    ├── hook.py                     # SessionEnd entry point
    ├── cli.py                      # `python -m history <verb>`
    ├── __main__.py                 # dispatches to cli.py
    ├── tests/
    │   ├── test_jsonl.py
    │   ├── test_extract.py
    │   ├── test_indexer.py
    │   └── fixtures/
    │       ├── short_session.jsonl
    │       ├── long_session.jsonl
    │       └── interrupted_session.jsonl
    └── README.md
```

Periscope's `server.py` adds:
- `import history` near the top
- `/api/history/search`, `/api/history/session/{id}` routes
- `mode="resume"` + `resume_id` param on existing `/api/window/new`
- `/history` is served by the existing `StaticFiles` mount (static/history.html)

## Schema

```sql
-- One row per Claude Code session.
CREATE TABLE sessions (
  session_id           TEXT PRIMARY KEY,         -- UUID from JSONL filename
  jsonl_path           TEXT NOT NULL UNIQUE,     -- absolute path for read + resume
  project_path         TEXT NOT NULL,            -- decoded from cwd in events
  branch               TEXT,                     -- last gitBranch seen in events

  started_at           INTEGER NOT NULL,         -- unix ts (s) of first non-meta event
  ended_at             INTEGER NOT NULL,         -- unix ts of last event
  duration_s           INTEGER NOT NULL,

  user_msg_count       INTEGER NOT NULL,         -- excludes tool-result wrappers
  asst_msg_count       INTEGER NOT NULL,
  tool_use_count       INTEGER NOT NULL,
  was_interrupted      INTEGER NOT NULL DEFAULT 0,
  ended_cleanly        INTEGER NOT NULL DEFAULT 0,  -- last event = assistant reply

  -- Haiku-derived (NULL if API key absent / call failed)
  summary              TEXT,
  tags                 TEXT,                     -- comma-separated, lowercased

  -- Cache control for summary
  summary_input_hash   TEXT,                     -- sha256 of canonical summary input
  summary_model        TEXT,                     -- e.g. "claude-haiku-4-5"

  -- Mechanically extracted
  first_user_msg       TEXT,                     -- truncated 500 chars
  last_user_msg        TEXT,                     -- truncated 500 chars
  final_assistant_msg  TEXT,                     -- truncated ~1000 chars; outcome signal
  files_touched        TEXT,                     -- JSON array, distinct, by first-touched
  notable_cmds         TEXT,                     -- JSON array of distinctive Bash lines
  tool_use_counts      TEXT,                     -- JSON dict {"Bash": 47, "Edit": 12}

  -- Indexer metadata
  indexed_at           INTEGER NOT NULL,
  mechanical_version   INTEGER NOT NULL,         -- matches meta.mechanical_version at write
  source_mtime         INTEGER NOT NULL,
  source_size          INTEGER NOT NULL
);

CREATE INDEX idx_sessions_project ON sessions(project_path);
CREATE INDEX idx_sessions_branch  ON sessions(branch);
CREATE INDEX idx_sessions_started ON sessions(started_at DESC);

CREATE VIRTUAL TABLE sessions_fts USING fts5(
  session_id     UNINDEXED,
  summary,                -- highest weight at query time
  tags,
  first_user_msg,
  last_user_msg,
  final_assistant_msg,    -- outcome signal in JSONL data
  user_messages,          -- all user turns concatenated, deduplicated
  assistant_text,         -- all assistant prose (no tool_use blocks)
  files_touched,
  notable_cmds,
  tokenize = "porter unicode61"
);

-- The indexer is the sole writer to this DB. UPSERTs to `sessions` are
-- paired with an explicit DELETE + INSERT to `sessions_fts` inside the
-- same transaction. This trigger is a safety net for raw DELETEs (e.g.
-- `python -m history clean`) so FTS rows don't leak. Don't remove the
-- explicit DELETE in the indexer thinking the trigger will cover it —
-- FTS5 has no uniqueness constraint and you'll get duplicate rows.
CREATE TRIGGER sessions_fts_after_delete AFTER DELETE ON sessions BEGIN
  DELETE FROM sessions_fts WHERE session_id = old.session_id;
END;

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- Required keys: schema_version, mechanical_version,
-- haiku_model, last_full_scan_at
```

### Version counter semantics

Two counters, plus per-row `summary_model`:

| Counter | Bumped when | Effect of bump |
|---|---|---|
| `schema_version` | Tables or columns change | Migration runs; may require full re-extract |
| `mechanical_version` | Field-extraction logic changes | Re-extract all rows; summary reused if `summary_input_hash` unchanged. **Free** (no Haiku calls). |

Re-summarization is driven by content (not a version counter): a session is
re-summarized when `summary_input_hash` changes, when `summary IS NULL`, or
when the configured `meta.haiku_model` differs from the row's
`summary_model`. The "deliberate prompt change" path is `python -m history
resummarize --all`, which clears `summary_input_hash` for every row and
re-runs the indexer — costs ~$10 once, no extra column needed.

The `summary_input_hash` is `sha256` over a canonical representation of
`(first_user_msg, all_user_messages, final_assistant_msg, files_touched,
branch, notable_cmds[:20])`. On every (re)index of a session, the new hash
is computed and compared:

- New hash == stored hash AND `summary_model` matches `meta.haiku_model` →
  reuse existing summary, **no Haiku call**.
- Triviality filter triggers (see below) → store heuristic summary, **no
  Haiku call**.
- Otherwise → Haiku call.

### Triviality filter

A session is "trivial" if `user_msg_count < 2` OR `duration_s < 60`. Trivial
sessions are indexed (mechanical fields, FTS) but get a heuristic summary like:

> "Short session (2 messages, 18s) — first user message: foo bar baz…"

…and are excluded from the default `/history` search UI (toggle to include).
This both saves Haiku spend and prevents false-start sessions from polluting
results.

## Indexer pipeline

For one session (one JSONL file):

```
read JSONL line-by-line (streaming, tolerant of truncated lines)
   │
   ▼
classify events by top-level `type` field:
   • known meta types → permission-mode, attachment, file-history-snapshot,
     agent-name, custom-title, last-prompt, pr-link, queue-operation, system
     → skip from counts; the indexer extracts what it needs and moves on
   • user / assistant → message text + content blocks
   • unknown type → log and skip (Claude Code adds event types across
     releases; indexer must never crash on novel types)

   Note: `tool_use` and `tool_result` are content BLOCK types inside
   `assistant.message.content` and `user.message.content` arrays — not
   top-level event types. Extract by iterating the content array of each
   assistant/user message.
   │
   ▼
aggregate:
   • started_at, ended_at, duration_s, branch (last seen `gitBranch`)
   • user_msg_count, asst_msg_count, tool_use_count
   • was_interrupted (any line containing "Request interrupted by user")
   • ended_cleanly (last event is an assistant reply, not mid-tool)
   • first_user_msg, last_user_msg
   • final_assistant_msg: text content of the last assistant message,
     truncated to ~1000 chars. Replaces the original (broken) `recap_blocks`
     plan — `※ recap:` blocks are tmux-TUI-only chrome and don't appear in
     the JSONL. final_assistant_msg captures the "what just got done"
     signal that recap_blocks was supposed to provide.
   • user_messages (joined)
   • assistant_text (all assistant prose, no tool_use bodies)
   • files_touched: paths from Edit / Write / Read / NotebookEdit tool_use
     blocks, deduplicated, ordered by first touch
   • notable_cmds: Bash commands that are non-trivial
     (filter: length ≥ 20 OR contains '|' '>' '<' '/' OR matches verb regex;
     excludes `ls`, `pwd`, `cat`, single-word commands)
   • tool_use_counts: Counter() of tool_use block `name` values
   │
   ▼
compute summary_input_hash from (first_user_msg + user_messages
   + final_assistant_msg + files_touched + branch + notable_cmds[:20])
   │
   ├── triviality filter OR hash match (with summary_model match) →
   │     reuse / heuristic summary, no Haiku call
   │
   └── otherwise → build Haiku prompt → forced-tool-call → parse `input` dict
   │
   ▼
single SQLite transaction:
   • UPSERT into sessions (all columns)
   • DELETE FROM sessions_fts WHERE session_id = ?  (explicit; see schema comment)
   • INSERT INTO sessions_fts (...)
```

### Haiku call shape

```python
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

SYSTEM_PROMPT = (
    "You summarize Claude Code coding sessions for a search index. "
    "Output is consumed by a developer searching their own history later. "
    "Bias toward concrete specifics (file names, error messages, decisions) "
    "over generic descriptions. Always call save_session_summary."
)

msg = client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=512,
    system=[
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ],
    tools=[SUMMARIZE_TOOL],
    tool_choice={"type": "tool", "name": "save_session_summary"},
    messages=[{"role": "user", "content": prompt_body}],
)
for block in msg.content:
    if block.type == "tool_use":
        return block.input
```

**Why forced tool-use over `messages.parse()`?** Both produce structured
output reliably; `messages.parse()` is simpler for a single schema. We
stay with tool-use here because the rerank path (Search API §below) is
a natural tool-use case (the model is being asked to *act on* candidates),
and one consistent pattern across summarize + rerank is worth more than
the line or two saved on the summarize side. Verified against Anthropic
Python SDK current docs; `tool_choice={"type": "tool", "name": "..."}`
is the correct shape for forced invocation.

**Prompt caching.** The `system` prompt + `tools` schema are stable across
all 1,984 sessions. With `cache_control={"type": "ephemeral"}` on the
system message, every backfill worker after the first lands a cache hit
on the prefix (~0.1× input cost) for 5 minutes. With the
`Semaphore(5)`-bounded backfill, this cuts ~40–60% off the $10 backfill
spend. Stable content goes in `system` and `tools`; per-session content
is the `messages[0].content` body.

The prompt body (the `messages[0].content`) is:

```
SESSION:
  project: {project_path}
  branch: {branch}
  duration: {duration_minutes} min
  files touched: {files_touched_first_15}
  notable commands: {notable_cmds_first_10}

USER MESSAGES (concatenated, may be truncated to ~6k tokens):
{user_messages_truncated}

FINAL ASSISTANT MESSAGE (outcome signal, truncated):
{final_assistant_msg}

Call save_session_summary with concrete details from this session.
```

Note: assistant prose mid-conversation is **not** included. User messages
+ final assistant message + files + commands capture intent + outcome at
~10× lower token cost than the full transcript. If summaries feel thin in
practice, we can add a sampling of assistant prose; not a v1 requirement.

### Live-session detection

Skip during scans / backfill:
- Last event's `timestamp` is within the last 5 minutes, OR
- JSONL `mtime` is within the last 60 seconds.

SessionEnd hook indexes the specific session unconditionally — but is still
subject to the `summary_input_hash` short-circuit (no Haiku call if content
hasn't moved).

### Concurrency

- SQLite in WAL mode. One writer at a time, many readers.
- Hook fires serialize through a process-level lock file (`~/.claude/history.db.lock`).
- Backfill uses `asyncio` with a `Semaphore(5)` for concurrent Haiku calls;
  UPSERTs are serialized via the writer lock.
- Web UI search is read-only — never contends.

## Search API

### Wire format

```
GET /api/history/search
  ?q=<query>                       required
  &project=<project_path>          optional, exact match
  &branch=<branch>                 optional, exact match
  &since=<unix_ts>                 optional
  &until=<unix_ts>                 optional
  &include_trivial=true            optional, default false
  &rerank=true                     optional, default true
  &limit=50                        optional, default 50

Response:
  {
    "query": "...",
    "rerank_used": true,
    "fts_candidates": 18,          how many FTS matched before rerank
    "took_ms": 942,
    "results": [
      {
        "session_id": "...",
        "project_path": "...",
        "branch": "...",
        "started_at": 1763123456,
        "duration_s": 2820,
        "summary": "...",
        "tags": ["...", "..."],
        "first_user_msg": "...",
        "files_touched": ["...", "..."],
        "rerank_reason": "...",      present if rerank=true
        "rank": 1,
        "fts_rank": 3,               original FTS rank
        "url": "/history#s=<session_id>"
      },
      ...
    ]
  }
```

### Server flow

```
1. parse query params; validate q nonempty
2. SQL: SELECT session_id FROM sessions_fts WHERE sessions_fts MATCH ?q
       JOIN metadata filters (project, branch, time window, triviality)
       ORDER BY bm25(sessions_fts) LIMIT 20
3. fetch the matching rows (full metadata) from sessions
4. if rerank=true AND ANTHROPIC_API_KEY present AND fts_candidates > 1:
     bundle (query, [(session_id, summary, tags, first_user_msg)]) into Haiku
     prompt with forced tool-use that returns
       {results: [{session_id, reason, score}]}
     reorder candidates by score
5. trim to limit, return JSON
```

The rerank tool schema:

```python
RERANK_TOOL = {
    "name": "rank_search_results",
    "description": "Reorder candidate Claude Code sessions by relevance to the user's query.",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "reason": {
                            "type": "string",
                            "description": "One sentence explaining why this session matches the query.",
                        },
                        "score": {
                            "type": "number",
                            "description": "0.0 (irrelevant) to 1.0 (perfect match).",
                        },
                    },
                    "required": ["session_id", "reason", "score"],
                },
            },
        },
        "required": ["results"],
    },
}
```

### Other endpoints

```
GET /api/history/session/{session_id}
  → reads jsonl_path on demand, parses into:
    {
      "session_id": "...",
      "session": <session row>,
      "messages": [
        {"role": "user", "ts": ..., "text": "..."},
        {"role": "assistant", "ts": ..., "text": "...",
         "tool_uses": [{"name": "Bash", "input": {...}}, ...]},
        ...
      ]
    }
  Errors: 404 if session not in DB; 410 if JSONL missing on disk.

POST /api/window/new   (extended)
  query params: session, mode, resume_id?
  mode="resume" + resume_id  → tmux send-keys "claude --resume <id>"
  All other fields same as today.
```

## Search UX

A new page at `/history`. Top nav strip lets you toggle between `/` (live) and
`/history` (search).

### Layout

Split panel, like an email client:

- **Left (~40%)**: search input + filters + result cards (50 max, "load more"
  paginates).
- **Right (~60%)**: selected session detail. Empty state until a result is
  clicked.

```
┌────────────────────────────────────────────────────────────────────────────┐
│  periscope ·  live  |  history                                              │
├────────────────────────────────────────────────────────────────────────────┤
│  [_________________________________]  Enter           ⚙ rerank ✓            │
│  filters:  project ▾   branch ▾   when ▾   ☐ include trivial                │
├──────────────────────────────────────┬─────────────────────────────────────┤
│  ┌────────────────────────────────┐  │  selected session                    │
│  │ 📁 fdy · cohorts               │  │  ──────────────                       │
│  │ 2026-04-13 · 21:27 · 47 min    │  │  user (21:28):                       │
│  │                                 │  │    investigating slow cohort query   │
│  │ Investigated slow cohort-       │  │                                       │
│  │ resolver query (P99 12s)…       │  │  assistant:                          │
│  │ tags: perf · pg · cohorts       │  │    ran EXPLAIN ANALYZE on …          │
│  │ files: resolve_cohort.py +2     │  │    [tool: Bash, 3 cmds] ▸            │
│  │ branch: feat/cohort-perf #1234  │  │                                       │
│  │ rerank: matches "slow cohort"   │  │  user (21:35):                       │
│  │ [▶ resume]  [open in new tab]   │  │    …                                  │
│  └────────────────────────────────┘  │                                       │
│  ┌────────────────────────────────┐  │                                       │
│  │ 📁 periscope                    │  │                                       │
│  │ …                                │  │                                       │
│  └────────────────────────────────┘  │                                       │
└──────────────────────────────────────┴─────────────────────────────────────┘
```

### Result card content

```
date · project · duration                       ← header
{summary || first_user_msg italicized}          ← lead text
tags                                             ← chip row
files (truncated, "+N more")
branch · PR badge (if known)
rerank reason (when rerank=true)
[▶ resume]  [open]                              ← actions
```

If `summary IS NULL`, the lead shows `first_user_msg` italicized with a
"no summary yet" marker. Still searchable, still resumable.

### Detail view (right pane)

Reads `/api/history/session/{id}` lazily on click. Renders as a Claude-style
chat:

- User and assistant messages alternated with timestamps.
- Tool uses collapsed by default (`[Bash, 3 cmds] ▸`). Click to expand each
  tool's input + result.
- The current search query (if any) highlights matched terms in the body.
- "Resume in new pane" button pinned to the top of the detail pane.

### Search behavior

- Single text input. **Enter to submit** — not type-as-you-search. Semantic
  queries don't benefit from streaming, and FTS turnaround is well under 100ms.
- `?q=...&rerank=...&project=...` reflected in the URL — bookmarkable,
  back-button-friendly.
- Result limit 50 default. "Load more" paginates.

### Rerank default = ON

- The headline feature. FTS-only ranks by keyword; rerank actually answers
  semantic queries.
- ~1s latency vs ~10ms FTS-only. ~$0.001 per query.
- Checkbox to disable for fast keyword scanning.
- Disabled automatically (no UI nag) when `ANTHROPIC_API_KEY` is missing.

## Resume flow

```
[▶ resume] click
   │
   ▼
POST /api/window/new?session=history&mode=resume&resume_id=<session_id>
   │
   ▼
server:
  - look up jsonl_path + project_path from sessions row
  - if jsonl_path missing on disk         → {ok:false, error:"jsonl deleted"}
  - if project_path missing on disk       → {ok:false, error:"cwd no longer exists: ..."}
  - if mtime(jsonl_path) < now - 60s      → {ok:false, error:"session looks live; wait"}
  - if _resuming[session_id] exists       → {ok:false, error:"already resumed in <target>",
                                              existing_target: ...}
  - ensure tmux session "history" exists; create -d if not (cwd=project_path)
  - new-window in "history" (cwd=project_path), capture window_index
  - sleep 100ms (let shell rc settle, mirrors mode="claude")
  - tmux send-keys "claude --resume <session_id>" Enter
  - _resuming[session_id] = {target, started_at: now}
  - note_focus(target)
  - return {ok:true, target:"history:N"}
   │
   ▼
frontend:
  - on ok: toast "resumed in history:N  [switch to it]" (link → /api/focus)
  - on "already resumed": toast "already in <target>  [switch to it]"
  - on "jsonl deleted" / "cwd missing": offer one-click fallback
```

The `_resuming` dict is purged lazily: any time `/api/state` finds that a
target it knew about is no longer in the tmux `list-windows` output, drop
the matching `_resuming` entry. Plus a hard 30-minute expiry as a backstop.

### Resume semantics (verified)

`claude --resume <id>` **appends** to the existing JSONL — it does NOT
fork a new file. One JSONL = one logical session that grows across every
resume. The newly-appended records carry the same `sessionId` and their
`parentUuid` chains continue off the last UUID of the pre-existing
conversation. Even just launching `claude --resume <id>` and immediately
`/exit`-ing mutates the file (Claude auto-injects `/clear` + `/exit`
events) — so the resume button is not a no-op preview.

This means **concurrent resume of the same session_id is a real
corruption risk** (two processes appending to one file with no
coordination), not a theoretical edge case.

### Resume edge cases

- **Original cwd missing.** Toast offers "resume in $HOME instead" as a
  one-click fallback that re-posts with `cwd_override=$HOME`. Note: the
  `cwd` field in appended JSONL events reflects the shell's actual cwd
  at resume time, not the original — so a $HOME-override resume will
  write `cwd: $HOME` into the appended events. Tolerable; documented.
- **JSONL deleted from disk.** DB row marked stale; search hides it
  until `python -m history clean` removes the row.
- **JSONL currently being written to** (`mtime < 60s ago` — matches the
  indexer's live-session heuristic). Refuse the resume with a clear
  toast: "this session looks live; pick a different one or wait."
- **Already-resumed-by-this-server.** Periscope keeps a process-local
  `_resuming: dict[session_id, {target, started_at}]` map. On resume
  request, check the map; if there's an entry younger than 30 minutes,
  refuse with "already resumed in `<target>` — switch to it instead"
  (with a one-click switch via `/api/focus`). Entry cleared on tmux
  window kill (detected lazily on next `/api/state` poll) or after 30
  min, whichever comes first. The 30-min expiry catches "the user
  abandoned the resumed window in the background and wants to resume
  again"; the mtime guard catches "Claude is actively writing right
  now"; together they make concurrent appends very unlikely.
- **`claude --resume` itself fails** (Claude CLI errors). The shell
  shows the error in the new pane. We don't try to detect this
  server-side — the user can see it in the pane.

## Failure modes

| Failure | Behavior |
|---|---|
| `ANTHROPIC_API_KEY` missing | Indexer skips Haiku; row stored with mechanical fields. Rerank silently disabled in search. FTS-only path always works. |
| Haiku 429 / 5xx | Exponential backoff (1s/2s/4s/8s), then leave `summary = NULL`. Logged. `resummarize --missing` retries later. |
| Haiku tool call fails / invalid schema | One retry. Second failure → `summary = NULL` + log. Almost never happens with forced tool choice. |
| Truncated JSONL line | Skip line, continue. If <50% of lines parse, log warning, skip the whole file. |
| Live JSONL during scan | Skip if last event < 5 min ago OR mtime < 60s. SessionEnd handles it once quiet. |
| JSONL deleted on disk | Row marked stale on next access; `python -m history clean` removes orphans. Search returns stale rows until cleaned. |
| Original cwd missing on resume | Toast with one-click "$HOME instead" fallback. |
| JSONL currently being written to | Resume endpoint refuses (`mtime < 60s ago`); toast suggests waiting. |
| Already-resumed-by-this-server | Resume endpoint refuses; toast offers one-click switch to the existing resumed window. |
| Resume "preview" mutates JSONL | Acknowledged. Even `claude --resume <id>` + immediate `/exit` appends ~40KB of `/clear`+`/exit` cruft. Indexer sees mtime change and re-extracts; if `final_assistant_msg` changes (it can if the auto-injected events get there), a re-summary may fire (~$0.005). Acceptable. |
| DB corruption | Source-of-truth = JSONL pays off: backup, drop, rebuild. ~30 min of backfill. |
| Trivial sessions | Indexed with heuristic summary; excluded from default UI; toggle to include. |

## CLI surface

```
python -m history backfill [--workers 5] [--since YYYY-MM-DD] [--dry-run]
    One-shot index of ~/.claude/projects/. Idempotent: skips rows already
    fresh via input-hash check. Resumes cleanly after Ctrl-C.

python -m history hook <jsonl-path>
    SessionEnd hook entry point. Indexes one session, idempotent.

python -m history search <query> [--rerank] [--limit 10] [--project PATH] [--branch NAME]
    CLI search, tab-separated output (date, id, project, summary).
    Pipes well through fzf or jq.

python -m history reindex --all
    Bump mechanical_version; re-extract every row without re-summarizing. Free.

python -m history resummarize --missing | --all
    Re-run Haiku. --missing only hits NULL-summary rows. --all clears
    summary_input_hash on every row so the next index pass re-summarizes
    them all (deliberate, ~$10 before prompt-cache savings).

python -m history stats
    Row counts, summarized vs not, estimated total Haiku spend, last scan time.

python -m history clean
    Remove DB rows whose JSONL is gone from disk.
```

## Hook installation

Claude Code's SessionEnd hooks pipe a JSON event over **stdin** to the
configured command — the hook reads `json.load(sys.stdin)` and extracts
`transcript_path` (snake_case). This is verifiable against the existing
hook at `~/.claude/hooks/index-conversation.py`, which reads stdin and
pulls `transcript_path` from the payload.

```jsonc
// ~/.claude/settings.json
{
  "hooks": {
    "SessionEnd": [
      // existing index-conversation hook stays alongside
      { "command": "python -m history hook" }
    ]
  }
}
```

The `python -m history hook` entry point:

```python
def cli_hook() -> None:
    payload = json.load(sys.stdin)
    transcript_path = payload.get("transcript_path")
    if not transcript_path or not os.path.isfile(transcript_path):
        return  # nothing to index
    indexer.index_one(transcript_path)
```

The existing `conversation-index.md` hook stays installed alongside the
new hook during shakedown. After ~2 weeks of trusting the new system,
the old hook is removed manually. The two hooks write to different files
and don't interact.

### Phase 0 detail: `auto_rename_*` migration

The tool schema for the `auto_rename_*` migration (Phase 0 in §Phases):

```python
RENAME_TOOL = {
    "name": "rename_windows",
    "description": "Rename one or more tmux windows.",
    "input_schema": {
        "type": "object",
        "properties": {
            "renames": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "name":  {"type": "string"},
                    },
                    "required": ["index", "name"],
                },
            },
        },
        "required": ["renames"],
    },
}
```

The existing `claude_complete()` helper in `server.py:612-622` returns
free-form text; replace with a sibling that returns the tool input dict
directly. Same model, same client singleton, same `ANTHROPIC_API_KEY`
loading. Existing callers `auto_rename_session` (~line 661) and
`auto_rename_window` (~line 732) destructure `{renames: [{index, name},
...]}` instead of `json.loads(cleaned)` of free-form JSON; remove the
code-fence stripping (`if cleaned.startswith("```"): ...`) and the
`JSONDecodeError` branch entirely.

## First-time UX

User clones periscope, runs `uv run server.py`, visits `/history`:

> No sessions indexed yet. Run `python -m history backfill` to index your
> ~1,984 Claude conversations (~13 min, ~$10 in Haiku calls). Search becomes
> available as sessions are indexed.

No auto-trigger from page load — explicit CLI keeps the cost surface
predictable. `backfill` is interrupt-safe and resumable, so even running it
once in the foreground is fine.

## Costs

| Operation | Frequency | Cost |
|---|---|---|
| Backfill (1,984 sessions × Haiku call) | One-time | ~$4–6 (after prompt caching) |
| SessionEnd indexing | Per Claude session ended | ~$0.005 |
| Search rerank | Per search with `rerank=true` | ~$0.001 |
| Search FTS-only | Per search | $0 |
| Re-summarize (manual `resummarize --all`) | Rare | ~$4–6 per 2k sessions |
| Re-extract (`mechanical_version` bump) | Manual | $0 |

Per-day ongoing: low single-digit cents in normal use. Backfill cost drops
40–60% vs naive pricing because the system prompt + tool schema land in the
Haiku prompt cache across the `Semaphore(5)`-bounded worker pool. The
Anthropic Batches API would discount another 50% on top with a ≤24h SLA,
but for a $4–6 one-shot expense it's not worth the async complexity in v1.

## Tests

```
history/tests/
├── test_jsonl.py         # event classification, malformed-line tolerance
├── test_extract.py       # files_touched / notable_cmds / recap parsing
├── test_indexer.py       # end-to-end with fixture JSONLs + mocked Haiku
├── test_search.py        # FTS queries against a seeded DB
├── test_rerank.py        # rerank flow with mocked Haiku
├── test_cli.py           # `python -m history <verb>` smoke tests
└── fixtures/
    ├── short_session.jsonl       # 2 messages, trivial
    ├── long_session.jsonl        # 200+ messages, recaps, tool uses
    ├── interrupted_session.jsonl # "Request interrupted by user"
    └── corrupted_session.jsonl   # truncated lines mid-file
```

No live-network tests. Haiku calls are mocked in `summarize.py` via a small
abstraction that swaps in a fixture-driven `FakeClient`.

## Open implementation questions (deferred)

These don't change the design but need verification during implementation:

- **`auto_rename_*` token cost delta** after the Phase 0 migration. Haiku
  adds ~100 tokens for the tool schema; should be a wash but worth a
  before/after spot-check in case Anthropic accounting changed.
- **Should `final_assistant_msg` filter out `/clear` and `/exit` cruft?**
  When a user "previews" a session by clicking resume and immediately
  exiting, the appended `<command-name>/exit</command-name>` and
  `<local-command-stdout>See ya!</local-command-stdout>` records become
  the new tail of the JSONL. If `final_assistant_msg` blindly takes the
  last assistant message text, these previews shift it. Worth a small
  filter ("if the last assistant message is just a `/clear`-style
  command echo, walk back to the previous substantive one") during
  Phase A implementation.

## Out of scope (v2 candidates)

- Cross-link from live dashboard cards: "see related past sessions on this
  branch". Small, additive, deferred.
- Embedding-based retrieval (would require a non-Anthropic provider).
- Multi-user / multi-machine sync.
- Sub-agent transcript indexing with parentUuid threading.
- Auto-cluster sessions into topics over time.
- Cost / token timeline charts.
- Day-in-review narrative summaries.

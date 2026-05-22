# Faceted history search — design

Date: 2026-05-21

## Context

periscope's `history/` package indexes every Claude Code transcript under
`~/.claude/projects/` into `~/.claude/history.db` (SQLite + FTS5), and
serves a keyword-search UI at `/history`. The per-session summarizer
(`history/summarize.py`) already calls Haiku with a forced `save_session_
summary` tool that emits a `summary` and 3-5 free-text `tags`.

Two facts found while scoping this work:

1. **The `history` SessionEnd hook is not installed.** The SessionEnd
   hook present in `~/.claude/settings.json` runs `index-conversation.py`
   (a separate conversation-index feature); `python -m history hook` was
   never wired up.
2. **`history.db` is therefore badly stale** — 359 sessions, all from on
   or before 2026-05-13, against **1581 transcripts on disk**. It was
   backfilled once when the feature was built and frozen since.
3. **Most transcripts on disk are not real sessions.** periscope's
   `/usage` scraper (`usage.py:scrape_usage_via_tmux`) launches a
   throwaway `claude` in periscope's own cwd per scrape. **1301 of the
   1581 (82%)** are these — 0 real user turns, 0 assistant turns,
   launched-and-killed. The real corpus is ≈ **280 sessions**.

This work makes history search **faceted** — filterable by structured,
AI-derived classifications, not just keyword — and along the way fixes
the staleness (full backfill + install the hook).

## Goals

- Add four structured, low-cardinality facets to each session, derived by
  extending the existing summarizer (one Haiku call per session, as now).
- A `/history` UI that filters by those facets, combined with keyword search.
- Bring `history.db` current (~280 real sessions) and keep it current via
  the hook — excluding periscope's `/usage` scrape transcripts.
- Spend a chunk of the expiring Claude credits on the one-shot
  classification (≈$12-15 on Sonnet over the real corpus).

## Non-goals

- The Anthropic Batches API — the synchronous parallel `backfill`/
  `resummarize` path already exists and is proven (~13 min for the
  original run); wiring batch is more work than $18 justifies.
- A separate classification pass — the facets ride the existing
  `save_session_summary` tool call, so the hook gets them for free.
- Putting the new columns in `sessions_fts` — FTS5 can't `ALTER`, and
  facets are filtered structurally (`WHERE`), not full-text.
- Fixing periscope's `/usage` scraper at the source (it litters
  `~/.claude/projects/` with a throwaway transcript per scrape) — the
  indexer filters these out; stopping them being written is a separate
  periscope change.

## Phasing

The credits expire **May 23**, but only the classification run spends
them. The UI does not. So the work splits:

- **Phase 1 — deadline-critical.** Schema migration, extend the
  summarizer, install the hook, dry-run the cost, run the full backfill +
  reclassify. After Phase 1 the credits are spent and `history.db` is
  complete, faceted, and staying fresh.
- **Phase 2 — no deadline.** The `/history` filter UI + search-API
  facet params. Reads data already in the DB; lands whenever.

## The facets

Four new fields per session:

| Field | Type | Values |
|---|---|---|
| `outcome` | controlled enum | `shipped` · `partial` · `abandoned` · `explored` · `blocked` |
| `category` | controlled enum | `feature` · `bugfix` · `refactor` · `debugging` · `research` · `ops` · `docs` · `review` |
| `notable` | 0/1 flag | substantial/revisit-worthy vs routine |
| `topics` | JSON array | 2-4 canonicalized tags (lowercase, deduped form) |

`project`, `branch`, `duration_s`, and the message counts are already
structured columns — the UI filters on those too, for free, no AI.

The existing free-text `tags` column stays as-is (it feeds FTS keyword
search). `topics` is the new, lower-cardinality facet layer — the
summarizer is instructed to canonicalize (lowercase, singular, prefer the
project name or a broad area term, max 4). Frequency in the UI does the
rest; this is not a closed vocabulary.

## Phase 1

### Schema

`db.py:apply_schema` currently only runs `schema.sql` (`CREATE TABLE IF
NOT EXISTS` — a no-op against an existing table). There is no real
migration step. Add one:

- Add the four columns to `schema.sql` (for fresh DBs).
- In `apply_schema`, after `executescript`, read `PRAGMA table_info(sessions)`
  and `ALTER TABLE sessions ADD COLUMN` each of the four that is missing
  (SQLite has no `ADD COLUMN IF NOT EXISTS`). Bump `SCHEMA_VERSION` to 2.

### The classifier — extend `summarize.py`

- `SUMMARIZE_TOOL.input_schema` gains `outcome`, `category`, `notable`,
  `topics`, with `enum` constraints on `outcome` and `category`.
- `SUMMARIZE_SYSTEM_PROMPT` + `build_summary_prompt` explain each facet
  and what evidence to weigh (`final_assistant_msg` is a strong outcome
  signal; commands/files hint at category).
- `SummaryResult` gains the four fields; `call_summarizer`'s tool-result
  parsing reads and normalizes them (unknown enum value → `None`).
- `indexer.py`'s write path persists the four columns.

A failed/missing classification stores `NULL` for the facet columns — the
UI treats a `NULL` facet as "unclassified", never crashes.

### Filter `/usage` scrape sessions

`backfill`/`indexer.py` skip transcripts that are periscope `/usage`
scrapes — identified by **`asst_msg_count == 0`** (a session where Claude
never produced an assistant turn is not real work; this cleanly catches
all 1301 scrape transcripts). Without the filter the index is 82% junk,
faceted search is meaningless, and the SessionEnd hook adds a fresh junk
row every few minutes. The same predicate runs as a one-time cleanup over
the existing 359 rows to drop any scrape sessions already indexed.

### Install the hook

Add `python -m history hook` as a **second** SessionEnd entry in
`~/.claude/settings.json` (Claude Code runs every hook in the array;
`index-conversation.py` stays). The invocation must run from the
periscope repo with `ANTHROPIC_API_KEY` available — the `history` package
gains a `load_dotenv()` of the repo `.env` (mirroring `server.py`) so the
hook self-sources the key. Exact wrapper form (a `~/.claude/hooks/`
shim vs. an inline `cd … && uv run …`) is settled in the plan.

### The burn

After the summarizer is extended, the scrape filter is in place, and both
are tested:

1. **Dry-run cost gate.** Classify ~50 real sessions on Sonnet, measure
   `usage.input_tokens` / `output_tokens` from the responses, extrapolate
   to the ~280 real corpus. Reference: the original Haiku run was ~$4-6
   for 359 sessions (~$0.013/session); the corpus is now small enough that
   **Sonnet fits** — ~280 × ≈3× ≈ **$12-15**, within budget and better
   classification on the corpus that matters. If the dry-run projects
   over ~$18, fall back to Haiku.
2. **Full run.** `backfill` indexes + classifies every real session not
   yet in the DB (scrape transcripts excluded by the filter above);
   `resummarize --all --model <sonnet>` re-classifies the rows already
   present. `resummarize` takes `--model` (add the flag if absent). Net:
   every real session carries the facets; no junk session is classified.

## Phase 2

### Search API — `history/search.py`

`/api/history/search` gains optional filters `outcome`, `category`,
`notable`, `topic` → additional `WHERE` clauses on the `sessions` table,
combined (AND) with the existing FTS query. A facet-count query (distinct
value → count, respecting the current keyword query) backs the UI chips —
exposed via `stats` or a small `/api/history/facets` endpoint.

### The `/history` UI — `static/history.{js,html}` + `history-styles.css`

A filter row above the results: outcome chips, category chips, a "notable
only" toggle, and the top-N `topics` as chips — each showing a count,
click to toggle, combining with the keyword box. An active filter set is
reflected in the results list. Unclassified sessions (`NULL` facets) are
included unless a facet filter excludes them.

## Ongoing cost

With the hook installed: one Haiku `save_session_summary` call per
session you finish — the same call that already produces the summary,
now returning four more small fields. Fractions of a cent each; ~cents/
month. No periscope-runtime cost (the hook is a Claude Code SessionEnd
event, not a periscope process).

## Testing

`history/tests/` (mirrors the package):
- `summarize`: the tool schema includes the four fields; `call_summarizer`
  parses a well-formed tool result into a full `SummaryResult`; an unknown
  `outcome`/`category` enum value normalizes to `None`; a missing field
  does not crash.
- `db`: the migration adds the four columns to a v1 DB and is idempotent
  on a v2 DB; a fresh DB gets them from `schema.sql`.
- `indexer`: a classified `SummaryResult` round-trips through the write
  path into the four columns; a transcript with `asst_msg_count == 0`
  (a `/usage` scrape) is skipped — not indexed.
- `search`: each facet filter narrows results; filters combine (AND) with
  each other and with the FTS query; the facet-count query returns
  correct counts.

Run with `uv run pytest -q` (and the existing `history` test layout).

## Files

`history/`: `schema.sql`, `db.py`, `summarize.py`, `indexer.py`,
`search.py`, `cli.py`, `__main__.py`/`hook.py` (the `load_dotenv`),
`tests/`. `static/`: `history.js`, `history.html`, `history-styles.css`.
Plus `~/.claude/settings.json` (hook install — user config, not a repo
file).

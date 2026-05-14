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

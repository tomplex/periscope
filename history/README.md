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

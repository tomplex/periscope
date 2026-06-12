# Narrator rail UX: model-composed rail fragment + full-width status row

**Date:** 2026-06-12
**Status:** Approved (design discussed with Tom; execution delegated as
autonomous)

## Problem

The rail gives the status line ~25 visible characters, but the narrator
composes a ≤72-char sentence and CSS truncates it mid-thought — users see
2-3 words. Worse, since auto-rename keeps window names semantically
current, the visible fragment usually *repeats the name's concept*
("analyzing figv2 target…" under `f2-post-deploy`) and the
differentiating information (the current action) is cut off.

## What this builds

**A. Write for the medium.** The narrator's existing Haiku call returns a
third field:

```json
{"status": "...", "rail": "comparing lookup hit rates", "rename": null}
```

- `rail` rules (in-prompt): ≤28 chars; leads with the current action;
  MUST NOT repeat the window name's concept (the name is rendered
  directly above it); no trailing ellipsis or punctuation; lowercase.
- Validation (`parse_response`): `rail` is optional. Missing, non-string,
  empty, or >28 chars → treated as absent (status/rename still accepted —
  a bad rail must not discard a good status).
- Storage: `pane_status` gains a nullable `rail` TEXT column.
  **Migration required:** the table exists in the prod db with rows;
  `_SCHEMA`'s `CREATE TABLE IF NOT EXISTS` will not add a column. Use the
  ALTER TABLE pattern (sqlite has no `ADD COLUMN IF NOT EXISTS`; probe
  via `PRAGMA table_info` or try/except `OperationalError`) — follow
  whatever migration precedent exists in `activity.py`/`store.py`, or
  establish the probe-then-ALTER one next to `_SCHEMA`.
- API: `pane_status_lines()` returns the rail; the `/api/state` merge
  adds `status_rail` alongside `status_line`/`status_at` (only when a
  non-empty rail exists — absent key otherwise, same contract as
  `status_line`).
- UI fallback chain: the rail row renders `status_rail || status_line`.
  Rows generated before this change (or with rejected rails) keep
  today's behavior automatically.

**B. Full-width status row.** The status currently lives inside the
label column and truncates at the state-dot gutter. Restructure the rail
row so the status is its own line spanning the full rail width beneath
the name+dot row (the dot stays aligned with the name). Pure
JSX/CSS; the stale-dim behavior (`STATUS_STALE_S`) and hover title are
unchanged.

## Out of scope

- Categorical verb chips (deferred to the babysitter design).
- Any change to rename behavior, cadence, cost (same single Haiku call),
  or the detail header (it keeps the full `status_line` + tooltip).

## Failure modes

- Old narrator rows without `rail`: UI falls back to truncated
  `status_line` (exactly today's rendering).
- Model omits/overflows `rail`: dropped by validation; status still
  lands; next generation retries naturally.
- Migration runs on a db that already has the column (dev/prod skew):
  probe-or-except makes it idempotent.

## Testing

- `parse_response`: rail accepted at 28 chars; rejected at 29 / empty /
  non-string — and the rejection must not discard status or rename.
- Prompt builder: rail rules present, references the current window name
  in the no-overlap instruction.
- CRUD + migration: upsert/read round-trips rail; opening a db whose
  `pane_status` predates the column gets it added (create old-shape
  table in a temp db, run migration, assert column exists and rows
  survive); migration idempotent on a current-shape db.
- Route: merged payload carries `status_rail` only when present.
- Frontend: browser-verified (project convention), plus bundle-grep for
  the new class names as the headless stand-in.

## Settled implementation facts (from the narrator project — do not
re-litigate)

- The tick/generation path, cooldown stamps, TOCTOU recheck, and
  session-id-first regeneration are unchanged; `rail` rides through
  `_generate`'s existing upsert.
- Lifespan tests mock `activity.run_worker`; keep it that way (live
  ticks fired from pytest before).
- Prod log is `~/.config/periscope/periscope-8765.log`.
- Prod db has live `pane_status` rows — the migration is exercised on
  deploy, not hypothetical.

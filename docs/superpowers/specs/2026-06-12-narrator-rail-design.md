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
- Storage: `pane_status` gains a nullable `rail` TEXT column, appended
  LAST everywhere (ALTER TABLE appends physically last; all reads SELECT
  by explicit column list, so only logical order matters).
  **Migration required:** the table exists in the prod db with rows;
  `_SCHEMA`'s `CREATE TABLE IF NOT EXISTS` will not add a column. Copy
  `history/db.py`'s probe-then-ALTER pattern (`PRAGMA table_info` → `if
  name not in have: ALTER TABLE ... ADD COLUMN`, provably idempotent)
  into `activity._conn()` right after `executescript(_SCHEMA)`, with the
  `history/tests/test_db.py` test shape (fabricate old-shape table,
  migrate, assert column + row survival, re-run for idempotence).
- **The lockstep set** (every site that must change together, all
  appending `rail` last):
  - `activity.py`: `_SCHEMA` CREATE TABLE; `_PANE_STATUS_COLS`;
    `PaneStatusRow` (new field `rail: str | None = None` — defaulted so
    existing keyword constructions and equality tests stay valid);
    `upsert_pane_status` placeholder count + `DO UPDATE SET` + values
    tuple; **`stamp_pane_rename`** — it interpolates `_PANE_STATUS_COLS`
    but hardcodes a 7-slot VALUES literal; growing the column list
    without adding the 8th value makes every manual/auto rename throw
    `OperationalError` (existing stamp tests gain a rail assertion);
    `pane_status_lines` (see API below).
  - `narrator.py`: `NarratorResult` gains `rail: str | None = None`;
    `parse_response` validates it (same strip-then-length treatment as
    `status`); `build_narrator_prompt` adds the rail rules;
    `_generate` passes `rail=result.rail` into the row (without this the
    column exists but is never written).
- API: `pane_status_lines()` returns a 3-tuple `(status, generated_at,
  rail)`. Exactly two consumers change: the `/api/state` merge's tuple
  unpack in `routes/state.py` (adds `status_rail` only when a non-empty
  rail exists — absent key otherwise, same contract as `status_line`),
  and `tests/test_activity.py`'s exact-dict-equality assertion on the
  return shape.
- UI fallback chain: the rail row renders `status_rail || status_line`.
  Rows generated before this change (or with rejected rails) keep
  today's behavior automatically.

**B. Full-width status row.** The status currently lives inside the
label column and truncates at the state-dot gutter. Restructure the rail
row so the status is its own line beneath the name+dot row (the dot
stays aligned with the name). **Geometry, decided here so the
implementer doesn't discover it mid-CSS:** "full width" means the full
label-column width *past the dot gutter* but **inside the tree-guide
indent** — the status must not render under the child-row vertical
guides. The child-row connector stubs/terminators in styles.css assume
single-line rows (`top:50%` / `bottom:50%`); pin them to the name line's
center via a fixed top offset (half the name line-height) instead of
percentages, so two-line rows don't skew the tree. The restructure needs
a nested flex wrapper (name row keeps burn/pin/dot/close flex order and
`margin-left:auto`); the `.rail-row:hover .rail-close` /
`.child-row:hover .rail-pin` reveal selectors and the row-level
draggable/onClick must keep working. Stale-dim (`STATUS_STALE_S`) and
hover title unchanged. The detail header is untouched (it keeps the full
`status_line`).

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

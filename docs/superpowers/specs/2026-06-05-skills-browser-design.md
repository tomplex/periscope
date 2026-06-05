# Skills browser — design

**Date:** 2026-06-05
**Status:** Design, pending review

## Problem

Tom authors skills under `~/.claude/skills/` (8 today: feature-store,
feature-store-qa, grill-me, k8s-job-logs, missions, project-skill,
resource-update-graph, writing-as-tom). To remember what a skill does or check
its content, he has to dig through the filesystem. He wants a scannable,
read-only reference inside periscope — the dashboard he already has open.

## Scope

**In scope:** Browse and read his *authored* skills (`~/.claude/skills/*/SKILL.md`
only). List them in the rail; render the selected one in the detail pane as
markdown.

**Explicitly out of scope (YAGNI):**
- Editing, creating, deleting, renaming skills — read-only.
- Plugin / marketplace skills (the ~72 vendored SKILL.md files). Authored skills
  only.
- Live file-watching. Refresh is boot + a manual button.

## Why this shape

Periscope already has every primitive this needs, so the feature is small:

- The rail renders **out-of-tree sections** (`NEEDS YOU`, `PINNED`, `ACTIVITY`)
  using `attn-row` classes, outside the draggable repo→worktree→pane tree
  (`split/AttentionSections.jsx`). A `SKILLS` section drops in alongside with no
  impact on the tree / drag / prefs machinery.
- The detail pane dispatches on a **string-prefix selection key**
  (`pane:<pid>` / `review:<worktree>`, see `split/Detail.jsx` `Detail()`).
  Adding a `skill:<name>` branch is a clean addition — no new route, no SPA.
- `split/markdown.jsx` already exports **`renderMarkdown(text)`** — body
  rendering is free.

## Architecture

Four small pieces.

### 1. Server — `periscope/skills.py`

```
list_skills() -> list[dict]
```

- Globs `~/.claude/skills/*/SKILL.md`.
- For each, reads the file and parses the `---` frontmatter block with a
  minimal scalar parser (no PyYAML dependency — frontmatter here is just
  `name:` / `description:` single-line scalars). Body is everything after the
  closing `---`.
- Returns `[{name, description, body, path, mtime}]`, sorted by name.
  `name` falls back to the directory name if frontmatter omits it.
- A directory with no `SKILL.md`, or an unreadable file, is skipped (logged via
  the standard `_bg`/log path, not raised).

The skills root is a constant in `periscope/config.py`
(`SKILLS_DIR = Path.home() / ".claude" / "skills"`), so tests can point it
elsewhere.

### 2. Route — `periscope/routes/skills.py`

- `GET /api/skills` → `{"skills": list_skills()}`. Read-only; no other verbs.
- Registered in `app.py`: add `skills` to the `from periscope.routes import (...)`
  block and to the `include_router` tuple, like every other route module
  (registration is an explicit list, not auto-discovery).
- Errors follow the project convention (`raise HTTPException`), though the only
  realistic failure is a missing skills dir, which returns an empty list rather
  than erroring.

### 3. Frontend rail — `SkillsSection`

- New component in `split/AttentionSections.jsx` (sibling to `ActivitySection`),
  rendered below Activity in `<Rail>`.
- `SectionHeader` with label `SKILLS`, count = number of skills, a small refresh
  affordance, collapsed-by-default (same `prefs.getRailCollapsed()` /
  `sec:skills` key pattern as Activity).
- One `attn-row` per skill: skill name as the label. Click sets
  `railSelection.value = "skill:<name>"` (string highlight key — no persisted
  `last_selected` object needed; skills aren't part of the restore flow, matching
  how the attention rows select without persisting).

### 4. Frontend store + detail

- `store.js`: a `skills` signal (array, default `[]`).
- Boot fetch in `main.jsx` (or wherever boot fetches live): `GET /api/skills` →
  `skills.value`. Manual refresh: the section-header button re-fetches.
- `split/Detail.jsx`: add `isSkill = sel?.startsWith("skill:")`. When true, look
  up the skill in `skills.value` by name and render a `SkillDetail`: a small
  header (name + description) followed by `renderMarkdown(body)`. Falls back to
  `EmptyDetail` if the name isn't found (e.g. selection survived a refresh that
  dropped the skill).

## Data flow

```
boot / refresh-click
  → GET /api/skills → list_skills() globs ~/.claude/skills/*/SKILL.md
  → skills signal

click SKILLS rail row
  → railSelection = "skill:<name>"
  → Detail() isSkill branch → lookup in skills signal → renderMarkdown(body)
```

No polling; the skills signal changes only on boot and manual refresh.

## Error handling

- Missing `~/.claude/skills/` → empty list, empty section (section can hide when
  count is 0, matching how `NEEDS YOU` hides when empty).
- A SKILL.md with no/garbled frontmatter → still listed (name from dir), empty
  description, full file as body.
- Selection pointing at a since-removed skill → `EmptyDetail`.

## Testing

- `tests/test_skills.py`: `list_skills()` against a temp `SKILLS_DIR` —
  frontmatter parse (name + description extracted), missing-frontmatter
  tolerance (dir name fallback, full body), dir with no SKILL.md skipped,
  sort order.
- `tests/routes/test_skills.py`: `GET /api/skills` returns the list shape;
  empty dir returns `{"skills": []}`.
- Frontend: no unit tests — verify in the browser (per project convention for
  UI). The interesting logic (frontmatter parse) is server-side and covered
  above.

## Files touched

| File | Change |
|---|---|
| `periscope/config.py` | add `SKILLS_DIR` |
| `periscope/skills.py` | new — `list_skills()` + minimal frontmatter parser |
| `periscope/routes/skills.py` | new — `GET /api/skills` |
| `periscope/app.py` | add `skills` to the routes import block + `include_router` tuple |
| `static/src/store.js` | add `skills` signal |
| `static/src/main.jsx` | boot fetch of `/api/skills` |
| `static/src/split/AttentionSections.jsx` | add `SkillsSection` |
| `static/src/split/Rail.jsx` | render `<SkillsSection>` below Activity |
| `static/src/split/Detail.jsx` | add `isSkill` branch + `SkillDetail` |
| `tests/test_skills.py` | new |
| `tests/routes/test_skills.py` | new |
| `static/dist/app.js` | rebuilt + committed (npm run build) |
```

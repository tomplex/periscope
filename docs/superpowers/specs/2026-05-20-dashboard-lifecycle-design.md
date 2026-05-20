# Dashboard lifecycle visibility — design

**Date:** 2026-05-20
**Status:** draft, awaiting review
**Author:** Tom + Claude

## Context

Periscope's grid shows panes and — since the project model — groups them
by project. It does not surface *where a piece of work is in its
lifecycle*. A feature moves through brainstorm → spec → plan → implement
→ review → merge, producing a chain of artifacts (design doc, plan doc,
walkthrough, PR). None of that is visible on the dashboard today: you
open the modal and read the terminal to reconstruct it.

Two daily-use frictions motivate this:

1. **Wrong visual hierarchy.** The grid treats every card equally. Cards
   that need you *now* don't stand out from cards quietly working or
   long-idle.
2. **Lost context across days.** After a day or two you can't tell a
   stale tab from a live one; titles drift, and there's no signal of
   what each piece of work has produced or where it stands.

This spec adds **lifecycle visibility**: a derived per-worktree lifecycle
state (phase + artifacts + staleness), card decoration that surfaces it,
and a three-zone grid that uses it to decide prominence.

It is *not* a new top-level object. The existing project + worktree +
tabs structure already groups related work; this spec decorates that
structure and makes periscope a better *viewer* of the lifecycle. LGTM
stays the review / interaction surface.

Companion to `2026-05-15-project-model-design.md` (the
project/worktree/tab data model) and `2026-05-15-workflow-management-design.md`
(the project-lifecycle verbs). This spec is about the *feature*
lifecycle, not the *project* lifecycle.

## Goals

1. **Derived lifecycle state per worktree** — phase, artifact list,
   staleness — computed server-side, no skill cooperation required.
2. **Lifecycle-first cards** — phase chip + unified artifact row; drop
   the unreliable status-line text.
3. **Three-zone grid** — needs-you / active / quiet, so prominence
   tracks relevance.

## Non-goals

- **A first-class Feature/Initiative object.** The (project, worktree,
  tabs) tuple already groups work. Rejected in brainstorming.
- **LGTM→Claude routing / one-click subagent dispatch.**
  Periscope-as-active-broker is interesting but out of scope; this spec
  is visibility-only. Escape hatch noted below (`note_artifact`) if
  precision is ever needed.
- **Fixing the status line.** The status-line *text* is unreliable; it
  is removed from the card. Making it useful again is a separate future
  project.
- **Skill coupling.** Phase is inferred from filesystem + git + existing
  PR/LGTM mirrors. No changes to superpowers skills.
- **Cross-machine.** Per-host, like every other periscope feature.

## Concepts

### The lifecycle unit: the worktree root

Phase and artifacts are scoped to a **worktree root** — one entry of
`git worktree list`, i.e. one checkout directory bound to one branch.
A worktree root is a real on-disk thing, but it is *not* persisted in
periscope today: `worktrees.py` resolves a pane's cwd against
`git worktree list` transiently (for the affiliation chip) and discards
the result each poll. State has `windows` + `projects` + `settings` —
no worktree key. This spec makes the worktree root a persisted unit.

**Grouping panes to a worktree root.** Each pane's cwd resolves to its
worktree root via git (`worktrees.py` already does the resolution).
Panes whose cwds resolve to the same root share one lifecycle. The
branch is read once per root via the existing `cached_git_state` — a
worktree has exactly one branch.

**A project may span multiple worktree roots — and the reverse.** This
is deliberate, not an edge case. Sibling-worktree tabs are first-class
(`2026-05-15-project-model-design.md`), so one project can have tabs in
several worktree roots; conversely periscope's own project pins to the
repo root and *every* tab on `main` resolves to that single root. The
lifecycle key is therefore the worktree root, **independent of the
project** — it is neither the project (`pinned_dir`) nor the pane.

**Persistence.** Derived lifecycle (phase/artifacts/staleness) is
recomputed every poll, never persisted. Only user intent persists: a new
top-level `worktrees{}` block in `state.json`, keyed by absolute
worktree-root path, holding `phase_manual_override`. GC: on load, prune
entries whose path is no longer a live `git worktree list` entry —
following the existing `store.py` migration pattern. (Pins live
elsewhere — see the grid section.)

### Lifecycle state

Computed by a new `periscope/lifecycle.py`, polled into state alongside
pane info:

```
lifecycle(worktree W) = {
  phase: "none" | "spec" | "plan" | "implementing" | "review" | "merged",
  phase_manual_override: null | "<phase>",
  artifacts: [ {kind, ...}, ... ],
  staleness_days: float,   # days since newest of
                           # {artifact mtime, last commit, last pane activity}
}
```

Artifact kinds:

| kind | detected from | click target |
|---|---|---|
| `spec` | configurable globs, working-set scoped | doc → LGTM Review tab (pinned) |
| `plan` | configurable globs, working-set scoped | doc → LGTM Review tab (pinned) |
| `walkthrough` | LGTM mirror (session has a walkthrough) | LGTM Review tab |
| `pr` | existing `git_pr.py` title-bar detection | Review tab / GitHub |
| `lgtm` | existing LGTM mirror | Review tab |
| `merge` | git: branch merged into base | merge commit |

### Artifact scope rule (the "shared cwd" problem)

"Newest match wins" breaks when two tabs share a cwd — periscope's own
case, where everyone works on `main`. Artifacts are filtered through
git's working set:

```
artifacts(W) = files matching globs AND (
    in `git status` (untracked or modified)
    OR in `git diff <base>...HEAD`
)
```

- **Feature-branch worktree**: the branch's diff against base is exactly
  the docs created for this feature. Historical `docs/` content (already
  in base) doesn't surface.
- **Base-branch worktree** (cwd on `main`): diff-against-base is empty;
  fall back to **uncommitted + committed-in-last-24h** (sliding window).
  Catches in-flight work without dragging in old specs.

**git calls.** Per worktree root, `lifecycle.py` runs: `git status
--porcelain` (untracked/modified), `git diff --name-only <base>...HEAD`
(committed since base), and — for the base-branch fallback —
`git log --since=24h --name-only`. The 24h-window pattern is already
proven cheap: `git_pr.py`'s `shared_activity_for` runs `git log -10
--since=24h` on a 60s SWR cache. `git_pr.py`'s `git_state_for` is *not*
reusable here — it runs `git diff HEAD --shortstat` (counts, not file
names). These calls go in a **new cache keyed by worktree-root path**
(not the repo-keyed `_git_cache` — `git status` is worktree-scoped),
short TTL.

**Known limitation.** Two concurrent features on the same branch produce
a *union* artifact view — periscope cannot disambiguate without help.
Accepted because (a) the union is still more informative than nothing,
and (b) the model nudges toward one-feature-per-worktree, which the
worktree-tab affordance already makes cheap. Escape hatch if it ever
bites: an opt-in `note_artifact(kind, path)` channel tool to tag a doc
to a pane; filesystem inference stays the floor.

### Phase derivation

Manual override wins; otherwise derived:

```
merged        if branch merged into base
review        elif PR open
implementing  elif plan exists AND commits-since-base > 0
plan          elif plan exists
spec          elif spec exists
none          otherwise
```

`commits-since-base` uses the same base-branch fallback as artifact
scoping: for a feature-branch worktree it is commits-ahead-of-base; for
a base-branch worktree it is commits in the last 24h.

**No-PR workflows.** Periscope's own work commits straight to `main`
with no PR, so its phase ladder tops out at `implementing` and never
reaches `review`/`merged`. That is acceptable: the chip reflects the
last known state, the worktree folds into the Quiet zone once artifacts
age past the staleness threshold, and manual override can set a terminal
phase if desired.

**`merged` detection is net-new, not a `git_pr.py` reuse.** `git_pr.py`
is open-PRs-only (`pr_state_for` queries `gh pr list --state open`); it
has no merged/closed query, and `git_state_for`'s `ahead` is computed
against `@{u}` (upstream), not the base branch. So the `merged` phase
and `merge` artifact need: (a) a new cached `gh pr view <N> --json
state,mergedAt` — the **primary** signal, because it catches
squash-merges; and (b) `git merge-base --is-ancestor <branch> <base>`
as a fallback when no PR is linked. `git branch --merged` is *not* the
primary signal — it misses squash-merges, the dominant GitHub workflow
(same caveat as `2026-05-15-workflow-management-design.md` Verb 5).
Cache keyed by worktree root, ~5min TTL.

### Detection globs

Per-project, stored in the `projects` entry in `state.json`. Defaults
(deliberately broad — no superpowers path lock-in):

```
spec_globs: ["docs/**/spec*/**/*.md", "docs/**/*-design.md", "docs/**/*-spec.md"]
plan_globs: ["docs/**/plan*/**/*.md", "docs/**/*-plan.md"]
```

## Card decoration

The card becomes lifecycle-first. The removed text is the cwd-derived
recap line (`last_line` / `recap` in `grid.js`) — a pure frontend
change. Claude's status-line *parsing* (`STATUS_RE` in `panes.py`) is
untouched; this only stops *rendering* the recap text on the card. The
activity glyph (`idle` / `done` / working-spinner / needs-input) stays —
it is the pane's pulse, and the grid zoning depends on needs-input.

```
┌──────────────────────────────────────┐
│  feature/auth-rework            ✨    │
│  ● implementing      ◐        💤 2d   │
│  📄 spec  📋 plan  🗺 walk  #1234✓ 👁 │
└──────────────────────────────────────┘
```

- **Phase chip** — one colored pill: `spec` blue, `plan` indigo,
  `implementing` amber, `review` orange, `merged` green, `none`
  grey/absent. Pin glyph when manually overridden.
- **Activity glyph** — retained.
- **Staleness** — `💤 Nd` shown when `staleness_days ≥ 3`.
- **Artifact row** — compact strip, one chip per artifact, each a click
  target. **Absorbs** today's standalone `#PR` badge and `👁 review`
  chip; they become artifacts in this unified row, not separate card
  elements. Hover → path/title/mtime tooltip.
- Worktree-scoped → identical on every tab in a worktree. Intentional:
  visually ties sibling tabs to one piece of work.

**Modal** — full artifact detail (paths, titles, mtimes, open buttons)
plus the manual phase-override dropdown.

**Judgment call.** `spec`/`plan` chips open the doc in the modal's LGTM
Review tab as a pinned document (reusing the pin-doc-as-tab feature),
matching the docs-reviewed-in-LGTM workflow.

## Three-zone grid

The grid becomes three horizontal bands. Hierarchy: needs-me → project
→ recency.

```
╔═ NEEDS YOU ═══════════════════════════╗
║  [card]  [card]  [card]               ║
╠═ ACTIVE ══════════════════════════════╣
║  ▾ periscope        ▾ tauri           ║
║    [card] [card]      [card] [card]   ║
╠═ QUIET · 7 ═══════════════ (collapsed)╣
╚═══════════════════════════════════════╝
```

**Zone 1 — Needs you.** Flat priority lane, no project grouping. A card
lands here when it has a pending ask on you: **needs-input**, **`done`**,
or **PR with CI failed**. Cards are lifted out of their project group but
still show their project name. Resolved → the card drops back into its
group. Empty → the band collapses to a thin rule.

**Zone 2 — Active.** Projects with any activity inside the **recency
window (24h)**. Rendered as collapsible project groups (the project
model exists; the grid just doesn't surface it yet). Cards sort by
recency within a group. Non-Claude shells live here too — no phase chip,
recency-sorted.

**Zone 3 — Quiet.** Projects/tabs past the **staleness threshold (3d)**.
Collapsed by default with a count. Home for "lost context across days" —
old work one click away, never cluttering. Note the ladder with
`workflow-management-design.md` Verb 5: 3d → folds into Quiet; 14d →
surfaces as a cleanup candidate.

**Drag-reorder resolution.** Today `grid.js` persists a full manual
order — `orderedSessions` reads a `session_order` pref
(`prefs.getSessionOrder` / `setSessionOrder`). Auto-zoning conflicts
with a hand-arranged order. Resolution:

- Zoning + within-zone recency sort is automatic; **drag's role shrinks
  to pinning.** Drag a card to Zone 1 → pins it there; drag a project
  group to the front of Zone 2 → pins the group. A pin shows 📌.
- The `session_order` pref is **dropped** and `orderedSessions` stops
  honoring it. Leaving it would let stale saved order silently keep
  sorting Zone 2 — a confusing hybrid. Migration removes the key.
- A **card pin** (Zone 1) is a new field on `windows[pid]`. A
  **project-group pin** (Zone 2 front) is a new field on
  `projects[pinned_dir]`. Pins live with the thing pinned;
  `phase_manual_override` lives on the `worktrees{}` block.
- `/api/window/move` — cross-session card drag, a real `tmux
  move-window` mutation — is a **separate gesture and is retained**.
  Free reordering goes away; moving a tab between sessions does not.

## Architecture

| Component | Change |
|---|---|
| `periscope/lifecycle.py` | **New.** cwd→worktree-root resolution, artifact detection (glob + git working-set scope), phase derivation, staleness. Takes a worktree-root path, returns a `lifecycle` dict. |
| `periscope/worktrees.py` | Reused — already resolves a cwd to its `git worktree list` entry (today transient, for the affiliation chip). `lifecycle.py` calls into it to group panes by worktree root. |
| `periscope/git_pr.py` | Reused for open-PR / CI state. **Net-new** here: a cached `gh pr view --json state,mergedAt` (merged detection) and `commits_since_base` — `git_pr.py` today is open-PRs-only and computes `ahead` vs `@{u}`, not base. |
| `periscope/lgtm.py` | Reused as-is for `lgtm` + `walkthrough` artifacts — `_lgtm_fetch_walkthrough` already mirrors walkthrough presence; just consume it. |
| `periscope/store.py` | New top-level `worktrees{}` block (keyed by worktree-root path) for `phase_manual_override`; `projects` entries gain `spec_globs`/`plan_globs` + a group-pin flag; `windows[pid]` gains a Zone-1 card-pin flag. State migration + prune-missing-worktrees GC. |
| `periscope/routes/*` | Endpoints to set phase override + pin/unpin; lifecycle data rides existing `/api/state`. |
| `static/grid.js` | Three-zone rendering, project groups, drag→pin. |
| card rendering | Phase chip, artifact row, remove status-line text. |
| `static/modal.js` | Artifact detail + phase-override control. |

## Data flow

`/api/state` poll (existing 3s cadence) → resolve each pane's cwd to a
worktree root → for each distinct worktree root, `lifecycle.py` computes
phase/artifacts/staleness. Git calls are cached in a new cache keyed by
worktree-root path, short TTL → lifecycle rides the existing state
payload, fanned out per-window in `build_window_view` → `grid.js`
buckets cards into zones and renders chips. No new poll loop.

## Testing

- `tests/test_lifecycle.py` — cwd→worktree-root resolution, artifact
  scoping (feature-branch vs base-branch fallback), the phase-derivation
  table, staleness math, glob matching. The shared-cwd union case and
  the prune-missing-worktree GC each get an explicit test.
- `tests/routes/` — phase-override + pin/unpin endpoints.
- Grid zoning is frontend; verify in the browser (periscope convention:
  no unit tests for view code).

## Phasing

Three PRs, each independently shippable:

- **Phase 1 — Lifecycle backend.** cwd→worktree-root resolution, the
  `worktrees{}` state block + migration/GC, `lifecycle.py`, artifact +
  phase + merge detection, `/api/state` enrichment. No UI yet;
  verifiable via the JSON payload.
- **Phase 2 — Card decoration.** Phase chip, artifact row, remove
  status-line text, modal artifact detail + override control.
- **Phase 3 — Three-zone grid.** Zones, project groups, drag→pin.

## Decisions

Resolved in brainstorming, recorded for traceability.

1. **No Feature object.** Decorate the existing project/worktree
   structure; periscope stays a viewer, LGTM stays the interaction
   surface.
2. **Worktree is the lifecycle unit**, not the pane.
3. **Phase inferred from artifacts** — light touch, configurable globs,
   no superpowers coupling.
4. **Artifacts working-set-scoped via git** — solves the shared-cwd
   pollution problem; base-branch worktrees fall back to a 24h commit
   window.
5. **Status-line text removed** — unreliable; the activity glyph stays;
   making the status line useful is a separate project.
6. **`done` belongs in Zone 1** alongside needs-input and CI-failed.
7. **Recency window 24h, staleness threshold 3d** — distinct values; 3d
   ladders below Verb 5's 14d cleanup threshold.
8. **Drag-reorder shrinks to pinning** — auto-zoning is the default,
   pin is the override.
9. **Lifecycle is keyed by the worktree root** — a `git worktree list`
   entry / checkout directory — not by project and not by pane. A
   project may span multiple worktree roots. `phase_manual_override`
   persists in a new path-keyed `worktrees{}` state block; pins persist
   on `windows[pid]` (card) and `projects[pinned_dir]` (group).

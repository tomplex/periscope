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

### Worktree is the lifecycle unit

Phase and artifacts are scoped to a **worktree** (≈ a branch checkout),
not a pane. Tabs sharing a worktree share one lifecycle. Rationale:
artifacts live on the worktree's filesystem, and a worktree maps to one
branch = one PR = one LGTM session.

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

### Detection globs

Per-project, stored in the `projects` entry in `state.json`. Defaults
(deliberately broad — no superpowers path lock-in):

```
spec_globs: ["docs/**/spec*/**/*.md", "docs/**/*-design.md", "docs/**/*-spec.md"]
plan_globs: ["docs/**/plan*/**/*.md", "docs/**/*-plan.md"]
```

## Card decoration

The card becomes lifecycle-first. The status-line *text* is removed; the
activity glyph (`idle` / `done` / working-spinner / needs-input) stays —
it is the pane's pulse, not the status line, and the grid zoning depends
on needs-input.

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

**Drag-reorder resolution.** Today `grid.js` supports free drag-reorder
(manual order). Auto-zoning conflicts with a hand-arranged layout.
Resolution: zoning + within-zone recency sort is automatic; **drag's
role shrinks to pinning** — drag a card to Zone 1 to pin it there; drag
a project group to pin it to the front of Zone 2. A pinned card/group
shows a 📌. Auto is the default; pin is the override.

## Architecture

| Component | Change |
|---|---|
| `periscope/lifecycle.py` | **New.** Artifact detection (glob + git working-set scope), phase derivation, staleness. Takes a worktree path + git state, returns a `lifecycle` dict. |
| `periscope/git_pr.py` | Reused for PR / CI / merge state. Add small `commits_since_base` / `branch_merged` helpers if not already present. |
| `periscope/lgtm.py` | Reused for `lgtm` + `walkthrough` artifacts. May need to expose whether a session has a walkthrough. |
| `periscope/store.py` | `projects` entries gain `spec_globs`, `plan_globs`; per-worktree `phase_manual_override` + pin flags. State migration. |
| `periscope/routes/*` | Endpoint to set phase override + pin/unpin a card/group; lifecycle data rides existing `/api/state`. |
| `static/grid.js` | Three-zone rendering, project groups, drag→pin. |
| card rendering | Phase chip, artifact row, remove status-line text. |
| `static/modal.js` | Artifact detail + phase-override control. |

## Data flow

`/api/state` poll (existing 3s cadence) → for each worktree,
`lifecycle.py` computes phase/artifacts/staleness; git calls are cached
per repo with a short TTL, matching the existing `git_pr.py` caching
discipline → lifecycle rides the existing state payload → `grid.js`
buckets cards into zones and renders chips. No new poll loop.

## Testing

- `tests/test_lifecycle.py` — artifact scoping (feature-branch vs
  base-branch fallback), the phase-derivation table, staleness math,
  glob matching. The shared-cwd union case gets an explicit test.
- `tests/routes/` — phase-override + pin/unpin endpoints.
- Grid zoning is frontend; verify in the browser (periscope convention:
  no unit tests for view code).

## Phasing

Three PRs, each independently shippable:

- **Phase 1 — Lifecycle backend.** `lifecycle.py`, state migration,
  detection, `/api/state` enrichment. No UI yet; verifiable via the
  JSON payload.
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

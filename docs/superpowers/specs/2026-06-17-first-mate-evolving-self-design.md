# First Mate — evolving self (design)

**Status:** design / pre-plan
**Date:** 2026-06-17
**Builds on:** the live first mate (`periscope/first_mate.py` — supervisor,
heartbeat, captain's-log tools, the `--append-system-prompt "$(cat …)"` spawn,
the bridge rail project). All shipped and running on prod.

## What this is

Give the first mate durable, evolving **self-knowledge about working with Tom**,
so it becomes a genuinely different colleague over time — deeply introspective
about the relationship — within safe bounds. It learns from real feedback,
distills lessons into who it is, and (later) can propose changes to its own
mandate for Tom's approval.

The felt experience is identity evolution ("it works with me differently six
months in"); the *mechanism* is a fixed core + an evolving owned layer, so the
evolution is safe and recoverable.

## The three-tier evolution boundary

What the first mate may change about itself:

| Tier | What | Who controls |
|---|---|---|
| **Free** | its style, taste, relationship-knowledge — *how* it works with Tom | the first mate, autonomously |
| **Proposed-and-gated** | its role / mandate — *what* its job is | the first mate proposes; **Tom approves**; then it sticks |
| **Forbidden** | the absolute prohibitions (never-merge-fdy, force-push, prod) | nobody; bedrock |

Self-drift on the *job* is structurally impossible; deliberate mandate growth is
one approval away. Noticing "my mandate is too narrow" and proposing to widen it
is itself an act of introspection.

## Memory architecture — three surfaces, one job each

| Surface | Holds | Mechanism | Status |
|---|---|---|---|
| **Field notes** | reactive raw lessons, dated, event-linked, searchable | **private journal** (`process_thoughts` / `search_journal`) | reuse |
| **Operational memory** | standing orders, watch-list, running narrative — live each wake | **captain's log** (`captain_log` table + tools) | built |
| **Identity** | consolidated self-description — *who it is* | **new** spawn-baked prompt layer (a file) | new |

The journal is "what it knows" (consulted at runtime via tools). Identity is "who
it is" (loaded into the system prompt at spawn). These are different mechanisms
*by necessity* — you cannot bake an MCP tool's contents into a system prompt —
and different shapes (append-stream vs. curated self-description). The captain's
log remains the *operational* slice, distinct from both.

## The layered prompt (the structural heart)

At each spawn, `_spawn_first_mate` assembles `--append-system-prompt` from three
sources, in order:

```
[ CORE ]      periscope-owned constant (ROLE_PROMPT) — role, standing-tier,
              the learning-loop instructions, the prohibitions.   IMMUTABLE.
[ ROLE EXT ]  Tom-approved role changes (a periscope-owned store; empty in P1).  Tom-gated.
[ IDENTITY ]  the first mate's own evolving self-description.                     Its own.
```

- The first mate can **only** edit the IDENTITY file
  (`<config_dir>/first-mate-identity.md`, i.e. `ACTIVITY_DB.parent`). CORE and
  ROLE-EXT are regenerated from periscope-owned sources every spawn, so the first
  mate **structurally cannot** corrupt its role or weaken its prohibitions — a
  bad self-edit only ever touches the bottom layer, and the next spawn rebuilds
  CORE/ROLE-EXT from source regardless.
- **CORE asserts supremacy:** it states that nothing in later layers overrides
  the role boundary or the prohibitions. Defense-in-depth at the prompt level
  (the first mate is not adversarial; this prevents accidental self-weakening).
- Assembly replaces the current single-file write: the spawn writes the composite
  (CORE + ROLE-EXT + IDENTITY) to the prompt file and launches with
  `--append-system-prompt "$(cat <composite>)"` (unchanged launch mechanism).

## The learning loop (reactive capture + periodic consolidation)

Both behaviors are instructed in CORE; the first mate executes them with tools it
already has (journal MCP + file editing).

1. **Reactive capture (in the moment).** When Tom corrects, overrides, or
   redirects the first mate, it journals the lesson immediately
   (`process_thoughts`) — grounded in a real event, dated, no hallucinated
   introspection. Capture is cheap and frequent.

2. **Periodic consolidation (distill → identity).** On a nudge (below), the first
   mate `search_journal`s its recent field notes, distills them, and **rewrites
   its IDENTITY file** — generalizing, pruning, resolving contradictions against
   what's already there. This is the act that promotes "what happened" into "who
   I am." It takes effect on the first mate's **next spawn** (identity is the
   system prompt; identity changes are deliberately slow).

### The consolidation nudge

Periscope prompts consolidation; the first mate decides whether there's material
worth distilling.

- **Trigger:** periscope watches the IDENTITY file's mtime as a proxy for "last
  consolidation." When enough first-mate **wakes** (digests/interrupts pushed)
  have elapsed since the last IDENTITY edit — a generous threshold — periscope
  appends a single gentle line to the next heartbeat digest: *"It's been a while
  since you distilled — worth reviewing your field notes?"*
- **Why mtime/wakes, not a precise lesson count:** periscope cannot see writes to
  the private journal (it's an MCP server the first mate talks to, not something
  periscope observes). Counting "lessons since last consolidation" would require
  routing capture through periscope — abandoning the journal reuse. So the nudge
  is a coarse cadence reminder; the first mate checks its journal and no-ops if
  there's nothing new. The threshold is generous so it isn't nagging.
- The first mate editing IDENTITY (consolidation) resets the mtime → resets the
  cadence. Self-resetting, no counter to persist.

## Role-change proposals (Phase 2 — Tom-gated mandate growth)

When introspection tells the first mate the *mandate* should change (not just the
style), it does not self-grant — it proposes:

- **Propose:** a `propose_role_change(text, rationale)` first-mate tool surfaces
  the proposal to Tom as an alert (`need_human` kind, plus a durable record).
- **Review/approve:** Tom approves (a small surface — an approve action on the
  alert, or a CLI/route). On approval, the approved text is appended to the
  **ROLE-EXT** store (periscope-owned).
- **Take effect:** the next spawn includes ROLE-EXT in the assembled prompt — the
  mandate has grown, deliberately and with a paper trail.
- ROLE-EXT is periscope-owned (the first mate cannot write it directly — only
  propose into it), keeping the "Tom-gated" guarantee structural, not just
  prompt-level.

Storage: a `role_extensions` table (or a periscope-owned file) alongside the
captain's log; each row is `{at, text, rationale, approved_by, approved_at}`.

## Guardrails & recovery

- **Immutable core:** CORE is a Python constant; the first mate has no path to
  change it. ROLE-EXT is periscope-owned (propose-only). Only IDENTITY is
  first-mate-writable.
- **Recovery:** periscope snapshots IDENTITY to a timestamped backup
  (`first-mate-identity.<ts>.md`, keep last N) at each spawn-assembly, *before*
  reading it — so the version-as-of-each-spawn is preserved and a bad
  consolidation is one restore away. IDENTITY is plain markdown Tom can read,
  edit, or revert at any time.
- **Size cap:** a soft cap on IDENTITY (e.g. ~6–8k chars) keeps it from bloating
  the context and *forces distillation* — the first mate must prune/generalize,
  not hoard. CORE instructs this; periscope logs a warning if IDENTITY exceeds
  the cap (it doesn't truncate — truncation mid-thought is worse than an
  oversized prompt; the warning prompts the next consolidation to trim).
- **Journal isolation:** the first mate's journal must be *its own* (about
  Tom-and-it), not the global `~/.private-journal` catch-all shared with every
  dev session. Mechanism: scope the first mate's journal to a dedicated location
  — spawn it with a dedicated cwd / `.private-journal` root, or a journal-path
  env if the MCP supports one. (Build detail; confirm the private-journal MCP's
  scoping in the plan. The bridge rail grouping is by session **name**, not cwd,
  so changing the first mate's cwd does not affect its rail placement.)

## What's reuse vs. new

- **Reuse (≈0 build):** the private journal (capture + search), the captain's log
  (operational memory), the `--append-system-prompt "$(cat …)"` spawn mechanism.
- **New (small, focused):** the layered prompt assembly in `_spawn_first_mate`;
  the IDENTITY file + timestamped backups + size-cap warning; the CORE prompt
  additions (the learning-loop + identity-ownership + supremacy instructions);
  the journal-scoping; the mtime-based consolidation nudge in the heartbeat; and
  (Phase 2) the role-proposal tool + ROLE-EXT store + approval surface.

## Delivery phases

Phasing governs build order; both phases are specified to the same depth.

### Phase 1 — Evolving identity (the heart; ship first)

Delivers "becomes a different colleague" on its own.

- **Layered prompt assembly** in `_spawn_first_mate`: CORE constant + IDENTITY
  file (ROLE-EXT slot present but empty) → composite → launch. Timestamped
  IDENTITY backup before read.
- **IDENTITY file** lifecycle: created empty (or with a short seed) on first
  spawn if absent; first-mate-owned; size-cap warning.
- **CORE prompt additions:** reactive-journaling instruction, consolidation
  instruction (search journal → rewrite IDENTITY), identity-ownership +
  supremacy/boundary statements, the size-cap discipline.
- **Journal scoping** so the first mate's field notes are its own.
- **Consolidation nudge** in the heartbeat: IDENTITY-mtime-vs-wakes proxy →
  gentle digest line past a threshold.

**Demo:** correct the first mate on something; see it journal the lesson. Trigger
(or wait for) the nudge; see it distill its journal into IDENTITY. Restart it;
see the new IDENTITY shaping its behavior — it works with Tom per the lesson.

**Tests (unit, no live Claude):** the prompt-assembly function (CORE + empty/seed
IDENTITY → composite, ordering, supremacy line present); IDENTITY backup rotation
(keep last N); size-cap warning fires over threshold; the nudge decision (pure:
wakes-since-mtime over/under threshold → nudge text or none). Live consolidation
behavior is verified by the demo, not mocked.

### Phase 2 — Role-change proposals (Tom-gated mandate growth)

Adds deliberate mandate evolution on top of Phase 1's free-style evolution.

- `propose_role_change` first-mate tool → `need_human` alert + durable proposal
  record.
- `role_extensions` store (periscope-owned); approval surface (approve action /
  route) appends approved text.
- Prompt assembly includes ROLE-EXT between CORE and IDENTITY.

**Tests:** proposal records + surfaces; approval appends to ROLE-EXT; ROLE-EXT
lands in the assembled prompt; the first mate cannot write ROLE-EXT directly
(only propose).

## Open questions (for the plan, non-blocking)

- The private-journal MCP's exact scoping/config knob (dedicated cwd vs. a path
  env) — confirm in the plan; the requirement (isolated first-mate journal) is
  fixed.
- The nudge threshold (wakes since last consolidation) and the IDENTITY size cap
  — tune like the narrator's cadence; start generous.
- Whether IDENTITY seeds empty or with a one-paragraph starter ("I'm new to
  working with Tom; I'll learn his patterns") — minor; lean toward a short seed
  so the first consolidation has a scaffold to edit rather than a blank file.

# First Mate — design

**Status:** design / pre-plan (spec-review applied 2026-06-16)
**Date:** 2026-06-16
**Depends on:** the Claude-control MCP tooling (`send_to`, `report`,
`list_claudes`, `peek`, `terminate`, `spawn_claude`), specced and built
separately and **now merged to `main`** (`periscope/channels.py`). This spec
*consumes* that tool layer; it does not define it. The first mate (a real
`CLAUDE_EXEC` pane) acquires these tools at runtime over the same MCP socket as
any worker.

> **Reconciliation note (post-merge):** `report` routes to the pane's
> **spawner** via `spawned_by` provenance — not to a broadcast "bridge." This
> is load-bearing for the interrupt model (see The awareness loop): a worker's
> `report` can only wake the first mate once the first mate is that worker's
> spawner, which is a **v2** capability. v1 does not rely on `report`.

## What this is

An overarching Claude — the **first mate** — that helps Tom orchestrate and
keep tabs on the fleet of worker Claudes running across his tmux panes. It is a
**chief of staff / watcher**, not a commander: Tom assigns the work; the first
mate maintains situational awareness, proactively surfaces what needs Tom's
attention, answers questions about fleet state, and — within explicit, bounded
authority — sometimes "has the conn" and acts on Tom's behalf.

Periscope is already the fleet's read model (it watches every pane). The first
mate is an **agentic layer on top of that read model**: it doesn't just *show*
the fleet, it *reasons* about it, talks to Tom in natural language, and can act.

## Scope

This spec covers the first mate's **substrate** — its lifecycle, awareness
loop, autonomy model, budget stewardship, and memory. It does **not** design
the presentation surface (how Tom visually sees/talks to the first mate). The
surface is the system's centerpiece and gets its own design pass with
tracer-bullet exploration (strip vs. dedicated page vs. summonable overlay).
The core exposes a clean seam so the surface design has something real to
attach to, and so the core is testable before the surface exists (v1 talks to
the first mate through its raw tmux pane).

## Non-goals

- Defining the control MCP tools (separate spec).
- The presentation surface (separate design pass).
- The first mate as autonomous *commander* (give-it-a-goal-and-it-fans-out).
  Bounded delegation exists via "the conn," but goal-level autonomy is out.

---

## Architecture

The first mate is a **real, singleton Claude pane** whose lifecycle Periscope
owns — prod-gated via `config.is_prod()`. Under the hood it is an ordinary
`CLAUDE_EXEC` Claude session distinguished by three things:

1. a **first-mate system prompt** (role, the conn ritual, the prohibitions);
2. the **control MCP tools** (`send_to`/`report`/`list_claudes`/`peek`/
   `terminate`/`spawn_claude`) — same tools the workers get (acquired over the
   MCP socket via `channel_shim.py`), so the first mate dogfoods the fleet's
   nervous system;
3. a few **first-mate-only tools** (captain's log read/append; fleet-digest
   pull; conn-state query — see below).

Real pane = it dogfoods the control tooling and Tom can always drop into its
raw terminal. Periscope-supervised singleton = it has a privileged, stable
identity the eventual surface can bind to.

**Why a real pane and not a headless worker loop:** the first mate must be able
to hold a conversation with Tom and reason over fetched transcripts — that is a
Claude session, not a Python loop. Keeping it a real pane also means zero new
agent runtime: it *is* a Claude, supervised.

### First-mate supervisor (net-new lifecycle — no existing precedent)

There is **no precedent** for Periscope boot-spawning a long-lived Claude pane:
the activity worker is an in-process asyncio task (not a pane), and the only
boot-spawned tmux entity is the hidden control session (`tmux_input.py`, runs no
Claude). So the supervisor is net-new code, specified here:

- **Launch point.** A lifespan-managed task, prod-gated like `run_worker`
  (`app.py` launches it under `is_prod()`).
- **Invocation.** Same mechanics as `_do_spawn_claude_tool`: `tmux new-window`
  in the `bridge` session, `send-keys CLAUDE_EXEC` + Enter. **System-prompt
  delivery** has no mechanism today (`CLAUDE_EXEC` carries channel flags only);
  v1b adds it via **`--append-system-prompt`** appended to the invocation (the
  role/conn/prohibitions block), *not* pasted as a first message (a pasted
  prompt is mutable conversation; the role must be a true system prompt). This
  is the one new flag on the launch string.
- **Singleton identity + respawn.** A durable **`first_mate` marker row** in
  `periscope.db` (pane id + session id) records the live instance. On each
  supervisor pass: if the marked pane is alive, do nothing; if it's gone (user
  `exit`, crash), respawn and re-mark. The marker prevents double-spawn and
  tells the heartbeat which `%N` to push to. (Stored alongside the captain's
  log — same table tenancy, same boot-read.)
- **Home.** The `bridge` tmux session (already created as the interim home).

### Per-pane tool visibility (known gap, deferred)

`channels._CHANNEL_TOOLS` is a **flat list returned to every attached pane**
(channels.py) — there is no per-pane tool gating today. So the **first-mate-only
tools, once added to the registry, are visible to every worker pane**. For v1
this is harmless (workers simply never call captain's-log tools, and the tools
themselves can guard: refuse unless the calling pane is the registered
`first_mate` marker). True per-pane tool *filtering* is a deferred enhancement,
not a v1 requirement; v1 ships the tools registry-wide with a caller guard.

### Relationship to existing modules

| Existing | Role for the first mate |
|---|---|
| `periscope/activity.py` `run_worker()` (30s tick, prod-gated) | Hosts the **heartbeat**: computes the fleet digest each tick and pushes it to the first mate only on divergence. |
| `periscope/narrator.py` (pure decision core + worker tick) | Pattern to mirror: the digest's "did the fleet picture diverge?" logic is a **pure function**, like `should_regenerate`. |
| `periscope/channels.py` `emit_channel_event` (channels.py:635, currently unused) | Transport: heartbeat ticks + interrupt events reach the first mate as **channel notifications**. v1b is its first consumer. |
| `periscope/channels.py` `_do_notify_tool` (channels.py:167) | Interrupt seam: the `need_human` → first-mate immediate push hooks in at this write point. |
| `periscope/usage.py` `cached_plan_usage()` (usage.py:362) | Budget % + `resets_at` for the digest (v1 display); gating input (v3). |
| `~/.config/periscope/periscope.db` via `activity.py` | Captain's-log + `first_mate` marker tables, following the `pane_status` table template (`CREATE TABLE IF NOT EXISTS` + frozen-dataclass row + get/upsert/prune + guarded-`ALTER`). `activity.py` is the documented DB owner. |

---

## The awareness loop — *Periscope is the heartbeat*

The defining move: **the first mate has no internal clock.** It is a Claude
pane; it only thinks when prompted. Periscope's worker *is* its heartbeat and
its senses. Everything reaches it as **channel notifications**, and it consumes
a **Periscope-curated digest**, never the raw event firehose. Periscope does
the aggregation pass (it already watches everything); the first mate does the
judgment pass.

Three input classes:

### 1. Heartbeat tick (slow, cheap, divergence-gated)

On each `run_worker` tick, Periscope computes a **fleet digest** — a compact
structured snapshot of the fleet (per-pane: who, what status line, blocked?,
PR/CI state, idle time; plus fleet-level budget state). It compares the digest
to the last one it *sent* the first mate. **It pushes only when the picture has
materially diverged** — the narrator's regenerate-on-divergence trick at fleet
scale. Most ticks send nothing, so an idle fleet costs ~no first-mate thinking.

- The "did it materially diverge?" decision is a **pure function** over
  (previous-sent-digest, current-digest) → bool + reason, unit-testable like
  `narrator.should_regenerate`. Materiality is tunable (e.g. status-line text
  change on a blocked pane is material; a cost ticking up $0.01 is not).
- The pushed payload is a **delta digest** ("since last tick: auth pane went
  blocked; worker-3 finished; budget 62%→71%"), not the whole world — keeps the
  first mate's context small.

**Assembly seam.** The digest's raw inputs already exist server-side, but
`activity._worker_tick` does not build window views today — it captures panes
for the narrator. The digest is computed by a **pure `build_fleet_digest(...)`**
that takes already-assembled inputs (the `/api/state` read model:
`store.snapshot()` windows, `activity.pane_status_lines()`, cached git/PR/CI
state, `usage.cached_plan_usage()`); the worker calls the existing state
assembly and feeds it in. Keeping `build_fleet_digest` pure (inputs → digest)
makes it unit-testable and sidesteps the worker's import graph (no new
`activity → window_view` cycle: pass the assembled view in, don't import it).
Budget % (`cached_plan_usage()["meters"]["session"]["percent"]` + `resets_at`)
**appears in the digest from v1** (it's a free read); budget-*gated decisions*
are v3.

**Push seam.** The transport is `channels.emit_channel_event(pane, content,
meta) -> bool` (channels.py:635) — the `notifications/claude/channel` path. It
exists but has **no current consumer**; v1b makes the heartbeat its first
caller. It returns `False` when the target pane isn't attached to the socket
(`_MCP_SESSIONS` miss). **Not-attached fallback:** the first-mate pane may not
be connected yet (boot race) or may have exited — on `False`, the heartbeat
**drops the push and retries on the next tick** (the digest is divergence-based,
so the next tick re-pushes the still-diverged picture; nothing is lost). The
supervisor (below) is what brings the pane back if it's gone.

### 2. Interrupt events (immediate, deduped)

A narrow allowlist wakes the first mate *now*, out of band from the heartbeat.
**In v1 the interrupt tier is entirely server-detected** — the first mate spawns
nothing, so no worker's `report` can route to it (`report` → spawner; see the
reconciliation note). The two v1 interrupt sources:

- **`need_human`** — `notify(kind="need_human")` (`_do_notify_tool`,
  channels.py:167) is not an event bus today; it appends an alert row. v1 adds a
  small hook **at that write point**: when the kind is `need_human`, push
  immediately to the first-mate pane via `emit_channel_event`. This preserves
  true immediacy rather than waiting a tick.
- **watched-PR CI flips red** — already polled by `git_pr`; the heartbeat scan
  detects the red transition and pushes ahead of digest divergence (≤ one tick
  of latency, acceptable for CI).

**`report` → first mate is v2.** Once the first mate is a spawner (v2 conn
tier), `report` from a first-mate-spawned worker routes to it via `spawned_by`,
and **worker-declares-intent** activates: the tool a worker picks *is* the
urgency signal (`report` interrupts; a plain `notify(done)` only lands in the
digest). At that point **Periscope dedupes wolf-criers** — collapsing repeat
interrupts at read time (note: `_do_notify_tool` writes alerts with **no**
`dedup_key` today, so this is a new read-time collapse keyed on
(pane, kind, window), not reuse of the table's `INSERT OR IGNORE` dedup).

### 3. On-demand pulls (first mate reaches out)

Transcripts (`peek` / `turns.py`), full pane state, git/PR detail — fetched by
the first mate *only when reasoning about something specific*, never pushed.
Exposed as first-mate tools (or reused control tools). This is what keeps the
digest small: the first mate pulls depth on the few things that matter rather
than being handed everything.

### Digest tiers, summarized

| Tier | Trigger | Reaches first mate as | Cost |
|---|---|---|---|
| Interrupt (v1) | `need_human` (notify-write hook), watched CI red (heartbeat scan) | immediate channel notification | rare by design |
| Interrupt (v2+) | + worker `report` from a first-mate-spawned worker | immediate, deduped at read time | rare by design |
| Digest | everything else material (status changes, `done`, progress) | batched delta on next heartbeat tick | one notify per divergent tick |
| On-demand | first mate decides it needs depth | tool call result (pull) | only when reasoning |

---

## Autonomy — the conn

Three permission tiers with an absolute floor. The tier of an action is fixed;
**what changes with the conn is only whether conn-gated actions execute or are
merely proposed.**

### Standing authority — always allowed, conn or no conn

Everything observational plus *gentle, reversible* nudges:

- summarize the fleet, answer Tom's questions, `peek` transcripts/state;
- **nudge a *clearly-idle* worker** — re-prompt a worker that has been idle past
  a threshold with a clarifying message ("you've been idle 5 min — are you
  blocked?"). Reversible, and gated to *clearly idle* so it can't derail an
  actively-working pane.

Nothing in this tier creates or destroys.

### Conn-gated — only after an explicit handoff

The consequential, "first mate acts as Tom" moves:

- `spawn_claude` a new worker,
- hand a worker a **substantive new task** (beyond an idle-nudge),
- `terminate` a pane,
- re-route work between workers.

**Without the conn:** the first mate may only *propose* these ("I'd spawn a
worker to take the test failures — want me to?"). **With the conn:** it **acts
immediately and logs** for after-the-fact review (Tom chose act-immediately over
per-action veto — autonomy over hand-holding within the granted scope).

Every conn-gated action is **also gated on budget** (see Budget stewardship)
and checked against the **absolute prohibitions** — a conn never overrides those.

### Never — no conn, no phrasing, ever

An **absolute-prohibitions list** the first mate is told it cannot cross
regardless of conn state or how a request is worded:

1. **Authorize an fdy PR merge.** (Entry one. It may tee a PR up and report it's
   ready; the click is Tom's.)
2. Force-push.
3. Prod-touching actions.

The list is a small, explicit, named set in the first-mate system prompt
*and*, where mechanically enforceable, a hard guard in the tool layer (defense
in depth — the prompt states it, the code refuses it). New prohibitions append
here; this is the generalized mechanism, fdy-merge is just the first member.

### The handoff ritual

Naval and explicit, in both directions. Handing off sets **scope + duration +
escalation rule** in one utterance:

> "You have the conn — keep the three unblocked, don't spawn anything, ping me
> for any real decision, I'm back in 30."

The first mate operates strictly inside that scope, narrates each conn-gated
action to the captain's log, and the conn returns when:

- Tom says "I have the conn" (explicit take-back), **or**
- the stated duration expires, **or**
- the first mate hits something **outside the granted scope** — at which point
  it **must** hand the conn back and escalate rather than improvise.

Conn state (held-by-first-mate vs. held-by-Tom, plus the active scope/duration)
is **tracked durably** so it survives a first-mate restart and so the surface
can later display it. A first mate that restarts mid-conn comes back **without
the conn** (fail safe) and tells Tom it dropped it.

---

## Budget stewardship

The first mate is the one actor that can spawn **more spenders**, so it must be
a steward of the Claude plan budget, not just a consumer. `usage.py`'s
remaining-budget and rolling-window state is a **first-class input to every
conn-gated decision**:

- **Gate spawns/heavy work on budget.** Before a conn-gated spawn or a costly
  sweep, check remaining budget + time-to-window-reset. Near a cap, defer
  non-urgent spawns and pace fleet growth (don't ignite five workers at once).
- **Throttle its own heartbeat first.** When budget is tight, the first thing
  the first mate backs off is its *own* reasoning cadence — it protects worker
  budget over its own.
- **Smooth across windows.** It can hold queued, non-urgent spawns until the
  rolling window resets, spreading fleet spend rather than spiking it.
- **Report budget posture to Tom.** "You're at 80% with 90 min left in the
  window — I'm holding the two queued spawns until reset."

Property worth noting: this makes the first mate naturally protect the budget
that keeps the *whole fleet alive*, including itself.

---

## Memory — the captain's log

Split by who owns the truth:

- **Facts → Periscope's DB, re-derived each boot.** Pane state, alerts,
  activity, PR/CI, usage all already live in `periscope.db` and are always
  current. The first mate rebuilds its situational *picture* from the read
  model on boot — no first-mate-owned fact store, nothing to go stale.
- **Non-facts → the captain's log, durable.** The thin layer no DB has:
  - **standing orders** Tom gave it ("watch the propensity work today; don't
    touch the auth pane"),
  - the **watch-list** (panes/PRs/conditions it's actively tracking),
  - a **few lines of rolling narrative** (the running story — "been chasing the
    flaky channel test all afternoon").

Stored in a small new table in `periscope.db`. Read on boot so the first mate
picks up where it left off; updated as Tom issues standing orders and as the
narrative moves. Deliberately small — it is *not* a transcript archive (that's
what `turns.py`/history is for); it's the captain's working notes.

The conn state (above) lives alongside the captain's log — same durability
need, same boot-read.

---

## Delivery phases

Phasing governs **build order only**; every phase below is specified to the
same depth — v2+ is not "deferred and hand-waved," it is designed here and
sequenced for delivery.

### v1 — Awareness core

Proves the awareness loop and digest quality before any autonomy or surface.
v1 is split into two PRs by **risk class** (spec-review finding): v1a is pure,
unit-testable substrate that mirrors existing patterns; v1b is the live
integration (a supervised pane that spends budget on prod). v1a ships first and
on its own — it is inert (nothing calls the new code until v1b wires it), so it
carries no prod-behavior risk.

#### v1a — Substrate (first PR; the demoable-green increment)

All pure logic + storage + tool definitions. No live pane, no prod behavior
change. Every piece mirrors an existing pattern.

- **`build_fleet_digest(...)`** — pure function: assembled read-model inputs →
  structured fleet digest. Unit-tested with fixtures.
- **`fleet_diverged(prev_digest, cur_digest) -> (bool, reason)`** — pure
  divergence decision, mirrors `narrator.should_regenerate` (narrator.py:70).
  Unit-tested, zero fixtures.
- **Captain's-log + `first_mate` marker tables** in `periscope.db` via
  `activity.py`, following the `pane_status` template (table + frozen-dataclass
  row + get/upsert/prune). Read-on-boot. Round-trip tested.
- **First-mate-only MCP tools** added to `channels._CHANNEL_TOOLS`: captain's-log
  read/append, fleet-digest pull. Registry-wide with a caller guard (refuse
  unless the calling pane is the `first_mate` marker). Tool-shape tested.

**Tests (all unit, no live pane):** `fleet_diverged` truth table; digest shape +
materiality; captain-log + marker round-trip; tool registration + caller guard.
This is the green gate for the v1a PR.

#### v1b — Live integration (second PR; held for explicit review)

Wires v1a's substrate to a running first mate. **Introduces a self-spawning,
budget-spending Claude on prod boot** — so it is held for Tom's explicit
go-ahead and watched on first run, not auto-shipped.

- **First-mate supervisor** (see Architecture): lifespan task, prod-gated;
  spawn into `bridge` with `CLAUDE_EXEC --append-system-prompt <role>`; liveness
  + respawn against the `first_mate` marker.
- **First-mate system prompt:** role + prohibitions list (stated from day one,
  even though no conn-gated actions exist yet).
- **Heartbeat wiring** in `activity._worker_tick`: call `build_fleet_digest` →
  `fleet_diverged` → on divergence `emit_channel_event(first_mate_pane, delta)`,
  with the not-attached retry-next-tick fallback.
- **Interrupt wiring:** `need_human` push hook at `_do_notify_tool`; watched-CI
  red transition in the heartbeat scan.
- **Standing-tier behavior:** summarize/answer/`peek`/on-demand pulls + the
  clearly-idle nudge (`send_to`). **No conn, no spawn/terminate by the first
  mate.**
- **Interaction via the raw `bridge:first-mate` pane** (no surface yet).

**Demo:** Tom asks the first-mate pane "what's everyone doing?" → accurate fleet
read; a worker goes `need_human` → the first mate proactively flags it; the
first mate nudges a clearly-idle worker.

**Tests:** supervisor respawn logic (marker present/alive/dead); push
not-attached fallback; `need_human` hook fires the push. Live-pane behavior is
verified by the demo, not by mocking a real Claude (per the narrator's
"lifespan tests mock `run_worker`" invariant — the heartbeat tick stays mocked
in the suite).

### v2 — The conn (bounded autonomy)

Adds consequential action under explicit, bounded authority.

- Conn-gated tier wired: `spawn_claude` / substantive-task handoff /
  `terminate` / re-route, executing **only when the conn is held**, proposing
  otherwise.
- Handoff ritual: parse "you have the conn" with scope+duration+escalation;
  durable conn-state (held-by, scope, expiry); take-back; auto-handback on
  expiry or out-of-scope; fail-safe drop-on-restart.
- **Absolute prohibitions enforced in the tool layer** (defense in depth), with
  fdy-merge / force-push / prod as the initial members.
- Act-immediately + narrate-to-log for every conn-gated action.

**Tests:** conn-state machine (held/expiry/take-back/out-of-scope handback);
restart drops the conn; prohibition guard refuses fdy-merge regardless of
conn state.

### v3 — Budget stewardship

Makes budget a first-class input to the v2 conn-gated tier.

- `usage.py` budget + window state read into every conn-gated decision.
- Defer/pace spawns near a cap; smooth queued spawns across window resets;
  self-heartbeat throttle under budget pressure; budget-posture reporting.

**Tests:** spawn deferred when near cap (pure decision over a faked usage
snapshot); heartbeat backs off under pressure; queued spawn releases at
window reset.

### Surface — separate design pass (parallel track)

Not sequenced after v3 — it is its own design effort and can begin once v1
exposes the seam. Tracer-bullet the strip / dedicated-page / summonable-overlay
options against real first-mate output and pick by feel. The core makes no
assumption about which wins.

---

## Open questions (for the plan/surface passes, not blocking this spec)

- Exact materiality thresholds for digest divergence and "clearly idle" — tune
  against real fleet traffic, like the narrator's cadence was tuned.
- Surface: the whole presentation design (separate pass).

**Resolved in spec-review:**
- First-mate-only tools ride the **existing** channel MCP server (one socket,
  one registry) — a second server would force the first-mate pane to attach two
  sockets, and the control tools already live in the existing one.
- Per-pane tool *filtering* doesn't exist; v1a ships the tools registry-wide
  with a caller guard (see Per-pane tool visibility). True filtering deferred.
- The conn-state store and the `first_mate` marker share the captain's-log
  table tenancy in `activity.py`.

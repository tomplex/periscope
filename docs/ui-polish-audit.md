# Periscope UI polish audit — Warp parity

Three independent audits (fidelity / legibility / responsiveness), July 2026.
Question asked: *what would it take to reach Warp-grade polish?*

## Verdict

**The terminal is not the gap. It is measured, and it is already good.**

| Path | p50 |
|---|---|
| Full periscope relay — client WS send → echoed byte back | **1.42 ms** |
| Raw tmux control-mode floor — no periscope at all | **0.15 ms** |
| `tmux send-keys` fork path — what `tmux_input.py` already replaced | **20.19 ms** |

Periscope's Python relay costs **~1.27 ms p50**. One display frame is 8.3–16.7 ms.
Rasterization is already GPU-accelerated (`WebglAddon`, `terminalCore.js:365-377`).
A native Rust terminal recovers an estimated 2–5% of keystroke→glyph latency —
under one frame, not perceivable in an A/B.

Meanwhile `ws.py:51-55` burns two ~20 ms forks per pane switch, and `ws.py:114`
another, for ~60 ms of removable overhead — **~45× what a rewrite recovers.**

**Of 13 fidelity defects, zero are caused by mirroring tmux instead of owning the
PTY.** Periscope's terminal surface is already byte-exact: `%output` streaming plus
an idempotent full-grid repaint every ≤1 s. Owning the PTY yields the same bytes
with the same semantics.

### The real diagnosis

Periscope is a **3-second-poll app pretending to be a live one**. All three audits
converged on this independently:

- 20 `transition` declarations in 3118 lines of CSS; **all 20 are hover
  affordances**. Zero on data-driven change. Every row, chip, and status is a hard swap.
- **No optimistic UI on any tmux mutation.** Rename visibly reverts before
  correcting — it reads as failure.
- Attention sections appear/vanish wholesale above the tree, shifting the rail
  tens of pixels every tick.
- 11 of 13 fidelity defects are periscope's own caching/invalidation/polling logic.

None of this is a language problem. A Rust rewrite carries all of it across unchanged.

### The one version of the rewrite that holds up

**"Own the PTY" and "own the agent loop" are different proposals.**

Driving the Agent SDK directly instead of spawning the `claude` CLI is the only
thing that fixes D1 (below) — periscope would hold the in-flight assistant message,
the pending `AskUserQuestion`, and rejected tool calls in memory before they touch
disk. It also dissolves session-identity discovery entirely (`pane_session_hook.py`,
duplicate-session masking, the cwd-fallback transcript lie).

That is a genuine thesis. It is also a much larger product bet than "rewrite in
Rust," and it does not require Rust.

---
## Status

The audit above is the original finding. What follows is where it actually
landed — kept current so this file stays a map of what's *left*, not a list of
things already done.

### Shipped

| Was | Now |
|---|---|
| T0.1 split-pane alerts GC'd every 3 s | GC keys on `list-panes -a` (`1a5673d`) |
| T0.3 ~60 ms of forks per pane switch | resize+geometry chained into one fork (`b85840f`) |
| T0.4 untracked files report "clean" | counted as `?N` (`c191298`) |
| T0.5 alerts vanish on restart | rehydrated from the events log (`624c57c`) |
| T0.6 `styles.css` brace imbalance | scar removed, braces balance (`b6ec9a1`) |
| T0.8 polls can commit a stale snapshot | chained `setTimeout` (`b473580`) |
| T0.9 merged PRs keep their badge | lifecycle resolved by number (`da0a5cb`) |
| T0.10 preview tabs never re-fetch | re-fetch on re-show (`961d551`) |
| T1.2 zero transitions on state change | stripes/dots/chips transition (`6b7168f`) |
| T1.3 rail shifts under the cursor | membership freezes while hovered (`6b7168f`) |
| T1.5 no real diff viewer | Changes tab: git-backed, two scopes, live (`83ea05b`) |
| T2.1 3 s poll ceiling | `/ws/state` push hub + alert feed folded onto it |

**T1.1 (optimistic UI) was obviated rather than fixed.** The push hub kicks on
tmux mutations, so the revert-then-correct window shrank from ~1.5 s to about
one round trip. Rail mutations are still `await`-then-nothing; that's now a
cosmetic detail rather than the "rename visibly reverts" bug it was.

**T0.2 / T0.7 were deliberately dropped.** MultiEdit-renders-nothing is real but
transcript-only, and transcript mode is unused here (see
`idea-own-the-agent-loop.md` for why that view isn't being invested in). The 6
undefined CSS tokens render fine via their fallbacks and would be rewritten by
T2.3 anyway — folding them into that rather than churning the lines twice.

---

## Open

Roughly by leverage.

**Cross-pane changes.** Aggregate the Changes diff across every pane in a track:
"what have all my Claudes touched." Composes directly with the existing hunk
renderer and `filesTouched`, and it is squarely the thing Claude Code cannot
show you — no CC surface spans panes. The natural next feature.

**Forkless pane open.** Spec'd in `specs/forkless-pane-open.md`. Sources the
handshake geometry from the mirror (which already reads it on subscribe) instead
of a second fork, taking pane-open from 2 forks to 1. ~20 ms per pane switch,
which matters because switching is the most frequent interaction in a
terminal-default workflow.

**T2.3 — Design-system reset.** Machine-counted from `styles.css` (re-measured
2026-07-23; the audit's original figure was 30, so this drifts as the file is
edited — re-count before acting on it): **28 distinct font sizes**, with a live
half-pixel tier and roughly eleven steps between 9 and 15 px. Also **90 distinct
padding shorthands for 145 declarations**, 22 shadows for 25
declarations, 21% dead or inert, three unrelated design languages in identifiable
line ranges. Keep the oklch token block (`:10-59`) verbatim — status colours
pinned at constant lightness/chroma varying only hue is a real system. Add
`--fs-*` and a 4 pt `--sp-*` scale up front. Real, but diffuse: low ceiling per
hour compared to anything above it.

**T2.4 — Hierarchy.** The detail header is a flat `·`-joined run of 8 items all
at 12 px, where `⚠ API error` has the same weight as the model name. Should be
three tiers — identity, state, metadata — with the alarm tier able to preempt.
The rail's *expanded* footer inverts importance (six items, all 10 px, one
weight) while the *compact* row gets it right; expansion currently buys more
chips instead of more hierarchy.

**T1.4 — Composer latency.** `Transcript.jsx` POSTs paste, waits 250 ms
client-side, POSTs Enter, then clears. `send.py:41-54` already does paste+keys in
one call with a 100 ms server-side gap. ~300 ms to acknowledge input, no
optimistic echo. Transcript-scoped, so it only matters if that view gets used.

**T1.6 — Loading states.** Omnibox catalog: 285–515 ms pop-in, refetched on every
⌘K, no cache. Launcher blocks on a real `git worktree add` with no busy state —
double-click makes two worktrees (`OpenOmnibox` gets this right,
`LauncherModal.jsx:97-110` doesn't). Boot flashes **"No tmux windows found"** —
the opposite of the truth.

**T2.2 — Transcript blind window.** Unfixable without owning the agent loop
(parked — see `idea-own-the-agent-loop.md`). Measured gaps, kept because they're
the evidence for that decision:

| Tool | n | p90 | max |
|---|---|---|---|
| Agent (subagent) | 38 | **452.9 s** | **1209.5 s** |
| AskUserQuestion | 6 | 211.3 s | 211.3 s |
| Bash | 277 | 5.3 s | 122.8 s |
| Edit | 40 | 2.7 s | 3.4 s |

Claude Code commits an assistant message only once every `tool_use` in it
resolves, and sidechain records are absent entirely. Cheap partial mitigation if
it ever matters: when `state === "needs-input"`, periscope already captures the
pane every 3 s and knows a dialog is open (`waiting_for`) — render that captured
dialog into the transcript tail.

---

## Dials on things already shipped

- **Layout freeze** (`layoutFreeze.js`): currently thaws only on mouse-leave. If
  holding still reads as *stuck* rather than *stable*, flush on a short idle
  timer as well. The `updates paused` hint exists so the pause is never silent.
- **Changes tab liveness**: refresh is keyed to the pane's `git` field, which has
  a 15 s TTL — so worst case a disk change takes ~15 s to appear. Lower that TTL
  or add a filesystem watch if it feels laggy.
- **Generated-file folding** (`noise.js`): path patterns only, deliberately not
  size — auto-folding by size would hide a large genuine refactor.

---

## Settled, not open

- **Rewrite in Rust / own the PTY** — measured to buy ~1.27 ms and *zero*
  fidelity defects. Not supported by evidence; see the Verdict above.
- **Own the agent loop** — the one version that holds up, and parked
  deliberately. Reasoning and the tripwires that would reopen it:
  `idea-own-the-agent-loop.md`.

---

## Confidence

**Measured on this machine:** all four latency numbers (40-sample probes against prod
:8765); split-pane invisibility (isolated tmux socket); untracked-file blindness (scratch
repo); transcript blind-window distribution (8 JSONLs, 2256 records, 451 tool-call pairs);
`/api/state` latency; omnibox catalog timings; CSS counts (machine-counted, reproducible);
brace imbalance; undefined tokens; `MultiEdit` render path.

**Code-read, not executed:** every other `file:line` claim.

**Inferred — treat as hypothesis:** that a Rust terminal recovers 2–5% of keystroke→glyph
(browser paint was not timed, Claude's Ink render cost was not measured); that tmux's
`#{client_activity}` can express user presence; bucket assignments for a hypothetical Rust
implementation are architectural reasoning, not measurement.

**Not covered:** the probe used one quiet pane — sustained streaming across ~10 panes could
expose GIL/queue contention it cannot see. **That is the one measurement that could still
flip the verdict, and it is worth running before committing to any rewrite.** Also
unexamined: `pids.py`, `resurrect.py`, `bg_commander.py`, `tracks.py`, the `/history` SPA.
The legibility pass never rendered the app — its hierarchy and density claims are derived
from markup and CSS, not from looking at it.

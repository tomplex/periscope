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

## Tier 0 — live bugs, fix regardless of any decision

**T0.1 — Split-pane Claudes are invisible and their alerts are destroyed every 3 s.**
`list-windows -a` with `#{pane_id}` resolves against the *active pane only* (verified
empirically on an isolated socket: a 2-pane window reports one pane). `routes/state.py:93`
feeds that set to `_channel_gc`, which at `channels.py:167` drops `_CHANNEL_ALERTS`
for every pane id not in it. A `notify(need_human)` from a background pane dies within
one poll. Fix: `list-panes -a -F '#{pane_id}'`. ~5 lines.

**T0.2 — `MultiEdit` renders nothing.** `Transcript.jsx:99-105` — `fullInput` has cases
for `Bash` and `Write`, then `default: return ""`; `isEdit` is `Edit` only. Expanding a
MultiEdit shows one line: `Applied 3 edits to /path`. The edits are already on the client
(`history/search.py:263` ships the full `input` dict). Same for `NotebookEdit`.

**T0.3 — ~60 ms of removable forks per pane switch.** `ws.py:51-55` (`setw
window-size manual` + `resize-window`) and `ws.py:114` (`capture-pane`) are subprocesses;
the persistent control client in `tmux_input.py` already exists.

**T0.4 — Untracked files report "clean".** `git_pr.py:58` derives dirtiness from
`git diff HEAD --shortstat`, which ignores untracked. Verified: scratch repo, one new
file, empty shortstat, chip doesn't render. Add `git ls-files -o --exclude-standard`.

**T0.5 — Alerts vanish on restart.** `routes/alerts.py` reads only in-memory
`_CHANNEL_ALERTS`; `channels.py:196-203` already writes every alert to the durable
`events` table. The read path just ignores it.

**T0.6 — `styles.css` doesn't parse cleanly.** 779 `{` vs 781 `}`. Lines 322-327 are an
amputation scar from the grid-view removal. Error recovery eats the following rule.
Harmless today; anything added there vanishes silently.

**T0.7 — 6 CSS tokens referenced, never defined.** `--warn` (7 uses), `--font-mono` (3),
`--muted`, `--border`, `--chip-bg`, `--chip-fg`. Hex fallbacks always render.

**T0.8 — Poll loops can commit an older snapshot over a newer one.** `poll.js:75`,
`alertFeed.js:69` — `setInterval` with async callback, no in-flight guard, no sequence
number. `Transcript.jsx:43` already does it correctly (chained `setTimeout`).

**T0.9 — Merged PRs keep their badge forever.** `window_view.py:158-172` — `linked_pr`
unconditionally overwrites auto-detected PR state and pops `ci`. No expiry, no
cross-check against the `gh` cache being fetched 15 lines earlier.

**T0.10 — Preview tabs never re-fetch.** `PreviewTabInner.jsx:149-176`, dep array is
`[entry.path]`. `/api/fs/read` returns no mtime or ETag.

---

## Tier 1 — the actual polish gap

**T1.1 — Optimistic UI on every tmux mutation.** The template already exists and is
good: `prefs.patchUI` (`prefs.js:106-126`) snapshots, writes eagerly, reverts on failure;
file tabs (`store.js:84-111`) add a 3 s hydration-skip so an in-flight poll can't stomp
the write. Not applied to: rename tab/track, close tab, move across tracks, dissolve,
new tab, auto-rename, start review. Rename is worst — it visibly reverts first.

**T1.2 — Transitions on data-driven change.** All 20 existing transitions are hover
affordances. Rows, chips, status lines, section membership all hard-swap. A correct
`prefers-reduced-motion` block already exists at `styles.css:1070`.

**T1.3 — Stop shifting the rail.** `AttentionSections.jsx:112,142,172,191` render each
section as `{rows.length > 0 && …}` above the tree; RUNNING churns constantly. Compounded
by `RailRows.jsx:143` growing a footer mid-tick. Reserve space or animate membership.

**T1.4 — Composer send is 2 RTTs plus a 250 ms client gap.** `Transcript.jsx:227-250`
POSTs paste, `setTimeout(250)`, POSTs Enter, then clears. `send.py:41-54` already handles
paste+keys in one call with a 100 ms server-side gap. ~300 ms to acknowledge input, up to
2.3 s to see your own turn, no optimistic echo.

**T1.5 — A real diff renderer.** Current `EditDiff` (`Transcript.jsx:84-93`) prints all
old lines as deletions then all new lines as additions — a 1-line change in a 40-line
pair renders 80 rows, 79 of them noise. Needs, in order:
1. Line matching (Myers or histogram) + `+N −M` in the collapsed header
2. MultiEdit (one file block, N hunks) and Write (new-file vs overwrite, labeled honestly)
3. Context collapsing with symbol-carrying hunk headers
4. Syntax highlighting — `preview/highlightCode.jsx` already wraps 8 lezer parsers
   emitting stable `tok-*` classes; needs a lazy import and a widened selector scope
   (currently `.md-doc .tok-*`). Same fix also highlights transcript code fences.
5. Intraline word-diff, only above ~0.5 pairing similarity
6. Gutters / line numbers as a real 2-column grid — the current `::before { content: "- " }`
   is inside the text flow, so it misaligns on wrap and gets copied with selections

Unified, not split: the transcript column is 500–700 px, which gives ~40 columns per side.

**Cumulative per-file diffs — two sources, do not blur them.** Transcript-derived
(`filesTouched.js` already walks it) can honestly render *"the hunks Claude applied this
session"* but has no "before" for the first op and misses Bash and your own edits.
Git-derived answers *"what actually changed on disk"* but needs a new endpoint and a
recorded per-session base ref. Ship the first, labeled exactly that; treat the second as
a separate per-worktree "Changes" tab.

**T1.6 — Loading states.** Omnibox catalog: 285–515 ms pop-in, refetched on every ⌘K,
no cache. Launcher blocks on a real `git worktree add` with no busy state — double-click
makes two worktrees (`OpenOmnibox` gets this right, `LauncherModal.jsx:97-110` doesn't).
Boot flashes **"No tmux windows found"** — the opposite of the truth.

---

## Tier 2 — architectural, inside Python

**T2.1 — Push instead of poll.** Five independent client clocks (3000/3000/1500/2000/3000 ms)
plus a 250 ms sampler. No `EventSource` anywhere in the frontend. `/api/state` costs
10–16 ms server-side — the latency is entirely the client's wait. This is the hard ceiling
on "feels live," and it is the single highest-leverage change in this document.
Note `poll()` is exported and documented as a force-refresh hook but **has zero call sites**.

**T2.2 — Close the worst half of the transcript blind window.** Measured gaps:

| Tool | n | p90 | max |
|---|---|---|---|
| Agent (subagent) | 38 | **452.9 s** | **1209.5 s** |
| AskUserQuestion | 6 | 211.3 s | 211.3 s |
| Bash | 277 | 5.3 s | 122.8 s |
| Edit | 40 | 2.7 s | 3.4 s |

Claude Code commits an assistant message only once every `tool_use` in it resolves, and
sidechain records are absent entirely — so a pane running subagents shows a frozen
transcript for 7.5 min p90, 20 min worst case, on exactly the panes the attention system
routes you to. A rejected tool call discards its whole turn permanently.

Cheap mitigation without any rewrite: when `state === "needs-input"`, periscope already
captures the pane every 3 s and already knows a dialog is open (`waiting_for`). Render
that captured dialog into the transcript tail.

**T2.3 — Design-system reset.** Machine-counted from `styles.css`: **30 distinct font
sizes** (eleven steps between 9 and 15 px, including a live half-pixel tier), **90 distinct
padding shorthands for 145 declarations**, 22 shadows for 25 declarations, 21% dead or
inert, three unrelated design languages in identifiable line ranges. Keep the oklch token
block (`:10-59`) verbatim — status colors pinned at constant lightness/chroma varying only
hue is a real system. Add `--fs-*` and a 4 pt `--sp-*` scale up front.

**T2.4 — Hierarchy.** The detail header is a flat `·`-joined run of 8 items all at 12 px,
where `⚠ API error` has the same weight as the model name. Should be three tiers — identity,
state, metadata — with the alarm tier able to preempt. The rail's *expanded* footer inverts
importance (six items, all 10 px, one weight) while the *compact* row gets it right; expansion
currently buys more chips instead of more hierarchy.

---

## Tier 3 — the fork in the road

Not a backlog item; a product decision.

- **Stay a viewer** of sessions started elsewhere. Everything above applies; nothing is wasted.
- **Own the agent loop** — drive the Agent SDK directly. The only thing that fixes T2.2
  properly, and it dissolves session-identity discovery. A different product, not a rewrite.
- **Own the PTY but keep the CLI** — measured to buy ~1.27 ms and zero fidelity defects.
  Not supported by evidence.

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

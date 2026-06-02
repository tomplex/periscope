# Execution prompt — Segmented Transcript (Warp-style blocks + Claude turns)

> Paste this to a fresh Claude Code session in the periscope repo to pick up
> and execute the segmented-transcript feature. It is self-contained: it points
> you at the spec, the foundation you're building on, and the process to follow.

---

## Mission

Give periscope's split-view **detail pane** a **segmented transcript** content
model: instead of (only) a flat xterm grid, a pane's scrollback renders as a
list of structured, addressable, collapsible **segments** — Preact components —
with the live/active segment optionally backed by a real terminal emulator. Two
segment *sources* feed one renderer:

- **Claude panes → conversation turns** (user msg → assistant work → tool
  calls), sourced from the existing `history/` JSONL pipeline. **No terminal
  emulation needed** — JSONL is already structured. This is the cheap,
  highest-ROI half; do it first.
- **Shell panes → command blocks** (the literal Warp model), recovered from
  **OSC 133** shell-integration sequences in the pane's byte stream. This needs
  per-block terminal emulation (the cost center) — do it last, as its own
  milestone.

## The source of truth

**Read the spec first — it is authoritative:**
`docs/superpowers/specs/2026-06-01-segmented-transcript-design.md`

It covers: the first-principles framing (Warp inverts terminal→block-log; the
unifying "segmented transcript" abstraction), the two segment sources, the
per-block emulation cost center (`pyte` server-side vs xterm-serialize
client-side; static-snapshot completed blocks + live active block + TUI escape
hatch), a phased cost estimate (~5–7 weeks for full shell blocks; the turns half
is ~3.5 days), the recommended sequencing, and the OSC-133-through-tmux spike.

It **supersedes the UI half** of
`docs/superpowers/specs/2026-06-01-claude-turns-overlay-design.md` (turns become
the Claude *segment source* of this renderer, not a separate xterm overlay). The
turns spec's **server half** — `messages_from_jsonl()` + the tool_use/result
pairing pass + the incremental parse cache, proposed for `history/search.py` —
is still valid and is what you build the Claude-turns source on.

## Foundation you're building on (READ THIS — it changed since the spec)

The spec was written assuming a *future* Preact + control-mode foundation. Since
then:

- **The Preact frontend migration shipped (cutover complete).** The frontend is
  now Preact + `@preact/signals` under `static/src/`, bundled to the committed
  `static/dist/app.js`. See the updated `CLAUDE.md` "Frontend" section. The
  segment renderer is a **Preact component** living in the detail pane.
- **The control-mode push model was NOT built** (it was deferred — the spec's
  "depends on control-mode" line is obsolete). The block-source byte stream for
  shell panes is the **existing `/ws/pane` feed** (per *selected* pane), not an
  all-panes feed. `periscope/routes/ws.py` is where pipe-pane + FIFO +
  capture-pane snapshot live; that stream carries the OSC 133 sequences (if they
  survive tmux — see the spike).
- **The detail pane is `static/src/split/Detail.jsx`.** It currently renders one
  of: `<PaneDetail>` (xterm via `<Terminal>`), `<ReviewDetail>` (LGTM iframe), or
  empty. The segmented transcript becomes a new content mode (likely a tab or a
  toggle) on the *pane* view. The `<Terminal>` wrapper
  (`src/terminal/Terminal.jsx` + `src/terminal/terminalCore.js`) is the live
  xterm; the segment renderer sits alongside it.

### Patterns from the migration you should reuse (they were hard-won)

- **Imperative widgets in a Preact-owned host.** The LGTM iframe is created with
  `document.createElement` and parked in a `display:contents` host div that
  Preact never reconciles (see `ReviewDetail`/`ReviewPane`), so re-renders never
  reload it. If a segment's "active" region is a live xterm, use the same
  pattern — don't let Preact reconciliation churn the terminal.
- **Persist-don't-remount.** `<Detail>` keeps opened review iframes mounted
  (CSS-hidden) so switching never reloads. Apply the same to expensive segment
  state.
- **Signals store** (`src/store.js`) is the transient read model; `src/prefs.js`
  (a signal) is the persistence boundary. Per-pane segment state (scroll
  position, expanded segments, active source) likely belongs in signals.
- **Static is served `Cache-Control: no-cache`** (`app.py:_RevalidateStaticFiles`)
  — rebuild + commit `static/dist/` after `static/src/` changes; no stale bundle.

## The process (follow Tom's spec→plan pipeline)

The spec exists but predates the cutover, so it needs a fresh pass against the
*current* (Preact) codebase before planning:

1. **Re-validate the spec against the code.** Dispatch `spec-reviewer` on the
   segmented-transcript spec with cross-references: `static/src/split/Detail.jsx`,
   `static/src/terminal/terminalCore.js`, `periscope/routes/ws.py`,
   `history/search.py` (for the turns parser), and a worry list led by: "does
   OSC 133 survive tmux into the pipe-pane stream?" and "is the control-mode
   assumption fully excised (we use `/ws/pane` per-pane)?". Address findings.
2. **`structure-proposer`** — propose the code structure: the segment renderer
   component(s), the segment-source interface (turns vs blocks), where the OSC
   133 parser lives, the per-block emulation boundary, per-module test strategy.
   Surface for Tom's review.
3. **`writing-plans`** — implementation plan on the approved structure. Phase it
   per the spec's recommended sequencing: (a) segment renderer + Claude-turns
   source (no emulation), (b) OSC 133 shell-integration snippet + block parser
   (output rendered naively first), (c) per-block emulation as a separate,
   greenlit-after milestone.
4. **`plan-reviewer`** before executing.
5. **Execute** in a git worktree (prod runs from `main` on :8765; iterate on
   :8766 — see CLAUDE.md "Development workflow"). Commit-as-you-go.

### The Phase-0 spike (do this before committing to shell blocks)

Verify **OSC 133 sequences survive the tmux layer into `/ws/pane`'s pipe-pane
output** (tmux interprets some OSC itself; recent tmux has passthrough,
unverified for 133). Ship a throwaway script that sources an OSC-133 shell
snippet, runs a command, and confirms the `A/B/C/D` markers appear in the
captured stream. If they don't survive, the shell-blocks half needs a different
boundary signal — but the **Claude-turns half is independent of this** and can
proceed regardless.

## Definition of done (turns half — the first shippable slice)

- A `static/src/...` segment renderer in the detail pane renders Claude turns
  for the selected pane: scrub between turns, expand a turn to see its text +
  tool calls (Bash command + stdout, Read paths, Edit diffs), in-flight tool
  calls show "running…".
- Server: `messages_from_jsonl()` (+ pairing pass + incremental parse cache) in
  `history/search.py`, reached by an endpoint the detail pane polls (piggyback
  on an existing per-pane poll if cheap; the cutover removed the old turns-overlay
  `/api/pane` extension assumption — re-decide the transport).
- No xterm/emulation work. Manual-verified in the browser (no frontend test
  suite — project convention). Backend parser is unit-tested.

## Known related follow-up

`docs/preact-migration-todos.md` tracks a separate rail bug (worktree meta
badges missing) — unrelated to this feature, but in the same area; don't
conflate.

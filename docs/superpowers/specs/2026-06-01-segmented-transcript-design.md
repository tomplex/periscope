# Segmented Transcript: Warp-Style Blocks + Claude Turns — Design Spec

**Date:** 2026-06-01
**Status:** exploration captured — **queued after** the frontend
re-architecture (`2026-06-01-frontend-architecture-design.md`). Depends on the
Preact + control-mode foundation.
**Author:** Tom + Claude (brainstorm session)

---

## Summary

Give periscope's detail pane a **segmented transcript** content model: a pane's
scrollback is rendered as a list of structured, addressable, collapsible
**segments** (Preact components) instead of a single flat terminal grid, with
the live/active segment optionally backed by a real terminal emulator.

"Segment" has **two instantiations**, because periscope has two pane types:

- **Shell panes → command blocks** (the literal Warp model), recovered from
  **OSC 133** shell-integration sequences.
- **Claude panes → conversation turns** (user message → assistant work → tool
  calls), sourced from the existing `history/` JSONL pipeline.

Both feed **one renderer**. This supersedes the UI half of the turns-overlay
spec (`2026-06-01-claude-turns-overlay-design.md`): turns become the Claude
*segment source*, not a bespoke xterm overlay.

## First-principles framing

A traditional terminal is *a 2D character grid that a pty paints*. xterm.js is
exactly that — faithful, and what periscope renders today. Warp **inverts the
model**: it is *a structured log of command-blocks, where each block's output
happens to be terminal-emulated*. The grid is subordinate to the block
structure, not the reverse.

The first-principles realization for periscope: **"blocks" is the general
pattern, and periscope's dominant pane type (Claude) needs the *turns*
instantiation, not the shell-command one.** Warp's block model only applies to
interactive shell usage — command in, output out. A Claude pane is a full-screen
TUI that paints the whole screen and emits no command boundaries. So the
unifying abstraction is the **segmented transcript**, and shell-blocks vs.
Claude-turns are two segment sources for one component renderer.

This is a *stronger* design than the turns-overlay's "overlay decorating the
xterm" — it makes turns and blocks share a renderer instead of being two
bespoke things, and it's the right home now that split view (persistent
`<Detail>` pane) is the default surface.

## Goals

- **One segment renderer** in the Preact `<Detail>` pane, fed by pluggable
  segment sources (Claude-turns, shell-blocks).
- **Claude turns as first-class segments**: scrub between turns, expand tool
  calls (Bash command + stdout, Read paths, Edit diffs), jump to a turn — the
  highest-ROI half, and it needs **zero terminal emulation** (JSONL is already
  structured).
- **Shell command blocks** (follow-on): OSC 133 boundaries → blocks with
  command/cwd/exit/duration/output, collapsible/copyable/re-runnable.
- **A live escape hatch**: a segment running a TUI (vim/less) goes fullscreen /
  falls back to the live terminal rather than rendering as a static block.

## Non-goals

- **Not in the foundation milestone.** This spec is the *next* one; it builds on
  the Preact + control-mode foundation.
- **No bespoke terminal reimplementation beyond block output.** We render block
  *output* with a vt emulator; we do not rebuild a full multiplexer.
- **No editing/branching from a past segment.** Read-only navigation.
- **Shell blocks for remote/ssh shells** without the integration snippet
  installed — out of scope; falls back to the raw grid.

## The two segment sources

### Claude turns (cheap; no emulation)

Source: the `history/` JSONL pipeline. The turns-overlay spec's server half is
reused verbatim — `messages_from_jsonl(path, since_ts)` in `history/search.py`,
the tool_use/tool_result pairing pass, the incremental parse cache, the
`compact_boundary` dividers, the `isMeta`/`isSidechain` filtering. What changes
is **only the UI home**: instead of a modal-side tab + xterm gutter overlay,
turns render as segments in the `<Detail>` pane's segment list.

Each turn segment: role tag, timestamp, 1-line preview, expandable body, and
expandable tool calls (command/args at top, output below; `result: null` →
"running…" spinner for in-flight tool uses).

### Shell command blocks (the cost center; needs emulation)

Source: **OSC 133 semantic-prompt sequences** emitted by the shell, carried in
the captured byte stream.

- `OSC 133;A` prompt start · `OSC 133;B` command-input start · `OSC 133;C`
  output start (command submitted) · `OSC 133;D;<exit>` command finished.
- `OSC 7` reports cwd; command text rides between B and C (or via the OSC 633
  superset that VSCode uses).
- These are emitted by the shell via `precmd`/`preexec` hooks. periscope ships a
  zsh/bash rc snippet the user sources (the iTerm2/VSCode shell-integration
  pattern). Without it: no block boundaries, raw-grid fallback.

The byte stream that carries these is the **same stream the foundation's
control-mode `%output` (and `/ws/pane` pipe-pane) already captures** — the block
parser lives where that feed is consumed. (The foundation spec's Phase 0 spike
includes confirming OSC 133 survives the tmux layer to `%output`.)

Each block: `{ command, cwd, exit_code, start_ts, duration, output }`,
independently selectable / collapsible / copyable / re-runnable.

## The cost center: per-block terminal emulation

Leaving the grid for block structure means partially reimplementing a terminal.
Block output isn't plain text — a command uses color, cursor moves, progress
bars, alt-screen. So:

- **Completed blocks** → render a *static* HTML snapshot, produced by a headless
  vt emulator. Options: server-side (`pyte` in Python → HTML per block) or
  client-side (xterm `serialize` addon / hidden xterm per block; expensive at
  scale). **Server-side `pyte` is the lean default** — one emulation pass per
  completed block, cached.
- **The active block** → stays a *live* xterm until the command finishes, then
  snapshots.
- **TUI escape hatch** → a block launching vim/less goes fullscreen / live
  rather than pretending to be a static block.

Claude turns need **none** of this — JSONL is already structured segments.

## Cost (all on top of the Preact + control-mode foundation)

| Piece | Estimate | Risk |
|---|---|---|
| Segment renderer in `<Detail>` (Preact) + Claude-turns source | ~3-5 days | Medium — depends on foundation |
| Shell-integration rc snippet (OSC 133 + OSC 7 + cmd capture, install flow) | ~1-2 days | Low — iTerm2/VSCode snippets are reference impls |
| Block parser (OSC 133 → per-pane block model over the captured stream) | ~3-5 days | Medium — interleaving with the cursor-sync path + raw-grid fallback |
| **Per-block terminal emulation** (static snapshot + live active + TUI escape) | **~1-2 weeks** | **High — the "reimplement a terminal" tax** |
| Block UI (headers, collapse/copy/rerun/select, kbd nav, search across blocks) | ~1 week | Medium |

**≈5-7 weeks** for the full Warp-quality shell-block experience; the emulation
milestone is the only genuinely risky piece. **The Claude-turns half is the
cheap, high-ROI start** — it's where periscope's pane-time concentrates and it
needs no emulation.

## Recommended sequencing

1. **Segment renderer + Claude turns** first. Highest ROI, no emulation. Proves
   the abstraction on the pane type that matters most.
2. **Shell-integration snippet + block parser** next. Blocks appear for shell
   panes, output rendered naively (text + SGR) — defer full emulation.
3. **Per-block emulation milestone** last, greenlit after 1-2 prove out. The
   risky tax is quarantined and optional.

## Dependencies & relationship to other specs

- **Depends on** `2026-06-01-frontend-architecture-design.md`: the Preact
  `<Detail>` pane (renderer home), the control-mode `%output` feed (block-source
  stream), and the OSC-133-passthrough spike result.
- **Supersedes the UI half** of `2026-06-01-claude-turns-overlay-design.md`. The
  turns server half (`messages_from_jsonl` + parse cache) carries over; the
  "xterm gutter overlay + modal-side tab" UI is replaced by the segment
  renderer.

## Open questions (resolve when this spec is picked up)

1. **Emulation location.** `pyte` server-side vs. xterm-serialize client-side
   for completed-block snapshots. Lean `pyte`; confirm against real block
   output (alt-screen, wide chars, SGR fidelity).
2. **Shell-integration install UX.** `bin/periscope shell-init` appends to
   `~/.zshrc`/`~/.bashrc` vs. a sourced snippet the user adds manually. Confirm
   the install path and which shells (zsh, bash, fish).
3. **Mixed scrollback.** A shell pane that ran Claude then dropped back to a
   shell: how do turn-segments and block-segments interleave in one pane's
   transcript? (Likely: segment source is per-region, switched at the
   Claude-launch / Claude-exit boundary.)
4. **Re-run semantics.** Does "re-run block" send the command back via
   `send-keys` to the live pane, or is it copy-only in v1? Lean copy + an
   explicit re-run affordance later.

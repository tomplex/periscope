# Idea (parked) — owning the agent loop

## What it means

Two different proposals hide under "rewrite periscope":

- **Own the PTY** — periscope runs its own terminal emulator, still spawning the
  `claude` CLI. Measured to buy ~1.27 ms of latency and zero fidelity defects
  (see `ui-polish-audit.md`). Dead on arrival.
- **Own the agent loop** — periscope drives the **Claude Agent SDK** directly
  (`claude-agent-sdk`, "Claude Code packaged as a library") instead of spawning
  the CLI. This is the interesting one, and this doc is about why we're *not*
  doing it.

## What it would uniquely fix

Everything downstream of periscope reading Claude Code's JSONL *after the fact*.
If periscope drove the loop, it would hold this in memory before any of it
touched disk:

- **The transcript blind window** (audit D1) — the worst defect in the system.
  A pane running subagents shows a frozen transcript for 7.5 min p90, 20 min
  worst case; a pending `AskUserQuestion` is invisible; a rejected tool call
  destroys its whole turn. All of it because the JSONL commits at
  assistant-message granularity. Owning the loop is the *only* thing that fixes
  this — the terminal mirror already delivers rendered bytes at ≤1 s, but bytes
  aren't structured turns.
- **Session identity.** No more `pane_session_hook.py`, no duplicate-session
  masking, no cwd-fallback transcript lie — periscope would *assign* session
  identity rather than *discover* it.

## Why we're parking it

**The keystone is Claude Code's own terminal integration, and it stays that way.**
Periscope's job is to watch every Claude session across every pane — sessions the
user starts in Claude Code, rendered by Claude Code, with Claude Code's TUI. That
is the product. Owning the loop turns periscope into the thing you *run Claude in*
— a different, competing product.

Three reasons, strongest first:

1. **Re-implementing CC's surface is unwinnable.** Slash commands, skills,
   subagents, permissions, the status line — CC ships new surfaces continuously,
   and an agent-loop-owning periscope would have to re-render each one, always
   late, always lower-fidelity. This is exactly the trap transcript mode fell
   into. The CC team's terminal integration is *the* place that work lives.

2. **Usage economics are uncertain and possibly worse.** *(Unverified.)* The
   Agent SDK can authenticate against the same OAuth subscription as Claude Code
   (per Anthropic's SDK docs it "honors the same profile resolution"), so it
   *might* draw on the same pool — but the docs don't confirm the subscription
   rate limits apply identically to SDK usage, and the API-key path is metered
   per-token at API rates. Until that's confirmed equal, assume driving the loop
   could cost more or hit different limits than the CLI the user already runs.

3. **It's a product bet, not a refactor.** It rewrites what periscope *is*, not
   how it's built. The 37k-line codebase (channels, history, narrator, git/PR,
   open_ops) exists to serve "dashboard over CC panes" and becomes dead weight
   under a different product thesis.

## The win we keep instead

Stop rendering what Claude Code already renders; render what it **can't**. CC has
no cross-pane view, no cross-worktree diff, no history search, no attention
routing. That surface is periscope's alone and needs no agent-loop ownership:

- Terminal stays the default view (CC's own TUI, GPU-rendered, ~1 ms relay).
- A real diff viewer as its own tab, sourced from `git diff` against the
  worktree — catches your edits and Bash's, which the transcript never can.
- Cross-pane attention, history, PR/CI/Linear surfacing — all reading, not driving.

## What would flip the decision

- Anthropic confirms the Agent SDK draws on the same subscription limits as
  Claude Code, at parity — removing reason 2.
- The transcript blind window (D1) becomes a felt, recurring pain *and* the
  cheap C-bucket mitigation (render the captured `needs-input` dialog into the
  transcript tail — see the audit) proves insufficient.

Even then, reason 1 stands on its own: don't compete with Claude Code on Claude
Code's turf.

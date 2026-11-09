# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

**Primary: the maintainers.** These are developers who run dozens of coding agents
at once (mostly Claude Code, some Codex), spread over 30+ tmux windows in several
sessions on macOS, often across more than one Claude subscription. They use
periscope all day and know every feature. When a design choice has to pick
between density and discoverability, their daily loop wins: density, speed and
keyboard reach count for more than explanation.

**Secondary: macOS developers who install a tagged release** (the `.dmg` or the
in-app Update button). They are served, but they don't break ties.

## Product Purpose

periscope puts every tmux pane on the machine in one place, so many coding
agents can run at once without the user cycling through windows to find out
which one is waiting, finished or broken. It reads each agent's state off its
screen and keeps the pane's branch, PR, CI and Linear ticket beside a live,
interactive terminal. From the same place the user can open new work, answer
the agent that needs them, review PRs and clean up.

Success means the user always knows which agent wants them and can get there in
one move, without ever losing their place in tmux.

## Positioning

All four of these are confirmed. A neighbouring agent dashboard could not
truthfully claim them together:

- **It observes your real tmux.** periscope mirrors the panes you already have,
  and nothing has to be launched through it. tmux stays the source of truth.
  Agent state is read off the screen (the composer and the status line), with
  hooks layered on top.
- **Agents coordinate through it.** The `periscope` MCP server gives each Claude
  pane tools that act on the dashboard and on other agents: `notify`, `link_pr`,
  `set_name`, `spawn_claude`, `peek`, `send_to`, `report` and more. One Claude
  can hand work out to others and hear back.
- **Many accounts, one pool.** periscope pools several Claude subscriptions. New
  sessions go to the account whose weekly limit resets soonest, skipping any
  that are walled. A pane can move to another account mid-session. With one
  account, all of this hides.
- **Attention triage across agents.** The rail's NEEDS YOU, READY, RUNNING and
  PINNED sections, Clawd, and native banners with action buttons all do one job:
  show which of many agents wants you right now and take you there.

## Operating Context

- **Local, single user, macOS.** A FastAPI server runs under launchd at
  `127.0.0.1:17374`. It has no auth and binds to localhost only, because it can
  type into every terminal on the machine. The UI runs in a browser tab or in the
  Periscope macOS app, a thin Tauri shell that loads the same dashboard. The app
  is the only place native notification banners (with Allow / Deny / Reply /
  fix buttons) and the dock badge exist.
- **Used beside the agents it watches.** The user glances at it between tasks,
  dives in to answer a permission prompt or a question, then leaves. State polls
  every 3 s and terminals stream live over a WebSocket.
- **Surfaces:**
  - the split view: the rail of tracks, branches and panes; the detail area with
    Terminal, Transcript, Changes and file tabs; the inspector with the PR,
    Linear ticket, notes, tags, activity timeline and touched files
  - the ⌘K omnibox and the + New tab launcher
  - the reviews queue and PR walkthroughs
  - `/history` search (⌘/)
  - Settings, cleanup, and the header's usage, resource and update pills
- **Workflows and rituals:**
  - one git worktree per branch, opened create-or-focus
  - PR review through walkthroughs that agents write with the bundled
    `walkthrough` skill, with comments routed back to the owning pane
  - a daily 08:00 session poke for each account
  - releases as `vX.Y.Z` tags with a prose changelog that the Update popover shows
- **Keyboard-first:** ⌘K, ⌘F in the terminal, ⌘/ for history, plus
  drag-to-reorder and double-click to rename on the rail.

## Capabilities and Constraints

- **Terminology. Use the codebase's words:**
  - **pane:** a tmux pane, the unit the rail lists
  - **track:** a rail group, one per repo by default or a goal the user names
  - **branch rows:** derived from the pane's live branch, not stored entities
  - **rail, detail, inspector**
  - **attention sections:** NEEDS YOU, READY, RUNNING, PINNED (★)
  - **file tabs:** previews on a pane, not rail tabs
  - also: **worktree**, **walkthrough**, **reviews**, **narrator**, **account**,
    **wall**, **poke**
  - the mascots: **Clawd**, **critters**, **Tamagotchi**
- **Claude panes** get the full treatment: state, transcript, MCP tools, plan
  usage, narrator and auto-rename.
- **Codex panes** get the live terminal and `working` / `idle` / `done` only.
  Ambiguous state is `unknown` and never fires a completion alert.
- **periscope never mutates git or GitHub for reviews.** Agents do. Merge stays
  blocked while walkthrough comments are open.
- **Banner buttons re-check the pane** before they act on an agent.
- **Settings → Fonts** lets the user choose the UI face and the code face from
  a fixed list (`static/src/fonts.js`), so typography must hold up under any
  pick from that list.
- **Requirements:** macOS, tmux, git, `uv` and the Claude Code CLI. Optional:
  `gh` for PRs, CI and reviews; an Anthropic API key for the narrator,
  auto-rename and history summaries; the Codex CLI.
- **Frontend:** Preact with `@preact/signals`, built by Vite into a committed
  `static/dist/` bundle with no build step at boot. Terminals are vendored
  xterm.js. CodeMirror and Mermaid load lazily.
- **Personal-setup assumptions are still baked in** (listed under README →
  Assumptions): the launchd label `com.tom.periscope`, repo roots under `~/dev`,
  account routing that prefers models by name, and the external `claude` zsh
  wrapper the profile picker depends on.
- **Open questions:**
  - whether those personal-setup assumptions get generalized
  - whether periscope ever owns the agent loop instead of mirroring the CLI
    (an idea, not a decision: `docs/idea-own-the-agent-loop.md`)

## Brand Commitments

- **Name:** "periscope", lowercase in running prose. "Periscope" is the macOS
  app's name and the form used at the start of a sentence.
- **Clawd and the Tamagotchi critters are core identity, to keep and extend.**
  Clawd keeps a daily schedule; when something is waiting, Clawd holds up the
  most urgent item and takes you there on click. The critters have hats, props,
  instruments and a stride, and Settings offers other critters. Future surfaces
  may draw on that personality. Sources: `static/src/chrome/critters.js`,
  `critterSprites.js`, `Tamagotchi.jsx`, `static/critters.css`.
- **Voice** (read from the README and CHANGELOG; not separately confirmed):
  plain, direct, second person and concrete. It says what happens and why, and
  it is candid about limits ("Experimental", "Release builds are **unsigned**").
  Release notes are prose for someone running periscope, not a list of commits.
  Rail section labels are terse and uppercase.

## Evidence on Hand

- `README.md`: the full feature account.
- `CHANGELOG.md`: release notes. v0.2.0 (2026-10-28) is the first tagged release.
- `docs/`: one long-form record per area of the failures that shaped it.
  `docs/invariants.md` holds the pane-identity and mirror rules.
- `docs/ui-polish-audit.md`, a Warp-parity audit from July 2026:
  - Terminal latency is measured and good: about 1.4 ms p50 for the full relay.
  - The UI reads as "a 3-second-poll app pretending to be a live one": every
    transition is a hover affordance, nothing moves on data change, there is no
    optimistic UI on tmux mutations, and the attention sections shift the rail
    as they appear and vanish.
- `docs/mockups/`: earlier rail and omnibox explorations, plus
  `rail-implemented-shot.png`.
- `docs/notes/`: field reports and a friction audit, all written by the
  maintainers.
- Icons: `static/icon-{32,192,512,1024}.png`, plus the Tauri bundle icons under
  `src-tauri/`.
- **Absent. Do not fabricate:** user research beyond the maintainers' own notes,
  adoption numbers, testimonials, and benchmarks other than those in the polish
  audit. There is no marketing site.

## Product Principles

1. **The maintainer's daily loop breaks ties.** Density, speed and keyboard
   reach beat explanation. Installers are served but don't set the bar.
2. **Attention first.** Every surface answers "which agent wants me, and how do
   I get there?" before anything else.
3. **Observe, don't own.** tmux and the agents stay the source of truth;
   periscope mirrors, reads and relays. It acts only after re-checking the live
   pane, and it reports ambiguous state as unknown rather than guessing.
4. **Agents are users too.** The MCP tools are a first-class interface to the
   same dashboard a human reads. What lands in READY should carry the reason an
   agent gave.
5. **Personality next to rigor.** The critters are core identity, not garnish,
   in a tool otherwise built for precision.

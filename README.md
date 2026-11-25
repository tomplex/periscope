# periscope

A dashboard for running a lot of coding agents in tmux at once. Every tmux
pane on the machine shows up in one place. Claude Code and Codex panes get
their state read off the screen (working, waiting on you, done), and each one
carries its branch, PR, CI and ticket next to a live terminal.

Built for the "I have 30 tmux windows across 5 sessions with agents running in
most of them, and I want a single pane of glass" workflow. It's a personal tool
first: macOS only, opinionated, and some of my own setup is still baked in (see
[Assumptions](#assumptions)).

## What it does

### The rail

The left side lists every pane, grouped into **tracks** - one per repo by
default, or a goal you name - and split by branch when a track spans more than
one. Above the tracks, attention sections pull out what matters right now:
**NEEDS YOU** (a permission prompt, a question, an API error), **READY**
(finished, not looked at yet), **RUNNING** and anything you've ★ pinned. Each
row shows the model, context usage, PR, CI status, Linear ticket and a
one-line summary of what the agent is doing.

Drag to reorder tracks and panes, double-click to rename, filter by state from
the header.

### A pane up close

Selecting a pane opens it on the right:

- **Terminal** - a live, interactive mirror of the real tmux pane (xterm.js
  over a WebSocket). Type into it, search it with ⌘F, paste an image straight
  into Claude.
- **Transcript** - the Claude conversation rendered as markdown, tool calls
  and edit diffs included.
- **Changes** - the diff for this session or for the whole branch.
- **File tabs** - files opened as tabs next to the pane: highlighted source,
  rendered markdown (Mermaid diagrams too), HTML and images. They refresh
  when the file changes.

The inspector beside it holds the linked PR and Linear ticket, notes and tags,
an activity timeline (commits, CI, clears, renames) and the files the session
touched.

### Opening work

⌘K is one palette for everything: a repo, a path, an existing worktree, a new
worktree off a freshly fetched default branch, a PR by `#123` or URL (checked
out into a worktree for review) or a new track. Opening is create-or-focus -
if a pane is already sitting in that directory, you go there instead. Each
track also has a **+ New tab** launcher: pick a branch, then what to run
(Claude, Codex or your own commands), then the account, model and effort.

Another app on this Mac can start work too. `POST /api/linear/start` takes a
repo, a branch, a Linear issue and a prompt: periscope makes the worktree,
starts Claude, sends it the prompt and links the issue to the card. With a
pull request number (`pr`) in place of the branch, periscope checks the pull
request out as `pr-<N>`, as ⌘K does, and links the PR too. A worktree already
on `pr-<N>` moves to the PR's latest commit by a fast-forward. When the fetch
fails, the worktree has local changes or the branch diverged, periscope uses
the worktree as it is. It answers with the new Claude's `session_id` and its
account's `email` (null when periscope cannot read it). A `warning` in the
answer tells of a problem that did not stop the start, for example a worktree
that stays behind its PR. If a Claude is already on that branch, it reports
that pane and starts nothing. A start with no Linear issue (`ticket`) links
none to the card.
`GET /api/open/catalog` gives each repo's `slug`, the `owner/name` of its
origin on GitHub, so the app can find the repo of a pull request.
`POST /api/select` with a pane's `pid` makes the dashboard select it.

Free text in ⌘K becomes a background command, a headless Claude that hands
the work to new or resumed sessions. **⋯ → cleanup** archives worktrees whose
PRs merged or that have sat idle.

### Agents talk back

Claude panes get an MCP server, `periscope`, whose tools act on the dashboard:

- **Their own card:** `notify` (done / needs you / info), `link_pr`,
  `link_linear`, `set_name`, `open_document` (opens a file as a tab on the
  pane)
- **Other agents:** `spawn_claude` (optionally in a new worktree, on a chosen
  account or model), `list_claudes`, `peek`, `send_to`, `report` (back to
  whoever spawned you), `terminate`
- **History:** `search_history`, `get_history_session`, `resume_session`
- **Repos:** `open`, `catalog`, `create_workspace`, `list_workspaces`

That's what lets one Claude fan work out to others and hear back, and it's
how a finished task lands in READY with a reason attached.

A Claude that periscope starts with a prompt (a `spawn_claude` worker, a
review pane, a Linear start) starts with `--session-id <uuid>`, and
`/api/linear/start` answers with that ID. The command lands in the pane's
shell history, and Claude refuses one `--session-id` twice: after the Claude
exits, Up+Enter fails with "Session ID … is already in use". Run
`claude --resume <uuid>` to continue that conversation, or `claude` to start a
new one.

### Reviews

**◉ reviews** lists every open PR that's yours or waiting on your review,
joined to the pane working on it. *Prepare* asks that pane - or a fresh one,
checking the PR out first - to write a walkthrough with the bundled
`walkthrough` skill. The walkthrough opens in periscope as a series of stops,
each claim with its diff hunks underneath and a comment box, and comments go
back to the owning pane. Merge stays blocked while comments are open.
Periscope itself never touches git or GitHub here; the agents do.

### Notifications

In the browser, attention shows up in the rail and on the mascot. The
[macOS app](#macos-app) adds native banners with buttons: Allow a permission
prompt, reply to a question, ask the agent to fix red CI, clean up the
worktree after a merge, spawn on another account when one is headed for its
limit. Buttons that act on an agent re-check its pane first, and the dock
badge counts what's waiting. **⋯ → settings** toggles each kind.

### Accounts and usage

If you have more than one Claude subscription, periscope pools them. The
header pill shows each account's 5-hour and weekly limits with reset
countdowns. New sessions go to the account whose weekly limit resets soonest,
skipping any that are walled or about to be, and header pins override the
account, model and effort. A pane can move accounts mid-session (→B on its row
resumes it there). At 08:00 each account gets a one-word Haiku message, so the
5-hour window resets around lunch rather than whenever you first type.

With one account all of this just hides.

### And the rest

- **History** (`/history`, ⌘/) - full-text search across every past Claude
  Code session, with project, branch and date filters and a resume button. See
  [`history/README.md`](./history/README.md).
- **Narrator** - a one-line status per Claude pane, and window names that
  follow the work as it drifts. A name you set by hand (or an agent sets with
  `set_name`) sticks.
- **Surviving a reboot** - tmux-resurrect and continuum save the layout, with
  each Claude pane saved as `claude --resume <session>` on its own account,
  model and effort.
- **Codex** - a launch target next to Claude, with working / idle state.
  Experimental, see [Codex](#codex).
- **Clawd** - the pixel critter in the corner keeps a daily schedule, and when
  something's waiting it holds up the most urgent thing and takes you there on
  click. Settings has other critters.

## Requirements

- macOS (launchd, the keychain, the native app)
- tmux, git, [`uv`](https://docs.astral.sh/uv/) (Python 3.11+) and the Claude
  Code CLI
- Node and npm, to build the frontend at install
- Optional: [`gh`](https://cli.github.com/), signed in, for PRs, CI and
  reviews. An Anthropic API key for the narrator, auto-rename and history
  summaries. The Codex CLI.

## Install

```sh
git clone https://github.com/tomplex/periscope.git ~/dev/periscope
cd ~/dev/periscope
bin/periscope install
```

`install` builds the frontend, writes a launchd service that keeps the server
running at <http://127.0.0.1:17374/>, and sets up the pieces that live outside
the repo:

- Claude Code hooks (pane-to-session mapping, the banner Allow button, the
  history indexer) in every account's `settings.json`, and Codex hooks in
  `$CODEX_HOME/hooks.json`
- the `walkthrough` skill, symlinked into every account's `skills/`
- tmux-resurrect and continuum via TPM, plus one `source` line in your
  `tmux.conf`

Then register periscope's MCP server with Claude Code, once per account
(prefix it with `CLAUDE_CONFIG_DIR=...` for each extra one):

```sh
claude mcp add -s user periscope -- uv run --script ~/dev/periscope/channel_shim.py
```

Sessions periscope launches run `claude --dangerously-load-development-channels
server:periscope`, which is what connects them - channels are a Claude Code
research preview. A session you start yourself needs the same flag to get the
tools.

Optionally put an Anthropic API key in `.env` (`cp .env.example .env`) and run
`gh auth login`.

The server binds 127.0.0.1 and has no auth. It can type into every terminal on
the machine, so keep it that way.

`bin/periscope start|stop|restart|status|tail` manages the service and
`bin/periscope uninstall` takes it all back out. To try periscope without the
service, `uv run server.py` serves the committed frontend bundle with no build
step.

### Updating

```sh
bin/periscope update
```

Moves the checkout to the newest [release](#releases), regenerates the launchd
plist, re-registers the hooks, restarts, and waits until `/api/healthz`
reports the new version. You rarely need to run it by hand: once a newer
release is out the header shows an `↑ vX.Y.Z` pill whose popover holds that
release's notes, and its Update button runs the same thing.

Updates install releases only, so commits on `main` that haven't been released
never show up there. To run unreleased code, `git pull` and
`bin/periscope restart`, plus `bin/periscope install-hook` / `install-skill`
if the change touched them. Prefer `update` whenever a release will do - the
plist and hook registration are *generated* by `bin/periscope`, so a plain
pull carries neither. `update` aborts without touching the running server if
the tree is dirty, the checkout has commits the release doesn't, no release
exists yet, or it's run from a linked worktree. The details are in
[`docs/updating.md`](./docs/updating.md).

## Releases

A release is a `vX.Y.Z` tag on `main`, and it's what every install's Update
button offers. Each one carries a prose account of what changed, in
[`CHANGELOG.md`](./CHANGELOG.md). To cut one:

1. Add a `## vX.Y.Z — YYYY-MM-DD` section at the top of `CHANGELOG.md` saying
   what changed for someone running periscope - what's new and what behaves
   differently, not a commit list.
2. Run `bin/release X.Y.Z` on an up-to-date `main`. It refuses without that
   section, or when nothing has landed since the last release. Otherwise it
   bumps the version files, commits them with the changelog, tags and pushes.

Installs pick the release up within the hour. The tag also builds the macOS
`.dmg` and publishes a GitHub Release with the same notes.

## macOS app

Periscope can run as a native app with its own Dock icon and Cmd-Tab entry,
and it's the only way to get notification banners. The app is a thin
[Tauri](https://tauri.app) shell that loads the dashboard from
`http://127.0.0.1:17374`, so the server still has to be running.

Download the latest `.dmg` from
[Releases](https://github.com/tomplex/periscope/releases), or build it from
source with `bin/periscope-app` (needs the Rust toolchain and `cargo tauri`).
Release builds are **unsigned**, so Gatekeeper blocks them on first launch.
After dragging Periscope.app into /Applications:

```sh
xattr -dr com.apple.quarantine /Applications/Periscope.app
```

## Codex

Choose **Codex** in the launcher to open it in the selected worktree. A Codex
resume UUID works too: periscope checks it against the local rollout catalog,
refuses one already bound to a live pane, and runs
`codex resume <uuid> -C <cwd>`.

Codex panes show `working`, `idle` and the derived `done`. Anything ambiguous
is `unknown` and never fires a completion alert. They get the live terminal
only - no transcript, MCP tools, plan usage, narrator or auto-rename.

`bin/periscope install-hook` writes periscope's hook definition to
`$CODEX_HOME/hooks.json` (default `~/.codex/hooks.json`), merged with whatever
is there. Codex won't run it until you open `/hooks` in Codex and trust that
exact command - periscope doesn't bypass hook trust, and neither should
`--dangerously-bypass-hook-trust`. If `/api/healthz` reports the hook installed
but never observed, check it's trusted, then start a fresh Codex session and
submit a prompt, and make sure the server and Codex agree on `$CODEX_HOME`.
Hook attribution is still marked unverified, so the screen and rollout
detectors remain the fallback and ambiguous cases stay `unknown`.

## Assumptions

This started as my tool, and some of my setup is still in it:

- The launchd label is `com.tom.periscope`. ⌘K lists repos under `~/dev` by
  default (⚙ settings → Repo roots changes that) and new worktrees go to
  `~/dev/worktrees/<repo>/<branch>` (or inline, per repo, in settings).
- Account routing prefers specific models by name -
  [`docs/account-routing.md`](./docs/account-routing.md).
- The launcher's profile picker depends on a `claude` zsh wrapper that isn't
  in this repo - [`docs/wrapper-profiles.md`](./docs/wrapper-profiles.md).
- Extra Claude accounts are set up by hand -
  [`docs/second-account-setup.md`](./docs/second-account-setup.md).
- A few settings only have an API (`PATCH /api/settings`): `poke_at` (`""`
  turns the morning poke off), `linear_team_keys` and `bg_account`.

## Development

```sh
npm install
npm run dev     # backend on :17375 + Vite on http://127.0.0.1:5174/
```

The dev backend keeps its own state files, never binds the MCP socket and
never runs the background worker (narrator, poke, update checks), so it can
run next to the real one without touching it. Production serves the committed
`static/dist/` bundle - after changing `static/src/`, run `npm run build` and
commit the result.

```sh
uv run pytest -q    # Python
npm test            # frontend helpers
bin/check           # ruff, ty and biome; --fix to apply
```

[`CLAUDE.md`](./CLAUDE.md) has the architecture, module map and dev workflow.
The `docs/` directory holds the long-form account of each area and the
failures that shaped it - read the relevant one before changing that area.

## License

MIT - see [LICENSE](./LICENSE).

# Account registration from the dashboard

**Status:** draft — decisions D1–D4 settled, O1–O5 open. Pick up with the
open questions, then spec-reviewer. Tier: **Full** (≥6 tasks, spans sessions).

Periscope pools Claude subscriptions ("accounts"), each a `CLAUDE_CONFIG_DIR`.
The account registry is data: `state.json` `accounts`, read through
`store.get_accounts()`, served on `/api/state` as `accounts`, and consumed by
every account surface (routing, usage, poke, pickers, move chip, MCP tools,
hook installer). Every account UI hides while one account is registered. What
is still manual is *adding* an account: the eight-step runbook in
`docs/second-account-setup.md`, ending with a hand edit of `state.json` while
periscope is stopped. This spec makes periscope do that.

## Decisions

### D1 — Scope: create and register (level "b")

Periscope builds the account's config dir, wires it, drives the login, verifies
it, and registers it. Out of scope: removing an account, renaming one, and
re-syncing the per-account files that drift (`.claude.json` MCP servers,
`settings.json` plugins/model/permissions). Removal is excluded because the
login is bound to the dir path and live panes on a removed account would show
as `unknown`; drift sync is its own feature.

### D2 — What "create" does (the runbook, automated)

In dependency order, matching `docs/second-account-setup.md`:

1. **Shell dir** at `~/.claude-<id>`: every top-level entry of `~/.claude`
   symlinked, except the per-account real files (`.claude.json`,
   `settings.json`, `settings.local.json`, `mcp-needs-auth-cache.json`).
   `projects` and `sessions` MUST be symlinks — every read path relies on it.
2. **Settings copied**, not symlinked (Claude replaces a symlinked
   `settings.json` on atomic write).
3. **Periscope's hooks** installed into the new `settings.json` — the one step
   whose absence fails silently (no transcript, narrator, burn, or resurrect
   for that account's panes).
4. **Login** — the human completes the browser OAuth flow (see O1).
5. **Verify** the login is a *different* subscription from every registered
   account (see O2), then **register**.
6. **User-level MCP servers** copied from `~/.claude.json` into the new
   `.claude.json` (after login — see worry W3).

### D3 — The path is `~/.claude-<id>` and never changes

The credential binds to the dir path (keychain item namespaced by
`sha256(path)[:8]`), so a moved dir is a logged-out account. Ids are the next
free lowercase letter (`c`, `d`, …) and the label its uppercase — the one
letter every surface already shows. No user-chosen paths.

### D4 — An unverified account is never routable

Nothing may launch on an account until step 5 passes: a half-registered
account that routing picks is a spawn that fails to authenticate, or — worse —
a second login of the *same* subscription that doubles apparent capacity.

## Open questions (resolve before structure)

**O1 — How the login step runs.** Recommendation: periscope opens a pane (its
normal spawn path, `CLAUDE_CONFIG_DIR` via `tmux -e`) running
`claude auth login --claudeai`, selects it, and watches for completion. The
human does the browser part. Alternative: a dashboard button that only tells
the human which command to run. The pane keeps the whole flow inside periscope
and gives the flow a visible place to fail.

**O2 — How "different subscription" is verified.** Candidates:
`claude auth status --json` (`email`, `orgId`, `subscriptionType`) under each
config dir, or `oauthAccount.emailAddress` in each `.claude.json` (the check
the runbook uses). Compare on email or org id? Two logins to the same email
must be rejected; is a different email in the same org a distinct
subscription? (Probably yes — limits are per user seat — but unverified.)

**O3 — Where a pending account lives.** (a) A registry entry with
`status: "pending"` that `get_accounts()` filters out of routing/usage/poke;
(b) a separate `pending_accounts` key promoted on verification. (a) keeps one
list but every consumer must honor the filter; (b) keeps `get_accounts()`
meaning "routable" with no change to consumers. Recommendation: (b), for
exactly that reason.

**O4 — Failure and abandonment.** The human closes the login pane, logs into
the wrong account, or walks away. Is the half-built dir deleted, left for a
retry, or retried in place? Deleting is safe before login (no credential yet);
after a *wrong* login the keychain item exists and the dir path is burned for
that credential (see D3) — retry must `claude auth logout` first.

**O5 — Dev instance.** The dev server shares the real home dir but has its own
`state-dev.json`. Registration from dev would create real `~/.claude-<id>`
dirs and real keychain items that prod never sees. Recommendation: disable
registration outside prod (`config.is_prod()`), like the activity worker.

## Surface (sketch — settled in structure)

- **UI:** an "add account" entry point that is visible with ONE account (it is
  the only way a second one appears). Candidates: the usage pill's expanded
  view, or the header spawn-pin block. Progress states: building → waiting for
  login → verifying → registered / failed (with reason).
- **API:** `POST /api/accounts` (create + start login) and a status read;
  errors as `HTTPException` with real codes, per repo convention.
- **Server home:** a new module owning registration (dir build, hook install,
  verification). Hook installation currently exists only as inline Python in
  `bin/periscope install-claude-hook`; registration needs it callable from the
  server, so that logic moves into the module and the CLI calls it (the
  `periscope.codex_hook_config` pattern: stdlib-only, `python3 -m`).
- **Not done by periscope:** a `claude-<id>` zsh function in the user's rc.
  Periscope spawns never need it; the setup doc keeps it as optional.

## Worry list (unverified claims — hand to spec-reviewer)

- **W1** `claude auth login` in a tmux pane opens the browser and completes
  without further TUI interaction; exit status signals success.
- **W2** `claude auth status --json` reports `email` for an OAuth login. On
  2026-09-17 it returned `email: null` for the default account from a shell
  that may carry an API key (`apiKeySource` was set) — check with API-key vars
  stripped, as periscope's spawn env does.
- **W3** Claude creates/rewrites `.claude.json` at first launch or login; an
  `mcpServers` block written *before* login may be overwritten. Hence D2 step 6
  runs after login — confirm the order is needed and sufficient.
- **W4** A fresh config dir triggers first-run onboarding (theme, trust
  dialogs) on the first interactive `claude`, which the first spawned pane
  would hit; `dismiss_dev_channels_consent_bg` only handles the dev-channels
  consent.
- **W5** New top-level entries added to `~/.claude` later are absent (not
  broken) in older account dirs — acceptable, or does create need a re-link
  pass available from the UI?
- **W6** The keychain item name for a new dir follows the same
  `sha256(path)[:8]` rule (`usage._keychain_item`), so usage works with no
  per-account discovery.

## Cross-reference files

`periscope/store.py` (registry, `seed_accounts_if_missing`),
`periscope/usage.py` (`_keychain_item`, `cached_plan_usage`),
`periscope/routes/sessions.py` (spawn env, `_window_new_resume`),
`periscope/channels.py` (`dismiss_dev_channels_consent_bg`),
`bin/periscope` (`install-claude-hook`, `account_settings_files`),
`periscope/codex_hook_config.py` (pattern for the moved hook logic),
`docs/second-account-setup.md` (the runbook this automates),
`docs/account-routing.md`, `docs/wrapper-profiles.md`.

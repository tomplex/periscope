# Updating (`bin/periscope update` + `periscope/updater.py`)

`bin/periscope update` pulls, re-provisions, and restarts. **`git pull` +
`bin/periscope restart` is NOT equivalent**, which is the whole reason the verb
exists:

- The launchd plist is *generated* by this script, so plist changes ship as
  changes to the generator. A pull doesn't rewrite `~/Library/LaunchAgents/`,
  and `restart` (`launchctl kickstart -k`) restarts the job against the
  **already-loaded** config — plist changes need `bootout` + `bootstrap`. A
  checkout that pulled past the `NumberOfFiles` 256→1024 fix but never
  re-provisioned still runs with the 256 cap that silently wedges the server.
- Hook registration (`install-hook`) is likewise a script action, not a file in
  the repo. A pull past the Codex-hook or multi-account-config-dir commits
  leaves those panes unhooked, and the transcript view / narrator / resurrect
  go dark for them with no error anywhere.

**Ordering is the safety property.** `git pull --ff-only` runs before anything
touches launchd, so the common failures (dirty tree, diverged branch) abort
with the running server completely untouched. That's what makes the
dashboard-driven path viable: a failed update leaves the server alive to serve
the reason back. Verified by running the verb with a dirty tree and on a branch
with no upstream — both exit 1 with prod's pid unchanged.

The verb deliberately does **not** run `npm run build`: `static/dist/app.js` is
committed, so the pull already carries it, and a build against drifted
`node_modules` can emit a different bundle — dirtying the tree and breaking the
NEXT `--ff-only` pull. It ends by polling `/api/healthz` until the served SHA
matches what it pulled, so "updated" is evidence rather than a claim (and
treats healthz's `unknown` — git absent from the launchd PATH — as success, or
it would report a timeout for an update that landed).

**Past `bootout`, a failure means nothing is running at all**, and the
dashboard that would report it is gone with it. Three guards, in order: `uv` is
resolved BEFORE the pull (a pull that lands then aborts leaves the new
committed bundle talking to the old Python — silent, permanent skew);
`plutil -lint` validates the generated plist before anything is torn down; and
`bootout` is followed by a poll on `launchctl print` until the job actually
leaves, then `bootstrap` retries. `bootout` is asynchronous and periscope has
twice lingered in teardown (20s once, 3+ min once — see below), which would
otherwise land exactly here.

**The verb refuses to run from a linked worktree.** `$REPO` is `dirname "$0"`,
so running it from `.claude/worktrees/foo` would pull the FEATURE branch and
write `WorkingDirectory=<worktree>` into the *prod* plist — leaving prod
pointing at a directory `ExitWorktree` later deletes. Detected by
`git rev-parse --git-dir` differing from `--git-common-dir`. This is separate
from `updater.start()`'s `is_prod()` gate; the script is a user-facing verb and
needs its own.

`GIT_TERMINAL_PROMPT=0` + `ssh -oBatchMode=yes`: under launchd there is no tty
to answer a credential or host-key prompt, and a wedged `git pull` would pin
`updater.running()` true forever, 409ing every later attempt. `STALE_PROC_S`
(15 min) is the backstop — past it, `start()` kills the wedged updater rather
than refusing forever.

**From the dashboard.** `updater.check()` runs on the activity worker's tick
(self-throttled hourly) and counts commits behind the tracked upstream; the
count rides `/api/state` as `update` and renders as a header pill. A probe that
can't answer (offline, no upstream) LEAVES THE LAST COUNT STANDING — going
offline doesn't make the checkout less behind, and publishing 0 would render as
"up to date", the one wrong answer. Assert that through `summary()`, not
`check()`'s return value: the caller discards the return, so a test on it
passes even while `_behind` is being clobbered. Clicking it
POSTs `/api/update`, which spawns the script **detached**
(`start_new_session=True`) — non-negotiable, because the script's `bootout`
tears down the launchd job and would otherwise kill the very process running
it. The POST cannot report success (a successful update kills the server
mid-request), so the two outcomes are read differently: success = the server
dies, the connection banner shows, and the next poll carries `behind: 0`;
failure = the server is still alive and `/api/update/status` has the log tail.

Both `check()` (worker-gated) and `start()` (explicitly gated) are prod-only. A
dev instance runs from a worktree on a feature branch, where `git pull
--ff-only` would fail or pull the WRONG branch over work in progress; `POST
/api/update` 409s there. This also means the pill is invisible in dev by
construction — hence the render test in
`static/src/chrome/__tests__/updatePillRender.test.jsx`, since the browser
can't exercise those states.

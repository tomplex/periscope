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

**Ordering is the safety property.** The fetch + fast-forward runs before anything
touches launchd, so the common failures (dirty tree, diverged branch) abort
with the running server completely untouched. That's what makes the
dashboard-driven path viable: a failed update leaves the server alive to serve
the reason back. Verified by running the verb with a dirty tree and on a branch
with no upstream — both exit 1 with prod's pid unchanged.

**Why `merge --ff-only @{u}` and not `git pull --ff-only`.** On git 2.33,
`git pull --ff-only` dies `Not possible to fast-forward, aborting.` whenever the
checkout is merely AHEAD of upstream — even with nothing to fetch. pull.c asks
"is the fetched head a descendant of HEAD" and never reaches an
already-up-to-date case, so being ahead by one local commit is fatal on its own.
On a fork checkout that can't push to `origin/main`, that wedged the verb
permanently: every update failed, including the ones with nothing to pull.
An explicit `git fetch` followed by `git merge --ff-only '@{u}'` answers all
three cases correctly — no-op when ahead only, fast-forward when behind, refuse
on genuine divergence — and keeps the refusal ahead of anything touching
launchd. Verified against both: `pull --ff-only` exits 128 where
`merge --ff-only @{u}` prints "Already up to date."

The verb deliberately does **not** run `npm run build`: `static/dist/app.js` is
committed, so the pull already carries it, and a build against drifted
`node_modules` can emit a different bundle — dirtying the tree and breaking the
NEXT fast-forward. It ends by polling `/api/healthz` until the served SHA
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
(self-throttled hourly) and counts commits behind AND ahead of the tracked
upstream — one `rev-list --count --left-right @{u}...HEAD`; the counts ride
`/api/state` as `update` and render as a header pill. **`ahead` is what makes
the pill honest.** The update fast-forwards to `@{u}`, which refuses once the
checkout carries local commits AND upstream has moved, so a behind-only pill
armed a button
that was arithmetically incapable of succeeding and gave no reason — the
reported "I can't get the update button to work" on a fork checkout whose
`main` held one unpushed commit while origin moved 10 ahead. With both counts
the popover names the blocker. The button stays ENABLED: the count is up to an
hour stale (an external rebase may already have cleared it), and a refused pull
aborts before launchd is touched. Warn, don't block. A probe that
can't answer (offline, no upstream) LEAVES THE LAST COUNT STANDING — going
offline doesn't make the checkout less behind, and publishing 0 would render as
"up to date", the one wrong answer. Assert that through `summary()`, not
`check()`'s return value: the caller discards the return, so a test on it
passes even while `_behind` is being clobbered. The same check records the
subjects of the commits it would pull (`git log HEAD..@{u}`, capped at
`COMMITS_LIMIT`); they ride `/api/update/status` only — never `/api/state` —
and the pill's popover fetches them on open. Its Update button
POSTs `/api/update`, which spawns the script **detached**
(`start_new_session=True`) — non-negotiable, because the script's `bootout`
tears down the launchd job and would otherwise kill the very process running
it. The POST cannot report success (a successful update kills the server
mid-request), so the two outcomes are read differently: success = the server
dies, the connection banner shows, and the next poll carries `behind: 0`;
failure = the server is still alive and `/api/update/status` has the log tail.

Both `check()` (worker-gated) and `start()` (explicitly gated) are prod-only. A
dev instance runs from a worktree on a feature branch, where `git pull
fast-forward would fail or pull the WRONG branch over work in progress; `POST
/api/update` 409s there. This also means the pill is invisible in dev by
construction — hence the render test in
`static/src/chrome/__tests__/updatePillRender.test.jsx`, since the browser
can't exercise those states.

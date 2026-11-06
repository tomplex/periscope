# Tests

There IS a test suite — small, surgical, run with `uv run`:

```sh
uv run pytest -q                     # full suite (incl. parse_pane / status-line regex regressions in tests/test_panes.py)
uv run pytest tests/test_channel_shim.py # channel-shim reconnect protocol (if these fail spuriously, `uv sync` — see .venv drift below)
uv run pytest tests/test_tmux_mirror.py  # mirror protocol + pyte convergence oracle (spawns a real tmux on -L periscope-mirror-test)
npm test                             # vitest over the Preact app's pure helpers (railTree, classify, attention, launcher branches, …)
```

These exist because each one tracks a class of regression that has bitten
us repeatedly: parse_pane every time Claude tweaks its TUI; the channel
smoke test every time we'd otherwise discover an SDK break only at
runtime when a pane connects. Add cases here when you find a new
variation, don't open a parallel framework.

**Test-isolation invariant — no leaked DB/network threads.** Tests must not
spawn real background threads that touch the activity DB. `cached_plan_usage()`
fires a `_bg("plan-usage", ...)` thread that does a live httpx fetch +
`record_usage_samples` write; leaked as a daemon, it lands in whatever per-test
`ACTIVITY_DB` is live when it finishes — bleeding real `usage_samples` rows into
unrelated tests AND racing `fresh_activity_db`'s connection close
(use-after-free → an intermittent CPython 3.14 sqlite segfault). Two guards in
`tests/conftest.py` keep this closed: the autouse `_no_plan_usage_refresh`
fixture seeds the cache so no plan-usage thread ever spawns, and
`fresh_activity_db` teardown holds `activity._LOCK` before closing `_CONN`.
Patching one call site (e.g. `periscope.app.cached_plan_usage`) is NOT enough —
`routes.state` holds its own binding, so the fix lives at the cache layer.

**The shared snapshots are seeded, so their readers don't fork.** conftest
seeds `tmux._panes_snapshot`, `agent_processes._snapshot_cache` and
`gh._token_cache` with unexpiring empty entries, and clears git_pr's per-path
caches (a stale entry would start a real `_bg` git refresh). So no test runs
`gh auth token`, and `all_pane_ids`, session_status and the process readers
fork no `list-panes` or `ps` — a test that calls the real `list_windows`
still forks one `list-panes`. A test of one of those paths resets the seed
itself (`tests/test_gh.py` overrides `_no_gh_token` by name). The live-tmux
oracle in `test_panes.py` and the live-`ps` tests in
`test_agent_processes.py` and `test_session_status.py` fork on purpose.

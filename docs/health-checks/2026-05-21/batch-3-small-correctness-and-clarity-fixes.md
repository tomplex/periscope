# Batch 3: Small correctness & clarity fixes

**Date:** 2026-05-21
**Classification:** mechanical
**Commit:** `7bf7374`

## Findings Resolved
- #14: Converted all seven `routes/prefs.py` mutation handlers from `async def` to plain `def` (verified none contain `await`); FastAPI now runs them in the threadpool so their synchronous `state.json` writes no longer block the event loop.
- #29: Removed the dead `except subprocess.CalledProcessError` branch from `_send_to_target` (`tmux()` never raises it) and the now-unused `import subprocess`.
- #33: `_refresh_scrape_into_cache` now stamps the cache timestamp on a failed scrape too (keeping the prior cached data), so `cached_scraped_usage` backs off for `USAGE_SCRAPE_REFRESH_S` instead of re-spawning a scrape every poll.
- #34: `renderStream` now sorts a copy (`[...opened].sort(...)`) instead of mutating the `opened` array in place.
- #36: `build_window_view` normalizes the `pr` field to `int` on the linked-PR branch (was `str(linked_pr)`), matching the auto-detected path; updated the one test that pinned the old `str` contract.
- #37: `_task` signature changed to `_task(name, coro)` (name-first, matching `_bg`); flipped all four call sites (`app.py` ×2, `routes/ws.py`, `lgtm.py`) plus the `tests/test_log.py` call site and a stale `tests/test_app.py` docstring.

## Verification
```
$ find . -name '__pycache__' -type d -exec rm -rf {} +
$ uv run pytest -q
........................................................................ [ 23%]
........................................................................ [ 47%]
........................................................................ [ 70%]
........................................................................ [ 94%]
..................                                                       [100%]
306 passed in 4.09s
```

## Notes
- The six fixes were dispatched to parallel subagents. Their individually-reported pytest runs showed transient cross-agent failures (`test_send.py` `assert 1 == 2`; `test_ws.py`/`test_app.py` `TypeError: a coroutine was expected`). These were mid-flight artifacts: one agent observed another agent's half-applied `_task` arg-order change (log.py edited before/after call sites), and another observed the `_send_to_target` agent's discarded `_tmux_mutate` experiment. The final combined state passes 306/306 on a clean serial run.
- #29: the subagent first tried the preferred `_tmux_mutate` switch but it broke `tests/routes/test_send.py` (those tests patch `periscope.routes.send.tmux` by name; switching the symbol bypasses the mock). It correctly fell back to removing the dead branch. Note for a future batch: `_send_to_target` still cannot detect a genuine tmux failure — the silent-success behavior remains; only the unreachable handler was removed.
- `ty` reported diagnostics in `views.py`, `ws.py`, `test_views.py`, `test_log.py` during this batch. All are pre-existing — Batch 3 touched those files for the first time this session, triggering re-analysis. Confirmed not introduced: the `_CHANNEL_ALERTS` "unresolved-import" in `views.py`/`test_views.py` resolves fine at runtime (every test would fail at import otherwise) and is pre-existing private-state coupling that Batch 16 (#39) will rework; the `clean_state`/`tmp_xdg_home` "unused" flags are pytest fixture-injection params `ty` doesn't model; the `ws.py` unused-var flags are on lines untouched by this batch.
- Stale `.pyc` caches caused one subagent a phantom `_task` failure; cleared `__pycache__` before the authoritative verification run.

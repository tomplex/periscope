# Batch 1: Dead code removal

**Date:** 2026-05-21
**Classification:** mechanical
**Commit:** `1966d7b`

## Findings Resolved
- #17: Removed `get_window` from the `periscope.store` import in cleanup.py (only `get_settings` is used).
- #18: Removed the `from typing import Any` line from git_pr.py.
- #19: Removed the `from periscope.panes import list_windows` line from pids.py.
- #20: Removed the unused exported `isLoaded()` function from prefs.js.
- #21: Removed the unused exported `lastLoadError()` function, the `lastError` variable declaration, and both `lastError` assignments inside `loadPrefs()` in prefs.js.

## Verification
```
$ uv run pytest -q
........................................................................ [ 23%]
........................................................................ [ 47%]
........................................................................ [ 70%]
........................................................................ [ 94%]
..................                                                       [100%]
306 passed in 4.86s
```

## Notes
- The `catch (err)` block in `loadPrefs()` no longer references `err` now that the `lastError` assignment is gone. Left the `(err)` binding in place — removing it is outside the scope of finding #21, and an unused catch binding is harmless.
- The `ty` LSP flagged pre-existing diagnostics during this batch that Batch 1 did **not** introduce (the diff confirms those lines were untouched): `git_pr.py` `re.search(...).group()` on a possible `None` — this is finding #31, already scheduled for Batch 3; and `cleanup.py:199` invalid-assignment plus `cleanup.py:262` unused `pid` — pre-existing type issues in `compute_candidates` (the function targeted by finding #5, Batch 9). No action taken — scope discipline.
- Frontend JS is not exercised by pytest, so the prefs.js removals are not test-covered; verified instead by grep that the removed symbols had no other references.

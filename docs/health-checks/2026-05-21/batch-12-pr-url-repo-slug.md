# Batch 12: PR URL repo-slug derivation

**Date:** 2026-05-21
**Classification:** architectural
**Commit:** `1b300b0`

## Findings Resolved
- #12: Removed the hardcoded `github.com/faradayio/fdy` PR URL (3 sites: `grid.js`, `modal.js` ×2). Added `gitutil.github_slug(directory)` — parses `git remote get-url origin` into an `owner/repo` slug (or None for a non-GitHub remote). `git_state_for` now includes `repo_slug` in its dict, which flows via `**git` into both the `/api/state` window view and the `/api/pane` detail payload. A new `prUrl(slug, pr)` helper in `util.js` builds the URL; the 3 frontend sites use it and render the PR badge as plain unlinked text when the slug is absent.

## Verification
```
$ uv run pytest -q
........................................................................ [ 23%]
........................................................................ [ 46%]
........................................................................ [ 70%]
........................................................................ [ 93%]
...................                                                      [100%]
307 passed in 3.95s
$ grep -rn "faradayio/fdy" static/ periscope/
(none)
```

## Notes
- Approach approved by Tom: derive the slug in `git_state_for` / `gitutil` (vs. in `pr_state_for`). `git_state_for` runs for every pane with a repo, so `repo_slug` is present for both auto-detected and Claude-linked PRs; deriving it in `pr_state_for` would leave the linked-PR badge with no slug.
- This was the one finding that is an actual correctness bug, not just debt — PR badges for any non-`fdy` repo previously linked to the wrong GitHub repository.
- `git_state_for` gains one extra `_run` (`git remote get-url origin`) per call — cached via `cached_git_state`'s TTL, and an instant local git op. `tests/test_git_pr.py` mocks `_run` with a command-inspecting callable (not an ordered list), so the added call is handled gracefully.
- Out of scope but noted for follow-up: `routes/pane.py:89` still does `pr["pr"] = str(linked_pr)` — the `str`-vs-`int` inconsistency that Batch 3 (#36) fixed in `window_view.py`. Finding #36 scoped only `window_view.py`; the same one-line fix in `routes/pane.py` was left untouched (not in any finding's scope).

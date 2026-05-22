# Batch 6: parse_pane decomposition

**Date:** 2026-05-21
**Classification:** architectural
**Commit:** `024b1a0`

## Findings Resolved
- #1: Decomposed the ~235-line `parse_pane` god function into a thin orchestrator over 10 module-private helpers — `_split_buffers`, `_is_chrome_line`, `_detect_status`, `_detect_spinner`, `_detect_needs_input`, `_detect_pending_input`, `_extract_recap`, `_last_meaningful_line`, `_detect_asked_question`, `_detect_api_error`, plus `_resolve_state` for the state-priority ladder. `parse_pane` is now a 35-line orchestrator. The chrome-skip predicate (previously copy-pasted in the last-line and asked-question scans) is the single shared `_is_chrome_line`.

## Verification
```
$ uv run pytest -q
........................................................................ [ 23%]
........................................................................ [ 46%]
........................................................................ [ 70%]
........................................................................ [ 93%]
...................                                                      [100%]
307 passed in 3.62s
```

## Notes
- Approach approved by Tom: full per-signal extraction (vs. the two smaller-scope alternatives offered).
- Pure code-movement refactor — every detector body is a verbatim move of the original block; behavior is byte-identical. Verified by `tests/test_panes.py`, which exercises `parse_pane` end-to-end through 5 dataset runners (68 case-rows: 19 parse, 6 ghost-text, 4 last-line, 6 api-error, 33 regex). All pass.
- Two behavior-preservation subtleties handled: `_detect_api_error` checks `API_ERROR_RE` before `TOOL_RESULT_RE` (an API-error line matches both — error check must win); `_detect_asked_question` returns the closest-non-chrome-line's `?`-test result after inspecting exactly one line (mirrors the original's unconditional `break`).
- Done in the `health-check` worktree (`/Users/tom/dev/periscope-health-check`); commit is on the `health-check` branch, not `main`.
- CLAUDE.md still references a top-level `test_parse_pane.py`; the parse_pane tests actually live in `tests/test_panes.py` (the "Peel 5" fold noted in that file's docstring). Pre-existing doc staleness, not addressed here.

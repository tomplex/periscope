# Batch 10: window_new decomposition

**Date:** 2026-05-21
**Classification:** architectural
**Commit:** `8ba5afb`

## Findings Resolved
- #6: Split the ~127-line `window_new` into a thin dispatcher (legacy `mode`→`exec` mapping, then route to resume or plain) plus `_window_new_resume` and `_window_new_plain`. The `_resuming` bookkeeping — previously written in two non-adjacent places — is now co-located inside `_window_new_resume` (both its new-session and existing-session branches). Extracted `_send_and_stamp(target, cmd)` for the shared "100ms settle + send-keys, then note_focus + note_action" tail; `window_new_worktree` now uses it too.

## Verification
```
$ uv run pytest -q
........................................................................ [ 23%]
........................................................................ [ 46%]
........................................................................ [ 70%]
........................................................................ [ 93%]
...................                                                      [100%]
307 passed in 4.48s
```

## Notes
- Approach approved by Tom: dispatcher + resume/plain split + a shared `_send_and_stamp` tail. The `new-window` vs `new-session` creation calls are NOT shared — they genuinely differ, and a shared head helper would have to bridge `window_new`'s `{ok:false}` convention and `window_new_worktree`'s `HTTPException` convention, entangling this batch with finding #3 (Batch 13). Deferred.
- All helpers stay in `routes/sessions.py`, so the `_patch(...)`-based test mocks (which target `periscope.routes.sessions.*`) are unaffected.
- Behavior preserved exactly, including two pre-existing quirks: the resume-into-new-session result dict omits the `exec` key that the resume-into-existing-session result includes; and the new-session branch sends the synthesized `claude --resume <id>` command (matching the original's hardcoded send) while the existing-session branch sends `exec_cmd` — these differ only for the never-issued `mode=resume` + explicit-`exec` combination.
- `window_new`'s resume success paths have no test coverage (only `unknown_session_id` is tested); the decomposition was kept byte-faithful regardless and verified by the full suite.

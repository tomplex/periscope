# Batch 13: FastAPI error-response convention

**Date:** 2026-05-21
**Classification:** architectural
**Commit:** `40714ee`

## Findings Resolved
- #3: Standardized route error reporting on `raise HTTPException(status, detail)` across all 9 modules that previously returned `{"ok": False, "error": ...}` — `auto_rename`, `channel`, `history`, `pane`, `paste_image`, `prefs`, `lgtm`, `send`, `sessions` (~50 sites). Real status codes are now used (400/404/409/500). Success responses keep their existing `{"ok": True, ...}` shape. Documented the convention in CLAUDE.md.
- #38: `cleanup_archive` no longer returns the overloaded `"ok": True` key — it returns `{"archived": [...], "failed": [...]}`; the list lengths describe a partial-success batch.

## Verification
```
$ uv run pytest -q
........................................................................ [ 23%]
........................................................................ [ 46%]
........................................................................ [ 70%]
........................................................................ [ 93%]
...................                                                      [100%]
307 passed in 3.73s
```

## Notes
- Approach approved by Tom: full migration to `HTTPException` (vs. document-only). Executed via 6 parallel subagents (one per route-file group, all disjoint), then reviewed and full-suite-verified here. ~10 route-test files had their error assertions updated from `body["ok"] is False` / `body["error"]` to `r.status_code == N` / `r.json()["detail"]`.
- Necessary structural consequence in `send.py`: `_send_to_target` now raises, so `send_bulk` wraps each fan-out call in a `_send_one` closure that catches `HTTPException` per-target and folds `e.detail` into the per-target `results` entry — one unreachable pane still doesn't fail the whole broadcast. The bulk response shape is unchanged.
- `history.py`'s 404 was a `JSONResponse({"ok": False...}, status_code=404)` — converted to `HTTPException(404, ...)`; the now-unused `JSONResponse` import was dropped.
- Two `HTTPException` detail-only losses: `auto_rename`'s two invalid-JSON errors dropped a `"raw": result[:500]` debug field, and `sessions.py`'s "already resumed" error dropped the separate `existing_target` field (the target is already in the message string). `HTTPException` carries only a string `detail`.
- Left intentionally: `channel_push` returns `{"ok": sent}` where `sent` is a delivery boolean — that is a *successful* route returning a result, not an error path, so it stays. It is a mild `ok`-overload (delivery vs. success) similar in spirit to #38 but was not in either finding's scope — noted as a possible future follow-up.

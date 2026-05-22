# Batch 15: grid.js module split

**Date:** 2026-05-22
**Classification:** architectural
**Commit:** `3a11ef5`

## Findings Resolved
- #16: Split `static/grid.js` (1475 lines, three concerns) — extracted the stream-view renderer to `static/stream.js` (265 lines) and the usage-meter pill to `static/usage-pill.js` (72 lines). `grid.js` is now 1161 lines: grid-view card/session rendering plus the `render`/`poll`/`initGrid` orchestration.

## Verification
```
$ uv run pytest -q
........................................................................ [ 23%]
........................................................................ [ 46%]
........................................................................ [ 70%]
........................................................................ [ 93%]
...................                                                      [100%]
307 passed in 3.95s
$ node --input-type=module --check < {grid,stream,usage-pill}.js   # all 3 OK
$ grep leftover-extracted-symbols static/grid.js   # only renderStream/updateUsagePill import+call remain
```

## Notes
- Approach approved by Tom: extract both modules (vs. usage-pill only + section dividers for stream).
- `usage-pill.js` is a clean one-way dependency: `grid.js`'s `poll()` imports `updateUsagePill`. The `usageEl` DOM lookup moved with it.
- `stream.js` ↔ `grid.js` is a deliberate circular import — `grid.js` imports `renderStream`; `stream.js` imports `passesFilter` / `updateToggleAll` / `poll` (now `export`ed from `grid.js`). All uses are call-time, so ESM resolves the cycle; this matches the documented, tolerated `grid.js` ↔ `modal.js` cycle. A header comment in `stream.js` records why.
- **Verification gap (flagged to Tom up front):** frontend has no automated tests and no browser available here. Verification: extracted blocks are verbatim moves; `grep` confirms no dangling references in `grid.js`; ESM syntax check passes on all three files; pytest confirms Python is untouched. The grid/stream view-switch and the usage pill should be browser-checked when the `health-check` branch is reviewed.

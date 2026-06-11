# Batch 1: Terminology renames

**Date:** 2026-06-05
**Classification:** mechanical
**Commit:** `32697fd`

## Findings Resolved
- #20: renamed the OS-process-id side of the `pid` collision — `_pid_is_periscope(pid)` → `_pid_is_periscope(os_pid)` in pidfile.py (the bounded side), with a comment noting `pid` everywhere else is the periscope per-window id. The window-id `pid` was intentionally left unchanged (full rename was a wire-format + persisted-prefs migration across ~300 sites — see Notes).
- Terminology rename (non-finding): `static/src/sidebar/Sidebar.jsx` → `static/src/inspector/Inspector.jsx`; component `Sidebar` → `Inspector`; updated importers (Detail.jsx, Modal.jsx) and stale `Sidebar` references in comments across store.js, prefs.js, Transcript.jsx, filesTouched.js, RailRows.jsx, Detail.jsx.

## Verification
```
$ uv run pytest -q
435 passed in 5.40s

$ npm run build
✓ 73 modules transformed.
dist/app.js  101.08 kB │ gzip: 31.09 kB
✓ built in 566ms

$ npx vitest run
Test Files  2 passed (2)
     Tests  31 passed (31)
```

## Notes
- Scope correction during the batch: #20 was originally triaged as a mechanical rename, but `pid` turned out to be a serialized `/api/state` wire field (`w.pid`), a persisted-prefs key (annotations stored at `P().windows[pid]`, route `/api/prefs/windows/{pid}`), and ~300 occurrences across 34 files including untyped JS property access. A full `pid`→`pscope_id` rename would be a wire-format + persisted-state migration, not a symbol rename. Tom chose to rename the bounded OS-pid side instead (1 file, zero wire/persistence impact) — the canonical concept name `pscope_id`/`pid`-as-window-id is captured in the lexicon (Batch 7) rather than enforced in code.
- Two `Sidebar` mentions remain in `modal/Modal.jsx` header comments; left untouched because that whole file is deleted in Batch 4.
- Rebuilt `static/dist/app.js` was byte-identical to the committed bundle (the renamed internal component minifies to a mangled name; changed import paths and comments don't appear in output), so no bundle change was committed.

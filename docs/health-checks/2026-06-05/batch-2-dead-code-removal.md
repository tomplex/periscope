# Batch 2: Dead code removal

**Date:** 2026-06-05
**Classification:** mechanical
**Commit:** `69b6c87`

## Findings Resolved
- #9: removed 8 unused exported functions from prefs.js (`getSessionOrder`, `getCollapsed`, `setSessionOrder`, `setCollapsed`, `hasAnnotation`, `deleteAnnotation`, `isPinnedFile`, `removeWorktreeFromRail`).
- #10: removed dead `setTerminalUrlCallback`, the `urlLinkCallback` declaration, and the unreachable `if (urlLinkCallback)` branch in terminalCore.js.
- #11: removed unused `alertDialog` from Dialog.jsx and its doc-comment line.
- #12: removed dead `_RESUME_RE` from resurrect.py and rewrote the misleading comment to describe the actual full-rebuild behavior.
- #13: dropped the unnecessary `export` keyword from six self-wiring overlay openers (openCleanupModal, openCommandsModal, openNewProjectModal, openReviewPRModal, openSettingsModal, openPicker).

## Verification
```
$ uv run pytest -q
435 passed in 5.14s

$ npm run build
dist/app.js  101.08 kB │ gzip: 31.09 kB
✓ built in 472ms

$ npx vitest run
Test Files  2 passed (2)
     Tests  31 passed (31)
```

## Notes
- `static/dist/app.js` was byte-identical after the build — Vite already tree-shakes unused exports, which independently confirms every removed symbol was genuinely dead.
- #9 deliberately scoped narrow: the `migrateLocalStorage` one-time boot migration still writes `session_order`/`collapsed_sessions` for legacy clients. Those keys are now written-but-unread (harmless); gutting that migration's revert logic was judged riskier than the unused keys, so it was left intact.
- #13: each opener self-registers as a click listener inside its own component's `useEffect` (e.g. `#cleanup-btn`), so it is referenced in-file — only the cross-module `export` was dead. `closeModal` (Modal.jsx) was skipped; that whole file is deleted in Batch 4.

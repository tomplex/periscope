# Batch 4: Delete dead Modal UI

**Date:** 2026-06-05
**Classification:** architectural
**Commit:** `44b8ad9`

## Findings Resolved
- #21: deleted `static/src/modal/Modal.jsx` (697 lines), the `openModal` bridge + `window.__periscopeOpenModal` indirection in poll.js, the `<Modal/>` mount + import in main.jsx, and the now-dead `modalTarget` / `modalRenaming` / `modalAutoRenaming` signals in store.js.

## Verification
```
$ grep -rn "modalTarget|modalRenaming|modalAutoRenaming|openModal|modal/Modal" static/src/
(none)

$ npm run build
✓ built in 481ms        # app.js 101.08 kB → 89.99 kB

$ npx vitest run
Test Files  2 passed (2)
     Tests  31 passed (31)
```

## Notes
- Pre-deletion dependency sweep confirmed the Modal subsystem was fully self-contained dead code: `modalTarget`/`modalRenaming`/`modalAutoRenaming` were referenced only by store.js (defs) and Modal.jsx; `openModal`/`__periscopeOpenModal` had no live callers; the modal never opened because nothing set `modalTarget` to a pane (split-view uses `activeTarget` + inline `<Detail>`). The CLAUDE.md note "the poll does not commit while modalRenaming" is stale — poll.js never referenced those signals.
- CSS left untouched on purpose: `.modal-side-*` classes are reused by the live Detail/Inspector (`#detail-side` reuses the modal's sidebar classes per CLAUDE.md), so removing them would break live styling. Orphaned modal-only CSS, if any, is out of scope.
- The build dropped app.js by ~11 KB (101→90), confirming real code was removed, not just an unmounted component.
- **Browser verification deferred to Tom.** Static analysis + a clean build (compiles with Modal gone → no live dependency) + green vitest fully establish correctness, but I did not restart the live prod dashboard to eyeball it (Tom is away; bouncing his load-bearing dashboard unprompted is the wrong call). Recommend a glance after the next `bin/periscope restart`.
- Commit note: the first commit attempt (1a4cae1) captured only the file deletion because a stray `git add` pathspec (the already-removed modal/ dir) aborted the add; amended to 44b8ad9 so the commit is atomic/buildable. Amend (vs. the usual new-commit rule) was the right call to avoid leaving a non-building commit in history.

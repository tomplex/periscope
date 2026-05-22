# Batch 14: Frontend modal-shell & apiCall consistency

**Date:** 2026-05-22
**Classification:** architectural
**Commit:** `5cec970`

## Findings Resolved
- #9: Added `static/modal-shell.js` — `createModalShell({modal, bodyClass, errorEl})` returns `{open, close, showError, clearError, request, isOpen}`. The four standalone modals (`new-project`, `review-pr`, `settings`, `cleanup`) each compose one shell instead of re-implementing the open/close lifecycle (`isOpen` guard, `hidden` class, body class, `pushEscape`/`popEscape`) and the identical `showError`/`clearError`.
- #10: The four modals route their fetches through `shell.request()` (fetch + inline-error-on-failure, errors stay in the modal's error element). The four non-modal grid.js mutation calls (`promote`, `adopt`, `patch`, `archive`) and `modal.js`'s LGTM `add-doc` call now go through the shared `apiCall` wrapper.

## Verification
```
$ uv run pytest -q
........................................................................ [ 23%]
........................................................................ [ 46%]
........................................................................ [ 70%]
........................................................................ [ 93%]
...................                                                      [100%]
307 passed in 3.57s
$ node --input-type=module --check < <each edited .js>   # all 7 OK
```

## Notes
- Approach approved by Tom: modal-shell with an inline-error `request()` method (vs. routing modal calls through `apiCall`, which would move modal errors to toasts). The 4 modals keep inline errors next to the form; the non-modal grid/modal.js calls use `apiCall` (toast), which is appropriate there.
- **Verification gap (flagged to Tom up front):** the frontend has no automated tests and no browser was available in this environment. Verification was: careful diff review + an ESM syntax check (`node --input-type=module --check`) on all 7 touched files + pytest (confirms Python untouched). The modal open/close/escape behavior should be browser-checked when the `health-check` branch is reviewed.
- `modal.js`'s `add-doc` previously read `payload.ok`/`payload.error` — Batch 13 made that route raise `HTTPException` (→ `{detail}`), which had silently broken that hand-rolled check; routing it through `apiCall` fixes it. Its error display moves from a red terminal line to a toast (consistent with other `apiCall` sites).
- `commands-modal.js` has a similar lifecycle shape but was not in finding #9's listed scope (4 modals) — left untouched.

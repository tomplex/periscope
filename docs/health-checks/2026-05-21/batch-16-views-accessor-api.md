# Batch 16: views.py accessor API

**Date:** 2026-05-22
**Classification:** architectural
**Commit:** `8b42955`

## Findings Resolved
- #39: Replaced `window_view.py`'s direct reach into 8 underscore-prefixed internals of sibling modules with an accessor API. Added `channels.channel_state_for(pane_id)` (returns `{attached, unread, alerts}`, holds `_CHANNELS_LOCK` internally) and `panes.record_state_transition(pid, target, state, now_ts)` + `panes.recency_stamps_for(target)`. `window_view.py` now imports only those three accessors plus the existing public functions — no more `channels._CHANNELS_LOCK/_CHANNEL_ALERTS/_CHANNEL_UNREAD/_MCP_SESSIONS` or `panes._acted_at/_completed_at/_focused_at/_prev_state`. `routes/pane.py`, which had the identical channel-state block, also uses `channel_state_for` now.

## Verification
```
$ uv run pytest -q
........................................................................ [ 23%]
........................................................................ [ 46%]
........................................................................ [ 70%]
........................................................................ [ 93%]
...................                                                      [100%]
307 passed in 3.79s
```

## Notes
- Approach approved by Tom: add the accessors and use them in both `window_view.py` AND `routes/pane.py` (vs. window_view.py only) — `routes/pane.py` had the verbatim-identical `with _CHANNELS_LOCK: ...` channel-state block, so leaving it on the privates would have meant the accessor was bypassed by a second consumer.
- Behavior preserved: `record_state_transition` runs before `recency_stamps_for` (the transition may bump `_completed_at`, and the stamp read must see the bumped value) — same ordering as the original inline code. The accessors read/mutate the same `panes`/`channels` module-level dicts, so existing tests that set that state still work; reading the dicts fresh through accessors (rather than capturing them by reference at import) also removes `window_view.py` from the dict-by-reference rebind landmine that `store.py`'s accessor API was designed to avoid.

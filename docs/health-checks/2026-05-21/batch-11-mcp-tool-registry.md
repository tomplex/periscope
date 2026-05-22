# Batch 11: MCP tool registry

**Date:** 2026-05-21
**Classification:** architectural
**Commit:** `a6ef6a0`

## Findings Resolved
- #13: Replaced the inline MCP tool dispatch in `channels.py` with a module-level `_CHANNEL_TOOLS` registry — a list of plain `{name, description, inputSchema, handler}` records. `_list_tools` maps the registry to `types.Tool` objects; `_call_tool` iterates it and dispatches (awaiting the handler when it is a coroutine function). The ~120-line inline `types.Tool(...)` schema block and the 4-branch `if name ==` dispatch are gone; adding a tool is now one registry record plus one `_do_*` handler.

## Verification
```
$ uv run pytest -q
........................................................................ [ 23%]
........................................................................ [ 46%]
........................................................................ [ 70%]
........................................................................ [ 93%]
...................                                                      [100%]
307 passed in 3.86s
```

## Notes
- Approach approved by Tom: a plain module-level list of dict records (vs. a registration decorator). The finding explicitly said "plain dict/list, no framework"; a decorator carrying 20-40 line `inputSchema` dicts above each handler would be more magic than the project's vanilla style.
- The registry holds plain data (not `types.Tool` objects) because mcp `types` is lazy-imported inside `_run_mcp_for_pane`; `_list_tools` constructs the `types.Tool` objects at call time. `_call_tool` uses `asyncio.iscoroutinefunction` to await the one async handler (`_do_spawn_claude_tool`) and call the three sync ones directly — no `is_async` bookkeeping field needed.
- Behavior identical — same 4 tools, same schemas, same dispatch, same `ValueError` on unknown tool. Verified by the full suite, which includes the channel wire-format tests (`tests/test_channels.py`, `tests/test_channel_shim.py`). Note: CLAUDE.md references a standalone `tests/test_channel_smoke.py` that does not exist in the repo — stale doc reference, the channel tests run inside the normal pytest collection.

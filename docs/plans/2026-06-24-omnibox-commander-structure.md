# Omnibox commander — code structure proposal

Structure for the omnibox-commander feature. Spec:
`docs/superpowers/specs/2026-06-23-omnibox-commander-design.md`. This is the
structural HOW, not a re-litigation of the spec; the spec already pins most
module touches and resolved review questions. Where I diverge or flag a close
call it is marked.

The feature is mostly a **rename + strip + small adds**, not greenfield. No new
classes, no new abstractions, no new patterns — it rides periscope's existing
flat MCP-tool registry, the existing `_send_to_target`, the existing
`open_target`/`build_catalog`/`create_workspace` functions, and the existing
`/api/pane/turns` transcript renderer. The structural work is: (1) doing the
rename cleanly, (2) one new async `ensure_commander` with a single-flight lock,
(3) three new tool records + one handler-behavior change, (4) one new thin
route, (5) one new frontend render branch.

---

## 1. Spec pushback

Nothing structural to push back on. The spec's escalation choices already match
the taste rules: everything stays plain functions over a frozen-dataclass marker
(`FirstMateMarker` → `CommanderMarker`), no service classes, no new exception
hierarchy, error mapping is the existing `ValueError → {"ok": False, "error"}`
convention for MCP tools and `HTTPException` for the route. Two items are worth a
second look but I land on the spec's side:

- **The `first_mate` → `commander` rename via refactor-mcp, done first as a
  mechanical commit.** Endorsed (see §4). The spec already flagged the churn
  (≈80 identifier hits across 5 source + 5 test files) and offered "keep the
  names" as a fallback. I recommend **doing the rename**, because the
  module's entire reason for existing inverts (observer → actuator) and leaving
  `first_mate` identifiers on a "commander" makes every future reader
  re-derive that the two are the same thing. But it MUST be one isolated
  mechanical commit before any behavior change — see §4 for the boundary.

- **The SQLite `first_mate` marker table rename.** I diverge slightly from a
  naive reading: **rename the table in the schema DDL to `commander`, but do NOT
  write a data migration.** Rationale below in §6. This is the one place "rename
  everything" would add a migration nobody needs.

---

## 2. Assumptions

Spec gaps I filled; each is a structural assumption the plan should confirm.

1. **`ensure_commander` lives in `commander.py`** (the renamed module), not in
   `activity.py` or the route. It is the renamed `supervisor_pass` logic. Both
   callers (lifespan, route) import it from there. (Spec implies this; I'm making
   it explicit.)
2. **The console-mode polling/idle state machine lives inside `OpenOmnibox.jsx`
   as a fourth render branch + a small `useCommanderConsole` hook in the same
   file**, not a separate `CommanderConsole.jsx` component file. Justified in §8
   — it is one of four sibling branches already co-located, and extraction would
   split a single state machine across two files for no reuse.
3. **`/api/command` request body is `{text: str}`** and the response is
   `{session: str, index: int}` (the commander pane's address for tailing).
   503 on un-ensurable commander (dev, not authed).
4. **`catalog()` and `create_workspace()` MCP tools are ungated** (any pane),
   matching the spec's "general channel tools" note. `open()` likewise. Only the
   captain's-log tools stay commander-gated.
5. **Idle detection threshold** is a module-level const in the frontend
   (`COMMANDER_IDLE_MS`), default ~4000ms of no transcript growth. Spec says
   "N seconds", v1 heuristic — I pin it as a named constant so it is tunable in
   one place.
6. **The marker's `pane_id` is the source of truth for the commander's address.**
   The route resolves `{session, index}` from `list_windows()` by matching
   `marker.pane_id` (spec §routes/command.py is explicit; restating because two
   other call sites — narrator skip, `spawn_claude` caller-is-commander — also key
   off `marker.pane_id == pane`, so it's a shared predicate worth naming once).

---

## 3. File layout

```
periscope/
  commander.py            RENAMED from first_mate.py. Strip the heartbeat/digest
                          machinery; keep spawn + role prompt + marker constants;
                          add async ensure_commander (module-level asyncio.Lock).
  channels.py             CHANGED. Drop fleet_digest tool + _serialize_digest +
                          _schedule_first_mate_emit + the need_human interrupt hook.
                          Rename _require_first_mate → _require_commander. Add three
                          tool records (create_workspace, open, catalog) + handlers.
                          Change _do_spawn_claude_tool: caller-is-commander → cwd-anchored.
  activity.py             CHANGED. Strip first-mate branch from _worker_tick + delete
                          _emit_pending_first_mate. Rename marker fns + table DDL
                          (set/get/clear_commander, CommanderMarker, `commander` table).
                          Keep captain_log untouched.
  narrator.py             CHANGED. Rename _is_first_mate → _is_commander + constant
                          imports; move the skip into the tick() candidate loop
                          (early continue) so a hidden pane costs no Haiku call.
  app.py                  CHANGED. Lifespan: replace register_bridge_project() with
                          `await commander.ensure_commander()` (prod-gated) + a
                          one-shot archive-stale-bridge-project migration.
  routes/
    command.py            NEW. POST /api/command {text} → ensure_commander +
                          _send_to_target + return {session, index}; 503 path.
  routes/state.py         CHANGED. Exclude the commander row from the final `result`
                          immediately before return (after _channel_gc + pid attach).

static/src/
  open/classify.js        CHANGED. Append synthetic {kind:"command"} card when query
                          non-empty (pinned last).
  overlays/OpenOmnibox.jsx CHANGED. Add `command` to KIND_META; add a command arm to
                          pick(); add the console render branch + useCommanderConsole
                          hook (transcript-tail poll + idle + busy + Esc-leaves-running).

tests/
  test_commander.py        RENAMED from test_first_mate.py. Delete heartbeat/
                           divergence/supervisor-respawn/register_bridge tests
                           (they import dropped pure fns). Add ensure_commander
                           single-flight test.
  test_commander_spawn.py  RENAMED from test_first_mate_spawn.py. Adapt to
                           _spawn_commander; drop respawn-loop assertions.
  test_activity.py         CHANGED. Rename marker tests; delete tick-emit tests.
  test_channels.py         CHANGED. Delete fleet_digest tests. Add create_workspace/
                           open/catalog handler tests + spawn-cwd-anchoring test.
  test_narrator.py         CHANGED. Rename + add skip-commander-in-tick assertion.
  routes/test_command.py   NEW. Route test (ensure + send mocked; 503 path).
  routes/test_state.py     CHANGED. Add commander-excluded-but-not-GC'd assertion.
```

---

## 4. The rename: structure & sequencing

**Rung: mechanical refactor, isolated commit, before any behavior change.**

The rename touches 5 source files (`first_mate.py`, `activity.py`, `channels.py`,
`narrator.py`, `app.py`) and 5 test files (~80 identifier hits total). Symbols:

| Old | New |
|---|---|
| `periscope/first_mate.py` | `periscope/commander.py` |
| `FIRST_MATE_SESSION` (`"bridge"`) / `FIRST_MATE_WINDOW` (`"first-mate"`) | `COMMANDER_SESSION` / `COMMANDER_WINDOW` (values: keep `"bridge"` / rename window to `"commander"`) |
| `set/get/clear_first_mate` | `set/get/clear_commander` |
| `FirstMateMarker` | `CommanderMarker` |
| `_require_first_mate` | `_require_commander` |
| `_is_first_mate` | `_is_commander` |
| `_spawn_first_mate` | `_spawn_commander` |
| `first_mate` SQLite table | `commander` table (DDL only — see §6) |

**Structural directive for the plan: do the rename as ONE commit that is a pure
identifier/file rename with zero behavior change**, via refactor-mcp (LSP-backed
symbol rename) so cross-module call sites move atomically. Run `uv run pytest -q`
green after it. *Then* the strip/add commits land on the renamed surface. Folding
the rename into behavior commits would make every behavior diff unreviewable
(rename noise drowns the real change) and break `git bisect` granularity — which
CLAUDE.md explicitly calls the recovery path for this single-user tool.

**Value of the tmux session/window constant values:** keep `COMMANDER_SESSION =
"bridge"` (a live prod run already has a `bridge` tmux session + the stale
`bridge` project row the migration archives; changing the session name strands
that running pane and the migration's `tmux_session == COMMANDER_SESSION` match).
Rename only the *window* (`"first-mate"` → `"commander"`) since nothing keys off
its value. Flag in plan: confirm no prod-side `bridge`-named expectation beyond
the project row.

**`refactor-mcp` caveat:** the `first_mate` *string* table name inside SQL DDL
and the `first-mate-prompt.txt` / `first-mate.disabled` file paths are string
literals, not symbols — LSP rename won't touch them. The plan handles those as
explicit edits (and `first_mate.disabled` is deleted outright with the kill-switch).

---

## 5. `periscope/commander.py` — module structure

**Rung: plain functions + one frozen dataclass (kept from the marker) + one
module-level `asyncio.Lock`.** No class. The module is spawn IO-glue + a role
prompt constant + the ensure function. This matches the spec and the taste rules
(no coupled mutable state to encapsulate; the marker is a frozen value-object
that lives in `activity.py`, not here).

**After the strip, the module is roughly:**

```python
COMMANDER_SESSION = "bridge"
COMMANDER_WINDOW = "commander"
ROLE_PROMPT = """…orchestrator prompt…"""   # rewritten (spec §Role prompt)

_SPAWN_LOCK = asyncio.Lock()   # single-flight: lifespan boot vs first /api/command

async def ensure_commander() -> activity.CommanderMarker | None:
    """Ensure exactly one live commander pane; (re)spawn if the marker's pane
    is gone. Single-flight via _SPAWN_LOCK. Returns the marker (None off-prod)."""
    async with _SPAWN_LOCK:
        marker = activity.get_commander()
        live = {w.get("pane_id") for w in list_windows()}
        if marker is not None and marker.pane_id in live:
            return marker
        await asyncio.to_thread(_spawn_commander, now=int(time.time()))
        return activity.get_commander()

def _spawn_commander(*, now: int) -> None:
    """Blocking: ensure session, open window, claude_exec + --append-system-prompt
    + --model sonnet + read-only tool flags, dismiss consent, stamp, set marker.
    Synchronous (time.sleep + blocking tmux) — callers wrap in asyncio.to_thread."""
```

**Why `ensure_commander` is async and `_spawn_commander` stays sync (the spec's
call, restated as a structural rule):** the spawn is `time.sleep` +
blocking-subprocess shaped and today runs in the worker *thread*. With the worker
tick gone, its only callers are the lifespan and the route — both on the single
main event loop. So the public entry point is `async def`, the lock is an
`asyncio.Lock` (not `threading.Lock` — wrong primitive for loop-affine callers),
and the blocking body runs via `asyncio.to_thread` so it never stalls the loop
serving every other pane's MCP connection. This is the one genuinely new piece of
structure in the feature; it is correct as specced.

**Read-only tool lockdown flags** (`--allowedTools Read,Grep,Glob` /
`--disallowedTools Bash,Edit,Write` — exact spelling TBD in planning per spec) and
`--model sonnet` are appended to the `exec_cmd` string in `_spawn_commander` only.
Structurally these are just extra tokens on the existing `claude_exec()` launch
line — no new function.

**Strip (delete from this module):** `fleet_diverged`, `heartbeat_decide`,
`_render_delta`, `Push`, `_LAST_SENT`, `build_fleet_digest`, `assemble_pane_views`,
`_curate_pane`, `PaneDigest`, `FleetDigest`, `supervisor_pass`,
`first_mate_disabled`, `register_bridge_project`. After the strip the module drops
from ~360 LOC to ~120 — a single tight concern (spawn + ensure a hidden pane).

**Module docstring** must be rewritten — the current one describes a "pure
decision core / digest substrate" that no longer exists.

**Test strategy (`tests/test_commander.py` + `tests/test_commander_spawn.py`):**
- `ensure_commander` single-flight: **unit** — two concurrent `await
  ensure_commander()` calls spawn once. Mock `_spawn_commander` (count calls),
  drive both coroutines via `asyncio.gather`, assert one spawn. The lock is the
  unit under test; a real tmux spawn adds nothing.
- `_spawn_commander`: **integration, real tmux** — adapt the existing
  `test_first_mate_spawn.py` (already `@needs_tmux`, isolated `-L` socket, stub
  `claude_exec`). Assert spawn → marker set with a real pane_id; assert the launch
  command carries `--model sonnet` + the lockdown flags. Drop the
  supervisor-respawn-loop assertions (no loop exists).
- Delete all heartbeat/divergence/digest/`register_bridge` tests — they import
  the now-deleted pure functions and break at collection if left.

---

## 6. `periscope/activity.py` — marker + table, strip the tick

**Rung: unchanged — plain functions over the frozen `CommanderMarker`.** The
marker accessors (`set/get/clear_commander`) stay exactly as they are, renamed.
`captain_log` + `append_captain_log` / `recent_captain_log` are **untouched**
(durable scratch, the spec keeps them even though no UI renders them).

**Strip:** delete `_emit_pending_first_mate`; remove its `await` call in
`run_worker`; remove the entire first-mate `try` block in `_worker_tick` (the
`supervisor_pass` call + `build_fleet_digest`/`assemble_pane_views`/
`heartbeat_decide` + the `_fm_push` stash). The narrator tick call stays. After
the strip `_worker_tick` is just: capture panes → narrator.tick → checkpoint.

**Table rename — the one divergence (DDL-only, no data migration):**

The `first_mate` table is a **singleton marker (id=1) holding a tmux `pane_id`** —
the most ephemeral possible value. A `pane_id` (`%47`) does not survive a tmux
server restart, let alone anything worth migrating. The boot-spawn + lazy-heal
path *rewrites* the marker on every prod start. So:

- Rename the `CREATE TABLE IF NOT EXISTS first_mate (…)` DDL to `commander (…)`.
- Do **not** copy rows from the old table. The old `first_mate` row (if present)
  holds a dead pane_id; the first `ensure_commander()` at boot overwrites the new
  `commander` row with a live one. A migration would copy a value that is
  guaranteed stale within one restart.
- Leave the orphaned `first_mate` table in live DBs (SQLite `CREATE TABLE IF NOT
  EXISTS` just stops referencing it). It is one empty/stale singleton row; not
  worth a `DROP`. *Flag for plan:* if Tom wants tidiness, one `DROP TABLE IF
  EXISTS first_mate` in the lifespan migration is cheap — but it is cosmetic.

This is the "rename everything" exception called out in §1: the *identifier*
rename is worth it; a *data* migration for a self-rewriting ephemeral marker is
pure churn.

**Test strategy (`tests/test_activity.py`):** rename the marker round-trip tests
(`set_commander` → `get_commander` → `clear_commander`) — **unit, real SQLite**
(`clean_state` fixture's tmp DB; these already run against a real sqlite file,
which is the right call — no mock). Delete the `_emit_pending_first_mate` /
tick-emit tests.

---

## 7. `periscope/channels.py` — re-gate, drop, add three tools

**Rung: unchanged — the flat `_CHANNEL_TOOLS` list-of-dicts registry + one
`_do_*_tool(pane, arguments)` handler per record.** This is exactly the existing
pattern; the three new tools are three records + three handlers, no new structure.
The registry comment already says "adding a tool is one record plus one handler" —
honor it.

**Drop (dead-code removal, spec §channels):**
- The `need_human` → `_schedule_first_mate_emit` interrupt block in
  `_do_notify_tool` (channels.py:203-212), and `_schedule_first_mate_emit` itself
  (its only caller).
- The `fleet_digest` tool record + `_do_fleet_digest_tool` + `_serialize_digest`
  (only caller is the dropped handler).
- The `if kind != "fleet_digest"` special-case in `emit_channel_event`
  (channels.py:757) — with no fleet_digest pushes, every channel event records to
  the timeline. **Flag:** simplify the guard to unconditional `activity.record`,
  but confirm no other caller passes a `kind` that should be suppressed (grep
  shows only fleet_digest used it).

**Re-gate:** `_require_first_mate` → `_require_commander` (rename only; same
singleton-marker check). Still guards `captains_log_read/append`.

**Add three tool records + handlers** (all ungated — general channel tools):

```python
def _do_create_workspace_tool(pane, arguments):
    name = str(arguments.get("name", "")).strip()
    if not name:
        return _tool_result({"ok": False, "error": "name is required"})
    from periscope import workspaces
    ws = workspaces.create_workspace(name=name, base_repo=arguments.get("base_repo"))
    return _tool_result({"ok": True, "id": ws.id})

def _do_open_tool(pane, arguments):
    from periscope import open_ops
    try:
        descriptor = _open_descriptor(arguments)   # path | repo+branch | repo+pr
        result = open_ops.open_target(descriptor)  # HTTP-free; ignores result.ui
    except ValueError as e:
        return _tool_result({"ok": False, "error": str(e)})
    return _tool_result({"ok": True, "session": result.tmux_session})

def _do_catalog_tool(pane, arguments):
    from periscope import open_ops
    return _tool_result({"ok": True, **open_ops.build_catalog()})
```

**Structural note — `_open_descriptor` helper:** `routes/open.py` already has
`_to_descriptor(OpenBody)` doing path|branch|pr dispatch, but it takes a Pydantic
`OpenBody` and raises `HTTPException`. The MCP handler takes a plain `dict` and
must raise `ValueError` (caught into `{"ok": False}`). These are **two different
boundaries** (HTTP vs MCP) with two different error types — do NOT try to share
the function. Add a small `_open_descriptor(arguments: dict) -> Descriptor` local
to channels.py that raises `ValueError`. This is the taste rule "validate at the
boundary, each boundary owns its error type" — not a reinvention, a deliberate
second boundary. (The shared core they *both* call is `open_ops.open_target`,
which is correctly already extracted.)

**Error mapping:** `open_target` raises plain `ValueError` (confirmed:
`routes/open.py` catches `ValueError`). The MCP convention here is
`{"ok": False, "error": str(e)}`, NOT `HTTPException` (that is the *route*
convention; MCP handlers return tool-result bodies). The spec has this right.

**`spawn_claude` caller-is-commander cwd-anchoring** (`_do_spawn_claude_tool`,
the one behavior change to an existing handler):

The current flow (channels.py:504-510): `workspace="new"` → `resolve_worktree_session(cwd)`;
else `session = caller_session or "spawned"`. The change, per spec:

```python
from periscope import activity, open_ops
marker = activity.get_commander()
is_commander = marker is not None and marker.pane_id == pane

if is_commander:
    # Placement derives from cwd, never the commander's own hidden session.
    anchored = open_ops.resolve_worktree_session(cwd)
    if anchored is None:                       # cwd not in a git repo
        return _tool_result({"ok": False, "error": "cwd is not in a git repo"})
    session, project = anchored
else:
    # unchanged: workspace="new" anchors; else caller session
    anchored = open_ops.resolve_worktree_session(cwd) if workspace == "new" else None
    session = anchored[0] if anchored else (arguments.get("session") or caller_session or "spawned")
```

The key structural point: the commander branch **must not fall through to the
`caller_session` default** (channels.py:510) — that default is the commander's own
hidden session, the exact misfile this prevents. A non-git `cwd` errors the tool
rather than defaulting. The `is_commander` predicate (`marker.pane_id == pane`) is
the same one `_require_commander` and the narrator skip use — three call sites
share it. **Close call (see §10):** whether to extract a tiny
`activity.is_commander_pane(pane) -> bool` helper vs inline the two-line check at
each site.

**Test strategy (`tests/test_channels.py`):**
- `create_workspace` / `open` / `catalog` handlers: **unit** — stub
  `workspaces.create_workspace` / `open_ops.open_target` / `open_ops.build_catalog`
  (spec §Testing). Assert the success body shape and the `ValueError →
  {"ok": False, "error"}` mapping for `open`. (`open_target` itself has real-tmux
  coverage in `test_open_ops.py` — don't re-test it here.)
- `spawn_claude` commander cwd-anchoring: **unit with the marker set** — set a
  commander marker to the caller pane, stub `resolve_worktree_session` to return a
  session for `cwd=<repo root>` and a different one for `cwd=<worktree path>`,
  assert the spawn derives session from cwd not `caller_session`; assert the
  non-git `cwd` → `{"ok": False, "error": "cwd is not in a git repo"}` path. This
  is the one place a mock is right (a real spawn needs a real Claude — a poor unit
  oracle); the *placement decision* is pure-ish logic reachable by stubbing the
  one resolver.
- Delete `test_emit_channel_event_skips_fleet_digest`, `test_fleet_digest_tool_*`.

---

## 8. `periscope/routes/command.py` — new route

**Rung: plain function, thin route — the existing route convention.** Mirrors
`routes/open.py`'s shape (Pydantic body in, dispatch to a core fn, `HTTPException`
on failure).

```python
class CommandBody(BaseModel):
    text: str

@router.post("/api/command")
async def command_endpoint(body: CommandBody):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "text is required")
    marker = await commander.ensure_commander()
    if marker is None:
        raise HTTPException(503, "commander unavailable (dev, or failed to start)")
    # Resolve {session, index} from the marker's pane_id via list_windows —
    # NOT from _send_to_target's return (the marker stores %N, not session:index).
    target = _target_for_pane(marker.pane_id)   # list_windows match → "session:index"
    if target is None:
        raise HTTPException(503, "commander pane not found in tmux")
    session, _, index = target.partition(":")
    _send_to_target(target, paste=text, keys=["Enter"])
    return {"session": session, "index": int(index)}
```

**Structural notes:**
- `_target_for_pane(pane_id) -> str | None` is a small local helper:
  `list_windows()` → find the row whose `pane_id == marker.pane_id` → build
  `f"{session}:{index}"`. It lives in `command.py` (single consumer). The spec is
  explicit that the address comes from `list_windows`, not from `_send_to_target`.
- **503, not 500**, for the un-ensurable / dev case (real status code per the
  route error convention — 503 "service unavailable" fits "commander can't start
  here"). `ensure_commander` returns `None` off-prod (the `is_prod()` gate inside
  `_spawn_commander` makes the marker never appear).
- No new transcript endpoint — the console reuses `GET /api/pane/turns`.

**Test strategy (`tests/routes/test_command.py`):** **unit** — mock
`commander.ensure_commander` and `_send_to_target`; stub `list_windows` to return
a row matching the marker pane_id. Assert: (a) delivers `paste=text, keys=["Enter"]`
to the resolved target and returns `{session, index}`; (b) `ensure_commander →
None` yields 503; (c) empty text → 400. End-to-end send→act is browser-verified
(a real Claude is a poor unit oracle — project norm).

---

## 9. `narrator.py`, `app.py`, `routes/state.py` — the visibility/strip edits

**`narrator.py` — skip the commander in the tick loop, not at the rename site.**
Structural rule from the spec, restated: move the skip into the **candidate-scan
loop in `tick()`** as an early `continue` once a pane is identified as the
commander (so it costs zero Haiku calls), and keep `_is_commander` (renamed) as a
pure predicate. Do NOT leave the skip only at the post-Haiku rename site
(narrator.py:332) — that still spends Haiku every 30s on a hidden pane. The
predicate stays a plain function. **Test:** unit — assert a pane matching the
commander marker is skipped before the Haiku call (mock the Haiku call, assert
it's not invoked for the commander pane).

**`app.py` lifespan — boot-spawn + stale-project archive migration.** Replace
`first_mate.register_bridge_project()` (app.py:97) with, prod-gated:

```python
await commander.ensure_commander()        # boot-spawn; lazy heal covers later death
_archive_stale_bridge_project()           # one-shot: find project whose
                                          # tmux_session == COMMANDER_SESSION, archive it
```

The archive migration is a small lifespan-local function: `all_projects()` → find
the one with `tmux_session == COMMANDER_SESSION` → `archive_project(...)` (spec
§Visibility #2; `routes/state.py:117` already drops archived projects). Plain
function. **Structural flag:** `ensure_commander()` is `async` and the lifespan is
already `async` — direct `await`, no `_task` wrapper (it must complete before the
app serves, and a spawn failure should be logged, not swallowed — wrap the body in
the existing try/except-log pattern if a failed boot-spawn shouldn't abort
startup; **flag for plan:** decide whether a failed boot-spawn is fatal or
best-effort. Recommend best-effort — lazy heal on `/api/command` is the safety
net, and a tmux hiccup at boot shouldn't take down the dashboard).

**`routes/state.py` — exclude the commander row from the final `result`.** Per
spec §Visibility #3: the exclusion lands **immediately before `return`**, after
`update_focus_from_windows`, `_attach_git_then_resolve_pids`, and `_channel_gc`
(state.py:91) have all seen the full window list. A one-liner filter on `result`
keyed off `marker.pane_id`. Do NOT filter `windows` early — that starves
`_channel_gc` and would GC the commander's own channel state every 3s.
**Test (`tests/routes/test_state.py`):** assert (a) the commander session is
absent from the returned `windows`, and (b) its channel state is NOT GC'd
(i.e. `_channel_gc` saw it) — the exclusion-point invariant.

---

## 10. Frontend — `classify.js` + `OpenOmnibox.jsx`

**`classify.js` — one appended synthetic card.** Pure function, unchanged shape:

```js
if (q) cards.push({ kind: "command", label: `⚡ run: ${q}`, text: q, descriptor: null });
```

Pinned last (after all deterministic cards). No `descriptor` (matches
`pr`/`worktree`/`workspace` cards). **Test:** unit (classify.js already has a
pure-classifier test surface) — assert the command card appears iff query
non-empty and sorts last.

**`OpenOmnibox.jsx` — KIND_META entry + pick arm + console branch.**

- **`KIND_META.command`**: `{ group: "Command", icon: "⚡" }`. Mandatory —
  `KIND_META[c.kind]` is indexed unconditionally (OpenOmnibox.jsx:101); a missing
  key crashes the render. (Spec caught this.)
- **`pick()` arm**: `if (card.kind === "command") return enterConsole(card.text);`
  — without it, a command card falls through to `setDrill({card})` (line 96) and
  renders nothing (no drill branch matches `command`).
- **Console mode = a fourth render branch**, sibling to `!drill` (Palette) and the
  three `drill.card.kind` branches. Gated on a `console` state object
  (`{ session, index, turns, busy }` or null).

**Structural decision: console mode is an inline branch + a `useCommanderConsole`
hook in the SAME file, NOT a separate `CommanderConsole.jsx`.** Rationale: it is
one of four sibling render branches already living in `OpenOmnibox.jsx`
(`Palette`, `BranchDrill`, `NameDrill`, `RepoDrill` are all in-file or local). The
console shares the omnibox's `close`/`open`/`error` state and its Esc stack
membership. Extracting it would split one state machine across two files with zero
reuse (nothing else renders a commander console). Keep the *polling logic* in a
local `useCommanderConsole(session, index)` hook (encapsulates the `setInterval`
tail + idle detection + cleanup) so the render branch stays declarative — that is
the right seam, not a component boundary.

```js
function useCommanderConsole(session, index) {
  // poll GET /api/pane/turns?session=&index= every ~1s; track lastGrewAt;
  // idle = now - lastGrewAt > COMMANDER_IDLE_MS; return { turns, idle };
  // cleanup interval on unmount / address change.
}
```

**State-machine fit (the genuinely fiddly part):**
- `busy` (turn in flight) — reuse the existing `busy` signal (OpenOmnibox.jsx:35);
  disable input/send while a `/api/command` POST is outstanding *and* until the
  console goes idle, showing "commander busy".
- **Esc-leaves-it-running** — this is the one `useEscape` divergence. Today
  `useEscape(close, open.value)` closes the whole omnibox. In console mode, Esc
  must dismiss the console view but leave the command running server-side (the
  commander pane keeps working; the rail updates on the next poll). Structurally:
  when console is active, the Esc handler clears the console state (and lets the
  omnibox close) **without** any server cancel call — there is no cancel endpoint
  and none is wanted (the spec's "Esc leaves it running"). Flag for plan: confirm
  the LIFO `useEscape` stack composes — console-Esc should pop the console first,
  then a second Esc closes the palette (or console-Esc closes the whole overlay;
  spec says "dismisses the console" — pin the exact two-step in the plan).

**Test strategy (frontend):** `classify.js` gets a unit test (pure). The
`OpenOmnibox` console state machine is **browser-verified** per the project's
UI-testing norm (CLAUDE.md: a real Claude is a poor unit oracle, and the console
renders real session JSONL). No new JS test harness — matches `classify.js` being
the only unit-tested frontend module today.

---

## 11. Test strategy summary (per module)

| Module | Test type | Real vs mocked | Notes |
|---|---|---|---|
| `commander.ensure_commander` | unit | mock `_spawn_commander` | single-flight lock is the unit; gather two calls, assert one spawn |
| `commander._spawn_commander` | integration | real tmux (`@needs_tmux`, `-L` socket, stub exec) | adapt existing spawn test; assert `--model sonnet` + lockdown flags |
| `activity` marker fns | unit | real SQLite (tmp DB) | rename round-trip; existing fixture |
| `channels` 3 new tools | unit | stub workspaces/open_ops fns | spec §Testing; open_target has its own real-tmux cover |
| `channels` spawn cwd-anchoring | unit | stub `resolve_worktree_session` + marker | the one right mock — real spawn is a poor oracle, placement logic is stub-reachable |
| `narrator` skip-commander | unit | mock Haiku call | assert no Haiku for commander pane (the spend-avoidance invariant) |
| `routes/command` | unit | mock ensure + send, stub list_windows | delivery + 503 + 400 paths |
| `routes/state` exclusion | integration-ish | real state fixture | commander absent from result AND not GC'd |
| `classify.js` command card | unit | pure | appears iff non-empty, sorts last |
| `OpenOmnibox` console | browser | real | project UI norm; real session JSONL |

**Testability flag (none blocking):** the `spawn_claude` cwd-anchoring is the only
business logic that would *force* a mock, and it's structured well — the placement
decision is reachable by stubbing the single `resolve_worktree_session` resolver
and setting the marker, so the *decision* is unit-testable without a real Claude.
No structural smell. After the strip, grep the suite for the dropped symbols
(`build_fleet_digest`, `_curate_pane`, `assemble_pane_views`, `fleet_digest`,
`_serialize_digest`, `_schedule_first_mate_emit`, `heartbeat_decide`, `Push`,
`_LAST_SENT`, `first_mate_disabled`, `register_bridge_project`) to confirm nothing
imports them (spec §Testing).

---

## 12. Decisions to sanity-check

1. **Table rename = DDL-only, no data migration (and orphan left in place).**
   Alternative: copy the `first_mate` row → `commander`, or `DROP` the old table.
   Close because "rename the table" naively implies migrating data — but the value
   is a dead `pane_id` rewritten on next boot, so a migration moves garbage. I
   leave the orphan (cheap `DROP IF EXISTS` available if Tom wants tidiness). If
   you'd rather not have an orphan table lingering, add the one-line drop.

2. **`is_commander` predicate: extract `activity.is_commander_pane(pane)` vs inline.**
   Three call sites share `marker is not None and marker.pane_id == pane`
   (`_require_commander`, narrator skip, `spawn_claude` anchoring) and a fourth in
   the route (`_target_for_pane` matches the same `pane_id`). Close because it is
   only two lines — but three identical copies of a marker-identity check is the
   under-DRY edge. I lean **extract** `activity.is_commander_pane(pane) -> bool`
   (it already owns the marker accessors); flag in case you prefer the inline
   check for locality. (`_require_commander` can then be `is_commander_pane`.)

3. **Console mode inline in `OpenOmnibox.jsx` vs `CommanderConsole.jsx`.** I chose
   inline + a `useCommanderConsole` hook. Close because the console is genuinely
   more involved than the sibling drill branches (polling + idle + a different Esc
   semantics), which is the usual trigger to extract a component. I keep it inline
   because it shares the omnibox's close/error/Esc-stack state and nothing else
   renders it — extraction splits one state machine for no reuse. If the console
   grows (cancel button, multi-turn history, retry), revisit and extract then.

4. **Boot-spawn fatal vs best-effort in the lifespan.** I recommend best-effort
   (log + continue; lazy heal on `/api/command` is the net). Close because a
   commander that silently fails to boot means the first command pays the
   spawn latency — but a tmux hiccup at startup taking down the whole dashboard is
   worse. Confirm the preference.

5. **Keep `COMMANDER_SESSION = "bridge"` (don't rename the value).** Close because
   "rename everything" pulls toward `"commander"` as the session name too, which
   reads cleaner — but a live prod `bridge` session + the migration's
   `tmux_session == COMMANDER_SESSION` match key off the literal `"bridge"`. I keep
   the value and rename only the constant + window name. Flag if a clean session
   name is worth a one-time prod-side reset.

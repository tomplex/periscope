# Omnibox commander — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the omnibox into a free-text command surface backed by a single persistent, hidden Claude Code "commander" pane (the repurposed first mate) that orchestrates periscope actions and streams its work into an omnibox console.

**Architecture:** Repurpose `first_mate` → `commander`: strip the proactive heartbeat/tick, keep the managed-pane/marker/spawn plumbing, add MCP actuator tools (`create_workspace`, `open`, `catalog`) + cwd-anchored spawn, a new `POST /api/command` route that ensures + sends to the commander, and an `OpenOmnibox` console that tails the commander's transcript. The commander is hidden from the rail and the narrator.

**Tech Stack:** Python 3 / FastAPI (`uv run`), Preact + signals (Vite → `static/dist/app.js`), tmux control, MCP channel tools, Anthropic via the `claude` CLI (subscription auth).

**Reference docs:** spec `docs/superpowers/specs/2026-06-23-omnibox-commander-design.md`; structure `docs/plans/2026-06-24-omnibox-commander-structure.md`. Read both before starting.

**Conventions to honor:**
- Route errors: `raise HTTPException(status, detail)`, never `return {"ok": False}` from a route. (MCP tool handlers DO use `_tool_result({"ok": False, "error": …})` — that's the channel convention, different layer.)
- Commit as you go: one commit per task, single-line message, stage by path (never `git add .`). Do NOT stage `docs/plans/*` or `docs/superpowers/specs/*` scratch beyond what's already committed.
- Rebuild the frontend bundle (`npm run build`) and commit `static/dist/app.js` whenever `static/src/` changes.
- Run `uv run pytest -q` green at the end of every task that touches Python.
- `.venv` drift landmine: if `tests/test_channel_shim.py` fails spuriously, `uv sync` (see CLAUDE.md).

---

## Task 0: Rename `first_mate` → `commander` (mechanical, zero behavior change)

Do this FIRST and in isolation. Folding the rename into behavior commits drowns the real diffs and breaks `git bisect`.

**Files:**
- Rename: `periscope/first_mate.py` → `periscope/commander.py`
- Modify (call sites): `periscope/channels.py`, `periscope/activity.py`, `periscope/narrator.py`, `periscope/app.py`
- Rename tests: `tests/test_first_mate.py` → `tests/test_commander.py`, `tests/test_first_mate_spawn.py` → `tests/test_commander_spawn.py`
- Modify tests: any in `tests/test_activity.py`, `tests/test_channels.py`, `tests/test_narrator.py` referencing first-mate symbols.

Symbol map:

| Old | New |
|---|---|
| module `first_mate` | `commander` |
| `FIRST_MATE_SESSION` (value `"bridge"`) | `COMMANDER_SESSION` (keep value `"bridge"`) |
| `FIRST_MATE_WINDOW` (value `"first-mate"`) | `COMMANDER_WINDOW` (value `"commander"`) |
| `FirstMateMarker` | `CommanderMarker` |
| `set_first_mate`/`get_first_mate`/`clear_first_mate` | `set_commander`/`get_commander`/`clear_commander` |
| `_require_first_mate` | `_require_commander` |
| `_is_first_mate` | `_is_commander` |
| `_spawn_first_mate` | `_spawn_commander` |
| `supervisor_pass` | `supervisor_pass` (deleted in Task 1; rename-carry for now) |
| SQLite table `first_mate` | `commander` (DDL string — refactor-mcp won't touch it) |

- [ ] **Step 1: Rename symbols via refactor-mcp (LSP-backed).**

Use the `refactor-mcp` `rename` tool for each Python symbol above (module move + identifiers) so cross-module call sites move atomically. Do the module file move first (`first_mate.py` → `commander.py`), then each identifier. Do NOT hand-edit + sed.

- [ ] **Step 2: Hand-edit the string literals refactor-mcp can't see.**

These are strings, not symbols:
- `COMMANDER_WINDOW = "commander"` (was `"first-mate"`).
- The SQLite DDL lives inside the single `_SCHEMA` string (`activity.py` ~30–92, run via `c.executescript(_SCHEMA)` ~105). Inside that string, rename `CREATE TABLE IF NOT EXISTS first_mate (…)` → `commander (…)`, and append a `DROP TABLE IF EXISTS first_mate;` statement after it (executescript runs the whole multi-statement script, so the orphan drops on next boot). Do NOT add a stray `cur.execute(...)` outside `_SCHEMA`.
- The prompt file path `first-mate-prompt.txt` → `commander-prompt.txt` in `_spawn_commander`.
- The sentinel path `first-mate.disabled` — leave for now; it's deleted with the kill-switch in Task 1.

- [ ] **Step 3: Run the full suite to verify the rename is behavior-neutral.**

Run: `uv run pytest -q`
Expected: PASS (same count as before, ~634). If `test_channel_shim.py` fails, `uv sync` and re-run.

- [ ] **Step 4: Commit.**

```bash
git add periscope/commander.py periscope/channels.py periscope/activity.py periscope/narrator.py periscope/app.py tests/test_commander.py tests/test_commander_spawn.py tests/test_activity.py tests/test_channels.py tests/test_narrator.py
git rm --cached periscope/first_mate.py tests/test_first_mate.py tests/test_first_mate_spawn.py 2>/dev/null || true
git commit -m "refactor(commander): rename first_mate -> commander (mechanical, no behavior change)"
```

---

## Task 1: Strip the proactive tick (heartbeat / supervisor / digest / interrupt)

**Files:**
- Modify: `periscope/commander.py` — delete `fleet_diverged`, `heartbeat_decide`, `_render_delta`, `Push`, `_LAST_SENT`, `build_fleet_digest`, `assemble_pane_views`, `_curate_pane`, `PaneDigest`, `FleetDigest`, `supervisor_pass`, `first_mate_disabled` (+ its sentinel file logic), `register_bridge_project`. Rewrite the module docstring (no more "pure decision core / digest substrate").
- Modify: `periscope/activity.py` — remove the commander branches from `_worker_tick` (the `supervisor_pass` call + the `assemble_pane_views`/`build_fleet_digest`/`heartbeat_decide`/`_fm_push` block) and delete `_emit_pending_first_mate`. Keep `set/get/clear_commander`, the `commander` table, `append_captain_log`/`recent_captain_log`.
- Modify: `periscope/channels.py` — delete the `need_human` → `_schedule_first_mate_emit` block in `_do_notify_tool`, delete `_schedule_first_mate_emit`, delete `_do_fleet_digest_tool`, delete `_serialize_digest`, and remove the `fleet_digest` record from the tool registry.
- Modify: `periscope/app.py` — remove the `register_bridge_project()` call (the boot-spawn replacement lands in Task 7).
- Delete tests: in `tests/test_commander.py` delete the heartbeat/divergence/`build_fleet_digest`/`_curate_pane`/`assemble_pane_views`/supervisor/`register_bridge_project`/kill-switch tests (they import deleted symbols → collection error if left). In `tests/test_channels.py` delete `test_emit_channel_event_skips_fleet_digest` and `test_fleet_digest_tool_*`. Keep the marker tests in `tests/test_activity.py`.

- [ ] **Step 1: Delete the dead code and dead tests** (per the file list above). Keep `_spawn_commander`, the marker accessors, `captain_log`, `ROLE_PROMPT` (rewritten in Task 3), and `COMMANDER_SESSION`/`COMMANDER_WINDOW`.

- [ ] **Step 2: Confirm nothing still imports a stripped symbol.**

Run:
```bash
grep -rn "fleet_diverged\|heartbeat_decide\|build_fleet_digest\|assemble_pane_views\|_curate_pane\|fleet_digest\|_serialize_digest\|supervisor_pass\|first_mate_disabled\|register_bridge_project\|_schedule_first_mate_emit\|_emit_pending_first_mate\|PaneDigest\|FleetDigest\b" periscope/ tests/
```
Expected: no matches (empty output).

- [ ] **Step 3: Run the suite.**

Run: `uv run pytest -q`
Expected: PASS (lower count — the deleted tests are gone). No import/collection errors.

- [ ] **Step 4: Commit.**

```bash
git add periscope/commander.py periscope/activity.py periscope/channels.py periscope/app.py tests/test_commander.py tests/test_channels.py
git commit -m "feat(commander): strip the proactive heartbeat/supervisor/digest tick"
```

---

## Task 2: Extract `activity.is_commander_pane(pane)` predicate

**Files:**
- Modify: `periscope/activity.py` — add the predicate near the marker accessors (~line 491).
- Modify: `periscope/channels.py` — `_require_commander` delegates to it.
- Test: `tests/test_activity.py`

- [ ] **Step 1: Write the failing test.**

In `tests/test_activity.py`:
```python
def test_is_commander_pane(clean_state):
    from periscope import activity
    assert activity.is_commander_pane("%9") is False
    activity.set_commander(pane_id="%9", session_id=None, at=1)
    assert activity.is_commander_pane("%9") is True
    assert activity.is_commander_pane("%8") is False
```

- [ ] **Step 2: Run it, expect failure.**

Run: `uv run pytest tests/test_activity.py::test_is_commander_pane -v`
Expected: FAIL (`AttributeError: module 'periscope.activity' has no attribute 'is_commander_pane'`).

- [ ] **Step 3: Implement.**

In `periscope/activity.py` after `clear_commander`:
```python
def is_commander_pane(pane: str) -> bool:
    """True iff `pane` (a tmux %N id) is the registered commander singleton."""
    marker = get_commander()
    return marker is not None and marker.pane_id == pane
```

In `periscope/channels.py`, replace `_require_commander`'s body:
```python
def _require_commander(pane: str) -> bool:
    """True iff `pane` is the registered commander singleton. Channel tools that
    are commander-only self-guard with this (the registry is flat)."""
    from periscope import activity
    return activity.is_commander_pane(pane)
```

- [ ] **Step 4: Run tests.**

Run: `uv run pytest tests/test_activity.py -q`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add periscope/activity.py periscope/channels.py tests/test_activity.py
git commit -m "feat(commander): extract activity.is_commander_pane predicate"
```

---

## Task 3: `ensure_commander` (async + single-flight) and orchestrator spawn

**Files:**
- Modify: `periscope/commander.py` — add `_SPAWN_LOCK`, `async def ensure_commander`, rewrite `ROLE_PROMPT`, and amend `_spawn_commander`'s launch command with `--model sonnet` + read-only tool lockdown.
- Test: `tests/test_commander.py` (single-flight unit), `tests/test_commander_spawn.py` (real-tmux integration).

- [ ] **Step 1: Write the single-flight failing test.**

In `tests/test_commander.py`:
```python
import asyncio

def test_ensure_commander_single_flight(clean_state, monkeypatch):
    from periscope import commander, activity
    calls = {"n": 0}

    def fake_spawn(*, now):
        calls["n"] += 1
        activity.set_commander(pane_id="%7", session_id=None, at=now)

    monkeypatch.setattr(commander, "_spawn_commander", fake_spawn)
    # No live windows → marker pane never "live" until set; both callers race.
    monkeypatch.setattr(commander, "list_windows", lambda: [{"pane_id": "%7"}])

    async def go():
        await asyncio.gather(commander.ensure_commander(), commander.ensure_commander())

    asyncio.run(go())
    assert calls["n"] == 1   # lock serialized; second caller sees the live marker
```

- [ ] **Step 2: Run it, expect failure.**

Run: `uv run pytest tests/test_commander.py::test_ensure_commander_single_flight -v`
Expected: FAIL (`ensure_commander` doesn't exist).

- [ ] **Step 3: Implement `ensure_commander` + lock.**

In `periscope/commander.py` (top-level, after the constants):
```python
import asyncio
import time

_SPAWN_LOCK = asyncio.Lock()   # single-flight: lifespan boot vs first /api/command


async def ensure_commander():
    """Ensure exactly one live commander pane; (re)spawn if the marker's pane is
    gone. Single-flight via _SPAWN_LOCK so a boot-spawn and a racing first
    /api/command can't double-spawn. Returns the CommanderMarker (or None off
    prod / on spawn failure). The blocking spawn runs in a thread so it never
    stalls the event loop serving other panes' MCP connections."""
    from periscope import activity
    async with _SPAWN_LOCK:
        marker = activity.get_commander()
        live = {w.get("pane_id") for w in list_windows()}
        if marker is not None and marker.pane_id in live:
            return marker
        await asyncio.to_thread(_spawn_commander, now=int(time.time()))
        return activity.get_commander()
```
(`list_windows` is already imported at module top in `_spawn_commander`'s neighborhood — if it's a function-level import there, hoist `from periscope.panes import list_windows` to module top so `ensure_commander` can use it.)

- [ ] **Step 4: Amend `_spawn_commander`'s launch command.**

In `_spawn_commander`, where `exec_cmd` is built, append the model + lockdown flags (confirm flag spelling against the installed `claude` — `claude --help | grep -i tool`; the spec uses `--allowedTools`/`--disallowedTools`):
```python
    exec_cmd = (f"{claude_exec()} --model sonnet "
                f"--allowedTools Read,Grep,Glob --disallowedTools Bash,Edit,Write "
                f"--append-system-prompt "
                f'"$(cat {shlex.quote(str(prompt_path))})"')
```
Keep the rest of `_spawn_commander` unchanged (it already writes the prompt file, sleeps 100ms, send-keys, dismisses consent, stamps, reads pane_id, `set_commander`). Update the prompt file name to `commander-prompt.txt` if not already done in Task 0.

- [ ] **Step 5: Rewrite `ROLE_PROMPT`** (orchestrator brief, per spec §Role prompt + §Placement). Replace the observer prompt with:
```python
ROLE_PROMPT = """\
You are periscope's commander. The user sends you commands from the omnibox; act
on them immediately with your tools, then narrate what you did concisely.

You ORCHESTRATE, you do not edit. To do work in a repo, spawn a worker
(spawn_claude) with a clear first-message prompt and an explicit cwd; the worker
has full tools. You have read-only code access (Read/Grep/Glob) to understand and
route — resolve fuzzy references ("the attribute config refactor" -> which
repo/dir) before acting. Call catalog() ONCE per command to see repos + worktrees
and reuse the result; do not poll it.

Placement — choose where each worker lands by the cwd you pass:
- Main checkout: spawn_claude(cwd=<repo root>).
- Fresh worktree: open(repo, branch=<new>) to create it, then spawn into it.
- Existing project/worktree: spawn_claude(cwd=<that dir>).
Heuristics: PR / refactor / "try" / risky -> worktree; quick edit / question /
look-at -> main checkout; "in <project>" -> that project. When genuinely
ambiguous, default to a fresh worktree. Honor the user's explicit placement.

Tools: catalog, create_workspace, open (open(repo, branch) creates a worktree),
spawn_claude, list_claudes, list_workspaces, peek, send_to, the captain's log.

Absolute prohibitions: never merge an fdy pull request; never force-push; never
take prod-touching actions.
"""
```

- [ ] **Step 6: Update the spawn integration test.**

In `tests/test_commander_spawn.py` (adapted from `test_first_mate_spawn.py`): assert `_spawn_commander` sets the marker with a real `pane_id`, and that the captured launch command contains `--model sonnet` and `--disallowedTools Bash,Edit,Write`. Drop any supervisor-respawn-loop assertions.

- [ ] **Step 7: Run tests.**

Run: `uv run pytest tests/test_commander.py tests/test_commander_spawn.py -q`
Expected: PASS (the real-tmux test is `@needs_tmux`-gated; it runs if tmux is available).

- [ ] **Step 8: Commit.**

```bash
git add periscope/commander.py tests/test_commander.py tests/test_commander_spawn.py
git commit -m "feat(commander): async ensure_commander single-flight + orchestrator role prompt + sonnet/read-only spawn"
```

---

## Task 4: Add `create_workspace`, `open`, `catalog` MCP tools

**Files:**
- Modify: `periscope/channels.py` — three handlers + three registry records.
- Test: `tests/test_channels.py`

- [ ] **Step 1: Write failing tests.**

In `tests/test_channels.py`:
```python
def test_create_workspace_tool(monkeypatch):
    from periscope import channels
    monkeypatch.setattr(channels.workspaces, "create_workspace",
                        lambda *, name, base_repo=None: {"id": "ws_x", "name": name})
    res = channels._do_create_workspace_tool("%1", {"name": "x", "base_repo": "/r"})
    body = _body(res)   # helper that json-loads the tool result text
    assert body["ok"] is True and body["workspace_id"] == "ws_x"

def test_open_tool_dispatches_path(monkeypatch):
    from periscope import channels, open_ops
    seen = {}
    def fake_open(desc):
        seen["desc"] = desc
        class R: tmux_session="s"; repo="/r"; claude_pid="@1"; claude_pane_id="%2"; ui={}
        return R()
    monkeypatch.setattr(open_ops, "open_target", fake_open)
    res = channels._do_open_tool("%1", {"path": "/r"})
    body = _body(res)
    assert body["ok"] is True and isinstance(seen["desc"], open_ops.PathTarget)

def test_open_tool_bad_args(monkeypatch):
    from periscope import channels
    res = channels._do_open_tool("%1", {})   # none of path|repo+branch|repo+pr
    assert _body(res)["ok"] is False

def test_catalog_tool(monkeypatch):
    from periscope import channels, open_ops
    monkeypatch.setattr(open_ops, "build_catalog", lambda: {"repos": [], "worktrees": []})
    res = channels._do_catalog_tool("%1", {})
    assert _body(res)["ok"] is True
```
(If a `_body` helper isn't already in the test module, add one: `import json; def _body(r): return json.loads(r[0].text)` — match the existing tool-result shape used by other channel tests.)

- [ ] **Step 2: Run them, expect failure.**

Run: `uv run pytest tests/test_channels.py -k "create_workspace_tool or open_tool or catalog_tool" -v`
Expected: FAIL (handlers don't exist).

- [ ] **Step 3: Implement the three handlers** (near the other `_do_*_tool` defs in `channels.py`):
```python
def _do_create_workspace_tool(pane: str, arguments: dict):
    """Create a periscope workspace (goal-scoped rail group)."""
    from periscope import workspaces
    name = str(arguments.get("name", "")).strip()
    if not name:
        return _tool_result({"ok": False, "error": "name is required"})
    base_repo = (arguments.get("base_repo") or None)
    # create_workspace returns a DICT (Workspace TypedDict), not an object —
    # subscript access, NOT ws.id (the structure proposal §7 is wrong on this).
    ws = workspaces.create_workspace(name=name, base_repo=base_repo)
    return _tool_result({"ok": True, "workspace_id": ws["id"], "name": ws["name"]})


def _open_descriptor(arguments: dict):
    """dict -> open_ops.Descriptor. Mirrors routes/open.py:_to_descriptor but
    over a tool-args dict and raising ValueError (the tool maps it to an error)."""
    from periscope import open_ops
    path = arguments.get("path")
    repo = arguments.get("repo")
    branch = arguments.get("branch")
    pr = arguments.get("pr")
    if path and not (repo or branch or pr):
        return open_ops.PathTarget(path=str(path))
    if repo and branch and pr is None and not path:
        return open_ops.BranchTarget(repo=str(repo), branch=str(branch))
    if repo and pr is not None and not (path or branch):
        return open_ops.PRTarget(repo=str(repo), pr=int(pr))
    raise ValueError("exactly one of {path | repo+branch | repo+pr} required")


def _do_open_tool(pane: str, arguments: dict):
    """Open a path / branch / PR into the rail (creates a worktree for repo+branch)."""
    from periscope import open_ops
    # Broad except on purpose: _open_descriptor raises ValueError for bad args,
    # but open_target's branch/PR paths (spawn_worktree / fetch_pr_into_worktree)
    # raise arbitrary git/subprocess errors — all become a clean tool error frame.
    try:
        descriptor = _open_descriptor(arguments)
        result = open_ops.open_target(descriptor)
    except Exception as e:
        return _tool_result({"ok": False, "error": str(e)})
    return _tool_result({"ok": True, "tmux_session": result.tmux_session,
                         "repo": result.repo, "pane_id": result.claude_pane_id})


def _do_catalog_tool(pane: str, arguments: dict):
    """List discoverable repos + their worktrees (dormant + live)."""
    from periscope import open_ops
    return _tool_result({"ok": True, **open_ops.build_catalog()})
```

- [ ] **Step 4: Register the three records** in the `_CHANNEL_TOOLS` list:
```python
    {
        "name": "create_workspace",
        "description": ("Create a periscope workspace — a goal-scoped rail group "
                        "that spawned tabs can be tagged into (pass the returned "
                        "workspace_id to spawn_claude). Args: name, optional base_repo."),
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string"},
            "base_repo": {"type": "string", "description": "absolute repo path (optional)"},
        }, "required": ["name"]},
        "handler": _do_create_workspace_tool,
    },
    {
        "name": "open",
        "description": ("Materialize a session into the rail. Exactly one of: "
                        "{path} to open a directory; {repo, branch} to open (and "
                        "create if absent) a worktree; {repo, pr} to fetch a PR "
                        "into a worktree. 'create a worktree for X' = open(repo, branch=X)."),
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}, "repo": {"type": "string"},
            "branch": {"type": "string"}, "pr": {"type": "integer"},
        }},
        "handler": _do_open_tool,
    },
    {
        "name": "catalog",
        "description": ("List discoverable repos and their worktrees (dormant + "
                        "live) to ground placement decisions. Git-subprocess-heavy "
                        "— call once per command and reuse; do not poll."),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _do_catalog_tool,
    },
```

- [ ] **Step 5: Run tests.**

Run: `uv run pytest tests/test_channels.py -k "create_workspace_tool or open_tool or catalog_tool" -q`
Expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add periscope/channels.py tests/test_channels.py
git commit -m "feat(commander): add create_workspace, open, catalog MCP tools"
```

---

## Task 5: `spawn_claude` cwd-anchoring + non-git guard for the commander

**Files:**
- Modify: `periscope/channels.py` `_do_spawn_claude_tool` (~line 471–510).
- Test: `tests/test_channels.py`

- [ ] **Step 1: Write failing tests.**

```python
def test_spawn_commander_anchors_on_cwd(clean_state, monkeypatch, mocker):
    from periscope import channels, activity, open_ops
    import asyncio
    activity.set_commander(pane_id="%C", session_id=None, at=1)   # caller IS commander
    monkeypatch.setattr(open_ops, "resolve_worktree_session",
                        lambda cwd: ("proj-sess", object()))      # git cwd resolves
    # Reproduce the FULL mock set from test_spawn_claude_writes_spawned_by
    # (tests/test_channels.py:377): the spawn handler shells out heavily, so all
    # of these must be stubbed or the test hits real tmux:
    mocker.patch.object(channels, "tmux", return_value="sess|/home/tom")
    mocker.patch.object(channels, "_run", return_value=(0, ""))
    cap = mocker.patch.object(channels, "_tmux_mutate", return_value=(True, "3"))
    mocker.patch("os.path.isdir", return_value=True)
    mocker.patch("asyncio.sleep", new_callable=mocker.AsyncMock)
    mocker.patch.object(channels, "_plain_pane_snapshot", return_value="auto mode on")
    mocker.patch.object(channels, "note_focus", return_value=None)
    mocker.patch.object(channels, "note_action", return_value=None)
    mocker.patch.object(channels, "stamp_new_window", return_value=None)
    mocker.patch.object(channels, "_resolve_pid_for_pane", return_value="@1")
    mocker.patch.object(channels, "set_window_fields", return_value=None)
    asyncio.run(channels._do_spawn_claude_tool("%C", {"prompt": "x", "cwd": "/r"}))
    # The session the worker landed in must be the RESOLVED "proj-sess",
    # never the commander's own caller session. Inspect the new-session/new-window
    # _tmux_mutate call args for "proj-sess".
    targets = [c.args for c in cap.call_args_list]
    assert any("proj-sess" in a for args in targets for a in args)

def test_spawn_commander_non_git_cwd_errors(clean_state, monkeypatch):
    from periscope import channels, activity, open_ops
    import asyncio
    activity.set_commander(pane_id="%C", session_id=None, at=1)
    monkeypatch.setattr(open_ops, "resolve_worktree_session", lambda cwd: None)
    monkeypatch.setattr("os.path.isdir", lambda p: True)
    res = asyncio.run(channels._do_spawn_claude_tool("%C", {"prompt": "x", "cwd": "/tmp"}))
    assert _body(res)["ok"] is False and "git" in _body(res)["error"].lower()
```
(The happy-path mock list mirrors `test_spawn_claude_writes_spawned_by` exactly — if a name there differs from the installed version, copy that test's actual patch set. The new assertion is purely "which session was chosen.")

- [ ] **Step 2: Run, expect failure.**

Run: `uv run pytest tests/test_channels.py -k spawn_commander -v`
Expected: FAIL (non-git cwd currently falls through to caller session; no error).

- [ ] **Step 3: Implement the guard** in `_do_spawn_claude_tool`, in the placement block (where `workspace`/`anchored`/`session` are resolved):
```python
    from periscope import open_ops, activity
    is_commander = activity.is_commander_pane(pane)
    workspace = str(arguments.get("workspace") or "same").strip().lower()
    # The commander is ALWAYS cwd-anchored: deriving the session from its own
    # (hidden) caller session would misfile the worker invisibly. Force the
    # cwd-resolution path and refuse a non-git cwd rather than fall back to the
    # caller session.
    if is_commander:
        anchored = open_ops.resolve_worktree_session(cwd)
        if anchored is None:
            return _tool_result({"ok": False,
                                 "error": "cwd is not in a git repo — the commander "
                                          "spawns must target a repo/worktree dir"})
        session, project = anchored
    else:
        anchored = open_ops.resolve_worktree_session(cwd) if workspace == "new" else None
        if anchored:
            session, project = anchored
        else:
            session = str(arguments.get("session") or caller_session or "spawned").strip()
```
(Preserve the rest of the handler — the `has-session`/`new-window` create path and the `workspace="new"` `place_in_rail`/`workspace_id` tagging downstream must still run for the commander. If `place_in_rail` is currently gated on `anchored`, ensure the commander's `anchored` set drives it.)

- [ ] **Step 4: Run tests.**

Run: `uv run pytest tests/test_channels.py -k "spawn" -q`
Expected: PASS (new + existing spawn tests).

- [ ] **Step 5: Commit.**

```bash
git add periscope/channels.py tests/test_channels.py
git commit -m "feat(commander): cwd-anchor spawn_claude + non-git guard for the commander caller"
```

---

## Task 6: `POST /api/command` route

**Files:**
- Create: `periscope/routes/command.py`
- Modify: `periscope/app.py` — include the router (find the `include_router` loop / list and add `command`).
- Test: `tests/routes/test_command.py`

- [ ] **Step 1: Write the failing route test.**

`tests/routes/test_command.py`:
```python
import asyncio
from fastapi.testclient import TestClient

def test_command_sends_to_commander(client, monkeypatch):
    from periscope import commander, activity
    from periscope.routes import command as cmd_route

    async def fake_ensure():
        activity.set_commander(pane_id="%C", session_id=None, at=1)
        return activity.get_commander()
    monkeypatch.setattr(commander, "ensure_commander", fake_ensure)
    monkeypatch.setattr(cmd_route, "list_windows",
                        lambda: [{"pane_id": "%C", "session": "bridge", "index": 0}])
    sent = {}
    monkeypatch.setattr(cmd_route, "_send_to_target",
                        lambda target, paste, keys: sent.update(target=target, paste=paste, keys=keys) or {})
    r = client.post("/api/command", json={"text": "do a thing"})
    assert r.status_code == 200
    assert r.json() == {"session": "bridge", "index": 0}
    assert sent["paste"] == "do a thing" and sent["keys"] == ["Enter"]
    assert sent["target"] == "bridge:0"

def test_command_503_when_no_commander(client, monkeypatch):
    from periscope import commander
    async def fake_ensure(): return None
    monkeypatch.setattr(commander, "ensure_commander", fake_ensure)
    r = client.post("/api/command", json={"text": "x"})
    assert r.status_code == 503
```

- [ ] **Step 2: Run, expect failure.**

Run: `uv run pytest tests/routes/test_command.py -v`
Expected: FAIL (route 404 / module missing).

- [ ] **Step 3: Implement the route.**

`periscope/routes/command.py`:
```python
"""POST /api/command — deliver a free-text command to the hidden commander pane."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope import commander
from periscope.panes import list_windows
from periscope.routes.send import _send_to_target

router = APIRouter()


class CommandBody(BaseModel):
    text: str


@router.post("/api/command")
async def command_endpoint(body: CommandBody):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "text must be non-empty")
    marker = await commander.ensure_commander()
    if marker is None:
        raise HTTPException(503, "commander unavailable (not prod, or spawn failed)")
    win = next((w for w in list_windows() if w.get("pane_id") == marker.pane_id), None)
    if win is None:
        raise HTTPException(503, "commander pane not found in tmux")
    target = f"{win['session']}:{win['index']}"
    _send_to_target(target, paste=text, keys=["Enter"])
    return {"session": win["session"], "index": win["index"]}
```
(Confirm `_send_to_target`'s real signature in `routes/send.py` — it's `(target, paste, keys)`. If it raises `HTTPException` on tmux failure, let it propagate.)

- [ ] **Step 4: Register the router** in `app.py` — this is a **two-site** edit: `app.py` imports route modules in a tuple (~line 24–27) and includes them in a `for r in (…): app.include_router(r.router)` loop (~line 133–138). Add `command` to **both** the import tuple and the include loop's tuple (by bare module name, not a standalone `app.include_router(command.router)` line).

- [ ] **Step 5: Run tests.**

Run: `uv run pytest tests/routes/test_command.py -q`
Expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add periscope/routes/command.py periscope/app.py tests/routes/test_command.py
git commit -m "feat(commander): POST /api/command route (ensure + send to commander)"
```

---

## Task 7: Boot-spawn + archive the stale bridge project (lifespan)

**Files:**
- Modify: `periscope/app.py` — in the prod-gated lifespan, archive any stale `bridge` project and best-effort boot-spawn the commander.
- Test: `tests/test_app.py` (or wherever lifespan is tested) for the archive migration; boot-spawn is covered by `ensure_commander` tests + manual prod verify.

- [ ] **Step 1: Write the archive-migration failing test.**

In the appropriate test module:
```python
def test_archive_stale_bridge_project(clean_state):
    from periscope import projects, commander, app as app_mod
    projects.create_project("/Users/x", name="bridge", tmux_session="bridge",
                            repo=None, base_branch=None)
    app_mod._archive_stale_commander_project()   # the migration helper
    p = projects.get_project("/Users/x")
    assert p.get("archived_at")   # archived, no longer rail-visible
```

- [ ] **Step 2: Run, expect failure.**

Run: `uv run pytest -k archive_stale_bridge -v`
Expected: FAIL (helper missing).

- [ ] **Step 3: Implement the migration helper + boot-spawn** in `app.py`:
```python
def _archive_stale_commander_project() -> None:
    """The old first-mate registered the bridge session as a rail project. The
    commander is hidden, so archive that project (projects has no delete API; the
    state route drops archived projects)."""
    from periscope import projects, commander
    for key, p in projects.all_projects().items():
        if p.get("tmux_session") == commander.COMMANDER_SESSION and not p.get("archived_at"):
            projects.archive_project(key)
```
In the prod-gated lifespan block (where `register_bridge_project()` used to be called), replace it with:
```python
    _archive_stale_commander_project()
    try:
        await commander.ensure_commander()   # best-effort; lazy-heal on /api/command
    except Exception:
        log.warning("commander boot-spawn failed; will lazy-heal on first command", exc_info=True)
```

- [ ] **Step 4: Run tests.**

Run: `uv run pytest -k "archive_stale_bridge or lifespan" -q`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add periscope/app.py tests/test_app.py
git commit -m "feat(commander): boot-spawn (best-effort) + archive stale bridge project"
```

---

## Task 8: Skip the commander in the narrator tick

**Files:**
- Modify: `periscope/narrator.py` — in the candidate-scan loop in `tick()`, `continue` past the commander pane (before the Haiku call). Keep `_is_commander` for any rename-suppression use, but the spend-avoiding skip is in the loop.
- Test: `tests/test_narrator.py`

- [ ] **Step 1: Write the failing test.**

```python
def test_narrator_skips_commander(clean_state, tick_env):
    # tick_env is the existing narrator fixture (test_narrator.py:317) that stubs
    # Haiku and records calls in tick_env["haiku_calls"]; _pane() (line 368) builds
    # a candidate window dict whose pane_id is the commander's.
    from periscope import narrator, activity
    activity.set_commander(pane_id="%1", session_id=None, at=1)   # _pane() uses %1
    narrator.tick([_pane()])
    assert tick_env["haiku_calls"] == []   # commander skipped → zero Haiku spend
```
(Model on `test_tick_generates_first_status` (test_narrator.py:373) — same fixture/`_pane()` helper; the only change is setting the commander marker to the candidate's pane_id and asserting zero Haiku calls. If `_pane()`'s default pane_id isn't `%1`, set the marker to whatever it is.)

- [ ] **Step 2: Run, expect failure.**

Run: `uv run pytest tests/test_narrator.py -k skips_commander -v`
Expected: FAIL (commander currently gets a status line generated).

- [ ] **Step 3: Implement the skip** in `narrator.py`'s `tick()` candidate loop, right after the pane id is known:
```python
        if activity.is_commander_pane(pane_id):
            continue   # hidden orchestrator — no status line, no Haiku spend
```

- [ ] **Step 4: Run tests.**

Run: `uv run pytest tests/test_narrator.py -q`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add periscope/narrator.py tests/test_narrator.py
git commit -m "feat(commander): narrator skips the hidden commander pane (no Haiku spend)"
```

---

## Task 9: Exclude the commander from the `/api/state` rail payload

**Files:**
- Modify: `periscope/routes/state.py` — drop the commander row from the FINAL `result` list (after `_channel_gc` + pid attach, immediately before `return`).
- Test: `tests/routes/test_state.py`

- [ ] **Step 1: Write the failing test.**

```python
def test_state_excludes_commander(client, monkeypatch, clean_state):
    from periscope import activity
    from periscope.routes import state as state_route
    activity.set_commander(pane_id="%C", session_id=None, at=1)
    # stub list_windows to include the commander pane + a normal pane
    monkeypatch.setattr(state_route, "list_windows", lambda: [
        {"session": "bridge", "index": 0, "pane_id": "%C", "name": "commander"},
        {"session": "proj", "index": 0, "pane_id": "%P", "name": "claude"},
    ])
    r = client.get("/api/state")
    panes = [w for w in r.json()["windows"]]
    assert all(w.get("pane_id") != "%C" for w in panes)
    assert any(w.get("pane_id") == "%P" for w in panes)
```
(Match the real `/api/state` response shape for the windows list key.)

- [ ] **Step 2: Run, expect failure.**

Run: `uv run pytest tests/routes/test_state.py -k excludes_commander -v`
Expected: FAIL (commander present).

- [ ] **Step 3: Implement.** In `routes/state.py`, just before the `return` that ships `result`:
```python
    from periscope import activity
    result = [w for w in result if not activity.is_commander_pane(w.get("pane_id", ""))]
```
(Use the actual final-list variable name; the structure proposal identifies it as `result`. Do NOT filter the raw `windows` earlier — that would let `_channel_gc` drop the commander's channel state and skip its pid attach.)

- [ ] **Step 4: Run tests.**

Run: `uv run pytest tests/routes/test_state.py -q`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add periscope/routes/state.py tests/routes/test_state.py
git commit -m "feat(commander): hide the commander pane from the /api/state rail payload"
```

---

## Task 10: Omnibox "⚡ run" card (`classify.js`)

**Files:**
- Modify: `static/src/open/classify.js` — append a synthetic `command` card.
- Modify: `static/src/overlays/OpenOmnibox.jsx` — add `command` to `KIND_META`.
- Test: `static/src/open/__tests__/classify.test.js` (or wherever classify is unit-tested).

- [ ] **Step 1: Write the failing test.**

In the classify test file:
```js
import { classify } from "../classify.js";
test("always offers a run-command card for non-empty query", () => {
  const cards = classify("create a worktree for foo", { repos: [], worktrees: [] });
  const cmd = cards.find(c => c.kind === "command");
  expect(cmd).toBeTruthy();
  expect(cmd.text).toBe("create a worktree for foo");
});
test("no command card for empty query", () => {
  expect(classify("", { repos: [], worktrees: [] })).toEqual([]);
});
```

- [ ] **Step 2: Run, expect failure.**

Run: `npm test -- classify` (or the project's test runner; check `package.json` scripts)
Expected: FAIL (no command card).

- [ ] **Step 3: Implement.** In `classify.js`, before `return cards;`:
```js
  cards.push({ kind: "command", label: `⚡ run: ${q}`, text: q });
```
In `OpenOmnibox.jsx` `KIND_META`, add:
```js
  command:  { group: "Command",       icon: "⚡" },
```

- [ ] **Step 4: Run tests.**

Run: `npm test -- classify`
Expected: PASS.

- [ ] **Step 5: Commit** (no bundle rebuild yet — Task 11 rebuilds once after the UI is done).

```bash
git add static/src/open/classify.js static/src/overlays/OpenOmnibox.jsx static/src/open/__tests__/classify.test.js
git commit -m "feat(commander): omnibox run-command card in classify"
```

---

## Task 11: Omnibox console mode (`OpenOmnibox.jsx`)

**Files:**
- Modify: `static/src/overlays/OpenOmnibox.jsx` — add console mode (a fourth render branch), a `useCommanderConsole` hook (POST + transcript-tail poll + idle), a `command` arm in `pick()`, and Esc-leaves-it-running.
- Rebuild: `static/dist/app.js` (`npm run build`).
- Verify: in the browser against prod (`/api/command` is prod-only).

- [ ] **Step 1: Add the `command` arm to `pick()`.** In `OpenOmnibox.jsx`, before the fall-through `setDrill({ card })`:
```js
    if (card.kind === "command") return setConsole({ text: card.text });
```
Add `const [console_, setConsole] = useState(null);` to the component state, and reset it (`setConsole(null)`) in the open-effect alongside `setDrill(null)`.

- [ ] **Step 2: Add the `useCommanderConsole` hook** (same file, above the component):
```js
function useCommanderConsole(text, onError) {
  const [lines, setLines] = useState([]);   // rendered transcript turns
  const [running, setRunning] = useState(true);
  useEffect(() => {
    let alive = true, pane = null, timer = null, lastGrow = Date.now();
    (async () => {
      const data = await apiCall("command", "/api/command", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!alive) return;
      if (!data) { setRunning(false); onError?.("command failed"); return; }
      pane = data;   // { session, index }
      const poll = async () => {
        if (!alive) return;
        const t = await apiCall("turns",
          `/api/pane/turns?session=${encodeURIComponent(pane.session)}&index=${pane.index}`);
        if (!alive) return;
        const turns = (t && t.turns) || [];
        setLines(prev => {
          if (turns.length > prev.length) lastGrow = Date.now();
          return turns;
        });
        // idle: no growth for 4s after at least one turn landed
        if (turns.length > 0 && Date.now() - lastGrow > 4000) { setRunning(false); return; }
        timer = setTimeout(poll, 1000);
      };
      poll();
    })();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [text]);
  return { lines, running };
}
```

- [ ] **Step 3: Render console mode.** Add a branch in the component's return, parallel to the `drill` branches:
```jsx
        {console_ && (
          <CommanderConsole text={console_.text}
            onClose={() => { setConsole(null); close(); }}
            onError={setError} />
        )}
```
And the inline render component (same file):
```jsx
function CommanderConsole({ text, onClose, onError }) {
  const { lines, running } = useCommanderConsole(text, onError);
  // Esc dismisses the console but lets the command keep running server-side.
  useEscape(onClose, true);
  return (
    <div class="open-omnibox-console">
      <div class="open-omnibox-console-cmd">⚡ {text}</div>
      <div class="open-omnibox-console-log">
        {lines.map((t, i) => <div class="open-omnibox-console-line" key={i}>{renderTurn(t)}</div>)}
        {running && <div class="open-omnibox-console-spin">commander working…</div>}
      </div>
    </div>
  );
}
```
`renderTurn(t)` should render a one-line summary of a transcript turn (role + a tool name or the first text line). Keep it minimal — match the turn shape returned by `/api/pane/turns` (inspect `get_turns_for_pane`'s output; render `t.role` + first text/tool block). Guard the top-level Palette branch so it only renders when `!drill && !console_`.

- [ ] **Step 4: Disable input while busy.** When `console_` is set, the Palette is not rendered, so input is already gated. (No extra "busy" wiring needed for v1 — a new command requires reopening the omnibox.)

- [ ] **Step 5: Add minimal CSS** for the console classes in the omnibox stylesheet (match the existing `open-omnibox-*` styles — a scrollable log, monospace, the spinner). Keep it terse.

- [ ] **Step 6: Build the bundle.**

Run: `npm run build`
Expected: writes `static/dist/app.js` with no errors.

- [ ] **Step 7: Verify in the browser (against prod).** Restart prod (`bin/periscope restart`), open the dashboard, ⌘K, type a command (e.g. "create a worktree called commander-smoke in <some repo>"), pick the ⚡ run row, and confirm: the console opens, the commander pane spawns (check tmux `bridge:commander`), tool calls stream into the console, and the resulting worktree/pane appears in the rail on the next poll. Esc dismisses the console without killing the command. Confirm the commander pane is NOT visible in the rail.

- [ ] **Step 8: Commit.**

```bash
git add static/src/overlays/OpenOmnibox.jsx static/src/styles/*.css static/dist/app.js
git commit -m "feat(commander): omnibox console mode — send command + tail commander transcript"
```

---

## Final verification

- [ ] Run the full Python suite: `uv run pytest -q` — must be green. Paste the last ~20 lines.
- [ ] Run the frontend tests: `npm test` — green.
- [ ] `grep -rn "first_mate\|first-mate\|FIRST_MATE" periscope/ static/src/` — only intentional residue (e.g. the `DROP TABLE IF EXISTS first_mate` migration string), nothing live.
- [ ] Confirm the dropped-symbol grep from Task 1 Step 2 is still empty.

## Notes for the executor

- **Prod-only loop:** `/api/command` + the commander spawn only work in prod (`is_prod()` = `PORT==8765 and not PERISCOPE_DEV`). Browser verification (Task 11 Step 7) must run against prod on :8765, not dev :8766.
- **Subscription auth dependency:** before trusting "free under subscription," confirm the spawned commander pane is subscription-authed (not API-keyed) — spawn a pane the way `_spawn_commander` does and check. If it comes up API-keyed, that's a blocker to raise (it doesn't break the feature, only the billing premise).
- **Flag spelling:** verify `--allowedTools`/`--disallowedTools`/`--model` against the installed `claude` (`claude --help`) before relying on Task 3's exact strings.

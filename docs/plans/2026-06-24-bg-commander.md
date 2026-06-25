# `--bg` commander + job tracking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace periscope's singleton hidden-pane commander with per-command ephemeral `claude --bg` commanders, each a trackable job in a `commands` table surfaced as an omnibox job list.

**Architecture:** A new `periscope/bg_commander.py` owns dispatch (`claude --bg` via `subprocess.Popen`), the `commands` table CRUD (in the shared activity DB, own schema), `claude agents --json` status sync, and `claude stop` cleanup. The channel registry (`_MCP_SESSIONS`) generalizes from pane-id to an opaque handle (`%N` for panes, `cmdr:<session_id>` for commanders); `is_commander(handle)` replaces the singleton marker. The omnibox live console becomes a server-backed job list.

**Tech Stack:** Python 3.11 / FastAPI / SQLite, Preact frontend, the installed `claude` CLI (v2.1.190 — flags verified: `--bg`, `--session-id`, `--append-system-prompt-file`, `--mcp-config`, `--strict-mcp-config`, `--model`, `--allowedTools`, `claude agents --json --all`).

**Source-of-truth docs:** spec `docs/superpowers/specs/2026-06-24-bg-commander-design.md`; structure `docs/plans/2026-06-24-bg-commander-structure.md`.

---

## Manual gates (NOT subagent tasks — flagged for the orchestrator)

- **Step 0 — launchd subscription auth (prod gate).** Before trusting dispatch in prod, confirm a launchd-context `claude --bg` (the prod FastAPI env, not an interactive shell) authenticates on subscription. Test the **bare `claude` binary** (`shutil.which("claude")`), NOT the interactive zsh `claude` wrapper function — `subprocess` invokes the binary, which the launchd env may not have on `PATH`. The whole billing premise rests on it. If it fails, only `bg_commander._dispatch_argv`/`_dispatch_env`/`config.claude_bin` change (e.g. wrap in a login shell, or pin an absolute path) — the module structure is isolated for exactly this. Verified in prod, not in this branch.
- **Step 6 — prod smoke.** Dispatch 2–3 concurrent commands in prod; confirm subscription auth, concurrency, close-and-come-back, correct delegation. Manual.
- **Env-propagation contingency (verify at Task 4 / prod).** The per-command identity `PERISCOPE_CALLER_ID=cmdr:<id>` rides on the dispatched process env, which `claude --bg` must propagate into the `channel_shim` it launches via `--mcp-config`. If the MCP child does NOT inherit parent env (only the static config's `env` block), the fallback is a **per-dispatch** mcp-config file with `PERISCOPE_CALLER_ID` baked into its `env` block. `write_mcp_config` is structured so this is a localized change. Primary path = static config + inherited env.

---

## Task 1: `bg_commander.py` — pure seams (TDD)

The decision logic with no I/O: status mapping, agents-json parsing, dispatch argv/env. Highest-value tests; locks the security-load-bearing flags and the grace rule.

**Files:**
- Create: `periscope/config.py` additions (constants only — see Step 1)
- Create: `periscope/bg_commander.py` (pure functions in this task; CRUD/dispatch/sync in Tasks 2–3)
- Test: `tests/test_bg_commander.py`

- [ ] **Step 1: Add config constants**

In `periscope/config.py`, add `import shutil` to the imports, then after the existing `ACTIVITY_DB = config_dir() / "periscope.db"` line add:

```python
# --- bg commander (per-command `claude --bg` dispatch) ---
def claude_bin() -> str:
    """The BARE `claude` executable for argv[0]. NOT claude_exec() — that returns
    a multi-word shell-command string with --dangerously-load-development-channels
    (the dev-channels MCP transport). A --bg commander reaches MCP via --mcp-config
    instead, dispatched through subprocess (no shell), so it needs the binary alone.
    PERISCOPE_CLAUDE_BIN overrides for tests."""
    return os.environ.get("PERISCOPE_CLAUDE_BIN") or shutil.which("claude") or "claude"


PERISCOPE_MCP_CONFIG = config_dir() / "bg-mcp-config.json"        # generated at boot
ORCHESTRATOR_PROMPT_FILE = config_dir() / "orchestrator-prompt.txt"  # written from bg_commander.ROLE_PROMPT
CHANNEL_SHIM_PATH = Path(__file__).resolve().parent.parent / "channel_shim.py"
BG_COMMANDER_MODEL = "sonnet"
# Pinned to the four pane-INDEPENDENT tools (NOT mcp__periscope__*). The wildcard
# would grant pane-dependent tools (notify/link_pr/open_document/report/…) that
# resolve the caller against a real %N window or feed the handle to tmux; a cmdr:
# handle matches no window, so those error or leak alert state _channel_gc never
# reaps. These four are exactly the delegator set the role prompt advertises.
BG_COMMANDER_ALLOWED_TOOLS = (
    "Read,Grep,Glob,"
    "mcp__periscope__catalog,mcp__periscope__open,"
    "mcp__periscope__create_workspace,mcp__periscope__spawn_claude"
)
```

Confirm `from pathlib import Path` is already imported in `config.py` (it is — `config_dir()` returns a `Path`).

- [ ] **Step 2: Write the failing tests for the pure seams**

Create `tests/test_bg_commander.py`:

```python
import json
from periscope import bg_commander as bgc


def test_parse_agents_json_keeps_only_background_sessions():
    raw = json.dumps([
        {"kind": "background", "sessionId": "a", "state": "done"},
        {"kind": "background", "sessionId": "b", "state": "blocked"},
        {"kind": "interactive", "sessionId": "c", "status": "idle"},
    ])
    assert bgc.parse_agents_json(raw) == {"a": "done", "b": "blocked"}


def test_parse_agents_json_tolerates_garbage():
    assert bgc.parse_agents_json("not json") == {}
    assert bgc.parse_agents_json("[]") == {}


def test_map_state_present():
    assert bgc.map_state("done", started_at=0, now=10, present=True) == "done"
    assert bgc.map_state("blocked", started_at=0, now=10, present=True) == "running"
    assert bgc.map_state("running", started_at=0, now=10, present=True) == "running"


def test_map_state_absent_young_stays_running():
    # absent within the grace window => not yet registered, keep running
    assert bgc.map_state(None, started_at=100, now=130, present=False) == "running"


def test_map_state_absent_old_is_done():
    assert bgc.map_state(None, started_at=100, now=200, present=False) == "done"


def test_dispatch_argv_pins_the_security_flags(monkeypatch):
    monkeypatch.setenv("PERISCOPE_CLAUDE_BIN", "/usr/bin/claude")
    argv = bgc._dispatch_argv(session_id="sid-1", text="do the thing")
    assert argv[0] == "/usr/bin/claude"        # the BARE binary, never the multi-word claude_exec() string
    assert "--bg" in argv
    assert argv[argv.index("--session-id") + 1] == "sid-1"
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--allowedTools") + 1] == bgc.config.BG_COMMANDER_ALLOWED_TOOLS
    assert argv[-1] == "do the thing"          # the command is the trailing positional


def test_dispatch_env_sets_caller_id():
    env = bgc._dispatch_env(session_id="sid-1")
    assert env["PERISCOPE_CALLER_ID"] == "cmdr:sid-1"
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_bg_commander.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'periscope.bg_commander'`.

- [ ] **Step 4: Create `periscope/bg_commander.py` with the pure seams**

```python
"""bg_commander — per-command ephemeral `claude --bg` commanders + job tracking.

Replaces the singleton commander pane (commander.py, deleted). Each omnibox
command dispatches a fresh `claude --bg` session that orchestrates workers and
exits; the session id IS the job id. A `commands` table (in the shared activity
DB, own schema) tracks each job; status syncs from `claude agents --json --all`.
prod-only (needs the MCP socket + subscription auth).

Imports only periscope.config / periscope.log + stdlib — never activity (the
worker tick imports US, one-way, to avoid a cycle).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass

from periscope import config
from periscope.log import log

ROLE_PROMPT = """\
<<<MOVE VERBATIM FROM periscope/commander.py ROLE_PROMPT — the full triple-quoted
string at commander.py:19-57. Do not reword; it is the delegator contract.>>>"""

_ABSENT_GRACE_S = 60   # absent from `claude agents` AND younger than this => still registering, keep running

_SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
  id          TEXT PRIMARY KEY,   -- the --bg session_id (uuid)
  text        TEXT NOT NULL,
  cwd         TEXT NOT NULL,
  status      TEXT NOT NULL,      -- 'running' | 'done'
  started_at  INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class Job:
    id: str
    text: str
    cwd: str
    status: str
    started_at: int


# --- pure seams (no I/O) ---

def parse_agents_json(raw: str) -> dict[str, str]:
    """{sessionId: state} for kind=="background" entries only. Empty on any parse
    error. Background sessions carry `state` (done/blocked/running/...), NOT the
    `status` field interactive sessions use — verified against claude v2.1.190."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, list):
        return {}
    out: dict[str, str] = {}
    for a in data:
        if isinstance(a, dict) and a.get("kind") == "background" and a.get("sessionId"):
            out[a["sessionId"]] = a.get("state", "")
    return out


def map_state(state: str | None, *, started_at: int, now: int, present: bool) -> str:
    """The spec's full rule. present => done iff state=='done', else running.
    absent => done iff older than the grace (reaped), else running (still
    registering — never flip a brand-new job to done before it appears)."""
    if present:
        return "done" if state == "done" else "running"
    return "done" if now - started_at >= _ABSENT_GRACE_S else "running"


def _dispatch_argv(*, session_id: str, text: str) -> list[str]:
    """The launchd-auth risk boundary (Step 0). If a login-shell/env wrapper is
    needed, ONLY this function changes."""
    return [
        config.claude_bin(), "--bg",
        "--session-id", session_id,
        "--append-system-prompt-file", str(config.ORCHESTRATOR_PROMPT_FILE),
        "--mcp-config", str(config.PERISCOPE_MCP_CONFIG), "--strict-mcp-config",
        "--model", config.BG_COMMANDER_MODEL,
        "--allowedTools", config.BG_COMMANDER_ALLOWED_TOOLS,
        text,
    ]


def _dispatch_env(*, session_id: str) -> dict[str, str]:
    """Inherit the process env + the per-command caller handle. channel_shim
    reads PERISCOPE_CALLER_ID (falling back to TMUX_PANE)."""
    return {**os.environ, "PERISCOPE_CALLER_ID": f"cmdr:{session_id}"}
```

- [ ] **Step 5: Run to verify the pure seams pass**

Run: `uv run pytest tests/test_bg_commander.py -q`
Expected: PASS (7 tests). The verbatim `ROLE_PROMPT` move is mechanical — copy commander.py:19-57's string literal exactly.

- [ ] **Step 6: Commit**

```bash
git add periscope/config.py periscope/bg_commander.py tests/test_bg_commander.py
git commit -m "feat(bg_commander): pure seams — agents-json parse, status map, dispatch argv/env + config"
```

---

## Task 2: `bg_commander.py` — table CRUD + dispatch (TDD)

**Files:**
- Modify: `periscope/bg_commander.py`
- Test: `tests/test_bg_commander.py`

- [ ] **Step 1: Write failing CRUD + dispatch tests**

Append to `tests/test_bg_commander.py`:

```python
import pytest
from periscope import bg_commander as bgc


@pytest.fixture(autouse=True)
def _db(fresh_activity_db):
    # bg_commander._conn() opens config.ACTIVITY_DB fresh each call; the fixture
    # repoints it at a temp file. No bg_commander-side connection cache to reset.
    yield


def test_insert_then_list_and_get():
    bgc.insert_job(id="j1", text="hello", cwd="/tmp", at=100)
    bgc.insert_job(id="j2", text="world", cwd="/tmp", at=200)
    jobs = bgc.list_jobs()
    assert [j.id for j in jobs] == ["j2", "j1"]          # newest-first
    assert bgc.get_job("j1") == bgc.Job(id="j1", text="hello", cwd="/tmp", status="running", started_at=100)
    assert bgc.get_job("nope") is None


def test_running_job_ids_excludes_done():
    bgc.insert_job(id="r", text="x", cwd="/tmp", at=1)
    bgc.insert_job(id="d", text="y", cwd="/tmp", at=2)
    bgc._set_status("d", "done")
    assert bgc.running_job_ids() == {"r"}


def test_dispatch_inserts_running_row_then_popens(monkeypatch):
    spawned = {}
    def fake_popen(argv, **kw):
        spawned["argv"], spawned["kw"] = argv, kw
        return object()
    monkeypatch.setattr(bgc.subprocess, "Popen", fake_popen)
    jid = bgc.dispatch("do it", cwd="/tmp")
    # row exists immediately (closes the absent-window race from the write side)
    job = bgc.get_job(jid)
    assert job is not None and job.status == "running" and job.text == "do it"
    assert spawned["kw"]["cwd"] == "/tmp"
    assert spawned["kw"]["env"]["PERISCOPE_CALLER_ID"] == f"cmdr:{jid}"
    assert spawned["argv"][-1] == "do it"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_bg_commander.py -q`
Expected: FAIL — `AttributeError: module 'periscope.bg_commander' has no attribute 'insert_job'`.

- [ ] **Step 3: Implement CRUD + dispatch**

Append to `periscope/bg_commander.py`:

```python
# --- table CRUD (shared ACTIVITY_DB file, own schema) ---

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(config.ACTIVITY_DB)
    c.execute(_SCHEMA.strip())
    return c


def insert_job(*, id: str, text: str, cwd: str, at: int) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO commands (id, text, cwd, status, started_at) VALUES (?,?,?,?,?)",
            (id, text, cwd, "running", at),
        )


def list_jobs() -> list[Job]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, text, cwd, status, started_at FROM commands "
            "ORDER BY started_at DESC, id DESC"
        ).fetchall()
    return [Job(*r) for r in rows]


def get_job(job_id: str) -> Job | None:
    with _conn() as c:
        row = c.execute(
            "SELECT id, text, cwd, status, started_at FROM commands WHERE id=?",
            (job_id,),
        ).fetchone()
    return Job(*row) if row else None


def running_job_ids() -> set[str]:
    """The validation set for is_commander() — a cmdr:<id> hello is trusted only
    if <id> is a live (status='running') dispatched job."""
    with _conn() as c:
        rows = c.execute("SELECT id FROM commands WHERE status='running'").fetchall()
    return {r[0] for r in rows}


def _set_status(job_id: str, status: str) -> None:
    with _conn() as c:
        c.execute("UPDATE commands SET status=? WHERE id=?", (status, job_id))


# --- dispatch ---

def dispatch(text: str, *, cwd: str | None = None) -> str:
    """Mint a session id, insert the running row, fire-and-forget `claude --bg`,
    return the id. Insert-before-Popen so is_commander() + the job list see the
    job the instant /api/command returns, independent of `claude agents` lag."""
    session_id = str(uuid.uuid4())
    cwd = cwd or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    insert_job(id=session_id, text=text, cwd=cwd, at=int(time.time()))
    subprocess.Popen(
        _dispatch_argv(session_id=session_id, text=text),
        cwd=cwd,
        env=_dispatch_env(session_id=session_id),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return session_id
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_bg_commander.py -q`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add periscope/bg_commander.py tests/test_bg_commander.py
git commit -m "feat(bg_commander): commands-table CRUD + fire-and-forget dispatch"
```

---

## Task 3: `bg_commander.py` — status sync + cleanup + mcp config (TDD)

**Files:**
- Modify: `periscope/bg_commander.py`
- Test: `tests/test_bg_commander.py`

- [ ] **Step 1: Write failing sync tests**

Append to `tests/test_bg_commander.py`:

```python
def test_sync_jobs_marks_done_and_stops_present(monkeypatch):
    bgc.insert_job(id="busy", text="x", cwd="/tmp", at=1000)
    bgc.insert_job(id="fin",  text="y", cwd="/tmp", at=1000)
    raw = json.dumps([
        {"kind": "background", "sessionId": "busy", "state": "blocked"},
        {"kind": "background", "sessionId": "fin",  "state": "done"},
    ])
    stopped = []
    bgc.sync_jobs(now=1001, agents_raw=raw, stop_fn=stopped.append)
    assert bgc.get_job("busy").status == "running"
    assert bgc.get_job("fin").status == "done"
    assert stopped == ["fin"]                      # proactive claude stop on a still-listed done session


def test_sync_jobs_absent_young_stays_running_old_reaped(monkeypatch):
    bgc.insert_job(id="young", text="x", cwd="/tmp", at=1000)
    bgc.insert_job(id="old",   text="y", cwd="/tmp", at=1000)
    stopped = []
    # young: now-started_at < 60 ; old: >= 60 ; neither present in the (empty) list
    bgc.sync_jobs(now=1030, agents_raw="[]", stop_fn=stopped.append)  # young still within grace
    assert bgc.get_job("young").status == "running"
    bgc.sync_jobs(now=1100, agents_raw="[]", stop_fn=stopped.append)  # both now old
    assert bgc.get_job("young").status == "done"
    assert bgc.get_job("old").status == "done"
    assert stopped == []                           # absent/reaped => no stop call (already gone)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_bg_commander.py -q`
Expected: FAIL — `AttributeError: ... 'sync_jobs'`.

- [ ] **Step 3: Implement sync + cleanup + mcp config**

Append to `periscope/bg_commander.py`:

```python
# --- status sync (called from the activity worker tick AND on-open from /jobs) ---

def _read_agents() -> str:
    try:
        return subprocess.run(
            [config.claude_bin(), "agents", "--json", "--all"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("claude agents read failed: %s", e)
        return ""


def _stop_session(session_id: str) -> None:
    try:
        subprocess.run(
            [config.claude_bin(), "stop", session_id],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("claude stop %s failed: %s", session_id, e)


def sync_jobs(*, now: int | None = None, agents_raw: str | None = None, stop_fn=None) -> None:
    """Reconcile running jobs against `claude agents --json --all`. Idempotent —
    safe from both the 30s worker tick and GET /api/command/jobs. agents_raw /
    stop_fn are injectable seams so tests never spawn `claude` (no subprocess
    mocking — the Q1 mocked-migration trap)."""
    now = now if now is not None else int(time.time())
    raw = agents_raw if agents_raw is not None else _read_agents()
    stop = stop_fn or _stop_session
    states = parse_agents_json(raw)
    for job in list_jobs():
        if job.status != "running":
            continue
        present = job.id in states
        new_status = map_state(states.get(job.id), started_at=job.started_at, now=now, present=present)
        if new_status == "done":
            _set_status(job.id, "done")
            if present:        # still listed => free the supervisor slot now (spec resolution #4)
                stop(job.id)


# --- boot-time generation (prod-only, called from app.py lifespan) ---

def write_mcp_config() -> None:
    """Generate the static MCP config (channel_shim → socket) and the orchestrator
    prompt file. The per-command identity is NOT here — it rides on the dispatched
    process env (PERISCOPE_CALLER_ID). Contingency: if the MCP child doesn't inherit
    parent env, switch to a per-dispatch config with the caller id in `env`."""
    import sys
    cfg = {
        "mcpServers": {
            "periscope": {
                "command": sys.executable,
                "args": [str(config.CHANNEL_SHIM_PATH)],
                "env": {"PERISCOPE_MCP_SOCKET_PATH": config.MCP_SOCKET_PATH},
            }
        }
    }
    config.PERISCOPE_MCP_CONFIG.write_text(json.dumps(cfg))
    config.ORCHESTRATOR_PROMPT_FILE.write_text(ROLE_PROMPT)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_bg_commander.py -q`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add periscope/bg_commander.py tests/test_bg_commander.py
git commit -m "feat(bg_commander): status sync (state-based, absent-grace) + proactive stop + mcp config"
```

---

## Task 4: channel handle generalization (`channels.py` + `channel_shim.py`) (TDD)

Generalize the registry key from pane-id to an opaque handle; accept `cmdr:` callers; `is_commander(handle)` validated against the running-jobs set; skip caller-context derivation for commanders. Remove captain's-log gating.

**Files:**
- Modify: `periscope/channels.py` (guard ~876, `is_commander` new, spawn_claude ~505, captain's-log ~208-245 + `_CHANNEL_TOOLS` ~1459-1486)
- Modify: `channel_shim.py` (caller-id source ~44, guard ~85, hello value ~169)
- Test: `tests/test_channels.py`, `tests/test_channel_shim.py`

- [ ] **Step 1: Write failing tests**

In `tests/test_channels.py` add (use `fresh_activity_db` so `running_job_ids` reads a temp DB):

```python
def test_is_commander_requires_live_job(fresh_activity_db):
    from periscope import channels, bg_commander
    assert channels.is_commander("%3") is False
    assert channels.is_commander("cmdr:unknown") is False
    bg_commander.insert_job(id="live", text="x", cwd="/tmp", at=1)
    assert channels.is_commander("cmdr:live") is True
    bg_commander._set_status("live", "done")
    assert channels.is_commander("cmdr:live") is False     # only running jobs count
```

In `tests/test_channel_shim.py`, add a unit assertion on the caller-id source (the existing file imports the shim module; mirror its style):

```python
def test_caller_id_prefers_explicit_handle(monkeypatch):
    import importlib, channel_shim
    monkeypatch.setenv("PERISCOPE_CALLER_ID", "cmdr:abc")
    monkeypatch.setenv("TMUX_PANE", "%9")
    importlib.reload(channel_shim)
    assert channel_shim.CALLER_ID == "cmdr:abc"
    monkeypatch.delenv("PERISCOPE_CALLER_ID")
    importlib.reload(channel_shim)
    assert channel_shim.CALLER_ID == "%9"
```

(After this test, reload once more in a fixture or at module teardown is unnecessary — pytest process-isolation across files is fine; if the reload leaks, set both envs back and reload in a `finally`. Keep it simple: this test runs standalone.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_channels.py::test_is_commander_requires_live_job tests/test_channel_shim.py::test_caller_id_prefers_explicit_handle -q`
Expected: FAIL — `AttributeError: ... 'is_commander'` and `CALLER_ID`.

- [ ] **Step 3: `channel_shim.py` — caller-id source + guard + hello value**

Replace line 44 (`TMUX_PANE = os.environ.get("TMUX_PANE", "")`) with:

```python
TMUX_PANE = os.environ.get("TMUX_PANE", "")
# The caller handle: an explicit commander id (cmdr:<session_id>) when periscope
# dispatched us via --bg, else the tmux pane id for a normal pane.
CALLER_ID = os.environ.get("PERISCOPE_CALLER_ID", "") or TMUX_PANE
```

Replace the `run()` guard (lines 85-90):

```python
        if not (CALLER_ID.startswith("%") or CALLER_ID.startswith("cmdr:")):
            _err(
                f"caller id missing or malformed ({CALLER_ID!r}); "
                "periscope MCP inactive for this session"
            )
            return
```

Replace the hello frame (line 169) — keep the JSON key `"pane"` (a private 2-file wire contract; the value is now any handle):

```python
        # Hello frame: periscope reads one JSON line on accept() to learn this
        # connection's caller handle (%N for a pane, cmdr:<id> for a commander).
        sock_w.write((json.dumps({"pane": CALLER_ID}) + "\n").encode())
```

- [ ] **Step 4: `channels.py` — relax the server guard + add `is_commander`**

Replace the connection guard (line 876) `if not pane.startswith("%"):` with:

```python
        if not (pane.startswith("%") or pane.startswith("cmdr:")):
            return
```

Add `is_commander` near the old `_require_commander` location (replacing it). Place it as a module-level function:

```python
def is_commander(handle: str) -> bool:
    """True iff `handle` is a live commander: the cmdr: prefix AND a currently
    running dispatched job (defense-in-depth against a self-asserted prefix; the
    socket is already owner-only). Replaces the singleton marker check."""
    if not handle.startswith("cmdr:"):
        return False
    from periscope import bg_commander
    return handle[len("cmdr:"):] in bg_commander.running_job_ids()
```

- [ ] **Step 5: `channels.py` — spawn_claude commander branch**

In `_do_spawn_claude_tool` (line 454+): the unconditional `tmux display-message -t <pane>` at lines 468-471 must be skipped for a commander handle (`cmdr:<id>` is not a tmux target). Restructure the top of the function so the commander check happens before the display-message call:

Replace lines 465-471 (the caller-context derivation block):

```python
    # Caller's pane → its session + cwd. Commanders have no pane (cmdr:<id> is
    # not a tmux target), so skip the derivation — they always pass explicit cwd.
    commander_caller = is_commander(pane)
    if commander_caller:
        caller_session, caller_cwd = "", ""
    else:
        info = tmux(
            "display-message", "-t", pane, "-p", "#{session_name}|#{pane_current_path}",
        ).strip()
        caller_session, _, caller_cwd = info.partition("|")
```

Then replace line 505 (`is_commander = activity.is_commander_pane(pane)`) — the local now exists as `commander_caller`; delete that line and update line 507's `if is_commander:` to `if commander_caller:`. Also drop the now-unused `from periscope import open_ops, activity` if `activity` is unused elsewhere in the function (keep `open_ops`; check — `activity` was only used for `is_commander_pane` here, so change the import to `from periscope import open_ops`).

- [ ] **Step 6: `channels.py` — remove captain's-log entirely**

Delete: `_CAPTAINS_LOG_KINDS` (line 208), `_require_commander` (lines 211-216), `_do_captains_log_read_tool` (219-229ish), `_do_captains_log_append_tool` (231-245ish), and the two `_CHANNEL_TOOLS` records for `captains_log_read` / `captains_log_append` (lines ~1459-1486). (Captain's-log was the persistent singleton's cross-command memory; the ephemeral model has no use for it and the pinned allowlist makes the tools unreachable anyway.)

- [ ] **Step 7: Remove the now-dead `test_channels.py` commander/captain's-log tests + rewrite the spawn_claude commander tests**

`tests/test_channels.py` has tests that reference the deleted surface — they will error-collect. Delete:
- The captain's-log tests: `test_commander_tools_are_registered`, `test_captains_log_tools_refuse_non_commander`, `test_captains_log_append_and_read_for_commander`, `..._rejects_bad_kind`, `..._rejects_empty_text` (around lines 718-763).

Rewrite the two spawn_claude commander tests (`test_spawn_commander_anchors_on_cwd` / `..._non_git_cwd_errors`, ~422-468) — they currently call `activity.set_commander(...)` to mark the caller a commander. The commander path is now `channels.is_commander(handle)` keyed on a live job. New shape: seed a running job and pass its handle as the caller:

```python
def test_spawn_claude_commander_skips_caller_derivation(fresh_activity_db, monkeypatch):
    from periscope import channels, bg_commander
    bg_commander.insert_job(id="c1", text="x", cwd="/tmp", at=1)
    # is_commander("cmdr:c1") is True; the commander branch must NOT call
    # `tmux display-message -t cmdr:c1` (not a tmux target) and must cwd-anchor.
    # ... mirror the existing test's resolve_worktree_session monkeypatch + assert
    #     no display-message call for the cmdr: handle, error on non-git cwd.
```

Keep the existing test's `open_ops.resolve_worktree_session` monkeypatch structure; just swap the commander-identity setup from `set_commander` to a seeded job + `cmdr:` handle.

- [ ] **Step 8: Run the changed tests + the channel suite**

Run: `uv run pytest tests/test_channels.py tests/test_channel_shim.py -q`
Expected: PASS. If `test_channel_shim.py` reconnect tests fail spuriously, `uv sync` first (the `.venv` drift landmine — see CLAUDE.md).

- [ ] **Step 9: Commit**

```bash
git add periscope/channels.py channel_shim.py tests/test_channels.py tests/test_channel_shim.py
git commit -m "feat(channels): handle-keyed registry — cmdr: callers, is_commander validation, drop captain's-log"
```

---

## Task 5: `routes/command.py` rewrite — dispatch + jobs + transcript (TDD)

**Files:**
- Rewrite: `periscope/routes/command.py`
- Test: `tests/routes/test_command.py`

- [ ] **Step 1: Write failing route tests**

Rewrite `tests/routes/test_command.py` (the route is thin — monkeypatch `bg_commander`). Use the repo's existing TestClient/app fixture (see other `tests/routes/test_*.py` for the `client` fixture pattern):

```python
def test_post_command_dispatches(client, monkeypatch):
    from periscope import bg_commander
    monkeypatch.setattr(bg_commander, "dispatch", lambda text, **kw: "job-xyz")
    r = client.post("/api/command", json={"text": "do it"})
    assert r.status_code == 200
    assert r.json() == {"job_id": "job-xyz"}


def test_post_command_rejects_empty(client):
    r = client.post("/api/command", json={"text": "   "})
    assert r.status_code == 400


def test_get_jobs_syncs_then_lists(client, monkeypatch):
    from periscope import bg_commander
    calls = {"synced": False}
    monkeypatch.setattr(bg_commander, "sync_jobs", lambda **kw: calls.__setitem__("synced", True))
    monkeypatch.setattr(bg_commander, "list_jobs",
        lambda: [bg_commander.Job(id="j1", text="t", cwd="/tmp", status="done", started_at=5)])
    r = client.get("/api/command/jobs")
    assert r.status_code == 200
    assert calls["synced"] is True
    assert r.json() == [{"id": "j1", "text": "t", "status": "done", "started_at": 5}]


def test_get_job_turns_404_on_unknown(client, monkeypatch):
    from periscope import bg_commander
    monkeypatch.setattr(bg_commander, "get_job", lambda jid: None)
    r = client.get("/api/command/jobs/nope/turns")
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/routes/test_command.py -q`
Expected: FAIL (the old route returns `{"session", "index"}`, has no `/jobs`).

- [ ] **Step 3: Rewrite the route**

Replace `periscope/routes/command.py` entirely:

```python
"""POST /api/command — dispatch a free-text command as a fresh `claude --bg`
commander (a tracked job). GET /api/command/jobs — the job list (newest-first,
status synced from `claude agents`). GET /api/command/jobs/{id}/turns — a job's
transcript from its session JSONL."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope import bg_commander, turns
from history.search import messages_from_jsonl

router = APIRouter()


class CommandBody(BaseModel):
    text: str


@router.post("/api/command")
def command_endpoint(body: CommandBody):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "text must be non-empty")
    return {"job_id": bg_commander.dispatch(text)}


@router.get("/api/command/jobs")
def command_jobs():
    bg_commander.sync_jobs()      # on-open fresh read (the worker tick also syncs every 30s)
    return [
        {"id": j.id, "text": j.text, "status": j.status, "started_at": j.started_at}
        for j in bg_commander.list_jobs()
    ]


@router.get("/api/command/jobs/{job_id}/turns")
def command_job_turns(job_id: str):
    if bg_commander.get_job(job_id) is None:
        raise HTTPException(404, "unknown job")
    jsonl = turns.jsonl_for_session(job_id)     # session id IS the job id
    if jsonl is None:
        raise HTTPException(404, "no transcript yet")
    return {"session_id": job_id, "messages": messages_from_jsonl(str(jsonl))}
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/routes/test_command.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/command.py tests/routes/test_command.py
git commit -m "feat(routes): /api/command dispatch + /jobs list + /jobs/{id}/turns transcript"
```

---

## Task 6: activity.py shrink + worker-tick sync hook (TDD)

Drop the commander marker + captain_log from activity.py; wire `sync_jobs` into the worker tick.

**Files:**
- Modify: `periscope/activity.py` (`_SCHEMA` lines 79-86, `CaptainLogRow`/`append_captain_log`/`recent_captain_log` ~433-465, commander accessors ~468-501, `_worker_tick` ~797)
- Test: `tests/test_activity.py`

- [ ] **Step 1: Write the failing tick test**

Add to `tests/test_activity.py`:

```python
def test_worker_tick_syncs_bg_jobs(monkeypatch, fresh_activity_db):
    from periscope import activity, bg_commander
    monkeypatch.setattr(activity, "list_windows", lambda: [])     # no panes => skip narrator path
    called = {"sync": False}
    monkeypatch.setattr(bg_commander, "sync_jobs", lambda **kw: called.__setitem__("sync", True))
    activity._worker_tick({})
    assert called["sync"] is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_activity.py::test_worker_tick_syncs_bg_jobs -q`
Expected: FAIL — `sync_jobs` not called.

- [ ] **Step 3: Wire sync into the tick + drop the dead tables/accessors**

In `_worker_tick` (after the `narrator.tick` try/except, before `checkpoint()`), add:

```python
    # bg commander job status sync (prod-only, same as the narrator above).
    try:
        from periscope import bg_commander
        bg_commander.sync_jobs()
    except Exception:
        log.exception("bg_commander sync failed")
```

Then delete the now-dead commander/captain_log surface:
- In `_SCHEMA`: remove the `captain_log` table + its index (lines 79-85) and the `commander` table (line 86+).
- Remove `CaptainLogRow` (line 433), `append_captain_log` (447), `recent_captain_log` (457).
- Remove `CommanderMarker`, `get_commander` (468), `set_commander` (477), `clear_commander` (492), `is_commander_pane` (499).

- [ ] **Step 4: Run the activity suite**

Run: `uv run pytest tests/test_activity.py -q`
Expected: PASS. Remove any pre-existing test assertions referencing captain_log / commander (they no longer exist) — grep the test file first: `grep -n "captain\|commander" tests/test_activity.py`.

- [ ] **Step 5: Commit**

```bash
git add periscope/activity.py tests/test_activity.py
git commit -m "refactor(activity): drop commander marker + captain_log; sync bg jobs from worker tick"
```

---

## Task 7: rip out the singleton (app.py, narrator.py, state.py, commander.py) + grep gate

**Files:**
- Modify: `periscope/app.py` (`_archive_stale_commander_project` ~36-49, boot block ~103-114)
- Modify: `periscope/narrator.py` (`_is_commander` ~145, `is_commander_pane` skip ~262, `_is_commander` use ~334)
- Modify: `periscope/routes/state.py` (rail-hide line ~130)
- Delete: `periscope/commander.py`, `tests/test_commander.py`, `tests/test_commander_spawn.py`

- [ ] **Step 1: `app.py` — remove archival + boot-spawn, add config generation**

- Delete `_archive_stale_commander_project()` (lines 36-49) entirely.
- In the lifespan boot block (around lines 103-114), remove the `from periscope import activity, commander` commander part, the `_archive_stale_commander_project()` call, and the `await commander.ensure_commander()` best-effort spawn + its try/except.
- In its place, generate the bg-commander config once at boot. **Fold this into the EXISTING `if config.is_prod():` block at app.py:102** (the one that boots the activity worker) — do not add a second guard:

```python
            # inside the existing `if config.is_prod():` block, alongside the worker boot
            from periscope import bg_commander
            try:
                bg_commander.write_mcp_config()
            except Exception:
                log.warning("bg_commander.write_mcp_config failed", exc_info=True)
```

(`config` is in scope via app.py:50, `log` via app.py:20 — both confirmed.)

- [ ] **Step 2: `narrator.py` — remove commander skips**

- Delete `_is_commander` (lines 145-153).
- At line ~262, delete the `if activity.is_commander_pane(pane_id): ...` skip block (read the surrounding lines to remove the whole guard cleanly — a commander is never a pane the narrator sees now).
- At line ~334, remove the `_is_commander(w, pane_id)` condition from the `if new_name and _is_commander(...)` rename-suppression block (and its comment 335-336). Read the block: if `_is_commander` was the only extra condition, the rename now applies normally; keep the rest of the `if new_name` logic intact.

- [ ] **Step 3: `routes/state.py` — remove the rail-hide line**

Delete lines ~125-130 (the comment + `result = [w for w in result if not activity.is_commander_pane(...)]`). A `--bg` commander is never a pane in `result`, so no filter is needed. The **local** `from periscope import activity` at line ~129 exists ONLY for that filter — remove it together with line 130. (Keep the module-level `from periscope.activity import pane_status_lines` at line 15 — that's a different, still-used import.)

- [ ] **Step 4: Delete the dead module + its tests; clean up `tests/test_app.py`**

```bash
git rm periscope/commander.py tests/test_commander.py tests/test_commander_spawn.py
```

`tests/test_app.py` references the deleted boot surface — these will error-collect. Remove:
- `test_archive_stale_bridge_project` and `test_archive_stale_..._leaves_others` (they call `app_mod._archive_stale_commander_project()`, ~lines 136-155).
- The `periscope.commander.ensure_commander` mocks/patches in the lifespan-boot tests (~lines 62-63, 123-127). Those tests stub the boot-spawn block that this task deletes; drop the mock (and the assertion that it was called). Re-read the surrounding test bodies and excise cleanly.

- [ ] **Step 5: Grep gate — confirm the singleton is fully gone**

Run (these going-away tokens must return ZERO hits):

```bash
grep -rn "COMMANDER_SESSION\|COMMANDER_WINDOW\|is_commander_pane\|ensure_commander\|_archive_stale_commander\|captain" periscope/ tests/ static/src/ server.py
```

Expected: zero hits. (`bg_commander` the module and `channels.is_commander` legitimately contain the substring "commander" — they're NOT in this gate's token list, so a clean run means the singleton is fully excised. If any listed token remains, fix it before committing.)

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS (was 634 on a clean run; the delta is the removed commander tests + the new bg_commander/route tests). If only the two `test_channel_shim.py` reconnect tests fail, `uv sync` and re-run (the `.venv` drift landmine).

- [ ] **Step 7: Commit**

```bash
git add periscope/app.py periscope/narrator.py periscope/routes/state.py tests/test_app.py
git commit -m "refactor: rip out singleton commander (boot-spawn, narrator skips, rail-hide); generate bg config at boot"
```

---

## Task 8: omnibox job list (frontend — verified in the browser, not unit-tested)

Per CLAUDE.md (UI work): verify in the browser, don't unit-test components before the feature works. `classify.js`'s pure classifier keeps its existing unit tests.

**Files:**
- Modify: `static/src/overlays/OpenOmnibox.jsx` (replace `useCommanderConsole` + `CommanderConsole` ~31-80, 217-240; the `command` card handling ~167; the render branch ~186-190)
- Reuse: `static/src/split/Transcript.jsx`'s `renderTurn` for the transcript body
- Build: `npm run build` (commits `static/dist/app.js`)

- [ ] **Step 1: Replace the console hook with a job-list view**

Remove `useCommanderConsole` and `CommanderConsole`. Add a job-list mode:
- State: `const [jobsView, setJobsView] = useState(false)` and `const [selectedJob, setSelectedJob] = useState(null)`.
- Picking the `⚡ run` card (line 167, currently `setConsole({text: card.text})`): POST `/api/command` via `apiCall`, then `setJobsView(true)` and refresh the job list (select the returned `job_id`).
- Job list: `apiCall("jobs", "/api/command/jobs")` → rows of `{id, text, status, started_at}`; status dot (● running / ✓ done via `status === "running"`), `relTime(started_at)` (import from `util.js`). Poll while the view is open (reuse the existing omnibox poll idiom; a 3s interval is fine).
- Selecting a row: `apiCall("job-turns", \`/api/command/jobs/${id}/turns\`)` → render `messages` with `renderTurn` (read-only). Keep the omnibox open; Esc returns to the list / closes (reuse `useEscape`).
- Closing and reopening the omnibox shows the same server-backed list (state lives server-side — no client persistence needed).

- [ ] **Step 2: Build the bundle**

Run: `npm run build`
Expected: rebuilds `static/dist/app.js` with no errors.

- [ ] **Step 3: Verify in the browser (dev)**

Run dev periscope: `PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py` (or `npm run dev`). Open the omnibox, type a query, pick `⚡ run`. NOTE: dispatch is **prod-only** (the MCP socket + auth) — in dev the job list renders and the POST succeeds, but the commander won't actually connect. Confirm: the `⚡ run` card dispatches without error, the job list renders with a row, selecting a row hits `/jobs/{id}/turns` (404 "no transcript yet" is the expected dev result — the list/empty-state UI should handle it gracefully). Full end-to-end is the prod smoke (manual gate, step 6).

- [ ] **Step 4: Commit**

```bash
git add static/src/overlays/OpenOmnibox.jsx static/dist/app.js
git commit -m "feat(omnibox): job list + read-only transcript replaces the live console"
```

---

## Self-Review (completed by plan author)

**Spec coverage:**
- Dispatch (spec §"The dispatch") → Tasks 1-3 + 5. ✓
- Caller identity / handle rework (spec §"The hard part") → Task 4 (both guards, `is_commander` + jobs-table validation, spawn_claude commander branch). ✓
- `commands` table + status sync (spec §"Job tracking") → Tasks 2-3 + 6 (state-not-status, absent-grace, proactive stop). ✓
- Endpoints (spec §"Job tracking") → Task 5. ✓
- Omnibox job list (spec §"Omnibox") → Task 8. ✓
- Removal completeness (spec §"Replaced / removed") → Tasks 6-7 + grep gate. ✓
- `PERISCOPE_MCP_CONFIG` / orchestrator prompt file → Task 3 (`write_mcp_config`) + Task 7 (boot call). ✓
- Open-question resolutions (validate cmdr, proactive stop, launchd gate) → Tasks 3-4 + manual gates. ✓

**Type consistency:** `Job(id,text,cwd,status,started_at)` used identically across Tasks 1/2/3/5/6. `is_commander(handle)`, `running_job_ids()`, `sync_jobs(*, now, agents_raw, stop_fn)`, `dispatch(text, *, cwd)`, `parse_agents_json`, `map_state` — signatures consistent across definition and call sites.

**Placeholder scan:** one deliberate placeholder — `ROLE_PROMPT` in Task 1 Step 4 is a verbatim-copy instruction (the literal lives at commander.py:19-57; reproducing 38 lines inline would risk drift). Every other step has concrete code/commands.

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
You are periscope's commander. The user sends you ONE command from the omnibox.
Your ONLY job is to SET UP and DELEGATE — never to do the work yourself.

HARD RULES (these override any instinct to be helpful by doing the task):
- NEVER do the task yourself. NEVER load or run a skill. NEVER write code, run a
  health check, edit files, run builds, or produce the task's actual deliverable.
  You delegate ALL real work to a worker you spawn with spawn_claude.
- NEVER ask the user a clarifying question — you cannot receive their answer (the
  omnibox is one-way). If the command is ambiguous, make a best guess and spawn;
  the WORKER you spawn will ask the user in its own pane if it needs to.
- Use Read/Grep/Glob ONLY to figure out WHICH repo/dir the command means — never
  to start solving the task. Call catalog() once to see repos + worktrees.

For EVERY command, do exactly this and then STOP:
1. Resolve the target repo/dir (catalog + a quick look if needed).
2. spawn_claude a worker with a clear first-message prompt that restates the
   user's full task in the worker's voice, placed per Placement below.
3. Reply with ONE short line: what you spawned and where. Then stop — do nothing else.

Placement — how you spawn the worker:
- Fresh worktree (the command says "worktree", or it's a PR / refactor / risky /
  ambiguous): spawn_claude(repo=<repo path>, branch=<new slug>, prompt=<task>).
  This creates the worktree AND places the worker in it in ONE call — YOU own the
  worktree creation. NEVER spawn in the main checkout and tell the worker to make
  the worktree itself. Branch slug: short + descriptive (e.g. tc/health-check).
- Main checkout (quick edit / question / look-at): spawn_claude(cwd=<repo root>, prompt=<task>).
- Existing worktree/project: spawn_claude(cwd=<that dir>, prompt=<task>).
Heuristics: PR / refactor / "try" / risky / "in a new worktree" -> worktree;
quick / read-only -> main checkout; "in <project>" -> that project. Ambiguous ->
fresh worktree. Honor the user's explicit placement.

Tools: catalog, spawn_claude (your MAIN tool — repo+branch makes a worktree and
spawns into it; workspace_id groups related spawns), open (open existing path /
branch / PR into the rail), create_workspace, list_claudes, list_workspaces. The
worker you spawn has FULL tools; you do not.

Prohibitions: never merge an fdy PR; never force-push; never prod-touching actions.
"""

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
    # config_dir() doesn't mkdir; on a first-ever boot the dir may not exist yet
    # (the activity-prune thread that normally creates it races this call).
    config.PERISCOPE_MCP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    config.PERISCOPE_MCP_CONFIG.write_text(json.dumps(cfg))
    config.ORCHESTRATOR_PROMPT_FILE.write_text(ROLE_PROMPT)

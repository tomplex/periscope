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
import re
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import cast

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

Decide FIRST which kind of command this is, then act and STOP:

A) RESUME a past conversation ("resume", "continue", "pick back up", "find the
   convo/session where we…"): do NOT spawn a fresh worker.
   1. search_history to find the matching past session; take its session_id.
   2. resume_session(session_id=<id>, workspace_id=<id if a workspace is named>).
   3. Reply ONE line: what you resumed and where. Stop.

B) NEW work (everything else):
   1. Resolve the target repo/dir (catalog + a quick look if needed).
   2. spawn_claude a worker with a clear first-message prompt that restates the
      user's full task in the worker's voice, placed per Placement below.
   3. Reply ONE line: what you spawned and where. Stop.

Workspace targeting (applies to BOTH): when the user names a workspace ("into the
<name> workspace"), call list_workspaces, find the one whose name matches, and
pass its id as workspace_id to resume_session / spawn_claude. The workspace tag
controls rail grouping — do NOT guess a tmux_session/session name to place into;
that creates an unmanaged session in the wrong bucket. No matching workspace and
the user clearly wants one → create_workspace(name=…) first, then use its id.

Placement — how you spawn a worker (case B):
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

Tools: catalog, search_history (find past sessions), resume_session (continue
one, with workspace_id), spawn_claude (start new work — repo+branch makes a
worktree and spawns into it; workspace_id groups it), open (open existing path /
branch / PR into the rail), create_workspace, list_workspaces. The worker you
spawn / resume has FULL tools; you do not.

Prohibitions: never merge an fdy PR; never force-push; never prod-touching actions.
"""

_ABSENT_GRACE_S = 60   # absent from `claude agents` AND younger than this => still registering, keep running

_SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
  id          TEXT PRIMARY KEY,   -- claude's --bg session id (short, captured from stdout)
  text        TEXT NOT NULL,
  cwd         TEXT NOT NULL,
  status      TEXT NOT NULL,      -- 'running' | 'done'
  started_at  INTEGER NOT NULL
);
"""

# `claude --bg` prints `backgrounded · <id>` and mints its OWN session id
# (it IGNORES --session-id). We capture that id as the job id.
_BG_ID_RE = re.compile(r"([0-9a-f]{8}(?:-[0-9a-f]+)*)")


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


def _dispatch_argv() -> list[str]:
    """The launchd-auth risk boundary (Step 0). If a login-shell/env wrapper is
    needed, ONLY this function changes.

    `-p` is REQUIRED: without it `claude --bg` creates an idle session and never
    submits the prompt. `--session-id` is omitted — `--bg` mints its own. The
    prompt is NOT a trailing positional: `--allowedTools <tools...>` is variadic
    and would swallow it (leaving -p with no input → crash). The prompt goes via
    stdin instead (see dispatch), so --allowedTools stays the last token."""
    return [
        config.claude_bin(), "--bg", "-p",
        "--append-system-prompt-file", str(config.ORCHESTRATOR_PROMPT_FILE),
        "--mcp-config", str(config.PERISCOPE_MCP_CONFIG), "--strict-mcp-config",
        "--model", config.BG_COMMANDER_MODEL,
        "--allowedTools", config.BG_COMMANDER_ALLOWED_TOOLS,
    ]


def _dispatch_env(*, handle: str) -> dict[str, str]:
    """Inherit the process env + a per-command caller handle. channel_shim reads
    PERISCOPE_CALLER_ID (falling back to TMUX_PANE). The handle is a unique
    cmdr:<token> — its only jobs are to (a) trip is_commander's prefix check and
    (b) key _MCP_SESSIONS uniquely across concurrent commanders. It is NOT the
    claude session id (which isn't known until claude prints it post-spawn).

    The Anthropic API-key auth vars are STRIPPED: server.py load_dotenv()s
    ANTHROPIC_API_KEY into os.environ (for the narrator/rename SDK calls), and an
    inherited key takes precedence over the claude.ai subscription login — the
    commander must bill on the subscription, not API credits (a spend leak)."""
    env = {**os.environ, "PERISCOPE_CALLER_ID": f"cmdr:{handle}"}
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    return cast("dict[str, str]", env)


def _parse_session_id(stdout: str) -> str | None:
    """Pull claude's minted session id out of the `backgrounded · <id>` line."""
    for line in stdout.splitlines():
        if "backgrounded" in line:
            m = _BG_ID_RE.search(line)
            if m:
                return m.group(1)
    return None


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


def _set_status(job_id: str, status: str) -> None:
    with _conn() as c:
        c.execute("UPDATE commands SET status=? WHERE id=?", (status, job_id))


# --- dispatch ---

def dispatch(text: str, *, cwd: str | None = None) -> str:
    """Spawn `claude --bg -p <text>`, capture the session id it mints (it ignores
    --session-id), record the running job, and return the id. `claude --bg`
    backgrounds itself to the supervisor and returns in seconds, so this blocks
    only briefly. Raises RuntimeError if the spawn fails or prints no id."""
    cwd = cwd or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    handle = uuid.uuid4().hex   # cmdr handle (env, pre-spawn) — distinct from claude's id
    try:
        proc = subprocess.run(
            _dispatch_argv(),
            input=text,                 # prompt via stdin (not a positional --allowedTools would eat)
            cwd=cwd, env=_dispatch_env(handle=handle),
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("bg commander dispatch failed: %s", e)
        raise RuntimeError(f"bg commander dispatch failed: {e}") from e
    session_id = _parse_session_id(proc.stdout or "")
    if not session_id:
        log.warning("bg dispatch: no session id in output (stdout=%r stderr=%r)",
                    proc.stdout, proc.stderr)
        raise RuntimeError("bg commander dispatch produced no session id")
    insert_job(id=session_id, text=text, cwd=cwd, at=int(time.time()))
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
        # job.id is claude's SHORT session id; `claude agents` reports the full
        # uuid (short is its prefix) — match by prefix.
        state = next((st for sid, st in states.items() if sid.startswith(job.id)), None)
        present = state is not None
        new_status = map_state(state, started_at=job.started_at, now=now, present=present)
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

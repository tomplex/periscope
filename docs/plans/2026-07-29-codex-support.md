# Implementation plan — Codex support phases 1–5

**Status:** proposed
**Spec:** `docs/specs/codex-support.md`
**Target:** Codex CLI 0.146.0 initially, with defensive compatibility behavior

## Outcome

Periscope can identify, launch, resume, and monitor interactive Codex sessions
running in tmux. Codex panes participate in the existing
`working → idle → done` attention flow without gaining transcript mode,
Claude-only channels, Claude plan usage, or narrator behavior.

The implementation uses:

- one provider discriminator: `agent = "claude" | "codex" | ... | None`;
- Codex lifecycle hooks for authoritative pane/session identity;
- hook and observed rollout lifecycle edges for live state;
- process-tree liveness and current-TUI markers as guards/fallbacks;
- `unknown` as a real no-opinion result so missing evidence cannot create
  completion alerts.

## Delivery shape

Ship this as nine ordered changes. Each change has its own tests and preserves a
runnable app:

```text
0 evidence gate
    ↓
1 shared agent discriminator
    ↓
2 provider-aware persistence beside legacy Claude bindings
    ↓
3 Codex hook + installation/trust health
    ↓
4 process/TUI
    ↓
5 launch/resume/catalog
    ↓
6 state sources + reconciliation
           ↓
7 window_view + attention integration
           ↓
8 UI/docs/live rollout
```

Do not combine stages 1–3 into one patch. They are the highest-regression
surface and need independently reviewable compatibility boundaries.

Phase acceptance is cumulative. Early refactor commits are deployable and keep
the app working, but they do not individually satisfy the complete Codex
feature acceptance criteria.

## Explicit non-goals

- Codex transcript rendering or `/history` indexing
- `needs-input` until a stable source is verified
- Codex rate-limit/usage aggregation
- Claude channels parity or asynchronous push
- narrator, context reset, or AI auto-rename for Codex
- App Server ownership of the TUI/agent loop
- automatic model, profile, sandbox, or approval overrides
- implementing OpenCode support as a prerequisite

## Stage 0 — evidence gate

This stage writes fixtures and a short verification note. It does not change
runtime behavior.

### 0.1 Capture documented hook payloads

Create sanitized fixtures under:

```text
tests/fixtures/codex/0.146.0/
  session_start_startup.json
  session_start_resume.json
  user_prompt_submit.json
  stop.json
  session_end.json
```

Capture from a disposable Codex/tmux session with a temporary hook that writes
stdin to a temporary directory. Confirm:

- `TMUX_PANE` reaches each hook process;
- common fields include `session_id`, `transcript_path`, `cwd`,
  `hook_event_name`, and `model`;
- prompt/stop fields include `turn_id`;
- SessionStart source values for fresh and resume;
- a zero-exit hook with no stdout is accepted by `Stop`;
- the transcript path points at the same rollout whose `session_meta` has the
  hook session ID.
- whether hook payloads contain a documented timestamp and its precision;
- whether user-level SessionEnd is discovered;
- whether hooks are enabled without a feature flag;
- whether Stop continuation produces another UserPromptSubmit edge.

Also run a root session that spawns a subagent. Determine whether the configured
root hooks fire for subagent lifecycle, whether `TMUX_PANE` is inherited, and
whether payload fields distinguish the root from subagents. If a subagent event
is indistinguishable, the hook must validate `transcript_path` metadata as a
root `codex-tui` session before binding.

Do not commit real prompts, assistant messages, usernames, home paths, access
tokens, or full base instructions. Replace paths and IDs consistently so
cross-record relationships survive sanitization.

### 0.2 Capture rollout lifecycle cases

Add minimal JSONL fixtures containing only metadata and lifecycle records:

```text
normal.jsonl
interrupted.jsonl
failed-or-cancelled.jsonl
approval-roundtrip.jsonl
stop-continuation.jsonl
partial-final-line.jsonl
```

The first is already locally verified to contain singular `task_complete`.
The other cases are gates:

- if they emit an explicit completion record, add that exact observed event to
  the reducer contract;
- if they leave an unmatched start, require the newer Stop hook to repair it;
- if evidence cannot be ordered, the expected result is `unknown`.

Do not invent event aliases to make fixtures pass.

### 0.3 Verify process layout

From a disposable pane, record only the structural facts needed by tests:

```text
tmux pane_pid
PID/PPID chain
executable basenames
process start timestamps
pane_current_command
```

Cover both direct `codex` launch and the configured wrapper/path on this
machine. Convert these into synthetic `ps` fixtures rather than committing a
raw process listing.

Verify Darwin and Linux `ps` formats separately before claiming cross-platform
support; macOS is the required v1 platform.

### 0.4 Gate decision

Write `docs/notes/2026-07-29-codex-integration-evidence.md` with:

- verified hook and rollout shapes;
- unresolved lifecycle cases;
- exact CLI version;
- which state cases intentionally return unknown.

Stop implementation if any is false:

1. `TMUX_PANE` is not inherited by Codex hooks.
2. SessionStart does not provide a stable session ID/path for interactive TUI
   sessions.
3. Subagent safety is proven: either root-session hook definitions do not fire
   for subagents, or payload/transcript metadata provides a verified
   discriminator that prevents a subagent from overwriting the root binding.

If any gate fails, revise the spec toward App Server or an explicit launch
wrapper before touching production state code. Do not ship heuristic
hook-based binding when root and subagent events are indistinguishable.

The evidence note must also answer:

- exact accepted user-level `hooks.json` shape and timeout units;
- behavior of `CODEX_HOME=""`;
- whether resume SessionStart reports the requested UUID/path;
- whether hook trust is per entry or invalidated by file/command changes;
- interruption/failure/cancellation terminal edges.

### Stage 0 verification

```sh
python3 -m json.tool tests/fixtures/codex/0.146.0/session_start_startup.json
uv run pytest -q tests/test_codex_evidence.py
```

`test_codex_evidence.py` should assert fixture relationships and redactability,
not duplicate the eventual parser tests.

## Stage 1 — shared agent discriminator

Goal: replace the Claude-vs-shell boolean with agent-vs-shell identity while
preserving all current Claude behavior. Codex may be rendered from synthetic
test data, but no live detection or launch lands yet.

### 1.1 Backend parsing contract

Files:

```text
periscope/panes.py
periscope/window_view.py
periscope/routes/pane.py
periscope/routes/state.py
tests/test_panes.py
tests/test_window_view.py
tests/routes/test_pane.py
tests/routes/test_state.py
```

Changes:

- `parse_pane()` returns `agent: "claude" | None` instead of `is_claude`.
- Replace `_claude_last_seen`/`smooth_is_claude` with `_agent_last_seen` and
  `smooth_agent(pane_id, detected_agent)`.
- Key agent and spinner smoothing by stable `pane_id`, never
  `session:index`.
- Extract the duplicated normalization ladder from `window_view.py` and
  `routes/pane.py` into:

  ```python
  def smooth_parsed(*, pane_id: str, parsed: dict) -> dict:
      ...
  ```

- Positive detection of a different agent overwrites sticky identity.
- No agent forces `state="shell"`.
- The Claude session-status override sets `agent="claude"` as corroboration.
- Error dictionaries use `agent=None`.

Do not add a compatibility `is_claude` field.

### 1.2 Audit generic vs Claude-only backend branches

Use the inventory in `docs/specs/opencode-support.md`, then verify with:

```sh
rg -n "is_claude|smooth_is_claude|_claude_last_seen" periscope tests
```

Agent-generic:

- state transition/done refinement;
- rail/attention eligibility;
- model/context display when present.

Claude-only:

- `session_status.py`;
- narrator and context-reset checks;
- plan usage;
- transcript/turn resolution;
- memory warning;
- channel discovery/control.

Every Claude-only gate becomes `agent == "claude"`, not truthiness.

### 1.3 Frontend contract

Files:

```text
static/src/util.js
static/src/filter.js
static/src/chrome/FilterBar.jsx
static/src/split/Rail.jsx
static/src/split/RailRows.jsx
static/src/split/AttentionSections.jsx
static/src/split/Detail.jsx
static/src/store.js
static/src/**/__tests__/*
static/styles.css
```

Add:

```js
export function paneLabel(w) {
  return w?.name || w?.agent || "shell";
}

export const AGENT_META = {
  claude: { label: "Claude", glyph: "✻", className: "icon-claude" },
  codex: { label: "Codex", glyph: "◇", className: "icon-codex" },
};
```

Use `w.agent` truthiness only for truly generic presentation. Keep transcript,
channel, and Claude-specific controls on `w.agent === "claude"`.

Rename the transient `claude` filter to `agents`. No persisted preference
migration is needed.

### 1.4 Stage 1 tests

- Existing Claude pane fixtures still produce identical state/model/context.
- Shell fixtures remain shell.
- Smoothing is isolated by `pane_id`.
- Provider swap overwrites stickiness.
- `paneLabel(null)` returns shell safely.
- Agent filter includes a synthetic Codex row.
- Codex row has no Transcript toggle or channel controls.
- Claude usage/narrator/reset tests prove non-Claude panes are excluded.

Commands:

```sh
uv run pytest -q tests/test_panes.py tests/test_window_view.py \
  tests/routes/test_pane.py tests/routes/test_state.py \
  tests/test_usage.py tests/test_activity.py
npm test
npm run build
bin/check
```

Commit boundary:

```text
refactor: generalize Claude pane identity to agent
```

## Stage 2 — provider-aware session persistence

Goal: add provider-aware bindings without changing the legacy Claude table or
creating an unsafe old-hook rolling-upgrade window.

### 2.1 Separate table and stdlib-only helper

New/changed files:

```text
periscope/session_binding_db.py   # NEW, stdlib only
periscope/activity.py
tests/test_session_binding_db.py
tests/test_turns.py
tests/test_resurrect.py
```

Leave `pane_sessions(pane_id, session_id, updated_at)` unchanged and permanently
Claude-only. Add:

```sql
CREATE TABLE IF NOT EXISTS agent_sessions (
  pane_id      TEXT PRIMARY KEY,
  provider     TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  session_path TEXT,
  updated_at   INTEGER NOT NULL,
  evidence     TEXT
);

CREATE TABLE IF NOT EXISTS agent_session_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  pane_id      TEXT NOT NULL,
  provider     TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  turn_id      TEXT,
  event        TEXT NOT NULL,
  hook_version INTEGER NOT NULL,
  cli_version  TEXT,
  observed_at  INTEGER NOT NULL
);
```

`session_binding_db.py` owns only:

```python
@dataclass(frozen=True)
class AgentSessionBinding: ...

def ensure_schema(conn: sqlite3.Connection) -> None: ...
def get_binding(conn, pane_id: str) -> AgentSessionBinding | None: ...
def upsert_binding(conn, binding: AgentSessionBinding) -> None: ...
def delete_binding(conn, pane_id: str) -> None: ...
def append_hook_event(conn, event: AgentHookEvent) -> int: ...
```

It imports only stdlib, accepts caller-owned connections, performs idempotent
DDL, and never touches application-global connections. Both the FastAPI process
and standalone Codex hook use it.

`activity.get_pane_session()` and all current Claude consumers stay unchanged.
Add `activity.get_agent_session()` as a thin locked wrapper over the shared
helper. Codex UUIDs can never enter Claude transcript, narrator, diff-baseline,
or resurrect paths.

The existing Claude hook remains untouched and compatible across every deploy
order. A future new Claude hook may dual-write `agent_sessions`, but Codex v1
does not require it.

### 2.2 Bootstrap and import path

The standalone Codex hook will be invoked with an absolute checkout path and
may run from an arbitrary cwd. The installed command uses:

```text
python3 /absolute/checkout/codex_pane_session_hook.py
```

That script prepends its own directory to `sys.path` before importing the
stdlib-only helper. It must not import `periscope.activity`, FastAPI, or other
application modules.

### 2.3 Stage 2 tests

Test the shared helper against:

1. an empty DB;
2. a DB containing the exact legacy `pane_sessions` schema/data;
3. a DB containing a partial/previous `agent_sessions` creation if Stage 0
   establishes any migration need;
4. repeated schema initialization and upserts;
5. pane reuse across providers;
6. a locked DB with bounded timeout behavior at the hook layer.

Prove legacy `get_pane_session()` remains Claude-only and unchanged while
`get_agent_session()` returns Codex.

Commands:

```sh
uv run pytest -q tests/test_session_binding_db.py tests/test_turns.py \
  tests/test_resurrect.py tests/test_activity.py
bin/check
```

Commit boundary:

```text
feat: add provider-aware agent session bindings
```

## Stage 3 — Codex lifecycle hook and health

Goal: make documented Codex hooks the authoritative pane/session identity
producer and a candidate state-edge producer.

### 3.1 Hook executable

New file:

```text
codex_pane_session_hook.py
```

Keep it stdlib-only and import-safe. Structure:

```python
def parse_payload(stdin) -> HookEvent | None: ...
def record(event: HookEvent, *, pane_id: str, db_path: Path) -> None: ...
def main() -> None: ...
```

Rules:

- require `TMUX_PANE` beginning with `%`;
- accept only `SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd`;
- require non-empty `session_id`;
- reject or ignore subagent-originated payloads according to the Stage 0
  discriminator; if none exists, validate root transcript metadata before
  overwriting a binding;
- store `transcript_path` only when it is a string;
- never store prompt or assistant-message content;
- open SQLite with a short timeout;
- always exit zero;
- emit no stdout or stderr;
- swallow failures so the hook cannot block Codex.

The hook opens its own connection, calls
`session_binding_db.ensure_schema(conn)`, upserts `agent_sessions`, commits, and
closes. It works whether it runs before or after the new server and against a DB
that contains only the legacy Claude schema.

The hook appends its sanitized event metadata to `agent_session_events` from
the first release. Stage 3 uses it for per-event health; Stage 6 begins reducing
the same rows into working/idle. This avoids changing the installed hook command
or persistence format midway through rollout.

### 3.2 Install/uninstall

Files:

```text
bin/periscope
README.md
CLAUDE.md
tests/test_codex_hook_install.py
```

Split commands to retain control:

```text
install-hook          installs both Claude and Codex hooks
install-claude-hook   provider-specific helper
install-codex-hook    provider-specific helper
uninstall-hook        removes both Periscope hook definitions only
uninstall-claude-hook provider-specific helper
uninstall-codex-hook  provider-specific helper
```

Codex config target:

```text
$CODEX_HOME/hooks.json, default ~/.codex/hooks.json
```

Merge, do not replace. Register a dedicated Periscope-owned group under each
event rather than appending into another tool's matcher group. The exact shape
is confirmed in Stage 0 and is expected to be:

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "python3 \"/absolute/checkout/codex_pane_session_hook.py\"",
        "timeout": 5
      }]
    }]
  }
}
```

Repeat the owned group for UserPromptSubmit, Stop, and SessionEnd if Stage 0
confirms each at user level. Use exact normalized command identity as ownership;
use a description/marker only if the accepted schema preserves it. Preserve
unrelated top-level/event/group/hook fields.

Installer algorithm:

1. resolve the same `CODEX_HOME` rules used by the launcher/catalog;
2. refuse malformed JSON without writing;
3. parse and deep-copy unknown fields;
4. add/remove only the exact owned command entry/group;
5. write a sibling temporary file, fsync, preserve original mode, and
   `os.replace` atomically;
6. print the resolved config path and changed events.

Hook trust cannot be granted programmatically. After installation print:

```text
Codex hook installed. Open /hooks in Codex and trust the Periscope hook.
```

Do not pass `--dangerously-bypass-hook-trust`.

### 3.3 Hook health

Add a read-only backend field under `/api/healthz` or settings rather than every
window:

```json
{
  "codex_hook": {
    "codex_home": "/Users/example/.codex",
    "definition": {
      "SessionStart": {"present": true, "target_exists": true},
      "UserPromptSubmit": {"present": true, "target_exists": true},
      "Stop": {"present": true, "target_exists": true},
      "SessionEnd": {"present": true, "target_exists": true}
    },
    "hook_version": 1,
    "observed": {
      "SessionStart": {"last_seen_at": 1785382332, "cli_version": "0.146.0"}
    }
  }
}
```

Do not expose session IDs or pane IDs in normal health output. They may appear
in debug logs. The server can inspect definition presence and command target,
but "trusted" is not inferred from the file. Observation proves only that this
event ran in at least one session, not that the current file/version is trusted
for all sessions. Apply a named stale threshold.

UI wording:

```text
installed, not yet observed
observed
not installed
```

For panes launched with a different/unknown `CODEX_HOME`, health is unknown.
Do not claim "awaiting trust" unless Codex exposes a documented
machine-readable trust status.

### 3.4 Stage 3 tests

- Every fixture event binds the same pane/session correctly.
- Subagent fixtures cannot overwrite the root binding.
- Malformed input, missing pane, missing session, locked DB all exit zero.
- Empty/legacy DB bootstrap works in a real subprocess from an arbitrary cwd.
- Hook output is empty, including Stop.
- Install merge is idempotent.
- Malformed hook JSON is preserved and rejected.
- Atomic replacement preserves unrelated content and file mode.
- Uninstall preserves unrelated hooks.
- Alternate/empty `CODEX_HOME` resolution is correct.
- Health is per event/version and distinguishes definition presence, valid
  target, observation, and staleness.

Commands:

```sh
uv run pytest -q tests/test_codex_pane_session_hook.py \
  tests/test_codex_hook_install.py tests/routes/test_healthz.py
bin/check
```

Commit boundary:

```text
feat: bind Codex panes through lifecycle hooks
```

## Stage 4 — Codex process and TUI detection

Goal: identify a currently live Codex TUI independently from session binding.
Bindings say what ran; process/TUI evidence says what is running now.

### 4.1 tmux metadata

Files:

```text
periscope/panes.py
periscope/session_status.py or new periscope/agent_processes.py
tests/test_panes.py
tests/test_session_status.py or tests/test_agent_processes.py
```

Append—never insert—`#{pane_pid}` and `#{pane_current_command}` after the
existing `window_id` field in `list_windows()`. They become indexes 9 and 10;
indexes 0–8 retain their current meaning. Preserve positional parsing tests and
tolerate missing tail fields from mocked/older fixtures.

Prefer a new stdlib-only `agent_processes.py` if adding Codex to
`session_status.py` would make its Claude-only status-file responsibility
unclear.

One cached `ps` snapshot contains:

```python
ProcessInfo(pid, ppid, started_at, comm, argv)
```

For each pane:

1. begin at `pane_pid`;
2. walk descendants in the snapshot;
3. identify verified Codex executable basename/path;
4. retain process start time with PID;
5. return `None` on snapshot failure, not "not running."

Cache:

```text
(pane_id, pane_pid, pane_process_started_at, agent_pid, agent_started_at)
```

Invalidate when either pane PID/start pair changes. Define and fixture the
macOS `ps` command first; add Linux parsing only after direct verification.
Ignore zombie descendants.

`list-windows` describes only the active pane of each window. Background panes
in a manually split window remain outside the dashboard's window model; this is
an existing limitation, not expanded by Codex support.

Do not classify from arbitrary `argv` substrings such as a shell command
mentioning the word codex.

### 4.2 TUI detector

Add `periscope/panes_codex.py` only after Stage 0 captures current idle and
working terminal fixtures. It returns a narrow result:

```python
{
    "agent": "codex",
    "state_marker": "working" | "idle" | None,
    "model": str | None,
    "context_pct": int | None,
}
```

Requirements:

- match current bottom chrome, not arbitrary scrollback;
- require at least two Codex-specific signals for identity;
- treat model/context as opportunistic;
- return no state marker when the layout is ambiguous;
- do not implement `needs-input`.

Detection precedence:

```text
live descendant process identifies provider
current TUI corroborates/fills a brief process-snapshot gap
binding alone never proves current liveness
```

### 4.3 Stage 4 tests

- direct and wrapped Codex process trees;
- unrelated process whose argv mentions Codex;
- PID reuse/start-time mismatch;
- failed process snapshot produces no opinion;
- zombie descendant, pane replacement, and cached start-time invalidation;
- multi-pane window documents/tests the active-pane-only behavior;
- idle/working/current-shell captures;
- stale Codex chrome outside the detection tail remains shell;
- Claude detector wins on an overlapping synthetic capture;
- sticky agent clears after verified exit/current shell.

Commands:

```sh
uv run pytest -q tests/test_agent_processes.py tests/test_panes.py \
  tests/test_window_view.py
bin/check
```

Commit boundary:

```text
feat: detect live Codex tmux panes
```

## Stage 5 — typed launch, resume, and session catalog

Goal: add Codex to the existing new-pane/worktree paths without accepting raw
provider command strings as the semantic API.

### 5.1 Configuration and command construction

Files:

```text
periscope/config.py
periscope/routes/sessions.py
periscope/worktree_spawn.py
periscope/open_ops.py
tests/test_config.py
tests/routes/test_sessions.py
tests/test_worktree_spawn.py
tests/test_open_ops.py
```

Add:

```python
def codex_exec() -> str: ...

def build_agent_command(
    agent: Literal["claude", "codex"],
    *,
    cwd: str,
    resume_id: str | None = None,
) -> list[str]: ...
```

Codex argv:

```python
[CODEX_EXEC, "-C", cwd]
[CODEX_EXEC, "resume", resume_id, "-C", cwd]
```

Serialize for `tmux send-keys` with `shlex.join`. Do not add model, sandbox,
approval, profile, hook-trust-bypass, or `--no-alt-screen` flags.

Keep arbitrary `exec_cmd` only for explicit shell/custom commands. Known agent
launches use typed server-side construction.

### 5.2 Route contract

Extend the existing `/api/window/new` query/body contract:

```python
agent: Literal["claude", "codex"] = "claude"
```

Do not rewrite query parameters into JSON solely for this work. Preserve Claude
as the default for existing clients.

For `mode=resume` dispatch by provider:

- Claude uses current history lookup/liveness behavior.
- Codex uses the Codex session catalog and provider-aware live bindings.

The route returns `agent` and `resumed_session_id`.

### 5.3 Open/worktree propagation

`/api/window/new` is not the only launch path. Thread the selected provider
through the primary open flow:

```text
OpenOmnibox
  → POST /api/open
  → routes/open.py
  → open_ops.open_target / _open_path / ensure_session
  → worktree_spawn._layout_two_window
```

Also cover `routes/workspaces.py`, which calls `open_target`.

Changes:

- add `agent: Literal["claude", "codex"] = "claude"` to the open/workspace
  request models;
- pass it through every function above;
- generalize `_layout_two_window` to build the selected agent command;
- rename `claude_pid`/`claude_pane_id` result fields and local variables to
  `agent_pid`/`agent_pane_id` in backend and frontend together;
- preserve Claude as the default for existing requests.

Dedupe key is `(canonical cwd, agent)`: focus an existing pane only when both
match. If the cwd already has Claude and Codex is requested, create an
additional Codex pane in the same worktree/track rather than focusing Claude or
creating another worktree. PR/link stamping applies to the selected new agent
pane through the same pane annotation/activity mechanisms.

Workspace spawning exposes the same provider selector or defaults explicitly to
Claude when its UI has no selector; route tests pin that behavior.

### 5.4 Session catalog

Add catalog metadata to `periscope/codex_sessions.py`:

```python
@dataclass(frozen=True)
class CodexSessionMeta:
    session_id: str
    path: Path
    cwd: str
    started_at: datetime | None
    cli_version: str | None

def catalog() -> dict[str, CodexSessionMeta]: ...
```

Rules:

- resolve `$CODEX_HOME`, treating unset as `~/.codex`; define behavior for an
  empty string explicitly in tests;
- scan session metadata defensively and cache by path/mtime/size;
- accept UUIDs only in v1 even though CLI also accepts names;
- reject an unknown ID;
- reject an ID bound to another live Codex pane;
- use recorded cwd when it exists;
- if recorded cwd is gone, fall back to the requested track/repo cwd and report
  that fallback in the response/log;
- explicit resume writes `evidence="launch-pending"` with a short expiry before
  send-keys;
- a matching SessionStart hook plus live process promotes it to
  `resume-explicit`;
- synchronous delivery failure deletes it;
- asynchronous executable/resume failure leaves it non-authoritative and it is
  pruned at expiry;
- duplicate-resume rejection trusts only a promoted live binding.

No history DB or message parsing.

### 5.5 Launcher UI

Files:

```text
static/src/overlays/LauncherModal.jsx
static/src/overlays/OpenOmnibox.jsx
static/src/open/*
static/src/**/__tests__/*
```

Add a Claude/Codex selector only in flows that launch an agent. Hide
Claude-channel-specific copy/options for Codex. A Codex resume picker can use
the narrow catalog through a small new endpoint or existing open catalog;
supporting a pasted/known UUID is the minimum acceptance requirement.

Do not add persisted per-repo agent defaults in this stage.

### 5.6 Stage 5 tests

- exact argv and `shlex.join` behavior, including spaces and metacharacters;
- Claude default unchanged;
- invalid provider rejected;
- Codex new pane in repo and worktree;
- UUID resume and pre-binding;
- launch-pending promotion, async-failure expiry, and pruning;
- same UUID already live rejected;
- missing cwd fallback;
- no unsafe flags;
- launcher sends provider and hides Claude-only controls.
- `/api/open` threads Codex through `_layout_two_window`;
- open dedupe is per `(cwd, agent)`;
- workspace spawn default/selection is pinned;
- result payload uses agent-neutral pane fields.

Commands:

```sh
uv run pytest -q tests/test_config.py tests/routes/test_sessions.py \
  tests/test_worktree_spawn.py tests/test_open_ops.py tests/routes/test_open.py \
  tests/routes/test_workspaces.py \
  tests/test_codex_sessions.py
npm test
npm run build
bin/check
```

Commit boundary:

```text
feat: launch and resume Codex panes
```

## Stage 6 — hook state, rollout fallback, and reconciliation

Goal: derive `working | idle | unknown` without reading transcript messages.

### 6.1 State persistence

Files:

```text
periscope/activity.py
codex_pane_session_hook.py
periscope/codex_sessions.py
tests/test_codex_pane_session_hook.py
tests/test_codex_sessions.py
```

Use the append-only `agent_session_events` table created in Stage 2 and written
by the Stage 3 hook:

```sql
CREATE TABLE IF NOT EXISTS agent_session_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  pane_id      TEXT NOT NULL,
  provider     TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  turn_id      TEXT,
  event        TEXT NOT NULL,
  hook_version INTEGER NOT NULL,
  cli_version  TEXT,
  observed_at  INTEGER NOT NULL
);
```

Hook writes:

```text
UserPromptSubmit -> state=working, event=user-prompt-submit
Stop             -> state=idle-candidate, event=stop
SessionStart     -> binding/health only
SessionEnd       -> event=session-end, not immediate shell/dead proof
```

The SQLite `id` transactionally orders hook receipts, but is **not** treated as
a total clock against rollout appends. `observed_at` is diagnostic only unless
Stage 0 proves a documented shared timestamp with matching semantics.

Prune event rows with dead pane IDs during existing pane-session housekeeping.

### 6.2 Incremental rollout reader

`codex_sessions.py` owns a bounded cursor cache:

```python
RolloutCursor(
    inode,
    offset,
    partial_line,
    session_id,
    active_turn_id,
    last_event_at,
    last_edge,
    cli_version,
)
```

Rules:

- initial scan from byte zero;
- subsequent growth from offset;
- buffer incomplete final line;
- reset on inode change/truncation;
- ignore unknown records/fields;
- skip malformed complete lines with debug diagnostics;
- validate session ID;
- prune unbound/dead entries.

Concurrency/safety:

- guard the in-process cursor cache with its own lock;
- key files by canonical path plus `(device, inode)`;
- cap bytes read per poll, maximum JSONL line length, and total first-scan size;
- treat hook `transcript_path` as untrusted local data until it resolves beneath
  the expected `$CODEX_HOME/sessions` root and its `session_meta` matches the
  binding;
- do not follow a symlink/hard-link escape solely because a hook supplied it.

Observed reducer:

```text
task_started(turn_id)  -> working edge
task_complete(turn_id) -> idle edge
```

Add other event names only from Stage 0 fixtures.

### 6.3 Reconciliation

Implement as a pure function:

```python
def reconcile_codex_state(
    *,
    session_id: str,
    process: ProcessState,
    hook_edge: StateEdge | None,
    rollout_edge: StateEdge | None,
    tui_marker: StateEdge | None,
    now_ms: int,
) -> ReconciledState:
    ...
```

`ReconciledState.state` is `working | idle | unknown`.

Rules:

1. Definitive process exit returns no live agent opinion; process snapshot
   failure returns unknown.
2. Reject edges for a different session.
3. Order rollout records only by byte offset within one rollout.
4. Order hook records only by SQLite event ID.
5. Join sources by exact `(session_id, turn_id)`.
6. For one turn, any valid task completion or settled Stop closes its matching
   start/prompt; a same-turn Stop can repair an unmatched rollout start.
7. Do not compare rollout offsets with hook IDs or receipt wall clocks.
8. When different turns or cross-source conflicts cannot be causally ordered,
   return unknown.
9. Debounce Stop long enough for immediate continuation to record a new prompt;
   if the new prompt is a distinct turn whose order is established in the hook
   stream, it becomes working.
10. TUI markers corroborate or provide a fixture-backed fallback; generic
   terminal quiet never means idle.
11. No arbitrary long-turn expiry.

Keep settle duration as a named constant and cover it with a fake clock. Choose
its value from Stage 0 measurements, not intuition.

### 6.4 Stage 6 tests

Pure reconciliation table:

| Rollout | Hook | Process | Expected |
|---|---|---|---|
| same-turn working | same-turn settled Stop | live | idle |
| old rollout idle | hook prompt for causally newer turn | live | working |
| working | none | dead | no opinion |
| working | none | snapshot failed | unknown |
| mismatched session | idle | live | unknown |
| unorderable conflict | conflict | live | unknown |
| none | Stop inside settle | live | previous/unknown |
| none | Stop after settle | live | idle |
| none | Stop then continuation prompt | live | working |

Also test append efficiency, partial lines, truncation, unknown events, and
fixture-derived interrupt/failure cases; cache locking, symlink/root
validation, device/inode replacement, and read/line caps.

Commands:

```sh
uv run pytest -q tests/test_codex_sessions.py \
  tests/test_codex_pane_session_hook.py
bin/check
```

Commit boundary:

```text
feat: derive Codex working and idle state
```

## Stage 7 — window state and attention integration

Goal: connect reconciled Codex state to the existing UI state machine without
allowing unknown evidence to create edges.

### 7.1 `window_view`

Files:

```text
periscope/window_view.py
periscope/panes.py
periscope/routes/pane.py
periscope/routes/state.py
tests/test_window_view.py
tests/routes/test_pane.py
tests/routes/test_state.py
```

Structure:

```python
if agent == "claude":
    opinion = claude_session_opinion(...)
elif agent == "codex":
    opinion = codex_session_opinion(...)
else:
    opinion = None
```

For a valid Codex opinion:

```python
parsed["state"] = opinion.state  # working or idle
parsed["needs_input"] = False
parsed["asked_question"] = False
parsed["waiting_for"] = None
```

For unknown:

- do not call `record_state_transition`;
- do not update `completed_at`;
- preserve the last valid transition baseline internally, keyed by
  `(pane_id, provider, session_id, turn_id)`;
- render the last valid state for a named grace period, then expose
  `state="unknown"` in the API;
- never synthesize idle/done.

A later valid idle completes a prior working edge only when session and turn
continuity are proven. Otherwise it establishes a new idle baseline. Agent exit
to shell, pane identity change, provider change, or session change clears the
prior transition baseline.

Split current state handling into two explicit operations if necessary:

```python
record_observed_state_transition(...)  # only valid working/idle evidence
refine_attention_state(...)            # acked_at/done rendering
```

Rename Claude-specific comments/docstrings in generic helpers.

### 7.2 Cache invariant

Structured Codex state is applied outside the quiet terminal-capture cache.
Tests must prove:

- cached idle + appended start becomes working without recapture;
- working + appended completion becomes done;
- missing hook/rollout becomes unknown/no transition;
- process snapshot failure cannot create done.

Do not force terminal capture on every working Codex poll merely to source
state.

### 7.3 Notifications and attention

Codex valid `working → idle` uses existing:

- completed timestamp persistence;
- ack suppression;
- ready/done section;
- native completion notification;
- sorting.

Codex does not enter:

- needs-input attention;
- channel alerts;
- Claude narrator/reset/usage paths.

### 7.4 Stage 7 tests

- first observed idle establishes baseline without done;
- working → idle yields exactly one completion;
- working → unknown → idle yields completion only if the valid edge can still
  be established for the same session/turn, never on unknown;
- working turn A → unknown → idle turn B establishes baseline, not done;
- session change during unknown and pane reuse clear the old baseline;
- restart while unknown exposes unknown without replaying completion;
- restart baseline does not replay old done;
- same-cwd pane states remain independent;
- reused pane index cannot inherit state;
- Claude behavior remains byte-for-byte/API-shape compatible aside from
  `agent` replacing `is_claude`.

Commands:

```sh
uv run pytest -q tests/test_window_view.py tests/test_panes.py \
  tests/routes/test_state.py tests/routes/test_pane.py \
  tests/test_activity.py
npm test
npm run build
bin/check
```

Commit boundary:

```text
feat: surface Codex state in attention views
```

## Stage 8 — UI polish, documentation, and rollout

### 8.1 UI

Files:

```text
static/src/split/RailRows.jsx
static/src/split/AttentionSections.jsx
static/src/split/Detail.jsx
static/src/chrome/*
static/styles.css
static/src/**/__tests__/*
```

Verify:

- Codex glyph/name are distinct;
- working/idle/done classes reuse the existing palette;
- unknown is visually quiet and not placed in ready/running sections;
- detail defaults to terminal;
- no Transcript toggle;
- no channel composer/link prompts that depend on Claude channels;
- no Claude memory/usage labels;
- agent filter includes Codex.

Avoid a broad visual redesign.

### 8.2 Documentation

Update:

```text
README.md
CLAUDE.md
docs/specs/codex-support.md status
```

Document:

- Codex CLI minimum version established by Stage 0;
- hook installation and `/hooks` trust step;
- how to diagnose installed-but-unobserved hooks;
- Codex launch/resume;
- supported state set and explicit omissions;
- `$CODEX_HOME`;
- schema/rollout internals are defensive fallbacks.

### 8.3 Full verification

Automated:

```sh
uv run pytest -q
npm test
npm run build
bin/check
git diff --check
```

Live smoke matrix in a disposable tmux session:

| Case | Expected |
|---|---|
| fresh Codex, trusted hook | binds immediately |
| manual Codex, trusted hook | binds on SessionStart/prompt |
| resume UUID | pre-bound, correct cwd |
| two same-cwd sessions | independent UUID/state |
| normal turn | idle → working → done |
| interrupt | settles idle/unknown per captured contract, never stuck false done |
| failed/cancelled turn | fixture-defined safe result |
| Stop continuation | no intermediate done alert |
| hook installed but untrusted | visible health warning, safe fallback/unknown |
| process killed working | no persistent working |
| Periscope restart mid-turn | recovers working without replaying old done |
| exit Codex to shell | agent clears after verified exit |
| Claude pane | all preexisting features unchanged |

Watch one normal workday before marking the spec complete. Exit criteria:

- zero observed false completion alerts;
- every trusted-hook normal turn has both prompt/start and completion/Stop
  coverage;
- no bound live pane remains unknown for more than two normal state-poll
  intervals unless its evidence is explicitly contradictory;
- no session-correlation ambiguity assigns a UUID.

Log only state-source metadata, never prompt/message content. Specifically
inspect:

- correlation ambiguity/fallback count;
- hook observed/stale count;
- unknown-state frequency/duration;
- false completion reports;
- rollout parse errors by CLI version.

### 8.4 Rollback

Every database change is additive. Rollback procedure:

1. disable/remove Codex hooks through `bin/periscope uninstall-codex-hook`;
2. deploy the previous server/frontend;
3. leave added SQLite tables in place—they are ignored by old code;
4. do not drop or rewrite existing Claude bindings.

If the `agent` field has already shipped to the frontend, backend and built
frontend must roll back together because there is deliberately no
`is_claude` compatibility field.

Commit boundary:

```text
docs: document Codex support and hook setup
```

## Cross-stage invariants

These are review blockers throughout implementation:

1. No `from server import ...` inside `periscope/`.
2. One agent discriminator; no `is_codex` or restored `is_claude`.
3. `pane_id` is identity; `session:index` is presentation/targeting only.
4. Legacy `pane_sessions` and `get_pane_session()` remain unchanged and
   Claude-only; provider-aware code uses `agent_sessions`.
5. Unknown evidence never records an idle edge or completion.
6. Hook/config installers merge and preserve unrelated user configuration.
7. Hooks never log prompt/assistant content and never block Codex.
8. No model, approval, sandbox, profile, or trust bypass.
9. Rollout JSONL is an observed fallback, not a stable public contract.
10. Codex remains the owner of its TUI; Periscope observes and launches it.
11. Frontend source changes rebuild committed `static/dist/app.js`.
12. Tests never touch the real activity DB, `~/.codex`, or real worktree root.

## Expected new files

```text
codex_pane_session_hook.py
periscope/agent_processes.py
periscope/codex_sessions.py
periscope/panes_codex.py
periscope/session_binding_db.py
tests/test_agent_processes.py
tests/test_codex_evidence.py
tests/test_codex_hook_install.py
tests/test_codex_pane_session_hook.py
tests/test_codex_sessions.py
tests/fixtures/codex/0.146.0/*
docs/notes/2026-07-29-codex-integration-evidence.md
```

The exact module split may collapse `panes_codex.py` into `panes.py` if the
fixture-backed detector is only a few expressions. Do not collapse
`codex_sessions.py` into `history/`; transcript/history is out of scope.

## Definition of done

- All Stage 0 evidence gates are recorded.
- All nine stages and their focused tests pass.
- Full Python, frontend, build, lint, and type gates pass.
- A trusted hook binds fresh/manual/resumed Codex panes.
- Normal Codex turns reliably produce one working edge and one completion edge.
- Missing, stale, or contradictory evidence produces unknown—not false done.
- Same-cwd concurrent panes remain independent.
- Claude launch, resume, transcript, channels, usage, narrator, resurrect, and
  attention behavior remain intact.
- Documentation explains hook trust and known omissions.
- The one-workday observation gate has no unexplained false completions.

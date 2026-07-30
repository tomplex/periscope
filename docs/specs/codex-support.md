# Spec — Codex support, phases 1–5

**Status:** implemented behind an unresolved live-evidence gate

The implementation is intentionally conservative. Verified local evidence and
remaining rollout gates are recorded in
[`docs/notes/2026-07-30-codex-integration-evidence.md`](../notes/2026-07-30-codex-integration-evidence.md).
Hook-derived identity/state must not be considered authoritative until
`TMUX_PANE`, SessionStart identity, and root-vs-subagent attribution are
captured from disposable live sessions.

## Problem

Periscope treats Claude Code as the only agent it can identify, launch, resume,
and assign live state to. A Codex TUI running in tmux already benefits from the
terminal mirror, Git/PR metadata, tracks, diffs, and worktree management, but it
looks like a shell:

- no agent glyph or state coloring;
- no `working → idle → done` attention transition;
- no Codex option in the launcher;
- no reliable pane → Codex session identity;
- no structured status source.

The missing product behavior is not transcript rendering. The useful v1 is:
"show me every Codex pane, tell me whether it is working or idle, and let me
start or resume it from the same workflows as Claude."

## Relationship to the OpenCode spec

`docs/specs/opencode-support.md` already specifies the foundational
`is_claude: bool` → `agent: str | None` refactor. This spec composes with that
design:

```text
agent = "claude" | "codex" | "opencode" | None
```

There must be one discriminator, not `is_claude` plus `is_codex` booleans.
If Codex support lands first, its first phase performs the shared refactor from
the OpenCode spec. If OpenCode support lands first, Codex extends the existing
field and helpers. The second implementation must not repeat the migration or
introduce a parallel provider framework.

This spec intentionally does **not** introduce a large `AgentProvider` class
hierarchy. The current variation points are small functions with different data
sources, and a protocol with launch, history, messaging, usage, and status
methods would imply parity that does not exist. Use a provider registry only
where there is real common behavior:

```python
AGENT_SPECS = {
    "claude": AgentSpec(label="Claude", glyph="✻"),
    "codex": AgentSpec(label="Codex", glyph="◇"),
    "opencode": AgentSpec(label="OpenCode", glyph="▣"),
}
```

Launch commands and status readers remain provider-specific functions.

## Scope

The five phases in this spec are:

1. provider identity in the backend and UI;
2. Codex launch and resume;
3. reliable pane → Codex session correlation through supported lifecycle hooks;
4. narrow hook-backed state with a defensive rollout fallback;
5. structured working/idle state integrated with Periscope's existing
   done/attention behavior.

| Surface | v1 |
|---|---|
| Detected as an agent | yes |
| Agent glyph, label, filter, state coloring | yes |
| Launch new Codex pane | yes |
| Resume Codex session by UUID | yes |
| `working`, `idle`, derived `done` | yes |
| `needs-input` / approval detection | no |
| Codex transcript view | no |
| Codex sessions in `/history` search | no |
| Codex model/context chip | opportunistic, not a gate |
| Codex plan-usage aggregation | no |
| Claude channels equivalent | no |
| Narrator / auto-rename provider migration | no |

## Ground truth and compatibility boundary

This spec was checked against `codex-cli 0.146.0`.

The CLI exposes the required interactive commands:

```text
codex [PROMPT]
codex -C <DIR>
codex resume <SESSION_ID> [PROMPT]
codex resume <SESSION_ID> -C <DIR>
```

`--no-alt-screen` is available, but Periscope must not force it in v1. The
xterm/tmux mirror already handles alternate-screen applications, and changing
Codex's terminal behavior is a user-visible policy choice unrelated to support.
It may be exposed as a user preference later.

Codex writes rollout JSONL beneath:

```text
$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl
```

with `$CODEX_HOME` defaulting to `~/.codex`. In the observed schema:

```json
{"type":"session_meta","payload":{
  "session_id":"019f…",
  "cwd":"/Users/tom/dev/periscope",
  "originator":"codex-tui",
  "cli_version":"0.146.0",
  "source":"cli"
}}
{"type":"event_msg","payload":{
  "type":"task_started",
  "turn_id":"019f…",
  "started_at":1785382332
}}
{"type":"event_msg","payload":{
  "type":"task_complete",
  "turn_id":"019f…"
}}
```

The files and event shapes are local CLI implementation details, not a stable
public API. Every reader therefore:

- accepts unknown record types and unknown payload fields;
- returns `None` rather than guessing on malformed data;
- records the observed `cli_version`;
- keeps fixtures tagged with the CLI version that produced them;
- falls back to a conservative TUI/process result when structured state is
  unavailable.

The failure mode for schema drift is "Codex pane remains an agent but its state
is unknown," never a server error, false `working`, or false `idle`.

Codex also exposes a documented lifecycle-hook interface. Command hooks receive
`session_id`, `transcript_path`, `cwd`, `hook_event_name`, and `model`; turn
hooks include `turn_id`. The relevant events are:

```text
SessionStart       session identity (startup, resume, clear, compact)
UserPromptSubmit   working candidate (the prompt can still be blocked)
Stop               turn reaches the point where Codex would stop
SessionEnd         session closes normally or ages out
```

Hooks are the primary integration surface because their payload is documented.
Rollout JSONL is a fallback/recovery source only. User-level hooks require an
explicit trust review in Codex, so Periscope must make hook health visible and
must degrade safely when they are absent or untrusted.

Hook-based identity is gated on subagent safety: either these root hook
definitions do not fire for subagents, or a fixture-verified payload/transcript
field distinguishes root from subagent. If neither is true, hooks cannot be the
authoritative pane binding source and implementation stops for an App Server or
launch-wrapper redesign.

## Phase 1 — provider identity

### Backend contract

Replace `is_claude: bool` with:

```python
agent: Literal["claude", "codex", "opencode"] | None
```

The `/api/state` and `/api/pane` window payloads expose `agent`. `None` means a
regular shell. No compatibility `is_claude` field is retained: two fields can
disagree and would let old UI branches accidentally treat Codex as Claude.

Agent-generic behavior keys on `agent is not None`:

- rail styling and agent glyph;
- model/context chips when present;
- attention sections;
- `working → idle → done` transition tracking;
- labels and the agent filter.

Claude-only behavior keys on `agent == "claude"`:

- `sessions/<pid>.json`;
- transcript resolution through `pane_sessions`;
- Claude plan usage;
- narrator and context-reset checks;
- channel push/reply UI;
- Claude process memory warnings;
- inter-Claude discovery and control tools.

Codex-specific behavior keys on `agent == "codex"`:

- rollout session resolution;
- rollout status reading;
- Codex resume construction.

The complete Claude-only/agent-generic call-site inventory in
`opencode-support.md` remains authoritative. Codex adds no exception to it.

### Detection

Detection has three sources with explicit precedence:

1. a valid persisted pane binding created by Periscope (`provider=codex`);
2. the live pane process tree/current command;
3. a Codex TUI footer detector, only as a fallback.

A persisted binding is not sufficient by itself after the pane returns to a
shell. It identifies what Periscope launched, not what is currently alive.
Positive live detection must come from a matching process or current TUI.

Add `pane_pid` to tmux enumeration and collect PID, PPID, process start time,
executable, and argv in one cached `ps` snapshot. Walk descendants from each
pane PID; do not run `ps` once per pane. Match verified executable
basenames/wrappers, not arbitrary argument substrings:

```text
claude, codex, opencode
```

`pane_current_command` is a useful fast path but not the only source because
shell wrappers can remain the direct pane process. Pair PID with process start
time to avoid trusting a recycled PID. If the process snapshot fails, return no
opinion rather than declaring the agent dead.

TUI parsing is deliberately secondary. Its only v1 requirement is to preserve
agent identity during transient process lookup gaps; structured rollout events,
not pixels, provide state.

### Smoothing and cache identity

Adopt the OpenCode spec's `smooth_agent` and `smooth_parsed` extraction:

- key smoothing by stable `pane_id`, not `session:index`;
- a positive different-agent detection overwrites the sticky entry;
- disappearance of every agent process plus a visible shell prompt clears the
  sticky entry;
- `/api/state` and `/api/pane` use the same normalization function.

`_view_cache` remains keyed by `(target, pane_id)`.

### Frontend

Replace:

```js
w.is_claude ? "claude" : "shell"
```

with the shared:

```js
paneLabel(w) // w.name || w.agent || "shell"
```

Use a centralized glyph/label table. The Codex glyph must be visually distinct
from Claude and shell; exact artwork is a UI implementation choice, covered by
a snapshot test.

The current `claude` filter becomes `agents`. Provider-specific filters are out
of scope until mixed-agent volume makes them useful.

Transcript toggles and channel controls remain gated by
`agent === "claude"`. A Codex pane opens in terminal mode and does not advertise
an empty transcript.

### Phase 1 acceptance

- A hand-started `codex` TUI is identified as `agent: "codex"`.
- A Periscope-started Codex pane is identified after launch.
- Exiting Codex to a shell eventually returns `agent: null`, `state: "shell"`.
- Claude transcript/channel/usage behavior is unchanged.
- Claude, Codex, and shell rows render distinct labels/glyphs.
- If the OpenCode discriminator has already landed, Codex extends it without
  changing OpenCode behavior; implementing OpenCode is not a Codex prerequisite.

## Phase 2 — launch and resume

### Request contract

Extend the existing window creation request with:

```python
agent: Literal["claude", "codex"] = "claude"
```

Defaulting to Claude preserves existing clients. `exec_cmd` must no longer be
the primary agent-selection contract. The server constructs known agent
commands; arbitrary commands remain available only for explicit shell/custom
launches.

Example:

```json
{
  "mode": "new",
  "agent": "codex",
  "track_id": "/Users/tom/dev/periscope",
  "cwd": "/Users/tom/dev/periscope"
}
```

Resume:

```json
{
  "mode": "resume",
  "agent": "codex",
  "resume_id": "019fb027-5e13-74c3-9ed9-0c69e1914367"
}
```

If the existing endpoint uses query parameters for these fields, keep that
transport in the first patch; the important change is the typed semantic
contract, not a route rewrite.

### Command construction

Add:

```python
def codex_exec() -> str: ...
def build_codex_command(*, cwd: str, resume_id: str | None = None) -> list[str]: ...
```

The logical argv is:

```python
["codex", "-C", cwd]
["codex", "resume", resume_id, "-C", cwd]
```

Periscope currently sends a command string through `tmux send-keys`. Serialize
the argv with `shlex.join`; never concatenate `cwd`, session IDs, profiles, or
future prompts into shell text.

Do not set:

- `--dangerously-bypass-approvals-and-sandbox`;
- `--ask-for-approval never`;
- `--no-alt-screen`;
- a model override.

Codex should inherit the user's `~/.codex/config.toml` and normal security
policy. Periscope is a launcher, not a policy override.

### Launcher UI

Add an agent selector to the new-pane flow:

```text
Claude | Codex
```

Remembering a per-repo default agent is out of scope. The transient last-used
choice may live in the frontend if it does not change server prefs.

The launcher must not present Claude-only modes/options after Codex is selected
(development channels, Claude resume records, Claude model labels).

### Resume lookup

Codex resume by UUID does not require transcript indexing. Add a narrow session
catalog that scans `session_meta` records and returns:

```python
CodexSessionMeta(
    session_id: str,
    path: Path,
    cwd: str,
    started_at: datetime | None,
    cli_version: str | None,
)
```

The Codex resume picker may be added now or later. The backend contract must
support a known UUID immediately; omnibox discovery can consume the catalog
without involving `history/`.

Before resume:

- reject an unknown UUID;
- use the recorded `cwd`, falling back only when it no longer exists;
- reject a session currently bound to a live pane;
- do not use the Claude JSONL `mtime < 60s` liveness heuristic.

Codex owns concurrency semantics for external/manual resumes, but Periscope must
prevent a duplicate it can identify among its own panes.

### Phase 2 acceptance

- Launcher creates a Codex TUI in the requested track/cwd.
- Resume executes `codex resume <UUID> -C <recorded cwd>`.
- Paths containing spaces are shell-safe.
- Codex inherits the user's normal config, approvals, sandbox, and model.
- Existing Claude launch/resume behavior is unchanged.

## Phase 3 — pane → Codex session correlation

### Why cwd and newest-mtime are rejected

Several Codex panes can share one repository. "Newest rollout in this cwd"
would assign the same session to multiple panes and can invert working/idle
state. That is the exact identity failure `pane_session_hook.py` fixed for
Claude. Cwd is a candidate filter, never identity.

The UUID is not known synchronously to the launcher for a fresh session, but
Codex's documented `SessionStart` hook supplies it directly. Do not recreate
identity from filesystem timing when an authoritative lifecycle payload exists.

### Binding model and backward compatibility

Do **not** alter the existing `pane_sessions` table. Its live schema is exactly
`(pane_id, session_id, updated_at)`, and an older installed Claude hook can only
write those fields. Adding a provider column creates an unsafe rolling-upgrade
case: an old hook can replace the UUID on a Codex-labeled row without resetting
its provider.

Keep `pane_sessions` permanently Claude-only and add a separate table:

```sql
agent_sessions(
    pane_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    session_id TEXT NOT NULL,
    session_path TEXT,
    updated_at INTEGER NOT NULL,
    evidence TEXT
)
```

`activity.get_pane_session(pane_id) -> str | None` and all current Claude
consumers (`turns`, narrator, diff scope, resurrect) remain unchanged and can
never receive a Codex UUID. Add `get_agent_session()` for provider dispatch.

The new Claude hook may optionally dual-write an `agent_sessions` Claude row for
generic diagnostics, but Codex support does not depend on that. An old Claude
hook remains safe because it touches only the legacy Claude table. Agent
detection/process validation decides whether a stale Codex row is currently
usable; a later Codex hook overwrites it on pane reuse.

Put the `agent_sessions` DDL/upsert in a stdlib-only helper shared by the server
and out-of-process Codex hook. The hook can therefore bootstrap an empty or
old-server database without importing FastAPI/application modules.

`evidence` is one of:

```text
codex-hook
resume-explicit
rollout-fallback
claude-hook
```

It exists for diagnostics and source precedence.

### Codex hook

Add `codex_pane_session_hook.py`, a best-effort stdlib-only command hook modeled
on `pane_session_hook.py`. It reads JSON from stdin and `TMUX_PANE` from its
environment, then performs a short SQLite upsert:

```text
pane_id       = $TMUX_PANE
provider      = codex
session_id    = payload.session_id
session_path  = payload.transcript_path
updated_at    = now
evidence      = codex-hook
```

It always exits zero and produces no stdout. For `Stop`, where stdout must be
valid JSON if present, silence is also the safest behavior.

Configure it for:

```text
SessionStart       authoritative initial/restart/resume/clear/compact binding
UserPromptSubmit   repairs panes that predate installation or missed startup
Stop               state edge and another binding repair
SessionEnd         optional cleanup/diagnostic edge, not immediate liveness
```

`SessionStart` sources include `startup`, `resume`, `clear`, and `compact`.
The same UUID normally survives compaction, but the hook always trusts the
current payload rather than assuming that.

Installation follows the existing `bin/periscope install-hook` pattern but must
merge into `~/.codex/hooks.json` or the user-level `hooks` table without
overwriting unrelated hooks. Non-managed hooks require the user to review and
trust their exact definitions in Codex. Periscope reports:

```text
not installed | awaiting trust | observed | stale
```

where only `observed` is proven by receipt of a hook event. Installation alone
does not claim that Codex executed it.

### Explicit resume correlation

Resume is deterministic:

1. validate the session catalog entry;
2. create the tmux pane;
3. persist `(pane_id, "codex", resume_id, path)` before sending the command;
4. launch `codex resume`;
5. remove the binding if launch fails synchronously.

This is stronger than fresh-launch correlation and requires no watcher.

### Fallback correlation

Hooks are user-configurable and may be missing, disabled, untrusted, or blocked
by managed policy. For an unbound live Codex pane, fallback correlation may
inspect rollout `session_meta`, process start time, cwd, and creation time, but
binds only on one unique candidate. It never serializes launches and never
guesses among same-cwd candidates. Its binding uses
`evidence="rollout-fallback"` and is overwritten by the next valid hook.

This fallback is recovery, not the primary design or a v1 correctness
dependency. A manually started Codex with no trusted hook may remain a
first-class agent with unknown state.

### Binding validation

On every state poll, a binding is trusted only when:

- the `pane_id` still exists;
- the pane still has a live Codex process;
- the provider is Codex;
- when a transcript path is present and readable, its `session_meta.session_id`
  matches the binding.

A completed turn does not invalidate a binding: an idle Codex TUI remains
attached to its session. Exiting Codex clears or ignores the binding. Historical
rows may remain for diagnostics, but resolvers must not return them for a shell.

### Phase 3 acceptance

- Two Codex panes in the same cwd bind to different UUIDs.
- A resumed pane binds before its first state poll.
- A trusted `SessionStart` hook binds fresh and manually started panes without
  timing correlation.
- `UserPromptSubmit` repairs a missing binding.
- Missing/untrusted hooks are visible and degrade to no binding, not a guess.
- Ambiguous fallback matches produce no binding.
- Moving/renumbering tmux windows preserves identity via `pane_id`.
- Closing and reusing `session:index` cannot inherit another pane's session.
- Existing three-column Claude bindings and old/new Claude hook writers remain
  untouched and safe alongside the new `agent_sessions` table.
- The Codex hook can bootstrap `agent_sessions` before the server starts.

## Phase 4 — narrow state sources

### Module boundary

Add `periscope/codex_sessions.py` as a stdlib-only leaf, analogous to
`session_status.py`, plus provider-aware persistence helpers in `activity.py`.
It does not import FastAPI, UI assembly, Claude history code, or `server`.

Public API:

```python
def codex_home() -> Path: ...
def session_meta(path: Path) -> CodexSessionMeta | None: ...
def catalog() -> dict[str, CodexSessionMeta]: ...
def rollout_state_for(path: Path, session_id: str) -> CodexSessionState | None: ...
def hook_state_for(pane_id: str, session_id: str) -> CodexSessionState | None: ...
def state_for(pane_id: str, binding: AgentSessionBinding) -> CodexSessionState | None: ...
```

`CodexSessionState` is deliberately narrow:

```python
class CodexSessionState(TypedDict):
    session_id: str
    state: Literal["working", "idle", "unknown"]
    active_turn_id: str | None
    last_event_at: int
    source: Literal["rollout", "hook", "tui"]
    model: str | None
    context_pct: int | None
    cli_version: str | None
```

There is no messages/turns array.

### Hook state

Add a small provider-neutral append-only event table rather than overloading
`pane_status` (which is narrator-owned):

```sql
CREATE TABLE IF NOT EXISTS agent_session_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  pane_id      TEXT NOT NULL,
  provider     TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  turn_id      TEXT,
  event        TEXT NOT NULL,
  observed_at  INTEGER NOT NULL
);
```

The Codex hook writes:

```text
UserPromptSubmit -> working candidate, turn_id from payload
Stop             -> idle candidate, same turn_id
SessionStart     -> identity only; no invented idle edge
SessionEnd       -> session-ended diagnostic; process detection owns liveness
```

These are supported lifecycle events, but hook execution has two subtleties:

- another `UserPromptSubmit` hook can block the prompt after Periscope's hook
  runs, so the working edge is evidence rather than proof that a model turn
  started;
- matching hooks run concurrently, and another `Stop` hook can continue the
  turn, so an idle edge may be followed immediately by a new prompt.

Therefore hook state is reconciled causally with rollout/TUI evidence and idle
is debounced for a short settle window. A continuation prompt ordered after
Stop in the hook event stream cancels pending idle. The settle duration is a
measured implementation constant covered by continuation-hook tests, not an
arbitrary long-turn timeout.

### Incremental reading

State polling happens every few seconds across many panes. Never reread a large
rollout from byte zero on every `/api/state`.

Cache per path:

```python
RolloutCursor(
    inode,
    offset,
    partial_line,
    session_id,
    active_turn_id,
    last_event_at,
    model,
    context_pct,
)
```

Rules:

- unchanged `(inode, size, mtime_ns)` returns the cached state;
- growth reads from `offset`;
- truncation or inode change resets and rereads;
- an incomplete final JSON line is buffered until the next read;
- malformed complete lines are skipped and counted/logged at debug level;
- cache entries are bounded and pruned for sessions no longer bound to panes.

The first read may scan the whole file for correctness. An optional optimization
can seed metadata from the first line and scan a bounded tail only after tests
prove it cannot miss an unmatched `task_started`.

### Event reduction

The observed rollout lifecycle reducer recognizes:

```text
task_started(turn_id=X)  -> active_turn_id = X, state = working
task_complete(turn_id=X) -> clear X, state = idle
```

These exact names are verified in local `codex-cli 0.146.0` rollouts, but are
undocumented implementation details—not a complete lifecycle contract.
Completion aliases observed in future fixtures may be added explicitly. Do not
infer completion from:

- `agent_message`;
- token-count events;
- quiet file mtime;
- absence of tool calls;
- terminal activity stopping.

Turn IDs are required when present:

- completion for the active turn clears it;
- completion for a different/stale turn does not;
- a new `task_started` replaces the prior active turn and logs the anomaly;
- duplicate start/complete records are idempotent.

Before any lifecycle event, rollout state is `unknown`, not idle. A valid
interactive session is idle only after a matched completion, a settled `Stop`
hook, or a fixture-backed current composer marker.

`thread_settings_applied` or equivalent observed metadata may update `model`.
`token_count.info` may update `context_pct` only when both total/context values
have unambiguous semantics. Neither is required for v1 acceptance.

Interrupted, failed, cancelled, approval-blocked, and continuation turns require
captured fixtures and live verification. The official App Server uses
`turn/started` and `turn/completed`, with completed/interrupted/failed status,
but Periscope is not an App Server client in this phase. Do not silently assume
those public event names apply to rollout JSONL.

### Source reconciliation, freshness, and crash semantics

An unmatched `task_started` can survive a killed Codex process. Structured state
is therefore conditioned on process liveness. Reconcile evidence causally by
`(session_id, turn_id)`; there is no reliable total clock across hook processes
and rollout appends:

```text
same turn has task_complete or settled Stop             -> idle
same turn has start/prompt and no completion             -> working
rollout records within one file                          -> byte-offset order
hook records                                              -> SQLite event id order
different-turn or cross-source order cannot be proven    -> unknown/no opinion
fixture-backed current TUI marker                        -> corroboration/fallback
no live Codex process                                    -> no Codex state opinion
```

A `Stop` for the **same turn ID** repairs an unmatched rollout start. A prompt
for a different turn does not automatically beat an older completion unless
one source establishes their order. When Stage 0 fixtures provide a documented
shared timestamp or another causal field, it may be used; receipt wall clocks
alone are insufficient.

Do not add an arbitrary "working expires after N minutes" timeout. Long Codex
turns are legitimate. Process exit clears stale working but does not prove that
a still-live TUI with an unmatched start is genuinely still working.

Failure to obtain a process snapshot is `unknown`, not proof that the process
exited.

### Phase 4 acceptance

- Fixtures reduce unknown → working → idle correctly.
- Hook fixtures cover SessionStart, UserPromptSubmit, Stop, and SessionEnd.
- Blocked prompts and Stop-hook continuation do not create lasting false state.
- A partial final line does not corrupt state.
- Duplicate and out-of-order unrelated completions do not flicker state.
- File growth reads only appended bytes after the first scan.
- A dead Codex process cannot remain `working`.
- Interrupt, failure, cancellation, approval, and continuation behavior is
  either fixture-backed or explicitly returns unknown.
- Unknown records and new fields do not break `/api/state`.

## Phase 5 — live state integration

This phase is intentionally about working/idle, not transcript mode or history
search.

### State source precedence

For a detected Codex pane:

1. a valid pane/session binding plus reconciled rollout/hook lifecycle state;
2. a conservative current-TUI state marker, if separately fixture-backed;
3. `unknown`/no state opinion when evidence is absent, stale, or contradictory;
4. `shell` when no agent process/TUI remains.

The core rule is:

> Missing evidence never creates an edge.

Periscope uses `working → idle` to create a `done` alert. Both a guessed working
state and a guessed idle state can therefore create attention noise. Fallback
parsers may change state only from current, specific markers, never generic
spinner glyphs, quiet output, or historical scrollback.

### `window_view` integration

Refactor the current Claude override into explicit provider branches:

```python
agent = parsed.get("agent")

if agent == "claude":
    apply_claude_session_status(...)
elif agent == "codex":
    apply_codex_session_status(...)
```

Both produce the common `parsed["state"]` when they have a valid opinion. Codex
sets:

```python
parsed["state"] = "working" | "idle"  # only with valid evidence
parsed["needs_input"] = False
parsed["asked_question"] = False
parsed["waiting_for"] = None
```

An `unknown` result does not overwrite the last known state and does not call
`record_state_transition`. A short grace may preserve the prior rendered state;
after the grace, the UI may render unknown, but it still must not synthesize
idle or done.

For valid working/idle evidence, the existing provider-neutral code runs:

```text
record_state_transition
completed_at persistence
acked_at suppression
idle → done refinement
attention sorting
native completion notification
```

`record_state_transition` should be renamed/documented as agent-generic if its
docstring still says Claude.

### Cache behavior

The current quiet-pane cache skips terminal capture when tmux activity is
unchanged, but structured status is applied outside that cache. Preserve that
invariant:

- rollout state is checked every poll even when terminal capture is skipped;
- a newly appended `task_started` promotes cached idle to working;
- a newly appended `task_complete` transitions working to idle/done;
- a missing hook/binding/process snapshot produces no transition;
- working panes need not force terminal recapture merely to learn state.

This makes Codex state cheaper and less fragile than TUI scraping.

### Done and acknowledgment

Codex uses exactly the current done semantics:

```text
working → idle
  => completed_at = now
  => state = done until acknowledged/focused under existing rules
```

Server restarts can lose the in-memory previous state. Do not attempt to
reconstruct a historical done edge by replaying the entire rollout on boot in
v1; that risks resurfacing old completions. After restart, the first observed
state establishes the baseline, and only a subsequent live transition creates
`done`.

### UI behavior

A Codex row:

- receives working/idle/done color and ordering;
- appears in agent attention sections;
- displays `"working"` when no richer spinner/activity phrase exists;
- opens the terminal detail mode;
- does not show `needs-input` treatment;
- does not show a Transcript toggle;
- does not show Claude channel controls;
- does not contribute to Claude plan usage.

### Phase 5 acceptance

End-to-end with a real Codex TUI:

1. Open a Codex pane at its composer: the rail shows Codex + idle.
2. Submit a task: within one state-poll interval it shows working.
3. Let the task finish: it shows done and enters the existing completion
   attention section.
4. Acknowledge/focus it: it settles to idle under existing behavior.
5. Start two same-cwd Codex panes and overlap their turns: their states remain
   independent.
6. Kill a working Codex process: it does not remain working.
7. Restart Periscope while Codex is working: it shows working after binding
   recovery, but does not synthesize an old done alert.
8. Disable or untrust the hook and temporarily hide the TUI marker: the pane
   becomes unknown or retains its grace state without producing done.

## Data-flow summary

```text
tmux pane
  ├─ stable pane_id + process tree ───────────────> agent = codex
  ├─ explicit resume ─────────────────────────────> immediate pane/session binding
  └─ live terminal bytes ─────────────────────────> fallback identity/state only

Codex lifecycle hook + inherited TMUX_PANE
  ├─ SessionStart ────────────────────────────────> authoritative pane/session binding
  ├─ UserPromptSubmit ────────────────────────────> working candidate
  └─ Stop ────────────────────────────────────────> debounced idle candidate

$CODEX_HOME/sessions/.../rollout-*.jsonl
  ├─ session_meta ────────────────────────────────> fallback UUID/path validation
  └─ task_started / task_complete ────────────────> observed state corroboration

window_view
  └─ normalized agent + state ────────────────────> done/attention + UI
```

## Persistence and migration

State migration must be additive and idempotent:

- add the separate provider-aware `agent_sessions` table;
- keep existing Claude `pane_sessions` rows/schema unchanged;
- existing window annotations and `@periscope_id` values are unchanged;
- no rollout contents are copied into Periscope's DB;
- no history schema migration is required for phases 1–5.

Persist provider/session identity and the latest hook edge. Working/idle remains
a reconciled live read model conditioned on process liveness; the hook row alone
is never sufficient after restart or process exit.

## Testing plan

### Python

`tests/test_codex_sessions.py`

- metadata parsing from a version-tagged fixture;
- SessionStart/UserPromptSubmit/Stop/SessionEnd hook payload reduction;
- idle, task start, task complete;
- interrupted, failed, cancelled, approval-blocked, and continued turns;
- mismatched turn completion;
- duplicate lifecycle events;
- partial line and malformed line;
- append cursor and truncation/inode reset;
- unknown event compatibility;
- dead-process stale-working suppression;
- alternate `CODEX_HOME`.

`tests/test_codex_pane_session_hook.py`

- hook stdin + inherited `TMUX_PANE` binding;
- silence/zero exit on valid and malformed input;
- provider reset on pane reuse;
- bootstrap of provider-aware tables beside the exact legacy three-column
  `pane_sessions` schema;
- old Claude hook and the new Codex hook can write their separate tables in the
  same DB.

`tests/test_window_view.py`

- Codex structured idle overrides ambiguous TUI state;
- task start promotes a cached quiet pane;
- task complete stamps `completed_at` and yields `done`;
- Codex never enters Claude status, transcript, usage, memory, or channel paths;
- unbound Codex remains an agent with conservative state.

`tests/test_panes.py`

- Codex process/TUI detection fixtures;
- shell after Codex exit;
- sticky identity keyed by `pane_id`;
- dialect precedence where captured text overlaps.

`tests/routes/test_sessions.py`

- typed Codex launch;
- shell-safe cwd;
- explicit resume binding;
- unknown/already-live resume rejection;
- Claude remains the default.

Hook/correlation tests use a temporary DB, temporary hook config, temporary
rollout directories, and fake clock/process snapshots. They must not read or
modify the developer's real `~/.codex`.

### Frontend

- agent label/glyph for Claude, Codex, and shell (plus OpenCode only if its
  discriminator has landed);
- `agents` filter includes Codex;
- Codex working/idle/done render classes;
- Codex detail defaults to terminal and has no transcript toggle;
- Claude-only controls remain absent for Codex.

### Live smoke test

Use a disposable tmux session and a harmless prompt. Confirm:

- fresh launch binding;
- resume binding;
- missing, untrusted, then trusted hook behavior;
- working/idle latency;
- same-cwd isolation;
- interrupted/failed turn behavior;
- continuation-hook behavior;
- process-kill behavior.

Do not automate a real model invocation in the normal test suite.

## Observability

Add debug diagnostics sufficient to answer "why is this Codex pane idle?":

```text
pane_id
detected agent and evidence
bound session UUID/path and binding evidence
rollout cursor offset/mtime
last lifecycle event and turn ID
live process present
final normalized state and source
```

Expose these through existing debug logging or a development-only diagnostic
function, not the normal `/api/state` payload.

Track counters for correlation timeout, ambiguity, malformed rollout records,
binding validation failure, and hook freshness/trust failure. Do not log prompt
or message contents.

## Non-goals

- Rendering Codex transcripts or adding Codex to `/history`
- Parsing tool calls, reasoning, assistant messages, or subagents
- Detecting `needs-input` until a stable structured source or captured fixture
  exists
- Sending asynchronous structured messages into Codex
- Codex usage/rate-limit aggregation
- Choosing or overriding Codex models, profiles, sandbox, or approval policy
- Running Codex through app-server instead of its TUI
- Replacing tmux or owning the agent loop
- Auto-renaming Codex sessions with an OpenAI API call

## Future extensions

The narrow reader deliberately leaves clean extension points:

- approval/`needs-input` events, if Codex exposes a stable source;
- transcript/history extraction as a separate rollout consumer;
- model/context chips from structured events;
- App Server events as an optional stronger state stream;
- provider-neutral messaging capabilities rather than assumed parity;
- per-repository preferred agent.

None requires changing the v1 `agent`, binding, or normalized-state contracts.

## Implementation sequence

Keep each mergeable step behaviorally complete:

1. Land the shared `agent` discriminator and frontend gates with Claude-only
   behavior unchanged.
2. Add Codex detection and identity rendering.
3. Add typed launch/resume plus deterministic resume bindings.
4. Add the backward-compatible binding table, Codex lifecycle hook,
   install/trust diagnostics, and session metadata catalog.
5. Add the hook state reducer and incremental rollout fallback.
6. Wire reconciled state through `window_view` and the existing done/attention
   state machine.
7. Run mixed-agent regression and live smoke tests.

Do not combine transcript/history work into this sequence.

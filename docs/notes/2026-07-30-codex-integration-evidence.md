# Codex integration evidence — 2026-07-30

This note records only evidence reproduced locally without changing the real
Codex configuration or launching a paid/model turn. The target CLI is
`codex-cli 0.146.0`.

## Gate decision

**The hook-based implementation is blocked.** Three required facts remain
unverified:

1. that `TMUX_PANE` is inherited by every relevant hook process;
2. that interactive `SessionStart` supplies a stable session ID and transcript
   path; and
3. that root-session hook events can be kept distinct from subagent events.

The repository did not have `~/.codex/hooks.json`. Verifying these claims
requires installing a temporary hook and exercising fresh, resumed, and
subagent interactive turns. That would mutate the user's Codex configuration
and launch model turns, so it was intentionally not done here. Hook payload
fixtures are absent rather than fabricated.

## Verified locally

- `codex --version` reports `codex-cli 0.146.0`.
- `codex features list` reports `hooks` as `stable` and enabled. This verifies
  the installed feature state, but not hook discovery or payload behavior.
- Root rollout metadata uses `originator: "codex-tui"` and
  `thread_source: "cli"`.
- A spawned subagent rollout uses `thread_source: "subagent"`, a structured
  `source.subagent.thread_spawn`, distinct `id`, and root references in
  `session_id`, `forked_from_id`, and `parent_thread_id`. This is a verified
  rollout discriminator; it does not prove which hooks fire for subagents.
- A normal root turn contains `event_msg` records with singular
  `task_started` and `task_complete` values and the same `turn_id`.
- The observed records carry RFC 3339 timestamps with millisecond precision.
  Lifecycle payloads also carry integer `started_at`/`completed_at` values.
- On macOS, a directly launched pane reported `pane_current_command=codex`.
  Its observed tree included `codex` and a descendant
  `codex-code-mode-host`; Darwin `ps -axo pid,ppid,lstart,comm` supplies the
  required PID, PPID, start time, and command fields.
- `codex resume --help` accepts a UUID or session name and supports `-C`.
- The CLI exposes `--dangerously-bypass-hook-trust`, proving that hook trust
  exists. It does not establish trust granularity or invalidation behavior.

Sanitized versions of these records live in
`tests/fixtures/codex/0.146.0/`. The partial-final-line fixture is derived from
the verified record shape solely to preserve the reader boundary condition; it
does not claim that a crash was observed.

## Unresolved manual evidence

The exact accepted user-level `hooks.json` shape and timeout units are
unverified. So are hook payload fields, `TMUX_PANE` inheritance, whether
user-level `SessionEnd` is discovered, whether a zero-output successful Stop
hook is accepted, Stop continuation ordering, and trust behavior after a file
or command changes.

Fresh and resumed `SessionStart` behavior remains unverified, including source
values and whether resume reports the requested UUID/path. `CODEX_HOME=""`
behavior is also unverified.

No controlled lifecycle captures exist yet for interruption,
failure/cancellation, approval round trips, or Stop continuation. Those cases
must return `unknown`; no aliases should be inferred. Linux process formatting
and wrapper/path launch layouts also remain unsupported until captured.

## Required manual capture

Use a disposable Codex home or a backed-up, atomically restored hooks file.
Capture hook stdin and selected environment fields into a temporary directory,
then exercise fresh, resume, prompt, normal Stop, continued Stop, SessionEnd,
and a root turn spawning a subagent. Sanitize IDs and paths consistently before
adding fixtures. Do not implement authoritative hook binding until the three
hard gates above pass.

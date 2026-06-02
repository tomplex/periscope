# Prompt: replace periscope's TUI scraping with authoritative data sources

Paste this into a fresh periscope session (ideally a dev worktree on port 8766).

---

Periscope currently infers each Claude pane's **state** by scraping the tmux TUI:
`capture-pane` + a wall of regexes in `periscope/panes.py` (`parse_pane` and its
`_detect_*` helpers) + spinner/`is_claude` hysteresis. This is fragile — it
breaks every time Claude Code tweaks its TUI, and it can't see things the TUI
never renders to a stable line (e.g. a pending `AskUserQuestion` blanks the
status line).

We've since confirmed (measured, see `docs/transcript-view-todos.md` §2 and the
journal) that Claude Code writes **authoritative state to disk** that periscope
does not read today:

- **`~/.claude/sessions/<pid>.json`** — live, event-driven session status.
  Fields: `status` ∈ {`busy`, `waiting`, `idle`, `shell`}, `waitingFor`
  (best-effort string: `"approve AskUserQuestion"`, `"dialog open"`,
  `"permission prompt"` — varies, key off `status`), plus `sessionId`, `cwd`,
  `pid`, `name`, `agent`. `updatedAt` is the last *transition* (not a heartbeat).
- **`~/.claude/tasks/<sessionId>/<n>.json`** — live todo list. One file per task:
  `{id, subject, description, activeForm, status: pending|in_progress|completed,
  blocks, blockedBy}`.
- **The session JSONL** (already parsed by `history.search.messages_from_jsonl`
  and resolved per-pane by `periscope/turns.py`).

## Goal

Replace TUI-scraped *state signals* with these real sources, keeping the
regex scraping only as a **fallback** for panes that can't be mapped to a
session (LGTM-integration philosophy: degrade silently, never regress).

## The mapping (already exists — reuse it)

`build_window_view` (in `periscope/window_view.py`) has `w["pane_id"]`. Resolve:
`pane_id` → `session_id_for_pane(pane_id)` (`periscope/turns.py`, reads
`PANE_SESSIONS_DIR/<pane_id>`) → match a `sessions/*.json` whose `sessionId`
equals it. Build a small `sessionId → session-file` index (scan the ~30 small
files; they're tiny). **Staleness guard:** only trust a session file whose `pid`
is alive (`os.kill(pid, 0)`), else fall back to scraping — a crashed session
leaves a stale file at its last status.

## Signal-by-signal replacement

| Signal (in `parse_pane` output) | Replace with |
|---|---|
| `state` (working/needs-input/idle/shell) | `sessions/<pid>.json` `status`: busy→`working`, waiting→`needs-input`, idle→`idle`, shell→`shell`. Then keep the existing **done-vs-idle** refinement in `build_window_view` (the `completed_at > acked_at` → `done` logic) untouched. |
| `needs_input` / `asked_question` | `status == "waiting"`. Surface `waitingFor` as a new optional field for the card/modal subtitle. This retires the `NEEDS_INPUT_FOOTER_RE` dialog-footer detection **and** the brittle `_detect_asked_question` "reply ends with `?`" heuristic. |
| `is_claude` | a live session file exists for the pane's sessionId. (Keeps working through dialogs that blank the status line — retires `smooth_is_claude` stickiness for mapped panes.) |
| `api_error` | last `tool_result` in the JSONL whose body starts with `"API Error:"` (the same rule, but read from the JSONL, not scrollback). |
| `model` | per-assistant-message `model` in the JSONL. |
| "what's it doing" label | the in-progress task's `activeForm` (`tasks/<sessionId>/`), or the in-flight tool name from the JSONL. Richer and real vs. the whimsical spinner verb. |

## Keep scraping (no data-source equivalent)

- **`pending_input`** — text typed at `❯` but not submitted lives only in the
  TUI. Keep `_detect_pending_input`.
- **`context_pct`** — Claude's own computed context %, only emitted in the
  status line. Keep `_detect_status` for this number (and as the `is_claude`
  fallback for unmapped panes).
- **`spinner`** whimsical verb — cosmetic; not on disk. Optional to keep for the
  unmapped fallback path; prefer `activeForm` when a session is mapped.

## Before you trust the status values — VERIFY empirically

The session-file schema is **undocumented Claude Code internals** (saw versions
2.1.159 / 2.1.161 in the wild). Before relying on it:

1. Confirm the exact semantics of each `status` value, especially `shell` (is it
   "claude REPL backgrounded" vs "claude exited to a shell"?) and how `waiting`
   covers permission prompts vs `AskUserQuestion` vs plan approval.
2. Confirm `updatedAt`/transition timing is prompt enough for the 3s poll.
3. There are reusable probes from the investigation: `/tmp/ask_probe.sh`
   (samples a session JSONL + `/api/pane/turns`) and `/tmp/askprobe3.sh`
   (watches which `~/.claude` files change during a pending question). Adapt
   them to characterize the status-file transitions across a real turn
   (busy → waiting → idle, /clear, resume, subagent runs).

## Structure & process

- Put the reader in a new module, e.g. `periscope/session_status.py`:
  `status_for_session(sid) -> {status, waitingFor, pid, live} | None`, with the
  sessionId index + pid-liveness guard. Unit-test it against a seeded
  `sessions/` dir in `tests/test_session_status.py`.
- Wire it into `build_window_view`: prefer the session-derived state, fall back
  to `parse_pane` when unmapped/stale. Don't reshape the `parse_pane` output
  contract — the frontend consumes `state`, `needs_input`, `spinner`,
  `api_error`, `model`, `context_pct` (see `static/src/` — Grid/Card, Header,
  FilterBar, RailRows, Modal). Add `waitingFor` as additive.
- `parse_pane`'s regexes are **regression-tested** in `tests/test_panes.py` and
  exist because each one tracks a class of TUI-break. Do NOT delete the tests;
  the scraping stays as the fallback path and must keep passing.
- Respect the invariants in `CLAUDE.md` (esp. #1 `focused_at` is server-tracked,
  #2 is_claude staleness, the `_STATE` rebind, no `from server import`).
- This touches a fragile, invariant-laden area: **go through the spec → plan
  pipeline** (brainstorm → spec → structure → plan → review) rather than
  free-handing it. Verify on the dev instance against live panes before merging.

## Out of scope (mention, don't do)

- `periscope/usage.py` also TUI-scrapes (`claude /usage` in a hidden tmux
  session) for plan-usage %. No obvious data-source replacement yet — leave it,
  but note it as the other remaining scraper.
- The transcript-view activity panel (`docs/transcript-view-todos.md` §2) is a
  separate consumer of these same sources; this migration is the server-side
  foundation it would build on.

"""Pane introspection: tmux window listing + Claude TUI parsing + focus
tracking + spinner/is_claude smoothing.

The smoothing dicts (`_spinner_last_seen`, `_claude_last_seen`) absorb
single-frame capture-pane glitches so the dashboard's "thinking"
indicator and is_claude classification don't flicker. The focus dicts
(`_focused_at`, `_acted_at`, etc.) drive the stream view's recency
ordering.

`_resuming` lives here too (pane-shaped state — used by /api/state for
the resume-in-flight check and by routes/sessions / routes/history for
the resume orchestration).
"""

import re
import time

from periscope.config import USAGE_SESSION_PREFIX
from periscope.tmux import tmux, _ANSI_SGR_RE, _FG_COLOR_RE


# Server-tracked "last user-focused" per target.
# Tmux's window_activity bumps on any output (Claude streaming, build logs, dev
# servers, etc), which surprises users expecting "last accessed" semantics.
# We instead record when each window most recently became the active window in
# its session, plus any time the user acts on it via the dashboard.
_focused_at: dict[str, int] = {}
# `_acted_at` is a *user-action-only* recency stamp. Unlike `_focused_at` it
# does NOT bump on tmux active-window changes (which fire when Tom switches
# between sessions in his terminal, not when he engages a window via the
# periscope UI). The grid view's within-session card sort and the stream view
# both order by this. Bumped from the periscope-side handlers only:
#   - /ws/pane WS-connect (modal-open is the canonical "opened in periscope")
#   - /api/send, /api/paste-image, /api/rename
#   - /api/session/new, /api/window/new (creation through periscope)
# In-memory cache only; the persistent counterpart lives in
# _STATE["windows"][pid]["acked_at"] and is the source of truth for the
# done-vs-idle split (see /api/state). Reset on process restart; the
# persisted value carries forward.
_acted_at: dict[str, int] = {}
# When the parser observed a working/needs-input → idle transition, stamped
# `now`. Paired with `_acted_at` to split idle into "done" (Claude finished
# something the user hasn't acknowledged) vs "idle" (acknowledged or never
# busy). Persisted alongside acked_at under each pid's state.json entry.
_completed_at: dict[str, int] = {}
# Previous parsed state per pid, used to detect the working/needs-input →
# idle edge that drives `_completed_at`. Keyed by pid (not target) so a
# session rename doesn't lose the prior state and refire the transition.
_prev_state: dict[str, str] = {}
_active_per_session: dict[str, str] = {}

# Active resume operations, keyed by session_id. Each entry tracks where a
# `claude --resume <id>` is currently running so we can refuse concurrent
# resume requests (they'd interleave appends into the same JSONL).
_resuming: dict[str, dict] = {}
RESUME_EXPIRY_S = 30 * 60  # forget about a resume after 30 min idle

# Per-target spinner hysteresis. Tmux capture-pane occasionally catches Claude's
# TUI mid-redraw, dropping the spinner line for one cycle even when Claude is
# still working. We remember the last positive detection per target and treat
# it as sticky for SPINNER_GRACE_S so cards + modal subtitles don't flicker.
_spinner_last_seen: dict[str, tuple[str, float]] = {}
SPINNER_GRACE_S = 4.0

# Per-target "is this a Claude pane" stickiness. Detection is via STATUS_RE
# matching CC's bottom status line, but CC's interactive dialogs (e.g.
# AskUserQuestion) take over the screen and temporarily hide that line — we
# don't want the card to flip back to "shell" while the user is mid-prompt.
_claude_last_seen: dict[str, float] = {}
CLAUDE_STICKY_S = 120.0


def smooth_spinner(target: str, current: str | None) -> str | None:
    """Side effect: records/expires this target's entry in `_spinner_last_seen`
    for hysteresis — not idempotent, repeated same-arg calls can differ."""
    now = time.time()
    if current:
        _spinner_last_seen[target] = (current, now)
        return current
    last = _spinner_last_seen.get(target)
    if last and now - last[1] < SPINNER_GRACE_S:
        return last[0]
    _spinner_last_seen.pop(target, None)
    return None


def smooth_is_claude(target: str, current: bool) -> bool:
    """Side effect: records/expires this target's entry in `_claude_last_seen`
    for stickiness — not idempotent, repeated same-arg calls can differ."""
    now = time.time()
    if current:
        _claude_last_seen[target] = now
        return True
    last = _claude_last_seen.get(target, 0)
    if now - last < CLAUDE_STICKY_S:
        return True
    _claude_last_seen.pop(target, None)
    return False


def note_focus(target: str) -> None:
    _focused_at[target] = int(time.time())


def note_action(target: str) -> None:
    """Stamp a periscope-side user action. Separate from `note_focus`: the
    stream view orders by *only* actions the user took through periscope,
    not tmux activity. Callers that bump focus due to a user action should
    bump both; tmux-derived bumps go through `note_focus` alone."""
    _acted_at[target] = int(time.time())


def update_focus_from_windows(windows: list[dict]) -> None:
    """Walk the freshly-listed windows and stamp focus times when the active
    window for a session changes."""
    by_session_active: dict[str, str] = {}
    for w in windows:
        if w.get("active"):
            by_session_active[w["session"]] = f"{w['session']}:{w['index']}"
    for session, target in by_session_active.items():
        prev = _active_per_session.get(session)
        if prev != target or target not in _focused_at:
            note_focus(target)
            _active_per_session[session] = target


def record_state_transition(
    pid: str, target: str, state: str | None, now_ts: int
) -> None:
    """Record this poll's parsed `state` for `pid`. On a working/needs-input
    → idle edge, stamp `target`'s completed-at to `now_ts` — the signal that
    Claude finished something the user hasn't acknowledged. No-op when `pid`
    is empty. Lets window_view drive the done-vs-idle edge without touching
    `_prev_state` / `_completed_at` directly."""
    if not pid:
        return
    prev = _prev_state.get(pid)
    if prev in ("working", "needs-input") and state == "idle":
        _completed_at[target] = now_ts
    _prev_state[pid] = state


def recency_stamps_for(target: str) -> dict:
    """In-memory focus / action / completion stamps for `target` — each 0
    when never observed in this process. The persisted counterparts live in
    state.json under the window's pid."""
    return {
        "focused_at": _focused_at.get(target, 0),
        "acted_at": _acted_at.get(target, 0),
        "completed_at": _completed_at.get(target, 0),
    }


# Status line at the bottom of every Claude pane:
#   "  24% | ↑235k ↓479 | $17.04 | Opus 4.7 (1M context)"
STATUS_RE = re.compile(
    r"^\s*(?P<context>\d+)%\s*\|\s*↑\S+\s+↓\S+\s*\|\s*\$[\d.,]+\s*\|\s*(?P<model>.+?)\s*$"
)

# Branch / PR / CI used to come from a custom statusline rendered in the line
# above STATUS_RE. We now pull those from the pane's cwd directly (git +
# `gh pr list`), independent of any statusline customization.

# Active-op detection — two patterns covering the variations Claude Code's
# TUI shows for a running operation. Both are used with `.match()` so the
# spinner glyph must be at line start (after optional indent); this rejects
# prose embeds where a previous response or user message quotes the marker
# mid-sentence.
#
# An active marker is always `<non-ASCII glyph> <verb-phrase>` followed by
# either a trailing `…`, a `(timing/tokens)` parenthetical, or both.
# Glyph enumeration is intentionally avoided (Claude rotates through
# ✻ ✶ ✷ ✳ ✦ ⏺ … and adds new ones over time) — `[^\x00-\x7f]` matches any.
#
# SPINNER_RE handles the ellipsis form, single- OR multi-word phrase:
#   "✻ Envisioning…"
#   "✳ Wiring resolve_pids into endpoints…(910m 2 · ↓ 14.78 tokens · ...)"
# The phrase character class excludes `(` so it can't grow into parens —
# without that, tool-call headers like `⏺ Bash(cd /Users/tom/… --skip-glo…)`
# would match (the `…` inside the bash invocation isn't an active marker).
SPINNER_RE = re.compile(r"^\s*[^\x00-\x7f]\s+(?P<phrase>[^(\n…]+?)…")

# ACTIVE_OP_RE handles the parens form (no trailing `…`):
#   "● Bootstrapping packages (7m 29s · ↑ 22.1k tokens · thought for 2s)"
# The `↑/↓ Nk tokens` is the live uplink/downlink meter — present only while
# the op is running. Completion drops the arrow (`Done (5 tool uses · 25.5k
# tokens · 21s)`), so completed lines don't match. Distinguishable from
# STATUS_RE because the status line has both arrows on the same line, no
# `tokens` word, and no parens around the metering.
ACTIVE_OP_RE = re.compile(
    r"^\s*[^\x00-\x7f]\s+\S+.*\([^)]*[↑↓]\s*[\d.]+\w*\s+tokens[^)]*\)"
)

# Past-tense indicator: when Claude finishes a thinking phase, the spinner
# line transforms from `<glyph> Verbing…` into `<glyph> Verb-past for Xs`
# (e.g. `Cooked for 3m 42s`, `Brewed for 31s`, `Thought for 10s`). Same glyph
# rotation, different verb form.
#
# Used as a positional "stop searching" boundary: when iterating from the
# bottom, hitting this line before any active marker means Claude is idle
# and any active-marker-shape lines higher up are stale (from scrollback,
# or from the assistant's own response quoting the marker form verbatim in
# code blocks). The shape is specific enough — `<glyph> <Verb-past> for
# <digits><h|m|s>` — that prose embeds are very unlikely to match.
IDLE_INDICATOR_RE = re.compile(
    r"^\s*[^\x00-\x7f]\s+(?:\w+ed|Thought)\s+for\s+\d+\s*[hms]"
)

# Pull out a verb-shaped word for the card label (`envisioning…`,
# `planning…`). Falls back to the first word if there's no clean verb.
SPINNER_VERB_RE = re.compile(r"\b([A-Z]\w+(?:ing|ed))\b")

# Needs-input: the numbered-choice permission dialog. `❯ 1.` plus the
# "Esc to cancel" footer is Claude-Code-specific; either alone false-positives
# (shells use ❯ as a prompt; "Esc to cancel" appears in transient toasts).
# Claude's choice dialogs always render a single footer line that combines
# navigation hints with the cancel marker — e.g. one of:
#   "Enter to select · Esc to cancel"
#   "Enter to select · ↑/↓ to navigate · Esc to cancel"
#   "Submit · Esc to cancel"
# Matching the whole footer pattern on a single line is much more specific
# than scanning for the marker and a numbered option anywhere in the tail:
# prose responses (or shell output) that happen to mention both in different
# places will no longer false-positive. The dialog's options can sit far
# above the footer, so we don't need to find them — the footer is sufficient.
NEEDS_INPUT_FOOTER_RE = re.compile(
    r"(?:Enter\s+to\s+\w+|↑/↓|Submit\b).*Esc\s+to\s+cancel",
)

RECAP_RE = re.compile(
    r"※ recap:\s*(?P<text>.+?)(?=\n\s*[─❯]|\Z)", re.DOTALL
)
PROMPT_LINE_RE = re.compile(r"^❯\s*(?P<input>.*)$")

# Tool-call header in scrollback: `<glyph> Word(args)`. Used as one of
# three "Claude started a new turn after this past-tense indicator" signals
# in the question-mark needs-input detection. Distinguished from spinner /
# active-op / prose by the no-space-before-`(` pattern.
TOOL_CALL_RE = re.compile(r"^[^\x00-\x7f]\s+\w+\(")

# Tool-result lines start with `⎿`. Claude Code renders rate-limit and
# transport failures as a normal tool result whose body begins with
# "API Error:" and then STOPS — it does not auto-retry; the turn just
# ends with the error sitting there until the user types something
# (`keep going`, etc.) to restart. When the most recent `⎿` line is an
# API Error the pane is silently blocked waiting on a human nudge. A
# later non-error `⎿` means a subsequent turn succeeded and the flag
# should clear.
TOOL_RESULT_RE = re.compile(r"^\s*⎿\s+")
API_ERROR_RE = re.compile(r"^\s*⎿\s+API Error:")


def list_windows() -> list[dict]:
    out = tmux(
        "list-windows",
        "-a",
        "-F",
        "#{session_name}\t#{window_index}\t#{window_name}\t#{window_active}\t#{pane_current_path}\t#{@periscope_id}\t#{pane_id}",
    )
    rows = []
    for line in out.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        # pane_current_path is the active pane's cwd; safe even when missing.
        # @periscope_id is empty for unmanaged windows — `resolve_pids` mints
        # one on first sighting and stamps it onto the window.
        s, idx, name, active = parts[:4]
        # Hide the hidden `/usage`-scraper sessions from every caller; they're
        # our internal scaffolding, not user-visible tmux state.
        if s.startswith(USAGE_SESSION_PREFIX):
            continue
        cwd = parts[4] if len(parts) > 4 else ""
        pid_raw = parts[5] if len(parts) > 5 else ""
        # pane_id (%N) is tmux's stable handle for the active pane within the
        # current server lifetime — the addressing key for channel pushes.
        pane_id = parts[6] if len(parts) > 6 else ""
        rows.append(
            {
                "session": s,
                "index": int(idx),
                "name": name,
                "active": active == "1",
                "cwd": cwd,
                "pid_raw": pid_raw,
                "pane_id": pane_id,
            }
        )
    return rows


# ── parse_pane: per-signal detectors + orchestrator ─────────────────────
# parse_pane runs eight independent passes over a pane's captured buffer,
# then resolves a single state. Each pass is its own `_detect_*` helper so
# the next "Claude tweaked its TUI" fix lands in one named place; parse_pane
# itself is just the orchestration + the state-priority ladder.


def _split_buffers(content: str) -> tuple[list[str], list[str], list[str]]:
    """Split capture() output into (raw_rows, plain_rows, lines).

    `content` includes SGR escape sequences (capture -e). `raw_rows` keeps
    them — the prompt-line ghost-text check needs the fg-color info to tell
    real input from Claude's greyed-out suggestion. `plain_rows` strips
    them; `lines` is the non-empty plain rows.
    """
    raw_rows = content.rstrip("\n").split("\n")
    plain_rows = [_ANSI_SGR_RE.sub("", row) for row in raw_rows]
    lines = [p for p in plain_rows if p.strip() != ""]
    return raw_rows, plain_rows, lines


def _is_chrome_line(line: str) -> bool:
    """True if `line` is Claude-TUI chrome rather than real content:
      ─ / ❯ / ⏵     separator, empty prompt, auto-mode footer hint
      STATUS_RE      `XX% | ↑Nk ↓N | $cost | model` status line
      title bar      `<repo> | <branch> | <diff> | github.com/<path>…`
                     (Claude Code renders this inline above the convo)
      SPINNER/ACTIVE active spinner line — the verb is already the card's
                     state label, so re-rendering it as a snippet is noise
    Used to walk past chrome when hunting the closest real content line.
    """
    s = line.strip()
    if s.startswith(("─", "❯", "⏵")):
        return True
    if STATUS_RE.match(line):
        return True
    if "github.com/" in line and line.count("|") >= 3:
        return True
    if SPINNER_RE.match(line) or ACTIVE_OP_RE.match(line):
        return True
    return False


def _detect_status(lines: list[str]) -> dict | None:
    """Claude Code's bottom status line ("X% | ↑n ↓n | $cost | model"),
    searched in the last 4 non-empty lines. Its presence signals "this is
    a Claude pane" and yields the context+model fields. Returns the regex
    groupdict, or None if absent.

    Branch/PR/CI used to be parsed from a custom statusline rendered above
    this — periscope now derives them from the pane's cwd via git/gh.
    """
    for line in reversed(lines[-4:]):
        m = STATUS_RE.match(line)
        if m:
            return m.groupdict()
    return None


def _detect_spinner(lines: list[str]) -> str | None:
    """Active-operation verb for the card label, or None when idle.

    Scans the last 15 lines bottom-up; whichever signal is closest to the
    prompt wins. IDLE_INDICATOR_RE (past-tense `✻ Brewed for Xs`) stops the
    search so stale/quoted markers higher up (scrollback, or the assistant
    quoting the marker form in a code block) don't false-positive. Verb
    extraction falls back to "working" when there's no clean
    [A-Z]\\w+(ing|ed) match — a phrase like "3 reasons why" used to surface
    "3…" via a first-word fallback, which was uninformative noise.
    """
    for line in reversed(lines[-15:]):
        if IDLE_INDICATOR_RE.match(line):
            break
        m = SPINNER_RE.match(line)
        if m:
            phrase = m.group("phrase").strip()
            vm = SPINNER_VERB_RE.search(phrase)
            return vm.group(1) if vm else "working"
        if ACTIVE_OP_RE.match(line):
            vm = SPINNER_VERB_RE.search(line)
            return vm.group(1) if vm else "working"
    return None


def _detect_needs_input(lines: list[str]) -> bool:
    """True when Claude's numbered-choice permission-dialog footer is in
    the last few lines. The footer is a single line at the bottom while a
    dialog is active; restricting to a tight tail avoids matching prose
    that merely discusses dialog UI.
    """
    return any(NEEDS_INPUT_FOOTER_RE.search(line) for line in lines[-5:])


def _detect_pending_input(
    raw_rows: list[str], plain_rows: list[str]
) -> str | None:
    """Text typed at the `❯` prompt but not yet submitted, or None.

    Ghost-text filter: Claude Code shows a greyed-out suggestion in the
    input slot when nothing's typed. It looks like real input in plain
    text, but in the colored row it shares the prompt prefix's fg color
    (single distinct fg code). Real typed input switches to a different fg
    color (≥2 distinct fg codes). A row with no SGR escapes at all (e.g.
    test fixtures) carries no color info — trust the visible text.

    Caller invokes this only when no dialog is open — `❯ 1.` would
    otherwise read as the dialog's selection line, not user typing.
    """
    for raw, plain in zip(reversed(raw_rows), reversed(plain_rows)):
        if not plain.strip():
            continue
        m = PROMPT_LINE_RE.match(plain.strip())
        if not m:
            continue
        input_text = m.group("input").strip()
        if not input_text:
            return None
        if "\x1b[" in raw:
            fg_codes = set(_FG_COLOR_RE.findall(raw))
            return input_text if len(fg_codes) >= 2 else None
        return input_text
    return None


def _extract_recap(lines: list[str]) -> str | None:
    """The most recent `※ recap:` block — whitespace-collapsed, capped at
    400 chars — or None.
    """
    full = "\n".join(lines)
    matches = list(RECAP_RE.finditer(full))
    if not matches:
        return None
    recap = matches[-1].group("text").strip()
    return re.sub(r"\s+", " ", recap)[:400]


def _last_meaningful_line(lines: list[str]) -> str:
    """The closest "real" content line from the bottom — recent prose, a
    subtask line, or a past-tense indicator — skipping TUI chrome. Used for
    shell panes and the card snippet fallback. "" if nothing real.
    """
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        if _is_chrome_line(line):
            continue
        return s[:200]
    return ""


def _detect_asked_question(lines: list[str]) -> bool:
    """True when Claude's last visible reply ends with `?` — the pane is
    waiting on a human even without a permission dialog.

    Anchors on the past-tense indicator (`✻ Brewed for Xs`): it always
    sits immediately after a finished assistant turn, with post-reply
    chrome (TodoWrite list, separator, prompt) below it. Two gates:
      1. The latest indicator must be followed by chrome only. A submitted
         user reply (`❯ <text>`), tool call (`⏺ Word(...)`), or tool
         result (`⎿ …`) below it means the conversation moved past the
         question and the indicator is stale (Claude is mid-new-turn).
      2. The closest non-chrome line above the indicator must end with `?`.
    No indicator visible (rare — pane scrolled past the last turn) →
    leave the flag off; better to miss the case than to false-positive on
    something further up the buffer.

    Caller invokes this only for Claude panes with no dialog open.
    """
    idle_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if IDLE_INDICATOR_RE.match(lines[i]):
            idle_idx = i
            break
    if idle_idx is None:
        return False
    # Gate 1: nothing meaningful between the indicator and the bottom.
    # "Meaningful" is narrow on purpose — TodoWrite list rows render below
    # the indicator as end-of-turn chrome and must NOT count. Only three
    # patterns prove a NEW turn started after the indicator:
    #   - `❯ <text>` — submitted user reply or pending typing.
    #   - `<glyph> Word(...)` — tool-call header.
    #   - `⎿ …` — tool-result indicator.
    for line in lines[idle_idx + 1:]:
        s = line.strip()
        if not s:
            continue
        if s.startswith("❯") and len(s) > 1 and s.lstrip("❯").strip():
            return False
        if s.startswith("⎿"):
            return False
        if TOOL_CALL_RE.match(s):
            return False
    # Gate 2: the closest non-chrome line above the indicator ends with `?`.
    for line in reversed(lines[:idle_idx]):
        s = line.strip()
        if not s:
            continue
        if _is_chrome_line(line):
            continue
        return s.rstrip().endswith("?")
    return False


def _detect_api_error(lines: list[str]) -> bool:
    """True when the most recent `⎿` tool-result line is an API Error.

    Claude Code renders rate-limit / transport failures as a normal tool
    result whose body begins "API Error:" and then STOPS — no auto-retry;
    the turn just ends until the user nudges it (`keep going`, etc.). The
    flag tracks "pane is silently blocked". A later non-error `⎿` means a
    subsequent turn succeeded and the flag clears.

    Caller invokes this only for Claude panes — prose mentioning "API
    Error" in a shell pane (logs, grep output) must not trip it.
    """
    for line in reversed(lines):
        if API_ERROR_RE.match(line):
            return True
        if TOOL_RESULT_RE.match(line):
            return False
    return False


def _resolve_state(
    is_claude: bool,
    needs_input: bool,
    asked_question: bool,
    spinner: str | None,
) -> str:
    """State priority: needs-input wins over working (a spinner glyph can
    linger in scrollback above the dialog), working wins over idle. `idle`
    is the parse-level neutral state — /api/state may refine it to `done`
    when there's an unacknowledged completion stamp.
    """
    if not is_claude:
        return "shell"
    if needs_input or asked_question:
        return "needs-input"
    if spinner:
        return "working"
    return "idle"


def parse_pane(content: str) -> dict:
    """Parse a captured tmux pane into the dashboard's per-pane signals.

    Orchestration only: each signal is detected by its own `_detect_*`
    helper above; the state-priority ladder lives in `_resolve_state`.
    """
    raw_rows, plain_rows, lines = _split_buffers(content)

    status = _detect_status(lines)
    is_claude = status is not None

    spinner = _detect_spinner(lines)

    needs_input = _detect_needs_input(lines)
    # The dialog footer is Claude-specific UI; seeing it means the pane IS
    # Claude even if STATUS_RE missed (the dialog occupies the bottom rows
    # where the status line normally lives).
    if needs_input:
        is_claude = True

    # `❯ 1.` is the dialog's selection line, not user typing — skip
    # pending-input detection entirely when a dialog is open.
    pending_input = (
        None if needs_input else _detect_pending_input(raw_rows, plain_rows)
    )

    recap = _extract_recap(lines)
    last_line = _last_meaningful_line(lines)

    asked_question = (
        _detect_asked_question(lines)
        if is_claude and not needs_input
        else False
    )
    api_error = _detect_api_error(lines) if is_claude else False

    state = _resolve_state(is_claude, needs_input, asked_question, spinner)

    return {
        "is_claude": is_claude,
        "state": state,
        "spinner": spinner,
        "needs_input": needs_input or asked_question,
        "asked_question": asked_question,
        "pending_input": pending_input,
        "recap": recap,
        "last_line": last_line,
        "api_error": api_error,
        "context_pct": int(status["context"]) if status else None,
        "model": status["model"].strip() if status else None,
    }

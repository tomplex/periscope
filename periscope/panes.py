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


def parse_pane(content: str) -> dict:
    # `content` from capture() includes SGR escape sequences (-e). Strip them
    # for the bulk of parsing; keep the raw rows for the prompt-line check
    # below, which needs the color info to distinguish real input from
    # Claude's ghost-text suggestion.
    raw_rows = content.rstrip("\n").split("\n")
    plain_rows = [_ANSI_SGR_RE.sub("", row) for row in raw_rows]
    lines = [p for p in plain_rows if p.strip() != ""]

    status = None
    # Claude Code's bottom status line ("X% | ↑n ↓n | $cost | model") signals
    # both "this is a Claude pane" and gives us the context+model fields.
    # Branch/PR/CI used to be parsed from a custom statusline rendered above
    # this — we now derive them from the pane's cwd via git/gh instead.
    tail = lines[-4:]
    for line in reversed(tail):
        m = STATUS_RE.match(line)
        if m:
            status = m.groupdict()
            break

    is_claude = status is not None

    # Iterate the bottom rows looking for a state signal. Whichever signal
    # is closest to the prompt wins:
    #   - IDLE_INDICATOR_RE: Claude finished thinking → spinner stays None.
    #     Stops the search so we never reach quoted/stale markers in
    #     scrollback or in the assistant's own response code blocks.
    #   - SPINNER_RE / ACTIVE_OP_RE: an active marker is below the past-tense
    #     line (or there is no past-tense line) → spinner gets the verb.
    #
    # Verb extraction always falls back to the string "working" if no clean
    # [A-Z]\w+(ing|ed) match — a phrase like "3 reasons why" used to surface
    # "3…" via a "first word" fallback, which was uninformative noise.
    spinner = None
    for line in reversed(lines[-15:]):
        if IDLE_INDICATOR_RE.match(line):
            break
        m = SPINNER_RE.match(line)
        if m:
            phrase = m.group("phrase").strip()
            vm = SPINNER_VERB_RE.search(phrase)
            spinner = vm.group(1) if vm else "working"
            break
        if ACTIVE_OP_RE.match(line):
            vm = SPINNER_VERB_RE.search(line)
            spinner = vm.group(1) if vm else "working"
            break

    # Needs-input: look for the dialog's footer line in the last few lines.
    # The footer is always a single line at the bottom of the pane when a
    # dialog is active, so restricting the search to a tight tail avoids
    # matching prose that happens to discuss dialog UI.
    needs_input = any(
        NEEDS_INPUT_FOOTER_RE.search(line) for line in lines[-5:]
    )
    # The dialog footer is Claude-specific UI; if we see it the pane IS
    # Claude even if STATUS_RE missed (the dialog occupies the bottom rows
    # where the status line normally lives).
    if needs_input:
        is_claude = True

    # Pending input: ❯ followed by some text the user has typed but not
    # submitted. Skip when needs_input is true — `❯ 1.` is the dialog's
    # selection line, not user typing.
    #
    # Ghost-text filter: Claude Code shows a greyed-out suggestion in the
    # input slot when nothing's been typed. The suggestion looks like real
    # input in plain text, but in the colored row it shares the prompt
    # prefix's fg color (single distinct fg code on the line). Real typed
    # input switches to a different fg color (≥2 distinct fg codes). When
    # the row carries no SGR escapes at all (e.g. test fixtures), we have
    # no color info and trust the visible text.
    pending_input = None
    if not needs_input:
        for raw, plain in zip(reversed(raw_rows), reversed(plain_rows)):
            if not plain.strip():
                continue
            m = PROMPT_LINE_RE.match(plain.strip())
            if not m:
                continue
            input_text = m.group("input").strip()
            if not input_text:
                break
            if "\x1b[" in raw:
                fg_codes = set(_FG_COLOR_RE.findall(raw))
                if len(fg_codes) >= 2:
                    pending_input = input_text
                # else: ghost text — leave pending_input as None
            else:
                pending_input = input_text
            break

    # Most recent recap block
    full = "\n".join(lines)
    recap = None
    matches = list(RECAP_RE.finditer(full))
    if matches:
        recap = matches[-1].group("text").strip()
        recap = re.sub(r"\s+", " ", recap)[:400]

    # Last meaningful line for shell panes / card snippet fallback. Walk up
    # from the bottom skipping TUI chrome — what's left is the closest
    # "real" content (recent prose, subtask line, or past-tense indicator).
    #   ─ / ❯           separator and empty prompt
    #   ⏵               `⏵⏵ auto mode on (shift+tab to cycle)` footer hint
    #   STATUS_RE       `XX% | ↑Nk ↓N | $cost | model` status line
    #   title bar       `<repo> | <branch> | <diff> | github.com/<path>…`
    #                   (Claude Code renders this inline above the convo)
    #   SPINNER/ACTIVE  active spinner line — the verb is already shown as
    #                   the card's state label, so re-rendering the full
    #                   spinner line as the snippet would be redundant.
    last_line = ""
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        if s.startswith(("─", "❯", "⏵")):
            continue
        if STATUS_RE.match(line):
            continue
        if "github.com/" in line and line.count("|") >= 3:
            continue
        if SPINNER_RE.match(line) or ACTIVE_OP_RE.match(line):
            continue
        last_line = s[:200]
        break

    # Question-mark needs-input: when Claude's last visible reply ends with
    # `?` we treat the pane as waiting on the user, even without a dialog.
    # Walk from the bottom, skipping TUI chrome (status / prompt / hint /
    # separator / title / active or past-tense indicators / blank); the
    # first remaining line is the visible tail of the most recent reply.
    # Only applies to Claude panes — shells use `?` in plenty of normal
    # output (`man bash` examples, error messages, etc.).
    asked_question = False
    if is_claude and not needs_input:
        for line in reversed(lines):
            s = line.strip()
            if not s:
                continue
            if s.startswith(("─", "❯", "⏵")):
                continue
            if STATUS_RE.match(line):
                continue
            if "github.com/" in line and line.count("|") >= 3:
                continue
            if SPINNER_RE.match(line) or ACTIVE_OP_RE.match(line):
                continue
            if IDLE_INDICATOR_RE.match(line):
                continue
            if s.rstrip().endswith("?"):
                asked_question = True
            break

    # State priority: needs-input wins over working (a spinner glyph can
    # linger in scrollback above the dialog), working wins over idle.
    # `idle` is the parse-level neutral state — /api/state may refine it to
    # `done` when there's an unacknowledged completion stamp.
    if not is_claude:
        state = "shell"
    elif needs_input or asked_question:
        state = "needs-input"
    elif spinner:
        state = "working"
    else:
        state = "idle"

    return {
        "is_claude": is_claude,
        "state": state,
        "spinner": spinner,
        "needs_input": needs_input or asked_question,
        "asked_question": asked_question,
        "pending_input": pending_input,
        "recap": recap,
        "last_line": last_line,
        "context_pct": int(status["context"]) if status else None,
        "model": status["model"].strip() if status else None,
    }

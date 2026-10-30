"""Pane introspection: tmux window listing + Claude TUI parsing + focus
tracking + spinner/agent smoothing.

The smoothing dicts (`_spinner_last_seen`, `_agent_last_seen`) absorb
single-frame capture-pane glitches so the dashboard's "thinking"
indicator and agent classification don't flicker. The focus dicts
(`_focused_at`, `_acted_at`, etc.) drive the stream view's recency
ordering.

`_resuming` lives here too (pane-shaped state — used by /api/state for
the resume-in-flight check and by routes/sessions / routes/history for
the resume orchestration).
"""

import hashlib
import re
import time

from periscope.config import INPUT_CTL_SESSION
from periscope.tmux import _ANSI_SGR_RE, tmux

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
#   - /api/open, /api/window/new (creation through periscope)
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
_prev_state: dict[str, str | None] = {}
# When the pane entered needs-input, stamped on that edge and cleared when it
# leaves. Keyed by pid like `_prev_state`. Published as `needs_since` so a
# question's age survives a browser reload (Clawd's red bell, 10 minutes in).
# Persisted per pid too (window_view): a restart that re-stamped a pane still
# waiting gave it a new episode, and the native banner keyed on it replayed.
_needs_since: dict[str, int] = {}
# Structured-agent transition baselines.  Unlike `_prev_state`, these carry
# session/turn identity so an idle edge from a different turn can never finish
# stale work.  Values are (provider, session_id, turn_id, state).
_agent_transition_baseline: dict[str, tuple[str, str, str, str]] = {}
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

# Per-target "is this a Claude pane" stickiness. Detection is via CC's
# composer or STATUS_RE (parse_pane), but CC's interactive dialogs (e.g.
# AskUserQuestion) take over the screen and temporarily hide that line — we
# don't want the card to flip back to "shell" while the user is mid-prompt.
_agent_last_seen: dict[str, tuple[str, float]] = {}
AGENT_STICKY_S = 120.0


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


def smooth_agent(pane_id: str, current: str | None) -> str | None:
    """Smooth transient agent-detection gaps for one stable tmux pane.

    A positive detection always wins, including a provider change. Missing
    detection retains the last provider briefly while dialogs/redraws hide
    identifying UI.
    """
    now = time.time()
    if current:
        _agent_last_seen[pane_id] = (current, now)
        return current
    last = _agent_last_seen.get(pane_id)
    if last and now - last[1] < AGENT_STICKY_S:
        return last[0]
    _agent_last_seen.pop(pane_id, None)
    return None


def forget_agent(pane_id: str, agent: str | None = None) -> None:
    """Drop sticky identity, optionally only when it belongs to ``agent``."""
    last = _agent_last_seen.get(pane_id)
    if last and (agent is None or last[0] == agent):
        _agent_last_seen.pop(pane_id, None)


def smooth_parsed(*, pane_id: str, parsed: dict) -> dict:
    """Apply the shared pane normalization ladder in-place."""
    parsed["spinner"] = smooth_spinner(pane_id, parsed.get("spinner"))
    parsed["agent"] = smooth_agent(pane_id, parsed.get("agent"))
    if not parsed["agent"]:
        parsed["state"] = "shell"
    if parsed.get("agent") and parsed.get("spinner") and parsed.get("state") not in ("working", "needs-input"):
        parsed["state"] = "working"
    return parsed


def note_focus(target: str) -> None:
    _focused_at[target] = int(time.time())


def note_action(target: str) -> None:
    """Stamp a periscope-side user action. Separate from `note_focus`: the
    stream view orders by *only* actions the user took through periscope,
    not tmux activity. Callers that bump focus due to a user action should
    bump both; tmux-derived bumps go through `note_focus` alone."""
    _acted_at[target] = int(time.time())


def drop_target_focus(target: str) -> None:
    """Drop a single window target's focus/acted bookkeeping. Shared by every
    per-target kill site (session_delete, cleanup_archive, window_move,
    window_delete) so the per-target cleanup rule lives in one place — a
    per-session prefix sweep would wipe a surviving workspace-tagged pane's
    state still living in that session."""
    _focused_at.pop(target, None)
    _acted_at.pop(target, None)


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


def record_state_transition(pid: str, target: str, state: str | None, now_ts: int, persisted_since: int = 0) -> None:
    """Record this poll's parsed `state` for `pid`. On a working/needs-input
    → idle edge, stamp `target`'s completed-at to `now_ts` — the signal that
    Claude finished something the user hasn't acknowledged. No-op when `pid`
    is empty. Lets window_view drive the done-vs-idle edge without touching
    `_prev_state` / `_completed_at` directly. `persisted_since` is the
    needs-input stamp state.json holds for `pid`: a pane first sighted
    waiting by this process continues that episode rather than starting one."""
    if not pid:
        return
    prev = _prev_state.get(pid)
    if prev in ("working", "needs-input") and state == "idle":
        _completed_at[target] = now_ts
    if state == "needs-input":
        _needs_since.setdefault(pid, persisted_since if prev is None and persisted_since else now_ts)
    else:
        _needs_since.pop(pid, None)
    _prev_state[pid] = state


def needs_since_for(pid: str) -> int:
    """When `pid` entered needs-input, or 0 when it isn't waiting."""
    return _needs_since.get(pid, 0)


def record_agent_state_transition(
    pid: str,
    target: str,
    *,
    provider: str,
    session_id: str,
    turn_id: str,
    state: str,
    now_ts: int,
) -> None:
    """Record a *valid* structured agent observation.

    Completion requires exact provider/session/turn continuity.  Callers must
    not invoke this for unknown evidence; preserving the last valid baseline
    across a temporary evidence gap is what permits a later same-turn idle
    edge without allowing the gap itself to synthesize completion.
    """
    if not pid or state not in {"working", "idle"}:
        return
    identity = (provider, session_id, turn_id)
    prev = _agent_transition_baseline.get(pid)
    if prev and prev[:3] == identity and prev[3] == "working" and state == "idle":
        _completed_at[target] = now_ts
    _agent_transition_baseline[pid] = (*identity, state)


def clear_agent_state_transition(pid: str) -> None:
    """Forget structured transition state when pane/agent identity ends."""
    if pid:
        _agent_transition_baseline.pop(pid, None)


def recency_stamps_for(target: str) -> dict:
    """In-memory focus / action / completion stamps for `target` — each 0
    when never observed in this process. The persisted counterparts live in
    state.json under the window's pid."""
    return {
        "focused_at": _focused_at.get(target, 0),
        "acted_at": _acted_at.get(target, 0),
        "completed_at": _completed_at.get(target, 0),
    }


# One statusLine script's output, the only one context % and model are read
# from. Other scripts' (or none) fall back to the composer (THI-434):
#   "  24% | ↑235k ↓479 | $17.04 | Opus 4.7 (1M context)"
STATUS_RE = re.compile(r"^\s*(?P<context>\d+)%\s*\|\s*↑\S+\s+↓\S+\s*\|\s*\$[\d.,]+\s*\|\s*(?P<model>.+?)\s*$")

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
ACTIVE_OP_RE = re.compile(r"^\s*[^\x00-\x7f]\s+\S+.*\([^)]*[↑↓]\s*[\d.]+\w*\s+tokens[^)]*\)")

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
IDLE_INDICATOR_RE = re.compile(r"^\s*[^\x00-\x7f]\s+(?:\w+ed|Thought)\s+for\s+\d+\s*[hms]")

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

# A permission dialog (Bash, Write, Edit, …), which a native banner's Allow
# answers by sending `1` (routes/pane_actions.py): a wrong match approves the
# wrong request, so every rule below fails closed. Its footer, "Esc to cancel ·
# Tab to amend" in 2.1.280, is outside NEEDS_INPUT_FOOTER_RE's family, so it's
# known by its own shape (parse_permission_dialog).
PERMISSION_QUESTION_RE = re.compile(r"^\s*Do you want to .+\?\s*$")
# ❯ is the cursor; the user may have moved it off option 1.
PERMISSION_YES_RE = re.compile(r"^\s*(?:❯\s*)?1\.\s+Yes\b")
DIALOG_CANCEL_RE = re.compile(r"\bEsc\s+to\s+cancel\b")
# The full-width rule Claude draws above the dialog, at column 0: a reply's hr
# is indented under its ⏺, a command's line under its gutter.
DIALOG_RULE_RE = re.compile(r"^─{20,}\s*$")
# A file tool's question names its path, relative to the cwd.
_SHOWN_PATH_RE = re.compile(r"^\s*Do you want to (?:create|overwrite|make this edit to) (.+?)\?\s*$")
# A turn, a finished spinner, a tool result: scrollback, never dialog. The
# search for the rule stops at one, so a rule-less screen can't pull the
# `⏺ Bash(…)` above it into the "dialog".
_SCROLLBACK_RE = re.compile(r"^\s*[⏺✻⎿]")
# Non-empty rows the question can sit above the footer: the options wrap
# (Bash's "always allow access to <dir>" does), but not past this.
_QUESTION_REACH = 16
# Rows the rule can sit above the question. A taller dialog (a long heredoc,
# a big preview) gets no Allow: a 409 that reveals the pane.
_RULE_REACH = 60
# Tools whose dialog Allow can verify (dialog_matches). Mirrors
# pane_permission_hook.VERIFIABLE_TOOLS: the hook runs on bare python3 and
# can't import this.
VERIFIABLE_TOOLS = ("Bash", "Edit", "Write")
_FILE_HEADERS = {"Write": ("Create file", "Overwrite file"), "Edit": ("Edit file",)}
# A Bash dialog's command and description sit at this indent or deeper. A
# leading Tip row sits at less, and a pane under ~84 columns wraps it onto a
# second shallow row that no `Tip:` test would catch. Without a leading Tip,
# a shallow row above the command is unknown chrome and stays in the block.
_COMMAND_INDENT = 3
# A multi-line command's `│ ` gutter; a blank command line leaves a bare `│`.
_GUTTER_RE = re.compile(r"^│(?=\s|$)")
# Mirrors pane_permission_hook._HIDDEN_RE (a test pins the two): the hook
# strips bidi, zero-width and other invisible controls from its key, so the
# screen side strips the same set. Escapes, never raw characters (trojan source).
_HIDDEN_RE = re.compile("[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")
_WS_RE = re.compile(r"\s+")
# Every CSI (colon-form SGR included) and OSC (OSC 8 hyperlinks included).
# capture() keeps escapes (-e), and _ANSI_SGR_RE's `[\d;]*m` leaves a
# `\x1b[4:3m` on ❯ or a linkified path in place, breaking the line's shape.
_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

RECAP_RE = re.compile(r"※ recap:\s*(?P<text>.+?)(?=\n\s*[─❯]|\Z)", re.DOTALL)
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
# should clear. A stream cut mid-response ("The response stopped
# arriving") renders as an assistant `⏺` line instead (THI-436), so both
# glyphs count as the error.
TOOL_RESULT_RE = re.compile(r"^\s*⎿\s+")
API_ERROR_RE = re.compile(r"^\s*[⎿⏺]\s+API Error:")


def all_pane_ids() -> set[str]:
    """Every live pane id across all sessions. `list_windows` reports one pane
    per window (the active one — `list-windows -F #{pane_id}` resolves against
    the active pane), so a split window's background panes are invisible to it.
    Channel-alert GC keys on pane id, so using the window set there would delete
    a background Claude pane's alerts every poll. This is the full set."""
    out = tmux("list-panes", "-a", "-F", "#{pane_id}")
    return {line.strip() for line in out.split("\n") if line.strip()}


def list_windows() -> list[dict]:
    out = tmux(
        "list-windows",
        "-a",
        "-F",
        "#{session_name}\t#{window_index}\t#{window_name}\t#{window_active}\t#{pane_current_path}\t#{@periscope_id}\t#{pane_id}\t#{window_activity}\t#{window_id}\t#{pane_pid}\t#{pane_current_command}\t#{@periscope_opened}",
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
        # Hide periscope's internal session from every caller — the
        # control-mode input client's session is scaffolding, not
        # user-visible tmux state.
        if s == INPUT_CTL_SESSION:
            continue
        cwd = parts[4] if len(parts) > 4 else ""
        pid_raw = parts[5] if len(parts) > 5 else ""
        # pane_id (%N) is tmux's stable handle for the active pane within the
        # current server lifetime — the addressing key for channel pushes.
        pane_id = parts[6] if len(parts) > 6 else ""
        # window_activity is tmux's last-output timestamp. Used ONLY as a
        # skip-recapture hint in window_view; it must NEVER feed focus
        # (invariant #1 — focus is keyed on window_active above).
        activity = int(parts[7]) if len(parts) > 7 and parts[7] else 0
        # window_id (@N) is tmux's stable handle for the WINDOW. Unlike
        # session:index it survives move-window/renumbering, so it is the only
        # safe target for writing @periscope_id back (see pids._resolve_one).
        window_id = parts[8] if len(parts) > 8 else ""
        pane_pid = parts[9] if len(parts) > 9 else ""
        current_command = parts[10] if len(parts) > 10 else ""
        # "<epoch> <dir>" on windows an open created (pids.stamp_new_window);
        # open_ops trusts it over the live cwd while the pane is fresh.
        opened = parts[11] if len(parts) > 11 else ""
        rows.append(
            {
                "session": s,
                "index": int(idx),
                "name": name,
                "active": active == "1",
                "cwd": cwd,
                "pid_raw": pid_raw,
                "pane_id": pane_id,
                "activity": activity,
                "window_id": window_id,
                "pane_pid": pane_pid,
                "current_command": current_command,
                "opened": opened,
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

    `content` includes escape sequences (capture -e). `raw_rows` keeps
    them — the prompt-line ghost-text check needs the fg-color info to tell
    real input from Claude's greyed-out suggestion. `plain_rows` strips
    them, OSC 8 hyperlinks included (a linkified ticket reached the done
    banner as `\\x1b]8;id=…`, THI-416); `lines` is the non-empty plain rows.
    """
    raw_rows = content.rstrip("\n").split("\n")
    plain_rows = [_strip_escapes(row) for row in raw_rows]
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
    return bool(SPINNER_RE.match(line) or ACTIVE_OP_RE.match(line))


def _detect_status(lines: list[str]) -> dict | None:
    """Claude Code's bottom status line ("X% | ↑n ↓n | $cost | model"),
    searched in the last 4 non-empty lines. Its presence signals "this is
    a Claude pane" (as does the composer, _detect_composer) and yields the
    context+model fields. Returns the regex groupdict, or None if absent.

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


def _strip_escapes(text: str) -> str:
    return _ESCAPE_RE.sub("", text)


def _squash(text: str) -> str:
    """All whitespace and hidden controls removed, as for the hook's match
    key: a long command wraps mid-word onto an indented row, so only a
    whitespace-free compare survives the wrap."""
    return _WS_RE.sub("", _HIDDEN_RE.sub("", text))


def _bash_key(squashed: str) -> str:
    # The hook keys a digest, so the WHOLE command never sits in the key:
    # commands carry tokens, and periscope.db is 0644. Its display summary
    # still holds the head and tail (pane_permission_hook.match_key).
    return "sha256:" + hashlib.sha256(squashed.encode()).hexdigest()


def parse_permission_dialog(content: str) -> dict | None:
    """The permission dialog ending a capture, or None. Returns its `text`
    (header row to footer row), `header` ("Bash command", "Create file", …),
    `body` (the non-empty rows between header and question),
    `question`, and `shown_path` (a file tool's question path, else None).

    Read bottom up, each step failing closed:
    - the footer is the LAST line and matches `Esc to cancel`, but not
      AskUserQuestion's "Enter to select · … · Esc to cancel", whose
      "Do you want to …?" + `❯ 1. Yes` otherwise pass for a permission dialog;
    - the nearest question above it has `1. Yes` right under it;
    - the full-width rule is above that, with no scrollback in between. The
      rule bounds the body: the scrollback's `⏺ Bash(touch probe.txt)` quotes
      a command too, and must never vouch for a different dialog."""
    rows = [row.rstrip() for row in _strip_escapes(content).split("\n")]
    filled = [i for i, row in enumerate(rows) if row.strip()]
    if not filled:
        return None
    footer = filled[-1]
    if not DIALOG_CANCEL_RE.search(rows[footer]) or NEEDS_INPUT_FOOTER_RE.search(rows[footer]):
        return None
    question = None
    for i in reversed(filled[-1 - _QUESTION_REACH : -1]):
        if PERMISSION_QUESTION_RE.match(rows[i]):
            question = i
            break
        if DIALOG_RULE_RE.match(rows[i]) or _SCROLLBACK_RE.match(rows[i]):
            return None
    if question is None or not PERMISSION_YES_RE.match(rows[question + 1]):
        return None
    rule = None
    for i in range(question - 1, max(question - 1 - _RULE_REACH, -1), -1):
        if DIALOG_RULE_RE.match(rows[i]):
            rule = i
            break
        if _SCROLLBACK_RE.match(rows[i]):
            return None
    if rule is None:
        return None
    inner = [i for i in filled if rule < i < question]
    if not inner:
        return None
    shown = _SHOWN_PATH_RE.match(rows[question])
    return {
        "text": "\n".join(rows[i] for i in filled if rule < i <= footer),
        "header": rows[inner[0]].strip(),
        "body": [rows[i] for i in inner[1:]],
        "question": rows[question].strip(),
        "shown_path": shown.group(1) if shown else None,
    }


def permission_dialog(content: str) -> str | None:
    """The permission dialog ending a capture, as its text from header to
    footer, or None (parse_permission_dialog)."""
    parsed = parse_permission_dialog(content)
    return parsed["text"] if parsed else None


def dialog_matches(parsed: dict | None, tool: str, match_key: str | None) -> bool:
    """Whether a parsed permission dialog asks exactly what the hook recorded
    (pane_permission_hook.match_key), so Allow's `1` approves that request
    and no other. No key, or a tool whose dialog isn't verified (NotebookEdit,
    WebFetch, MCP tools, …), never matches.

    Bash: the digest of the whole command + description block (after a
    leading Tip and its wrap, up to the first row indented 3+), gutter and
    whitespace squashed, EQUALS the key. Never a prefix or a substring: `git
    push origin main` vouched for `… --force`, two `cd <repo> && …` share
    their first 40 characters, and every `python3 - <<'EOF'` its first line.
    Accepted limit: squashing loses whitespace-only differences and the
    command/description boundary (`rm -rf / tmp/x` squashes as `rm -rf
    /tmp/x`), which the wrapped screen can't recover; the single-row rule and
    the request id narrow it.
    Write / Edit: the header names the tool, and the question's path is a
    `/`-boundary suffix of the absolute key (the dialog shows it relative to
    the cwd); the boundary stops `notes.md` vouching for `old-notes.md`. The
    body (a preview, a diff) is never consulted: it can mention any file.
    Accepted limits: a file tool matches by FILE, not by change, so two
    pending edits to one file are interchangeable (the banner's consent is
    file-level); and a relative path is suffix-matched, so a cwd change
    within one wait could pair a stale row with a same-named file elsewhere
    (that takes a hook miss, a cd and a same name in one needs-input
    episode)."""
    if not (parsed and isinstance(match_key, str) and match_key and tool in VERIFIABLE_TOOLS):
        return False
    if tool == "Bash":
        if parsed["header"] != "Bash command":
            return False
        body = parsed["body"]
        start = 0
        if body and body[0].lstrip().startswith("Tip:"):
            start = next((i for i, r in enumerate(body) if len(r) - len(r.lstrip()) >= _COMMAND_INDENT), len(body))
        block = "".join(_GUTTER_RE.sub("", row.lstrip()) for row in body[start:])
        return _bash_key(_squash(block)) == match_key
    shown = parsed["shown_path"]
    return parsed["header"] in _FILE_HEADERS[tool] and bool(shown) and ("/" + match_key).endswith("/" + shown)


def dialog_open(content: str) -> bool:
    """Whether a Claude dialog may be up in a capture. Text typed into one
    picks an option — `1 sounds good` + Enter answers "Yes" — so POST
    /api/pane/reply refuses while one is open. Fails closed: an `Esc to
    cancel` anywhere in the last three lines counts, so a dialog
    parse_permission_dialog can't read (its question wrapped in a narrow
    pane, a new footer) still blocks a reply. A false positive only costs a
    409 that reveals the pane."""
    lines = [row for row in _strip_escapes(content).split("\n") if row.strip()]
    return (
        _detect_needs_input(lines)
        or parse_permission_dialog(content) is not None
        or any(DIALOG_CANCEL_RE.search(line) for line in lines[-3:])
    )


_SGR_SPLIT_RE = re.compile(r"(\x1b\[[0-9;]*m)")
# Ghost suggestions are drawn dim, bright-black, or a mid grey: 256-color
# ramp entries 238-247 (levels 68-158), or an RGB grey of 64-160. Anything
# else after the prompt is treated as typed, a near-black grey included: that
# is a draft in a dark theme's text colour, and reading a draft as a
# suggestion lets a send be appended to it and submitted with it.
_GHOST_FLOOR = 64
_GHOST_256 = frozenset({8, *range(238, 248)})


def _sgr_fg(params: list[str], fg: tuple | None, dim: bool) -> tuple[tuple | None, bool]:
    """Apply one SGR sequence's params to the (fg, dim) state."""
    i = 0
    while i < len(params):
        p = params[i] or "0"
        if p == "0":
            fg, dim = None, False
        elif p == "2":
            dim = True
        elif p == "22":
            dim = False
        elif p == "39":
            fg = None
        elif p in ("38", "48"):
            mode = params[i + 1] if i + 1 < len(params) else ""
            n = 1 if mode == "5" else 3 if mode == "2" else 0
            args = params[i + 2 : i + 2 + n]
            if p == "38" and len(args) == n and n and all(a.isdigit() for a in args):
                fg = ("256", int(args[0])) if n == 1 else ("rgb", *map(int, args))
            i += 1 + (n + 1 if n else 0)
            continue
        elif p.isdigit() and (30 <= int(p) <= 37 or 90 <= int(p) <= 97):
            fg = ("basic", int(p))
        i += 1
    return fg, dim


def _ghost_colored(fg: tuple | None, dim: bool) -> bool:
    if dim or fg == ("basic", 90):
        return True
    if fg and fg[0] == "256":
        return fg[1] in _GHOST_256
    if fg and fg[0] == "rgb":
        return max(fg[1:]) - min(fg[1:]) <= 24 and min(fg[1:]) >= _GHOST_FLOOR and max(fg[1:]) <= 160
    return False


# Claude Code's placeholder for a pasted block, drawn grey like a suggestion but
# very much a draft — and possibly the whole of it.
_PASTE_PLACEHOLDER_RE = re.compile(r"\[Pasted text #\d+")


def _input_is_ghost(raw: str) -> bool:
    """True when EVERY glyph after the `❯` is drawn like Claude Code's
    greyed-out suggestion: dim, bright-black, or dark grey.

    Decided positively, so anything unrecognized counts as typed. The old rule
    ("typed input switches to a second fg color") broke on Claude Code 2.1.280,
    which draws a draft in the default fg, the same as the ❯. A draft read as
    ghost text, and the per-pane /effort picker pasted onto it and submitted
    it. Reading a ghost suggestion as a draft only costs a refused send.

    Every glyph, not the first: a draft can open with a grey paste placeholder
    and continue in the default fg.
    """
    if _PASTE_PLACEHOLDER_RE.search(_ANSI_SGR_RE.sub("", raw)):
        return False
    fg: tuple | None = None
    dim = False
    after_prompt = False
    seen = False
    for part in _SGR_SPLIT_RE.split(raw):
        if part.startswith("\x1b["):
            fg, dim = _sgr_fg(part[2:-1].split(";"), fg, dim)
            continue
        for ch in part:
            if not after_prompt:
                after_prompt = ch == "❯"
            elif not ch.isspace():
                if not _ghost_colored(fg, dim):
                    return False
                seen = True
    return seen


def _detect_pending_input(raw_rows: list[str], plain_rows: list[str]) -> str | None:
    """Text typed at the `❯` prompt but not yet submitted, or None.

    Ghost-text filter: Claude Code shows a greyed-out suggestion in the input
    slot when nothing's typed, which looks like input in plain text. It is
    told apart by color (_input_is_ghost). A row with no SGR escapes at all
    (e.g. test fixtures) carries no color info, so the visible text is trusted.

    Caller invokes this only when no dialog is open — `❯ 1.` would
    otherwise read as the dialog's selection line, not user typing.
    """
    for raw, plain in zip(reversed(raw_rows), reversed(plain_rows), strict=False):
        if not plain.strip():
            continue
        m = PROMPT_LINE_RE.match(plain.strip())
        if not m:
            continue
        input_text = m.group("input").strip()
        if not input_text:
            return None
        return None if _input_is_ghost(raw) else input_text
    return None


# Non-empty rows the footer under the composer's lower rule may take: the
# mode hint, warnings, a status line (live 2.1.280 panes show 2-4). A Claude
# that exited leaves its last frame above the shell's output and prompt,
# which push the count past this once the shell has printed anything.
_FOOTER_REACH = 6
# Right after exit they don't: "Press Ctrl-C again to exit", this line, the
# resume command and the prompt is four rows (THI-434, from live scrollback).
_EXITED_RE = re.compile(r"^Resume this session with:")
# The footer reach for telling a Claude pane from a shell, where nothing is
# typed into the pane on the strength of it: background agents list under the
# status line one row each (7 rows with 3 of them, live), and a reply's
# tighter _FOOTER_REACH would read those panes as shells.
_DETECT_FOOTER_REACH = 12
# How much of a reply's start the composer must show back before its Enter:
# enough to tell it from anything else, short enough for the first row.
_HOLDS_PREFIX = 24
# Non-empty rows above the composer's top rule searched for a dialog drawn
# over the screen mid-redraw: Claude rewrites only the rows that change, so a
# capture can catch a dialog's top half above a composer still intact below.
_DIALOG_TOP_REACH = 16


def _dialog_above(rows: list[str]) -> bool:
    """Whether the rows above a composer show part of a dialog: its own
    full-width rule (a reply's hr is indented, never at column 0), an `Esc to
    cancel`, or a `1. Yes` option under its cursor or right under a "Do you
    want to …?". A bare "Do you want to …?" doesn't count: that's how Claude
    asks in prose, which is what a reply answers."""
    above = [row for row in rows if row.strip()][-_DIALOG_TOP_REACH:]
    for i, row in enumerate(above):
        if DIALOG_RULE_RE.match(row) or DIALOG_CANCEL_RE.search(row):
            return True
        if PERMISSION_YES_RE.match(row) and (
            row.lstrip().startswith("❯") or (i and PERMISSION_QUESTION_RE.match(above[i - 1]))
        ):
            return True
    return False


# The composer's top rule: plain, or a named session's with the name at its
# right end (`──── thi-416-banner ─`), which hid the composer and handed the
# footer to last_line (THI-416). The lower rule and a dialog's stay plain.
_COMPOSER_TOP_RE = re.compile(r"^─{20,}(?: [^─]+ ─+)?\s*$")


def _composer_rows(content: str, footer_reach: int = _FOOTER_REACH) -> list[tuple[str, str]] | None:
    """(raw, plain) of the non-empty rows between the LAST two full-width
    rules, where Claude draws its composer, or None when there is no such
    pair, more than `footer_reach` rows follow it, Claude's exit line follows
    it, or part of a dialog shows above it (_dialog_above)."""
    raw_rows = content.rstrip("\n").split("\n")
    plain_rows = [_strip_escapes(row).rstrip() for row in raw_rows]
    rules = [i for i, row in enumerate(plain_rows) if DIALOG_RULE_RE.match(row)]
    if not rules:
        return None
    bottom = rules[-1]
    top = next((i for i in reversed(range(bottom)) if _COMPOSER_TOP_RE.match(plain_rows[i])), None)
    if top is None:
        return None
    footer = [row for row in plain_rows[bottom + 1 :] if row.strip()]
    if len(footer) > footer_reach or any(_EXITED_RE.match(row) for row in footer):
        return None
    if _dialog_above(plain_rows[:top]):
        return None
    return [(raw_rows[i], plain_rows[i]) for i in range(top + 1, bottom) if plain_rows[i].strip()]


def _detect_composer(content: str) -> bool:
    """Whether Claude's composer, `❯` (or shell mode's `!`) between its two
    full-width rules, sits at the bottom of the pane. Claude's own chrome,
    unlike the status line, which is whatever the user's statusLine script
    prints: STATUS_RE knows one script's, so every other pane read as a
    shell (THI-434)."""
    rows = _composer_rows(content, _DETECT_FOOTER_REACH)
    return bool(rows) and rows[0][1].lstrip().startswith(("❯", "!"))


def _last_rule_index(lines: list[str]) -> int:
    """Index of the last full-width rule in `lines`, the composer's lower one
    when _detect_composer found it; -1 when there is none."""
    for i in range(len(lines) - 1, -1, -1):
        if DIALOG_RULE_RE.match(_strip_escapes(lines[i])):
            return i
    return -1


def composer_ready(content: str) -> bool:
    """Whether Claude's composer sits at a plain, empty `❯` prompt, so a
    paste + Enter (POST /api/pane/reply) is a new message and nothing else.

    Decided positively, so anything unrecognised is "not ready": the composer
    is the one non-empty row between the LAST two full-width rules, with no
    more than a footer's worth of rows (mode hint, warnings, a status line)
    below them; it opens with `❯`, and after it there's nothing or only
    Claude's dim/grey suggestion (_input_is_ghost, on the -e capture). Shell
    mode swaps the `❯` for `!` (2.1.280), and Enter then RUNS the text as a
    shell command; a fresh session has no scrollback `❯` row for
    _detect_pending_input to fall through to, so that scan can't be the
    gate. A draft, a pasted block, a multi-row composer, a dialog (one rule,
    no composer) or any other leading glyph all read as not ready: a false
    "not ready" costs a 409 that reveals the pane."""
    rows = _composer_rows(content)
    if not rows or len(rows) != 1:
        return False
    raw, plain = rows[0]
    row = plain.lstrip()
    if not row.startswith("❯"):
        return False
    return not row[1:].strip() or _input_is_ghost(raw)


def composer_holds(content: str, text: str) -> bool:
    """Whether the composer now holds `text`, pasted a moment ago into the
    empty composer composer_ready saw: the second look POST /api/pane/reply
    takes before its Enter, because a dialog that renders in between takes
    focus, and Enter on it confirms `1. Yes`.

    The composer rows (as composer_ready finds them) must open with `❯`,
    drawn as typed rather than as a suggestion, and read, whitespace
    squashed since a long reply wraps, as the start of `text`; or show the
    `[Pasted text #N …]` placeholder Claude collapses a long paste into,
    which can only be this paste in a composer that was empty. A dialog has
    no composer rows, and a shell-mode `!` row isn't `❯`."""
    rows = _composer_rows(content)
    if not rows:
        return False
    raw, plain = rows[0]
    head = plain.lstrip()
    if not head.startswith("❯") or _input_is_ghost(raw):
        return False
    shown = head[1:] + "".join(row for _raw, row in rows[1:])
    if _PASTE_PLACEHOLDER_RE.match(shown.strip()):
        return True
    want = _squash(text)[:_HOLDS_PREFIX]
    return bool(want) and _squash(shown).startswith(want)


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


# Claude draws hints like `new task? /clear to save 433.4k tokens` flush right,
# just above the composer. A reply's prose is indented 2 under its ⏺, a list a
# few more.
_RIGHT_ALIGNED_INDENT = 20


def _is_turn_chrome(line: str) -> bool:
    """True for the turn's own lines that aren't the reply: its `✻ Brewed for
    42s · done 1:27 PM` or `✻ Waiting for 1 background agent` line, a `⎿` tool
    result or tip, a right-aligned hint. Not in _is_chrome_line: the asked-
    question check reads the line above the `✻` indicator through it."""
    s = line.strip()
    if s.startswith(("✻", "⎿")) or IDLE_INDICATOR_RE.match(line):
        return True
    return len(line) - len(line.lstrip()) >= _RIGHT_ALIGNED_INDENT


# A collapsed tool group (`⏺ Read 3 files (ctrl+o to expand)`) or an MCP call
# (`⏺ periscope - notify (MCP)(kind: …)`): tool headers TOOL_CALL_RE misses.
_TOOL_HEADER_MARKS = ("(ctrl+o to expand)", "(MCP)(")


def _last_meaningful_line(rows: list[str]) -> str:
    """What a done banner says when the transcript can't (THI-416): the newest
    reply on screen from its start, its first paragraph's wrapped rows joined.
    Its last row was the banner once, half a sentence ("that use it would
    fail."). "" when the newest ⏺ is a tool call: the turn ended without words.
    With no ⏺ on screen, the closest line from the bottom that isn't chrome or
    the turn's own (_is_turn_chrome). `rows` keeps blank rows: they end the
    paragraph.
    """
    for i in range(len(rows) - 1, -1, -1):
        s = rows[i].strip()
        if not s.startswith("⏺"):
            continue
        if TOOL_CALL_RE.match(s) or any(mark in s for mark in _TOOL_HEADER_MARKS):
            return ""
        # Claude wraps a reply itself, each continuation row indented under ⏺.
        para = [s.removeprefix("⏺").strip()]
        for row in rows[i + 1 :]:
            if not row.startswith("  ") or not row.strip() or _is_chrome_line(row) or _is_turn_chrome(row):
                break
            para.append(row.strip())
        return " ".join(para)[:300]
    for row in reversed(rows):
        s = row.strip()
        if s and not _is_chrome_line(row) and not _is_turn_chrome(row):
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
    for line in lines[idle_idx + 1 :]:
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
    """True when the most recent `⎿` tool-result line is an API Error,
    or a `⏺ API Error:` line sits below it.

    Claude Code renders rate-limit / transport failures as a normal tool
    result whose body begins "API Error:" (or, for a stream cut, as an
    assistant `⏺ API Error:` line) and then STOPS — no auto-retry;
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
    agent: str | None,
    needs_input: bool,
    asked_question: bool,
    spinner: str | None,
) -> str:
    """State priority: needs-input wins over working (a spinner glyph can
    linger in scrollback above the dialog), working wins over idle. `idle`
    is the parse-level neutral state — /api/state may refine it to `done`
    when there's an unacknowledged completion stamp.
    """
    if not agent:
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
    composer = _detect_composer(content)
    agent = "claude" if status is not None or composer else None
    # Under the composer's lower rule is footer: mode hints, warnings and the
    # statusLine script's output, which can read as a spinner (a script's
    # line truncated to `🤖 op…`) or as the last reply. Content is above it.
    body = lines[: _last_rule_index(lines) + 1] if composer else lines

    spinner = _detect_spinner(body)

    # A permission dialog's footer ("Esc to cancel · Tab to amend" in 2.1.280)
    # is outside NEEDS_INPUT_FOOTER_RE's family, so it's read by its own shape
    # (THI-435); the loose "Esc to cancel" dialog_open takes also fires on toasts.
    needs_input = _detect_needs_input(lines) or parse_permission_dialog(content) is not None
    # The dialog footer is Claude-specific UI; seeing it means the pane IS
    # Claude even if the composer and status line didn't show (the dialog
    # occupies the bottom rows where they normally live).
    if needs_input:
        agent = "claude"

    # `❯ 1.` is the dialog's selection line, not user typing — skip
    # pending-input detection entirely when a dialog is open.
    pending_input = None if needs_input else _detect_pending_input(raw_rows, plain_rows)

    recap = _extract_recap(lines)
    # The same cut over every row: blank rows end the reply's first paragraph.
    last_line = _last_meaningful_line(plain_rows[: _last_rule_index(plain_rows) + 1] if composer else plain_rows)

    asked_question = _detect_asked_question(body) if agent == "claude" and not needs_input else False
    api_error = _detect_api_error(body) if agent == "claude" else False

    state = _resolve_state(agent, needs_input, asked_question, spinner)

    return {
        "agent": agent,
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

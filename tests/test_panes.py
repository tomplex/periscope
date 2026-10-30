"""Pane introspection: regexes + parse_pane + smoothing + focus tracking
+ list_windows.

The four datasets (REGEX_CASES, PARSE_CASES, GHOST_CASES, LAST_LINE_CASES)
were originally maintained in `test_parse_pane.py` at the repo root as a
flat case matrix; Peel 5 folded them into pytest here. New variations
should be appended to the matching dataset rather than dropped into a
new test function — the failure-collection runners (one per dataset)
preserve the "report every failing case in one go" property the original
file had.
"""

import hashlib
import re
import textwrap
from pathlib import Path

import pytest

import pane_permission_hook as hook
from periscope.panes import (
    ACTIVE_OP_RE,
    AGENT_STICKY_S,
    SPINNER_GRACE_S,
    SPINNER_RE,
    _acted_at,
    _active_per_session,
    _agent_last_seen,
    _focused_at,
    _spinner_last_seen,
    composer_holds,
    composer_ready,
    dialog_matches,
    dialog_open,
    list_windows,
    note_action,
    note_focus,
    parse_pane,
    parse_permission_dialog,
    permission_dialog,
    smooth_agent,
    smooth_spinner,
    update_focus_from_windows,
)

# ── Regex-level cases ───────────────────────────────────────────────────
# Each row: (tag, line, should_match_either_regex).
#   A* = active markers (should match)
#   N* = negative cases (must not match)

REGEX_CASES: list[tuple[str, str, bool]] = [
    # === Active markers — ellipsis form, single- and multi-word phrases ===
    ("A1", "✻ Envisioning…", True),
    ("A2", "✶ Bootstrapping…", True),
    ("A3", "✳ Wiring resolve_pids into endpoints…", True),
    ("A4", "✳ Wiring resolve_pids into endpoints…(910m 2 · ↓ 14.78 tokens · thought for 10s)", True),
    # === Active markers — parens form with ↑ or ↓ arrow ===
    ("A5", "● Bootstrapping packages (7m 29s · ↑ 22.1k tokens · thought for 2s)", True),
    ("A6", "● Researching codebase (3m 12s · ↑ 5.2k tokens · thought for 8s)", True),
    ("A7", "  ●  Bootstrapping packages (7m · ↑ 1k tokens · ...)", True),
    ("A8", "✶ Combining results from agents (1m · ↓ 800 tokens · thought for 3s)", True),
    # === Tool-call headers (ellipsis is INSIDE parens — not active) ===
    ("N1", "⏺ Bash(cd /Users/tom/… --skip-glo…)", False),
    ("N2", "⏺ Glob(some/path/…)", False),
    # === Completed operation lines (parens but no arrow) ===
    ("N3", "  ⎿  Done (18 tool uses · 117.7k tokens · 2m 37s)", False),
    ("N4", "   Done (5 tool uses · 25.5k tokens · 21s)", False),
    # === Agent headers (parens but no token meter inside) ===
    ("N5", "● Agent(Combined review Task 2.2) Haiku 4.5", False),
    ("N6", "● Agent(Implement Task 2.3) Haiku 4.5", False),
    # === Other prose-in-parens that isn't an activity meter ===
    ("N7", "● Found 2 new diagnostic issues in 1 file (ctrl+o to expand)", False),
    # === Pane status line (has arrows but no parens-around-tokens) ===
    ("N8", "  32%  |  ↑130k ↓74  |  $34.87  |  Opus 4.7 (1M context)", False),
    # === Prose embedding the marker mid-sentence (anchoring guards) ===
    ("N9", "That literal ● Bootstrapping packages (... ↑ 22.1k tokens · thought for 2s) line ended up...", False),
    ("N10", "  Recap: the line ●  Bootstrapping packages (... ↑Nk tokens) was caught", False),
    (
        "N11",
        "OK   active=True  verb=Bootstrapping  ●  Bootstrapping packages (7m 29s · ↑ 22.1k tokens · thought for 2s)",
        False,
    ),
    # === Subtask / tool-output lines without a leading glyph ===
    ("N12", "     Running…", False),
    ("N13", "  ⎿  ◼ Task 2.3: Wire resolve_pids into /api/state", False),
    ("N14", "  ⎿  ◻ Task 2.4: Post-poll GC of stale id-only entries", False),
    ("N15", "      … +5 pending, 7 completed", False),
    # === Past-tense thought indicators ===
    ("N16", "✻ Brewed for 31s", False),
    ("N17", "✻ Brewed for 7m 16s", False),
]


# ── parse_pane end-to-end cases ─────────────────────────────────────────
# Each row: (tag, pane_content, expected_state, expected_spinner).
# expected_spinner=None means "spinner must be None"; pass a string to assert
# the verb extraction landed on that exact word.

STATUS_LINE = "  17% | ↑30k ↓74 | $0.50 | Opus 4.7 (1M context)"
# THI-434: the status line is whatever the user's statusLine script prints, and
# STATUS_RE only knows the one above. This is another script's, from a live pane.
CUSTOM_STATUS_LINE = (
    "  📁 periscope 🌿 main ?3 ✅ CI:pass  |  🧠█░░░░░░░░░ 13% 📨 2 📤 381"
    " | session: 134.0k in / 381 out 💰 $2.33 🤖 opus ⚡xhigh"
)
# The full-width rules Claude draws above and below its composer.
RULE = "─" * 60

PARSE_CASES: list[tuple[str, str, str, str | None]] = [
    # The screenshot Tom hit: multi-word phrase + trailing `…` + ↓-arrow
    # parens with garbled timing. Used to read as "waiting" because neither
    # old regex matched. Now should detect via SPINNER_RE.
    (
        "screenshot",
        textwrap.dedent(f"""\
        Agent(Combined review Task 2.2) Haiku 4.5
          ⎿  Done (18 tool uses · 117.7k tokens · 2m 37s)
          (ctrl+o to expand)

          Agent(Implement Task 2.3) Haiku 4.5
          ⎿  Bash(sed -n '1626,1720p' server.py)
             Running…
             Bash(sed -n '940,945p' server.py)
             Running…
             … +10 tool uses (ctrl+o to expand)

        ✳ Wiring resolve_pids into endpoints…(910m 2 · ↓ 14.78 tokens · thought for 10s)
          ⎿  ◼ Task 2.3: Wire resolve_pids into endpoints
             ◻ Task 2.4: Post-poll GC
              … +5 pending, 7 completed

        ❯
        {STATUS_LINE}
    """),
        "working",
        "Wiring",
    ),
    # Idle Claude pane — status line present, no active marker.
    (
        "idle",
        textwrap.dedent(f"""\
        Some prior assistant output.

        ✻ Brewed for 7m 16s

        ❯
        {STATUS_LINE}
    """),
        "idle",
        None,
    ),
    # No status line at all = shell pane.
    (
        "shell",
        textwrap.dedent("""\
        $ ls
        foo.py bar.py
        $ cat foo.py
        print('hello')
        $
    """),
        "shell",
        None,
    ),
    # Prose embedding the active marker mid-sentence — was the self-quote
    # false-positive that took two iterations to fix via .match() anchoring.
    (
        "prose-embed",
        textwrap.dedent(f"""\
        The previous fix added ACTIVE_OP_RE to catch the marker.

        That literal ● Bootstrapping packages (... ↑ 22.1k tokens · thought for 2s) line ended up in scrollback.

        ✻ Brewed for 3s

        ❯
        {STATUS_LINE}
    """),
        "idle",
        None,
    ),
    # Old single-word ellipsis form (the original spinner case).
    (
        "old-ellipsis",
        textwrap.dedent(f"""\
        ✻ Envisioning…

        ❯
        {STATUS_LINE}
    """),
        "working",
        "Envisioning",
    ),
    # Parens form with ↑ arrow (the form added in iteration 2).
    (
        "parens-up",
        textwrap.dedent(f"""\
        ● Researching codebase (3m 12s · ↑ 5.2k tokens · thought for 8s)
          tool calls below the marker

        ❯
        {STATUS_LINE}
    """),
        "working",
        "Researching",
    ),
    # IDLE_INDICATOR_RE acts as a positional "stop searching" boundary.
    # Code-block marker quotes from the assistant's own response sit ABOVE
    # the past-tense indicator and must not false-positive as working.
    # (Iteration 6 — the bug that showed `working 3…` because a stale phrase
    # starting with a digit got picked up by the old first-word fallback.)
    (
        "idle-with-code-block-marker",
        textwrap.dedent(f"""\
        Look at these Claude TUI active marker examples:

        ✻ Envisioning…
        ✶ Bootstrapping…
        ✳ Wiring resolve_pids into endpoints…(910m 2 · ↓ 14.78 tokens · thought for 10s)
        ● Bootstrapping packages (7m 29s · ↑ 22.1k tokens · thought for 2s)

        normal-0 jumped #2 → #1, e151d2dc jumped #5 → #3, both real semantic wins.

        ✻ Cooked for 3m 42s

        ❯
        {STATUS_LINE}
    """),
        "idle",
        None,
    ),
    # Past-tense lines in the bottom rows for each verb form we know about.
    (
        "idle-brewed",
        textwrap.dedent(f"""\
        ● Some prior response.
        ✻ Brewed for 31s
        ❯
        {STATUS_LINE}
    """),
        "idle",
        None,
    ),
    (
        "idle-thought",
        textwrap.dedent(f"""\
        ● Some prior response.
        ✻ Thought for 10s
        ❯
        {STATUS_LINE}
    """),
        "idle",
        None,
    ),
    (
        "idle-pondered",
        textwrap.dedent(f"""\
        ● Some prior response.
        ✻ Pondered for 2m 15s
        ❯
        {STATUS_LINE}
    """),
        "idle",
        None,
    ),
    # Active marker present BELOW an older past-tense line from a prior
    # turn — iteration hits the active marker first (it's closer to the
    # bottom), so we correctly report working.
    (
        "active-after-old-brewed",
        textwrap.dedent(f"""\
        First turn response.
        ✻ Brewed for 30s

        Second turn started.
        ✳ Working on the next task…
          ⎿  subtask 1
             subtask 2

        ❯
        {STATUS_LINE}
    """),
        "working",
        "Working",
    ),
    # Verb fallback: an active marker with a digit-starting phrase used to
    # surface `3…` via the first-word fallback. Now falls back to "working".
    (
        "verb-fallback",
        textwrap.dedent(f"""\
        ✻ 3 items remaining…

        ❯
        {STATUS_LINE}
    """),
        "working",
        "working",
    ),
    # Question-mark needs-input: Claude's last reply ends with `?`, no dialog
    # open, no active spinner. Should promote idle → needs-input so the card
    # shows the pane is awaiting a human reply.
    (
        "question-mark-trailing",
        textwrap.dedent(f"""\
        ⏺ I checked the codebase. Should I delete the obsolete helper?

        ✻ Brewed for 3s

        ❯
        {STATUS_LINE}
    """),
        "needs-input",
        None,
    ),
    # Question mark with the question on a wrapped continuation line, last
    # visible line is the `?`-terminated sentence (not the leading prose).
    (
        "question-mark-continuation",
        textwrap.dedent(f"""\
        ⏺ Two paths here:
          - keep it as-is
          - rewrite the function
          Which one do you prefer?

        ✻ Thought for 8s

        ❯
        {STATUS_LINE}
    """),
        "needs-input",
        None,
    ),
    # Question mark NOT in the last line — `?` appears in the middle of the
    # reply but the reply ends with a statement. Must NOT trip needs-input.
    (
        "question-mark-not-trailing",
        textwrap.dedent(f"""\
        ⏺ Did you know X? Anyway, here is the answer: foo.

        ✻ Brewed for 2s

        ❯
        {STATUS_LINE}
    """),
        "idle",
        None,
    ),
    # Tool-call header below the question — Claude proceeded to act, so the
    # question is no longer the most recent line above the prompt. The last
    # content line (the tool-call header) doesn't end with `?`.
    (
        "question-followed-by-tool-call",
        textwrap.dedent(f"""\
        ⏺ Should I delete this file?

        ⏺ Bash(rm file.py)
          ⎿  removed file.py

        ✻ Brewed for 1s

        ❯
        {STATUS_LINE}
    """),
        "idle",
        None,
    ),
    # Shell panes: a `?` in normal output (man page snippet, error text)
    # must NOT promote to needs-input — only Claude panes get this treatment.
    (
        "question-mark-shell",
        textwrap.dedent("""\
        $ man bash | head -3
        What are you looking for?
        $
    """),
        "shell",
        None,
    ),
    # Stale indicator: Claude asked a question, the user replied, Claude is
    # now mid-new-turn (Bash tool call running). The OLD past-tense indicator
    # is still the latest visible one — but a submitted `❯ <text>` reply and
    # tool-call lines below it prove the conversation moved past the
    # question. Must NOT flag needs-input.
    (
        "question-stale-indicator-user-replied",
        textwrap.dedent(f"""\
        ⏺ Ready to execute. Same subagent-driven flow?

        ✻ Brewed for 11m 1s

        ❯ execute it, subagent-driven

        ⏺ Bash(git -C /Users/tom/dev/periscope status --short)
          ⎿  ?? .private-journal/
             … +3 lines (ctrl+o to expand)

        ❯
        {STATUS_LINE}
    """),
        "idle",
        None,
    ),
    # TodoWrite list rendered below the past-tense indicator. The actual
    # question lives ABOVE the indicator; bottom-up scanning without anchor
    # would land on the last todo row (ends with `)`) and miss the `?`.
    # This is the real-world failure case from `lookup-undermatch-audit`.
    (
        "question-with-todo-below-indicator",
        textwrap.dedent(f"""\
        ⏺ I have enough to present the design. Walking through it in sections.

          1. Scope & architecture
          - In scope: cache write path, read path, version bump
          - Out of scope: refresh cadence (separate spec)

          Reaction to the scope before I move to the next section?

        ✻ Crunched for 8m 51s

          5 tasks (1 done, 1 in progress, 3 open)
          ✔ Create worktree for lookup-substrate redesign
          ◼ Brainstorm lookup layer redesign
          ◻ Write spec for lookup layer redesign
          ◻ Run spec-reviewer + share via LGTM
          ◻ Spec lookup cache refresh cadence (separate)

        ❯
        {STATUS_LINE}
    """),
        "needs-input",
        None,
    ),
    # THI-434: a status line STATUS_RE can't read. The composer between
    # Claude's two rules is Claude's own, whatever the statusLine script.
    (
        "custom-statusline-idle",
        textwrap.dedent(f"""\
        ⏺ Done.

        ✻ Brewed for 5s

        {RULE}
        ❯
        {RULE}
        {CUSTOM_STATUS_LINE}
          ⏵⏵ auto mode on (shift+tab to cycle) · ← 2 agents
    """),
        "idle",
        None,
    ),
    (
        "custom-statusline-working",
        textwrap.dedent(f"""\
        ✽ Schlepping… (21s · ↓ 1.3k tokens)
        {RULE}
        ❯
        {RULE}
        {CUSTOM_STATUS_LINE}
          ⏵⏵ auto mode on (shift+tab to cycle) · ← 2 agents
    """),
        "working",
        "Schlepping",
    ),
    # Background agents list themselves under the status line, one row each,
    # so the footer outgrows the 6 rows a reply allows (live pane, 3 agents).
    (
        "custom-statusline-background-agents",
        textwrap.dedent(f"""\
        ✻ Waiting for 3 background agents to finish
        {RULE}
        ❯
        {RULE}
        {CUSTOM_STATUS_LINE}
          ⏵⏵ auto mode on (shift+tab to cycle) · ← 2 agents
          ⏺ main
          ◯ general-purpose  Checking victory pull_data.py bq_env
          ◯ general-purpose  Updating contrast tokens in head.html
          ◯ general-purpose  Testing compact_json script escaping
          ⧉  wusa-artifact-weekly
    """),
        "idle",
        None,
    ),
    # What Claude leaves on exit (a live 2.1.280 pane's scrollback): its last
    # frame, composer and all, then the resume hint and the shell's prompt.
    (
        "exited-leaves-its-composer",
        textwrap.dedent(f"""\
        ✻ Cooked for 11s · done 1:58 PM

        {RULE}
        ❯
        {RULE}
          Press Ctrl-C again to exit

        Resume this session with:
        claude --resume 66bbba62-eef3-4417-bd6d-7f2cd292c5b3
        ➜  periscope git:(main) ✗
    """),
        "shell",
        None,
    ),
]


# ── Ghost-text filter cases ─────────────────────────────────────────────
# Each row: (tag, content_with_ansi, expected_pending_input).
# These fixtures embed SGR escapes (`\x1b[38;5;Nm`) — what tmux capture-pane
# -e emits. parse_pane drops `pending_input` only when every glyph after the
# prompt is drawn like Claude Code's ghost suggestion: dim, bright-black, or
# dark grey (panes._input_is_ghost). Anything else counts as typed.

GHOST_CASES: list[tuple[str, str, str | None]] = [
    # Ghost text only — single fg color across the prompt line. The
    # visible text is Claude Code's suggestion, not user input.
    ("ghost-leave-it", f"\x1b[38;5;239m❯ leave it\x1b[39m\n{STATUS_LINE}\n", None),
    # Real input — prefix is dim grey, typed text switches to bright fg.
    ("real-input", f"\x1b[38;5;239m❯ \x1b[38;5;231mhello world\x1b[39m\n{STATUS_LINE}\n", "hello world"),
    # Empty prompt — no text after `❯ `, no pending input regardless of color.
    ("empty-prompt", f"\x1b[38;5;239m❯ \x1b[39m\n{STATUS_LINE}\n", None),
    # Backward-compat: a fixture with no SGR escapes at all (existing tests
    # don't include them). Trust the visible text — can't distinguish ghost
    # from real without color info.
    ("no-ansi-trust-visible", f"❯ some text the user typed\n{STATUS_LINE}\n", "some text the user typed"),
    # Realistic shape from periscope:4 capture: bg color escape mixed in
    # with fg escapes. Only fg codes count toward the distinct-color check.
    (
        "real-with-bg-color",
        f"\x1b[38;5;239m\x1b[48;5;237m❯ \x1b[38;5;231mreal user input\x1b[39m\n{STATUS_LINE}\n",
        "real user input",
    ),
    # Claude Code 2.1.280: a typed draft shares the prompt glyph's DEFAULT fg
    # (39), with a no-break space after ❯. The old "≥2 fg colors" rule read
    # this as ghost text, and the per-pane /effort picker pasted onto the
    # draft and submitted it. Ghost text is the one that's dim or grey.
    ("default-fg-draft", f"\x1b[39m❯\xa0half a thought\n{STATUS_LINE}\n", "half a thought"),
    # Dim (SGR 2) suggestion after the prompt — ghost, not input.
    ("dim-ghost", f"\x1b[39m❯\xa0\x1b[2mtry the tests\x1b[22m\n{STATUS_LINE}\n", None),
    # Bright-black (90) suggestion — ghost too.
    ("bright-black-ghost", f"\x1b[90m❯ try the tests\x1b[39m\n{STATUS_LINE}\n", None),
    # A paste placeholder is drawn grey like a suggestion but IS the draft,
    # whether the user typed on after it or not. Deciding from the first
    # glyph alone would read either as ghost and let a slash command be
    # appended to the paste and submitted.
    (
        "paste-placeholder-then-typed",
        f"\x1b[39m❯\xa0\x1b[38;5;240m[Pasted text #1 +40 lines]\x1b[39m and fix it\n{STATUS_LINE}\n",
        "[Pasted text #1 +40 lines] and fix it",
    ),
    (
        "paste-placeholder-alone",
        f"\x1b[39m❯\xa0\x1b[38;5;240m[Pasted text #1 +40 lines]\x1b[39m\n{STATUS_LINE}\n",
        "[Pasted text #1 +40 lines]",
    ),
    # Grey prefix then default-fg text, with no placeholder: the ghost rule is
    # all-or-nothing, so one typed glyph makes it a draft.
    ("grey-then-default-fg", f"\x1b[39m❯\xa0\x1b[38;5;240mdraft\x1b[39m continues\n{STATUS_LINE}\n", "draft continues"),
    # A mid-grey RGB suggestion is ghost text...
    ("rgb-grey-ghost", f"\x1b[39m❯\xa0\x1b[38;2;102;102;102mtry the tests\x1b[39m\n{STATUS_LINE}\n", None),
    # ...but a near-black one is a draft in a dark text colour, whether RGB or
    # the 256-color ramp's dark end: below the grey floor counts as typed.
    ("rgb-black-draft", f"\x1b[39m❯\xa0\x1b[38;2;0;0;0mhalf a thought\x1b[39m\n{STATUS_LINE}\n", "half a thought"),
    (
        "rgb-dark-grey-draft",
        f"\x1b[39m❯\xa0\x1b[38;2;40;40;40mhalf a thought\x1b[39m\n{STATUS_LINE}\n",
        "half a thought",
    ),
    ("256-near-black-draft", f"\x1b[39m❯\xa0\x1b[38;5;233mhalf a thought\x1b[39m\n{STATUS_LINE}\n", "half a thought"),
]


# ── last_line filter cases ─────────────────────────────────────────────
# Each row: (tag, content, expected_last_line). These verify the card
# snippet doesn't surface TUI chrome (auto-mode footer, title bar, etc.).

LAST_LINE_CASES: list[tuple[str, str, str]] = [
    # Auto-mode footer at the bottom — must be filtered. Falls through to
    # the actual response content above.
    (
        "filter-auto-mode",
        textwrap.dedent(f"""\
        Some recent response text from the assistant.
        ✻ Brewed for 31s
        ❯
        ─────────────────────
        {STATUS_LINE}
        ⏵⏵ auto mode on (shift+tab to cycle)
    """),
        "✻ Brewed for 31s",
    ),
    # Auto-mode footer with the "← for agents" suffix — same filter.
    (
        "filter-auto-mode-agents",
        textwrap.dedent(f"""\
        Some recent response text.
        ✻ Cooked for 1m 15s
        ❯
        {STATUS_LINE}
        ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
    """),
        "✻ Cooked for 1m 15s",
    ),
    # Title bar at top + auto-mode at bottom — both filtered.
    (
        "filter-title-bar",
        textwrap.dedent(f"""\
        periscope | main | +3 -26 4m * | github.com/tomplex/periscope/compare/master...main
        Assistant said something useful here.
        ✻ Brewed for 5s
        ❯
        {STATUS_LINE}
        ⏵⏵ auto mode on (shift+tab to cycle)
    """),
        "✻ Brewed for 5s",
    ),
    # Active spinner line shouldn't surface as snippet (duplicate of state
    # label). Falls through to whatever's below the spinner.
    (
        "filter-active-spinner",
        textwrap.dedent(f"""\
        ✳ Wiring resolve_pids into endpoints…
          ⎿  ◼ Task 2.3: subtask line
             ◻ Task 2.4: another subtask
        ❯
        {STATUS_LINE}
        ⏵⏵ auto mode on (shift+tab to cycle)
    """),
        "◻ Task 2.4: another subtask",
    ),
]


# ── API error cases ─────────────────────────────────────────────────────
# Each row: (tag, content, expected_api_error).
# Claude Code renders rate-limit / transport failures as a normal `⎿` tool
# result whose body starts with "API Error:". We flag panes whose MOST
# RECENT tool result is an error so the dashboard can show a stuck-on-
# retry pane at a glance. A subsequent successful tool result clears it.

API_ERROR_CASES: list[tuple[str, str, bool]] = [
    # The screenshot Tom hit: three consecutive rate-limit hits, the user
    # repeatedly typed `keep going`, and the bottom shows Claude resuming.
    # The latest tool result above the resumed turn is still the API
    # error → flag should be on so the card surfaces the stuck retry.
    (
        "rate-limited-stuck",
        textwrap.dedent(f"""\
        Reshaping the query — payload columns are huge.

          Called fdy 5 times (ctrl+o to expand)
          ⎿  API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited

        ✻ Worked for 7m 22s

        ❯ keep going

          Called fdy 10 times (ctrl+o to expand)
          ⎿  API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited

        ✻ Crunched for 1m 56s

        ❯ keep going
          ⎿  API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited

        ✻ Brewed for 0s

        ❯ keep going

          Called fdy (ctrl+o to expand)

        ⏺ Skipping reminder. One last lookup — there's a closest_store_by_address variant I haven't traced yet.
        {STATUS_LINE}
    """),
        True,
    ),
    # API Error appeared, but Claude retried and a fresh tool result
    # succeeded after it. Bottom-up scan hits the success first → flag off.
    (
        "recovered-after-error",
        textwrap.dedent(f"""\
        ⏺ Bash(curl https://api.example.com/data)
          ⎿  API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited

        ⏺ Bash(curl https://api.example.com/data)
          ⎿  {{"ok": true}}

        ✻ Brewed for 3s

        ❯
        {STATUS_LINE}
    """),
        False,
    ),
    # Vanilla idle pane, no error anywhere → flag off (regression guard).
    (
        "no-error",
        textwrap.dedent(f"""\
        ⏺ Some normal response.

        ✻ Brewed for 5s

        ❯
        {STATUS_LINE}
    """),
        False,
    ),
    # Shell pane that happens to mention "API Error" in prose must NOT
    # trip the flag — only Claude panes get the treatment.
    (
        "shell-mentions-api-error",
        textwrap.dedent("""\
        $ grep -r "API Error" .
        ./logs/yesterday.log: ⎿  API Error: foo
        $
    """),
        False,
    ),
    # API error mid-buffer with no later `⎿` tool results — Claude is
    # presumably retrying / waiting. Flag on.
    (
        "error-then-only-text",
        textwrap.dedent(f"""\
          ⎿  API Error: Server is temporarily limiting requests · Rate limited

        ⏺ Retrying that lookup…

        ✻ Brewed for 1s

        ❯
        {STATUS_LINE}
    """),
        True,
    ),
    # THI-436: a stream cut mid-response renders as an assistant `⏺` line,
    # not a `⎿` tool result — and a successful tool result sits above it,
    # which the `⎿`-only scan stopped at and read as "recovered".
    (
        "assistant-glyph-stream-cut",
        textwrap.dedent(f"""\
        ⏺ Bash(uv run pytest -q tests/test_panes.py)
          ⎿  412 passed in 3.1s

        ⏺ API Error: The response stopped arriving. The response above may be incomplete.

        ✻ Brewed for 4m 37s · done 9:29 AM

        ❯
        {STATUS_LINE}
    """),
        True,
    ),
    # The same error under a status line STATUS_RE can't read (THI-434): the
    # pane must still count as Claude for the error to count at all.
    (
        "assistant-glyph-custom-statusline",
        textwrap.dedent(f"""\
        ⏺ API Error: The response stopped arriving. The response above may be incomplete.

        ✻ Brewed for 4m 37s · done 9:29 AM

        {RULE}
        ❯
        {RULE}
        {CUSTOM_STATUS_LINE}
          ⏵⏵ auto mode on (shift+tab to cycle) · ← 2 agents
    """),
        True,
    ),
    # Same error, nudged, and the resumed turn's tool call succeeded → off.
    (
        "assistant-glyph-recovered",
        textwrap.dedent(f"""\
        ⏺ API Error: The response stopped arriving. The response above may be incomplete.

        ✻ Brewed for 4m 37s · done 9:29 AM

        ❯ keep going

        ⏺ Bash(uv run pytest -q tests/test_panes.py)
          ⎿  412 passed in 3.1s

        ✻ Brewed for 6s

        ❯
        {STATUS_LINE}
    """),
        False,
    ),
]


# ── Folded runners (one test per dataset) ───────────────────────────────


def test_regex_cases():
    failures = []
    for tag, line, want in REGEX_CASES:
        s_m = SPINNER_RE.match(line)
        a_m = ACTIVE_OP_RE.match(line)
        got = bool(s_m or a_m)
        via = "SPINNER" if s_m else "ACTIVE" if a_m else "-"
        if got != want:
            failures.append(f"[{tag}] match={got} want={want} via={via}  {line[:80]}")
    assert not failures, "\n".join(failures)


def test_parse_cases():
    failures = []
    for tag, content, want_state, want_spinner in PARSE_CASES:
        result = parse_pane(content)
        ok_state = result["state"] == want_state
        ok_spinner = result["spinner"] == want_spinner
        if not (ok_state and ok_spinner):
            failures.append(
                f"[{tag}] state={result['state']!r} (want {want_state!r})  "
                f"spinner={result['spinner']!r} (want {want_spinner!r})"
            )
    assert not failures, "\n".join(failures)


def test_ghost_cases():
    failures = []
    for tag, content, want_pending in GHOST_CASES:
        result = parse_pane(content)
        got = result.get("pending_input")
        if got != want_pending:
            failures.append(f"[{tag}] pending_input={got!r} (want {want_pending!r})")
    assert not failures, "\n".join(failures)


def test_last_line_cases():
    failures = []
    for tag, content, want in LAST_LINE_CASES:
        result = parse_pane(content)
        got = result.get("last_line")
        if got != want:
            failures.append(f"[{tag}] last_line={got!r} (want {want!r})")
    assert not failures, "\n".join(failures)


def test_api_error_cases():
    failures = []
    for tag, content, want in API_ERROR_CASES:
        result = parse_pane(content)
        got = result.get("api_error")
        if got != want:
            failures.append(f"[{tag}] api_error={got!r} (want {want!r})")
    assert not failures, "\n".join(failures)


# ── Smoothing + focus tracking + list_windows ──────────────────────────


@pytest.fixture(autouse=True)
def reset_panes_state():
    """Clear in-memory pane state between tests."""
    _focused_at.clear()
    _acted_at.clear()
    _spinner_last_seen.clear()
    _agent_last_seen.clear()
    _active_per_session.clear()
    yield
    _focused_at.clear()
    _acted_at.clear()
    _spinner_last_seen.clear()
    _agent_last_seen.clear()
    _active_per_session.clear()


def test_smooth_spinner_returns_current_when_present():
    out = smooth_spinner("foo:0", "Envisioning")
    assert out == "Envisioning"
    assert "foo:0" in _spinner_last_seen


def test_smooth_spinner_returns_last_seen_within_grace_when_current_none():
    smooth_spinner("foo:0", "Envisioning")
    # Within grace window — should return the cached value.
    out = smooth_spinner("foo:0", None)
    assert out == "Envisioning"


def test_smooth_spinner_returns_none_after_grace_expires(mocker):
    mocker.patch(
        "periscope.panes.time.time",
        side_effect=[100.0, 100.0 + SPINNER_GRACE_S + 1.0],
    )
    smooth_spinner("foo:0", "Envisioning")
    out = smooth_spinner("foo:0", None)
    assert out is None


def test_smooth_agent_positive_passes_through():
    assert smooth_agent("%1", "claude") == "claude"
    assert "%1" in _agent_last_seen


def test_smooth_agent_none_after_stickiness_expires(mocker):
    mocker.patch(
        "periscope.panes.time.time",
        side_effect=[100.0, 100.0 + AGENT_STICKY_S + 1.0],
    )
    smooth_agent("%1", "claude")
    assert smooth_agent("%1", None) is None


def test_smooth_agent_sticky_within_window(mocker):
    """A momentary missed detection retains the provider."""
    mocker.patch("periscope.panes.time.time", side_effect=[100.0, 100.5])
    smooth_agent("%1", "claude")
    assert smooth_agent("%1", None) == "claude"


def test_smooth_agent_provider_swap_overwrites_stickiness():
    smooth_agent("%1", "claude")
    assert smooth_agent("%1", "codex") == "codex"
    assert smooth_agent("%1", None) == "codex"


def test_smooth_agent_isolated_by_pane_id():
    smooth_agent("%1", "claude")
    assert smooth_agent("%2", None) is None


def test_note_focus_stamps_now():
    note_focus("foo:0")
    assert _focused_at["foo:0"] > 0
    assert "foo:0" not in _acted_at  # focus alone doesn't bump acted_at


def test_note_action_stamps_acted_at():
    note_action("foo:0")
    assert _acted_at["foo:0"] > 0


def test_update_focus_from_windows_stamps_active_window():
    windows = [
        {"session": "main", "index": 0, "active": True},
        {"session": "main", "index": 1, "active": False},
    ]
    update_focus_from_windows(windows)
    assert "main:0" in _focused_at
    assert "main:1" not in _focused_at


def test_list_windows_parses_tmux_list_output(mocker):
    sample = "main\t0\tshell\t1\t/home/tom/dev/foo\t1234abcd\t%5\nmain\t1\tclaude\t0\t/home/tom/dev/bar\t1235abcd\t%6\n"
    mocker.patch("periscope.panes.tmux", return_value=sample)
    out = list_windows()
    assert len(out) == 2
    assert out[0]["session"] == "main"
    assert out[0]["index"] == 0
    assert out[0]["name"] == "shell"
    assert out[0]["active"] is True
    assert out[0]["cwd"] == "/home/tom/dev/foo"
    assert out[0]["pid_raw"] == "1234abcd"
    assert out[0]["pane_id"] == "%5"
    assert out[1]["active"] is False
    assert out[0]["pane_pid"] == ""
    assert out[0]["current_command"] == ""
    assert out[0]["opened"] == ""


def test_list_windows_appends_process_metadata(mocker):
    sample = "main\t0\tcodex\t1\t/home/tom/dev/foo\t1234abcd\t%5\t1717250000\t@9\t4242\tcodex\t1717249990 /home/tom/dev/my foo\n"
    mocker.patch("periscope.panes.tmux", return_value=sample)
    row = list_windows()[0]
    assert row["window_id"] == "@9"
    assert row["pane_pid"] == "4242"
    assert row["current_command"] == "codex"
    assert row["opened"] == "1717249990 /home/tom/dev/my foo"


def test_all_pane_ids_parses_and_dedupes(mocker):
    """list-panes -a yields every pane id (including a window's background
    panes, which list_windows collapses to just the active one)."""
    from periscope import panes

    mocker.patch("periscope.panes.tmux", return_value="%5\n%6\n%7\n")
    assert panes.all_pane_ids() == {"%5", "%6", "%7"}


def test_all_pane_ids_empty_on_blank(mocker):
    from periscope import panes

    mocker.patch("periscope.panes.tmux", return_value="")
    assert panes.all_pane_ids() == set()


def test_list_windows_parses_window_activity(mocker):
    """list_windows surfaces #{window_activity} as an int `activity` field."""
    from periscope import panes

    mocker.patch(
        "periscope.panes.tmux",
        return_value="sess\t0\twin\t1\t/cwd\tpid-abc\t%5\t1717250000\n",
    )
    rows = panes.list_windows()
    assert rows[0]["activity"] == 1717250000


def test_list_windows_activity_defaults_zero_when_absent(mocker):
    """A short row (no activity column) defaults activity to 0, not a crash."""
    from periscope import panes

    mocker.patch(
        "periscope.panes.tmux",
        return_value="sess\t0\twin\t1\t/cwd\tpid-abc\t%5\n",
    )
    rows = panes.list_windows()
    assert rows[0]["activity"] == 0


def test_update_focus_ignores_window_activity(mocker):
    """Invariant #1 regression: focus is keyed on window_active, never activity.
    Two polls where only `activity` changes (same active window) must NOT
    re-stamp focus."""
    from periscope import panes

    panes._focused_at.clear()
    panes._active_per_session.clear()
    windows_t1 = [{"session": "s", "index": 0, "active": True, "activity": 100}]
    windows_t2 = [{"session": "s", "index": 0, "active": True, "activity": 999}]
    panes.update_focus_from_windows(windows_t1)
    first = panes._focused_at["s:0"]
    panes.update_focus_from_windows(windows_t2)
    assert panes._focused_at["s:0"] == first  # activity bump did not touch focus


def test_list_windows_filters_input_ctl_session(mocker):
    sample = "main\t0\tshell\t1\t/home/tom\t\t%5\nperiscope-input\t0\tshell\t1\t/tmp\t\t%6\n"
    mocker.patch("periscope.panes.tmux", return_value=sample)
    out = list_windows()
    # periscope's internal control-mode session is scaffolding, not state.
    assert len(out) == 1
    assert out[0]["session"] == "main"


def test_drop_target_focus_pops_both_dicts():
    from periscope import panes

    panes._focused_at["sess:1"] = 100
    panes._acted_at["sess:1"] = 100
    panes._focused_at["sess:2"] = 200
    panes.drop_target_focus("sess:1")
    assert "sess:1" not in panes._focused_at
    assert "sess:1" not in panes._acted_at
    assert panes._focused_at["sess:2"] == 200  # untouched
    panes._focused_at.pop("sess:2", None)  # cleanup


# ── Permission dialogs (a banner's Allow; routes/pane_actions.py) ──
# Real captures from Claude Code 2.1.280 (`tmux capture-pane -p`), the scratch
# path replaced by /tmp/probe and padded so option 2 still wraps mid-word, as
# it did live. Allow sends `1` on dialog_matches' word, so every negative here
# is a dialog it must never approve.

_DIALOGS = Path(__file__).parent / "fixtures" / "permission_dialogs"
_RULE = "─" * 60
_FOOTER = " Esc to cancel · Tab to amend"
# bash.txt's command and description rows, which the synthetic dialogs swap.
_TIP = ' Tip: auto mode handles these prompts for you — choose "switch to auto mode" below'
_BASH_BLOCK = "   touch probe.txt\n   Create empty probe.txt file"
# An idle Claude pane: no dialog, the input box at the bottom.
_IDLE = "\n".join(
    [
        "⏺ Done — notes.md is written.",
        "",
        "✻ Brewed for 3s",
        "",
        _RULE,
        "❯ ",
        _RULE,
        "  ⏵⏵ auto mode on (shift+tab to cycle)",
    ]
)
# A reply quoting a dialog verbatim: every marker is there, but the input box
# is below it, so it isn't on screen as a dialog.
_QUOTED = "\n".join(
    [
        "⏺ The prompt read:",
        "",
        " Do you want to proceed?",
        " ❯ 1. Yes",
        "   2. No",
        "",
        _FOOTER,
        "",
        _RULE,
        "❯ ",
        _RULE,
        "  ⏵⏵ auto mode on (shift+tab to cycle)",
    ]
)
_ASK_FOOTER = "Enter to select · ↑/↓ to navigate · Esc to cancel"
# AskUserQuestion: a choice dialog, not a permission one.
_ASK = "\n".join(
    [
        _RULE,
        " ☐ Database",
        "",
        "Which database should I use?",
        "",
        "❯ 1. Postgres",
        "  2. SQLite",
        "",
        _ASK_FOOTER,
    ]
)
# AskUserQuestion asking yes/no: the rule, "Do you want to …?" and `❯ 1. Yes`
# are all there; only its footer says it's no permission dialog.
_ASK_YES = "\n".join(
    [
        "⏺ Bash(npm publish)",
        "  ⎿  Interrupted",
        _RULE,
        " ☐ Publish",
        " Do you want to run npm publish now?",
        " ❯ 1. Yes",
        "   2. No",
        "",
        _ASK_FOOTER,
    ]
)


def _dialog(name: str) -> str:
    return (_DIALOGS / name).read_text()


def _bash_screen(block: str) -> str:
    """bash.txt with its command and description rows replaced by `block`."""
    return _dialog("bash.txt").replace(_BASH_BLOCK, block)


def _write_screen(shown: str, preview: str = "  1 hello") -> str:
    """write.txt asking about `shown` (its path row and question), with
    `preview` for its one preview row."""
    return (
        _dialog("write.txt")
        .replace(" notes.md\n╌", f" {shown}\n╌")
        .replace("create notes.md?", f"create {shown}?")
        .replace("  1 hello", preview)
    )


def _sha(squashed: str) -> str:
    return "sha256:" + hashlib.sha256(squashed.encode()).hexdigest()


def _bash_key(command: str, description: str = "") -> str:
    """A Bash request's key as the hook stores it (pane_permission_hook.
    match_key; the agreement tests at the end pin the two): a digest of the
    command and description, whitespace squashed out."""
    return _sha(re.sub(r"\s+", "", command) + re.sub(r"\s+", "", description))


# The request bash_requires_approval.txt shows (its command as Claude passed it).
_PY_COMMAND = 'python3 -c "open(\\"py.txt\\",\\"w\\").close()"'
_PY_DESCRIPTION = "Create empty py.txt file with Python"

# Each capture with the key of the request it shows.
_FIXTURE_KEYS = [
    ("bash.txt", "Bash", _sha("touchprobe.txtCreateemptyprobe.txtfile")),
    # The same request in a 70-column pane, its Tip wrapped onto a second row.
    ("bash_narrow.txt", "Bash", _sha("touchprobe.txtCreateemptyprobe.txtfile")),
    ("bash_multiline.txt", "Bash", _sha("toucha.txttouchb.txtCreatetwofiles")),
    # `python3 -c …` gets a "This command requires approval" row under its
    # description, which is chrome, not part of the request.
    ("bash_requires_approval.txt", "Bash", _bash_key(_PY_COMMAND, _PY_DESCRIPTION)),
    ("write.txt", "Write", "/tmp/probe/notes.md"),
    ("edit.txt", "Edit", "/tmp/probe/notes.md"),
]


@pytest.mark.parametrize(
    ("name", "header", "body", "question", "shown_path"),
    [
        (
            "bash.txt",
            "Bash command",
            [_TIP, "   touch probe.txt", "   Create empty probe.txt file"],
            "Do you want to proceed?",
            None,
        ),
        (
            "bash_narrow.txt",
            "Bash command",
            [_TIP[:65], " " + _TIP[66:], "   touch probe.txt", "   Create empty probe.txt file"],
            "Do you want to proceed?",
            None,
        ),
        (
            "bash_multiline.txt",
            "Bash command",
            [_TIP, "   │ touch a.txt", "   │ touch b.txt", "   Create two files"],
            "Do you want to proceed?",
            None,
        ),
        (
            "bash_requires_approval.txt",
            "Bash command",
            [_TIP, "   " + _PY_COMMAND, "   " + _PY_DESCRIPTION, " This command requires approval"],
            "Do you want to proceed?",
            None,
        ),
        (
            "write.txt",
            "Create file",
            [" notes.md", "╌" * 110, "  1 hello", "╌" * 110],
            "Do you want to create notes.md?",
            "notes.md",
        ),
        (
            "edit.txt",
            "Edit file",
            [" notes.md", "╌" * 110, " 1 -hello", " 1 +goodbye", "╌" * 110],
            "Do you want to make this edit to notes.md?",
            "notes.md",
        ),
    ],
)
def test_parse_permission_dialog_reads_each_capture(name, header, body, question, shown_path):
    parsed = parse_permission_dialog(_dialog(name))
    assert parsed is not None
    assert parsed["header"] == header
    assert parsed["body"] == body
    assert parsed["question"] == question
    assert parsed["shown_path"] == shown_path
    rows = parsed["text"].splitlines()
    assert rows[0] == " " + header
    assert rows[-1] == _FOOTER
    # Bounded by the rule: the scrollback above it (`⏺ Bash(touch …)`,
    # `⏺ Update(notes.md)`) names the request too, and must never be read.
    assert "⏺" not in parsed["text"]
    assert permission_dialog(_dialog(name)) == parsed["text"]


@pytest.mark.parametrize(("name", "tool", "key"), _FIXTURE_KEYS)
def test_dialog_matches_each_capture_with_its_own_key(name, tool, key):
    assert dialog_matches(parse_permission_dialog(_dialog(name)), tool, key)


@pytest.mark.parametrize(
    ("name", "tool", "key"),
    [
        (name, tool, key)
        for name, own_tool, own_key in _FIXTURE_KEYS
        for _other, tool, key in _FIXTURE_KEYS
        if (tool, key) != (own_tool, own_key)
    ],
)
def test_dialog_matches_no_other_captures_key(name, tool, key):
    # A Write key on the Edit dialog of the same file, a Bash key on a file
    # dialog, …: the header names the tool, and Allow answers only that tool.
    assert not dialog_matches(parse_permission_dialog(_dialog(name)), tool, key)


def test_a_bash_dialog_needs_its_description_too():
    parsed = parse_permission_dialog(_dialog("bash.txt"))
    assert dialog_matches(parsed, "Bash", _bash_key("touch probe.txt", "Create empty probe.txt file"))
    assert not dialog_matches(parsed, "Bash", _bash_key("touch probe.txt", "Touch the probe"))
    assert not dialog_matches(parsed, "Bash", _bash_key("touch probe.txt"))
    # A key stored in the clear (not the hook's digest) never matches.
    assert not dialog_matches(parsed, "Bash", "touchprobe.txtCreateemptyprobe.txtfile")


def test_a_bash_key_that_is_a_prefix_of_the_dialog_does_not_match():
    # The old screen token matched anywhere: `git push origin main` vouched
    # for this dialog.
    parsed = parse_permission_dialog(_bash_screen("   git push origin main --force\n   Push to main"))
    assert dialog_matches(parsed, "Bash", _bash_key("git push origin main --force", "Push to main"))
    assert not dialog_matches(parsed, "Bash", _bash_key("git push origin main", "Push to main"))
    assert not dialog_matches(parsed, "Bash", _bash_key("git push origin main"))


def test_bash_commands_sharing_a_cd_prefix_do_not_match():
    # Same cwd, so the same first 40 characters: the old token's whole reach.
    cwd = "/Users/someone/github/tomplex/periscope"
    live, stale = f"cd {cwd} && git push --force origin main", f"cd {cwd} && uv run pytest -q"
    assert live[:40] == stale[:40]
    parsed = parse_permission_dialog(_bash_screen(f"   {live}\n   Run it"))
    assert dialog_matches(parsed, "Bash", _bash_key(live, "Run it"))
    assert not dialog_matches(parsed, "Bash", _bash_key(stale, "Run it"))


def test_bash_heredocs_sharing_a_first_line_do_not_match():
    live = "python3 - <<'EOF'\nimport shutil; shutil.rmtree('build')\nEOF"
    stale = "python3 - <<'EOF'\nprint('hello')\nEOF"
    gutter = "\n".join(f"   │ {row}" for row in live.split("\n"))
    parsed = parse_permission_dialog(_bash_screen(f"{gutter}\n   Run it"))
    assert dialog_matches(parsed, "Bash", _bash_key(live, "Run it"))
    assert not dialog_matches(parsed, "Bash", _bash_key(stale, "Run it"))


def test_a_command_beginning_tip_stays_in_the_bash_block():
    # Only the rows above the command's indent are the Tip; a command row is
    # kept whatever it starts with, with the Tip row above it or not.
    key = _bash_key("Tip: echo hi", "Say hi")
    with_tip = parse_permission_dialog(_bash_screen("   Tip: echo hi\n   Say hi"))
    assert dialog_matches(with_tip, "Bash", key)
    assert not dialog_matches(with_tip, "Bash", _bash_key("echo hi", "Say hi"))
    without = parse_permission_dialog(_bash_screen("   Tip: echo hi\n   Say hi").replace(_TIP + "\n", ""))
    assert without is not None and without["body"][0] == "   Tip: echo hi"
    assert dialog_matches(without, "Bash", key)


def test_unknown_chrome_above_the_command_fails_closed():
    # Only a leading Tip (and its wrap) is skipped. Any other shallow row above
    # the command, a future "⚠ sandbox disabled" say, stays in the block.
    key = _bash_key("touch probe.txt", "Create empty probe.txt file")
    warned = parse_permission_dialog(_dialog("bash.txt").replace(_TIP, " ⚠ sandbox disabled"))
    assert warned is not None
    assert not dialog_matches(warned, "Bash", key)
    for name in ("bash.txt", "bash_narrow.txt"):
        assert dialog_matches(parse_permission_dialog(_dialog(name)), "Bash", key)


def test_only_known_chrome_below_the_command_is_skipped():
    # "This command requires approval" under the description is skipped (the
    # real capture matches its own key, above). Any other shallow row there
    # stays in the block and fails closed, and so does the known text above the
    # description or at the command's indent: it's part of the request then.
    key = _bash_key(_PY_COMMAND, _PY_DESCRIPTION)
    capture = _dialog("bash_requires_approval.txt")
    unknown = parse_permission_dialog(capture.replace(" This command requires approval", " ⚠ Sandbox disabled"))
    assert unknown is not None
    assert not dialog_matches(unknown, "Bash", key)
    above = parse_permission_dialog(
        capture.replace(
            f"   {_PY_DESCRIPTION}\n\n This command requires approval",
            f" This command requires approval\n   {_PY_DESCRIPTION}",
        )
    )
    assert above is not None and above["body"][-1] == f"   {_PY_DESCRIPTION}"
    assert not dialog_matches(above, "Bash", key)
    deep = parse_permission_dialog(
        capture.replace(" This command requires approval", "   This command requires approval")
    )
    assert not dialog_matches(deep, "Bash", key)
    assert dialog_matches(deep, "Bash", _bash_key(_PY_COMMAND, _PY_DESCRIPTION + "This command requires approval"))


def test_a_bash_dialog_matches_across_a_wrap_and_a_bare_gutter():
    # A long row wraps mid-word onto an indented row; a blank line in a
    # multi-line command leaves a bare `│`. Neither may break the match.
    wrapped = _bash_screen("   python3 scripts/regenerate_fix\n   tures.py --out tests\n   Regenerate")
    key = _bash_key("python3 scripts/regenerate_fixtures.py --out tests", "Regenerate")
    assert dialog_matches(parse_permission_dialog(wrapped), "Bash", key)
    blank = _bash_screen(
        "   │ python3 - <<'EOF'\n   │ import os\n   │\n   │ print(os.getcwd())\n   │ EOF\n   Print the cwd"
    )
    key = _bash_key("python3 - <<'EOF'\nimport os\n\nprint(os.getcwd())\nEOF", "Print the cwd")
    assert dialog_matches(parse_permission_dialog(blank), "Bash", key)


def test_a_file_key_never_matches_a_bash_dialog_naming_the_file():
    parsed = parse_permission_dialog(_bash_screen("   rm notes.md\n   Remove the notes"))
    assert parsed is not None
    assert not dialog_matches(parsed, "Write", "/tmp/probe/notes.md")
    assert not dialog_matches(parsed, "Edit", "/tmp/probe/notes.md")


def test_a_file_dialog_matches_its_path_on_a_slash_boundary():
    # The dialog shows the path relative to the cwd; the key is absolute.
    write = parse_permission_dialog(_dialog("write.txt"))
    assert not dialog_matches(write, "Write", "/tmp/probe/old-notes.md")
    assert not dialog_matches(write, "Write", "/tmp/probe/notes.md.bak")
    docs = parse_permission_dialog(_write_screen("docs/notes.md"))
    assert docs is not None and docs["shown_path"] == "docs/notes.md"
    assert dialog_matches(docs, "Write", "/tmp/probe/docs/notes.md")
    assert not dialog_matches(docs, "Write", "/tmp/probe/notes.md")
    # A path shown absolute must match whole.
    hosts = parse_permission_dialog(_write_screen("/etc/hosts"))
    assert dialog_matches(hosts, "Write", "/etc/hosts")
    assert not dialog_matches(hosts, "Write", "/tmp/etc/hosts")


def test_a_write_preview_naming_another_file_does_not_vouch_for_it():
    parsed = parse_permission_dialog(_write_screen("notes.md", "  1 see /tmp/probe/other.md"))
    assert dialog_matches(parsed, "Write", "/tmp/probe/notes.md")
    assert not dialog_matches(parsed, "Write", "/tmp/probe/other.md")


@pytest.mark.parametrize("tool", ["NotebookEdit", "WebFetch", "mcp__linear__save_issue", ""])
def test_dialog_matches_never_vouches_for_an_unverified_tool(tool):
    for name, _tool, key in _FIXTURE_KEYS:
        assert not dialog_matches(parse_permission_dialog(_dialog(name)), tool, key)


@pytest.mark.parametrize("key", [None, ""])
def test_dialog_matches_needs_a_key(key):
    for name, tool, _key in _FIXTURE_KEYS:
        assert not dialog_matches(parse_permission_dialog(_dialog(name)), tool, key)


def test_dialog_matches_needs_a_dialog():
    for _name, tool, key in _FIXTURE_KEYS:
        assert not dialog_matches(None, tool, key)


@pytest.mark.parametrize(
    "screen", [_IDLE, _QUOTED, _ASK, _ASK_YES, ""], ids=["idle", "quoted", "ask", "ask-yes", "empty"]
)
def test_permission_dialog_is_none_without_one(screen):
    assert parse_permission_dialog(screen) is None
    assert permission_dialog(screen) is None


def test_an_ask_user_question_is_turned_away_by_its_footer():
    # Everything else about _ASK_YES reads as a permission dialog.
    assert parse_permission_dialog(_ASK_YES.replace(_ASK_FOOTER, _FOOTER)) is not None


def test_parse_permission_dialog_stops_at_the_scrollback_without_a_rule():
    # bash.txt's rule gone: the search must not run on into `⏺ Bash(touch
    # probe.txt)`, nor take a rule beyond it for the dialog's.
    no_rule = _dialog("bash.txt").replace("─" * 110 + "\n", "")
    assert parse_permission_dialog(no_rule) is None
    beyond = no_rule.replace("⏺ Bash(touch probe.txt)", "─" * 110 + "\n⏺ Bash(touch probe.txt)")
    assert parse_permission_dialog(beyond) is None


def test_parse_permission_dialog_looks_60_rows_up_for_the_rule():
    tall = [f"echo {i}" for i in range(50)]
    screen = _bash_screen("\n".join(f"   │ {row}" for row in tall) + "\n   Echo")
    assert dialog_matches(parse_permission_dialog(screen), "Bash", _bash_key("\n".join(tall), "Echo"))
    taller = _bash_screen("\n".join(f"   │ echo {i}" for i in range(70)) + "\n   Echo")
    assert parse_permission_dialog(taller) is None


def test_parse_permission_dialog_takes_only_a_column_0_rule():
    # Indented, it's a reply's hr or a command's line, not the dialog's edge.
    screen = "\n".join(
        [_RULE, " Bash command", "   touch probe.txt", " Do you want to proceed?", " ❯ 1. Yes", "   2. No", "", _FOOTER]
    )
    assert parse_permission_dialog(screen) is not None
    assert parse_permission_dialog(screen.replace(_RULE, "  " + _RULE)) is None


def test_parse_permission_dialog_needs_the_footer_last_and_yes_right_under_the_question():
    bash = _dialog("bash.txt")
    assert parse_permission_dialog(bash + "$ ") is None  # a shell that cat'd it
    assert parse_permission_dialog(bash.replace(" ❯ 1. Yes", " ❯ 1. Allow")) is None
    assert parse_permission_dialog(bash.replace(" Do you want to proceed?\n", " Do you want to proceed?\n\n")) is None


def test_dialog_open_sees_permission_and_choice_dialogs():
    for name, *_ in _FIXTURE_KEYS:
        assert dialog_open(_dialog(name))
    assert dialog_open(_ASK)
    assert dialog_open(_ASK_YES)
    assert not dialog_open(_IDLE)
    assert not dialog_open(_QUOTED)
    assert not dialog_open("")


def test_a_dialog_with_the_cursor_on_option_2_is_still_open_and_still_matches():
    moved = (
        _dialog("bash.txt").replace(" ❯ 1. Yes", "   1. Yes").replace("   2. Yes, and always", " ❯ 2. Yes, and always")
    )
    assert dialog_open(moved)
    # `1` picks Yes wherever the cursor is, so Allow still applies.
    assert dialog_matches(parse_permission_dialog(moved), "Bash", _FIXTURE_KEYS[0][2])


def test_dialog_open_fails_closed_on_a_dialog_it_cannot_parse():
    # A narrow pane wraps the question: no Allow, and no Reply typed into it.
    narrow = _dialog("edit.txt").replace(
        " Do you want to make this edit to notes.md?", " Do you want to make this\n edit to notes.md?"
    )
    assert parse_permission_dialog(narrow) is None
    assert dialog_open(narrow)
    # A row under the footer (a new status line, say): the same.
    trailing = _dialog("bash.txt") + "  ⏵⏵ accept edits on"
    assert parse_permission_dialog(trailing) is None
    assert dialog_open(trailing)


@pytest.mark.parametrize(("name", "tool", "key"), _FIXTURE_KEYS)
def test_escapes_in_the_capture_neither_hide_the_dialog_nor_reach_the_match(name, tool, key):
    # capture() keeps escapes (-e): colon-form SGR and OSC 8 links, which
    # _ANSI_SGR_RE misses, on every row and around the names that matter.
    screen = "\n".join(f"\x1b[4:3m{row}\x1b[0m" if row else row for row in _dialog(name).split("\n"))
    for word in ("probe.txt", "a.txt", "notes.md"):
        screen = screen.replace(word, f"\x1b]8;;http://x\x1b\\{word}\x1b]8;;\x1b\\")
    parsed = parse_permission_dialog(screen)
    assert parsed is not None
    assert "\x1b" not in parsed["text"]
    assert dialog_matches(parsed, tool, key)
    assert dialog_open(screen)


def test_truecolour_and_bel_terminated_links_are_stripped_too():
    bash = _dialog("bash.txt").replace(
        " ❯ 1. Yes",
        " \x1b[38;2;177;185;249m❯\x1b[39m \x1b[38;2;153;153;153m1.\x1b[39m \x1b[38;2;177;185;249mYes\x1b[39m",
    )
    assert dialog_matches(parse_permission_dialog(bash), "Bash", _FIXTURE_KEYS[0][2])
    write = _dialog("write.txt").replace(
        "create notes.md?", "create \x1b]8;;file:///tmp/probe/notes.md\x07notes.md\x1b]8;;\x07?"
    )
    parsed = parse_permission_dialog(write)
    assert parsed is not None and parsed["shown_path"] == "notes.md"
    assert dialog_matches(parsed, "Write", "/tmp/probe/notes.md")


# ── The hook's half of the match. pane_permission_hook.py runs on bare
# python3 and can't import periscope, so what both sides share is mirrored,
# and pinned here.


def test_verifiable_tools_agree_with_the_hook():
    from periscope import panes

    assert panes.VERIFIABLE_TOOLS == hook.VERIFIABLE_TOOLS


def test_hidden_controls_agree_with_the_hook():
    # The hook strips them from its key; the screen side must strip the same.
    from periscope import panes

    every = "".join(map(chr, range(0x110000)))
    assert set(panes._HIDDEN_RE.findall(every)) == set(hook._HIDDEN_RE.findall(every))


@pytest.mark.parametrize(
    ("name", "tool", "tool_input"),
    [
        ("bash.txt", "Bash", {"command": "touch probe.txt", "description": "Create empty probe.txt file"}),
        ("bash_narrow.txt", "Bash", {"command": "touch probe.txt", "description": "Create empty probe.txt file"}),
        ("bash_multiline.txt", "Bash", {"command": "touch a.txt\ntouch b.txt", "description": "Create two files"}),
        ("bash_requires_approval.txt", "Bash", {"command": _PY_COMMAND, "description": _PY_DESCRIPTION}),
        ("write.txt", "Write", {"file_path": "/tmp/probe/notes.md", "content": "hello\n"}),
        ("edit.txt", "Edit", {"file_path": "/tmp/probe/notes.md", "old_string": "hello", "new_string": "goodbye"}),
    ],
)
def test_the_hooks_key_for_each_captured_request_matches_its_dialog(name, tool, tool_input):
    key = hook.match_key(tool, tool_input)
    assert key == next(k for n, _t, k in _FIXTURE_KEYS if n == name)
    assert dialog_matches(parse_permission_dialog(_dialog(name)), tool, key)


def test_a_hidden_control_in_a_command_does_not_break_the_match():
    command = "touch\N{ZERO WIDTH SPACE} probe.txt\N{SOFT HYPHEN}"
    key = hook.match_key("Bash", {"command": command, "description": "Create empty probe.txt file"})
    screen = _bash_screen(f"   {command}\n   Create empty probe.txt file")
    assert dialog_matches(parse_permission_dialog(screen), "Bash", key)


@pytest.mark.parametrize(
    "name", ["bash.txt", "bash_multiline.txt", "bash_narrow.txt", "bash_requires_approval.txt", "edit.txt", "write.txt"]
)
def test_parse_pane_reads_a_permission_dialog_as_needs_input(name):
    # THI-435: its footer, "Esc to cancel · Tab to amend", is outside
    # NEEDS_INPUT_FOOTER_RE, and the dialog hides the composer and status
    # line, so an unmapped pane read as a shell typing "1. Yes".
    parsed = parse_pane(_dialog(name))
    assert parsed["agent"] == "claude"
    assert parsed["state"] == "needs-input"
    assert parsed["needs_input"] is True
    assert parsed["pending_input"] is None


# ── composer_ready: the plain, empty ❯ prompt a reply needs ────────────
# Real Claude Code 2.1.280 captures (capture-pane -e, 100 columns, escapes
# kept), the probe's scratch path in the banner replaced by /tmp/probe.
_COMPOSER = Path(__file__).parent / "fixtures" / "composer"


def _composer_capture(name: str) -> str:
    return (_COMPOSER / name).read_text()


def _with_composer_row(row: str) -> str:
    # The idle capture with its composer row swapped for `row`.
    lines = _composer_capture("idle.txt").split("\n")
    assert lines[6].startswith("\x1b[39m❯")
    lines[6] = row
    return "\n".join(lines)


@pytest.mark.parametrize(
    ("screen", "ready"),
    [
        # `❯ Try "fix lint errors"`, the suggestion dim (SGR 2).
        (lambda: _composer_capture("idle.txt"), True),
        (lambda: _with_composer_row("\x1b[39m❯\xa0"), True),
        # Shell mode: `!` instead of `❯`, its rules pink, `! for shell mode` in
        # the footer. Enter would run the text as a shell command, and this
        # fresh session has no scrollback `❯` row a fallback scan could find.
        (lambda: _composer_capture("bash_empty.txt"), False),
        (lambda: _composer_capture("bash_draft.txt"), False),
        # Shell mode with no suggestion: nothing after the glyph, still not `❯`.
        (lambda: _with_composer_row("!\xa0"), False),
        # A typed draft is drawn in the default fg; a pasted block is grey
        # but is the draft.
        (lambda: _with_composer_row("\x1b[39m❯\xa0half a thought"), False),
        (lambda: _with_composer_row("\x1b[39m❯\xa0\x1b[38;5;240m[Pasted text #1 +40 lines]\x1b[39m"), False),
        # Any other glyph opening the composer.
        (lambda: _with_composer_row("#\xa0\x1b[2mremember this\x1b[0m"), False),
        # A two-row composer is a multi-line draft.
        (lambda: _with_composer_row("\x1b[39m❯\xa0\n  second line"), False),
        # A dialog has one rule and no composer.
        (lambda: _dialog("bash.txt"), False),
        (lambda: "tom@mac periscope % \n", False),
    ],
    ids=[
        "idle-suggestion",
        "idle-empty",
        "shell-mode-empty",
        "shell-mode-draft",
        "shell-mode-bare",
        "typed-draft",
        "pasted-block",
        "other-glyph",
        "two-rows",
        "permission-dialog",
        "no-composer",
    ],
)
def test_composer_ready(screen, ready):
    assert composer_ready(screen()) is ready


# What a Claude that exited leaves: its last frame (composer and footer), then
# the shell's output and prompt under it.
_AFTER_EXIT = (
    "\nResume this session with:\nclaude --resume 2f0c7d3e-5d8e-4b1a-9c55-0d7a1e2b3c4d\n"
    "tom@mac periscope % git status\nOn branch main\ntom@mac periscope % \n"
)


def test_a_leftover_composer_above_a_shell_is_not_ready():
    screen = _composer_capture("idle.txt").rstrip("\n") + _AFTER_EXIT
    assert composer_ready(_composer_capture("idle.txt"))
    assert not composer_ready(screen)
    assert not composer_holds(screen.replace('\x1b[2mTry "fix lint errors"', "Postgres"), "Postgres")


def test_a_composer_left_by_a_claude_that_just_exited_is_not_ready():
    # Right after exit nothing has pushed the frame up yet: "Press Ctrl-C again
    # to exit", the resume hint and the prompt fit inside _FOOTER_REACH.
    screen = _composer_capture("idle.txt").rstrip("\n") + (
        "\n  Press Ctrl-C again to exit\n\nResume this session with:\n"
        "claude --resume 66bbba62-eef3-4417-bd6d-7f2cd292c5b3\n➜  periscope git:(main) ✗ \n"
    )
    assert not composer_ready(screen)
    assert parse_pane(screen)["agent"] is None


def test_a_claude_with_no_status_line_parses_as_claude():
    # No statusLine configured at all (the default): THI-434.
    parsed = parse_pane(_composer_capture("idle.txt"))
    assert parsed["agent"] == "claude"
    assert parsed["state"] == "idle"
    assert parsed["context_pct"] is None


def test_a_footer_of_a_few_rows_still_reads_as_ready():
    # Live 2.1.280 panes show 2-4 footer rows: mode hint, warnings, a status line.
    extra = "\n  17% | ↑30k ↓74 | $0.50 | Opus 4.7 (1M context)\n  ⎿ 2 background tasks\n"
    assert composer_ready(_composer_capture("idle.txt").rstrip("\n") + extra)


_REPLY = "CI failed on #12: lint, unit. Please investigate and fix."


@pytest.mark.parametrize(
    ("screen", "holds"),
    [
        (lambda: _with_composer_row(f"\x1b[39m❯\xa0{_REPLY}"), True),
        # Wrapped onto a second row, mid-word: whitespace is squashed out.
        (lambda: _with_composer_row(f"\x1b[39m❯\xa0{_REPLY[:20]}\n  {_REPLY[20:]}"), True),
        # A long paste Claude collapsed: the composer was empty, so it's ours.
        (lambda: _with_composer_row("\x1b[39m❯\xa0\x1b[38;5;240m[Pasted text #1 +3 lines]\x1b[39m"), True),
        # Not there yet.
        (lambda: _composer_capture("idle.txt"), False),
        (lambda: _with_composer_row("\x1b[39m❯\xa0"), False),
        # The same words as a dim suggestion are not the paste.
        (lambda: _with_composer_row(f"\x1b[39m❯\xa0\x1b[2m{_REPLY}\x1b[0m"), False),
        # Something else is in the composer.
        (lambda: _with_composer_row("\x1b[39m❯\xa0rm -rf build"), False),
        # Shell mode: Enter would run it.
        (lambda: _with_composer_row(f"!\xa0{_REPLY}"), False),
        # A dialog took focus: no composer.
        (lambda: _dialog("bash.txt"), False),
    ],
    ids=[
        "typed",
        "wrapped",
        "collapsed-paste",
        "suggestion-only",
        "empty",
        "same-words-as-suggestion",
        "other-text",
        "shell-mode",
        "dialog",
    ],
)
def test_composer_holds(screen, holds):
    assert composer_holds(screen(), _REPLY) is holds


# Real 2.1.280 captures of the composer just after a bracketed paste: a short
# reply shown as typed (default fg), and 29 lines collapsed to a placeholder
# (its footer reads "paste again to expand").
_PASTED_SHORT = "CI failed on #7: test (ubuntu, 3.12). Please investigate and fix."


def test_a_real_short_paste_is_held_and_only_that_text():
    screen = _composer_capture("pasted_short.txt")
    assert composer_holds(screen, _PASTED_SHORT)
    assert not composer_holds(screen, "CI failed on #8: lint. Please investigate and fix.")
    assert not composer_ready(screen)


def test_a_real_collapsed_paste_is_held_by_its_placeholder():
    screen = _composer_capture("pasted_long.txt")
    assert composer_holds(screen, "\n".join(f"line {i}: something to fix" for i in range(29)))
    assert not composer_ready(screen)


def test_composer_holds_compares_the_start_of_the_reply():
    # The first look saw an empty composer; this text isn't the reply.
    assert not composer_holds(_with_composer_row("\x1b[39m❯\xa0Postgres is down"), "Postgres, please")
    assert composer_holds(_with_composer_row("\x1b[39m❯\xa0Postgres, please"), "Postgres, please")


def _over_composer(rows: str, composer_row: str = "\x1b[39m❯\xa0Postgres, please") -> str:
    """The idle capture with `rows` drawn just above the composer's top rule,
    as a mid-redraw capture can catch a dialog's top half."""
    screen = _with_composer_row(composer_row)
    rule = "\x1b[38;2;136;136;136m" + "─" * 100
    assert screen.count(rule) == 2
    return screen.replace(rule, rows.rstrip("\n") + "\n" + rule, 1)


@pytest.mark.parametrize(
    "rows",
    [
        # The dialog's own full-width rule, header and question, options not yet drawn.
        "─" * 100 + "\n Bash command\n\n   rm -rf build\n\n Do you want to proceed?",
        # Its question with `1. Yes` under it, the cursor there or moved down.
        " Do you want to proceed?\n ❯ 1. Yes",
        " Do you want to proceed?\n   1. Yes\n ❯ 2. Yes, and don't ask again",
        # `1. Yes` under the cursor, the question cut off above.
        " ❯ 1. Yes\n   2. No",
        # Its footer.
        " Esc to cancel · Tab to amend",
    ],
    ids=["rule-and-question", "question-and-yes", "question-then-yes-cursor-below", "cursor-on-yes", "footer"],
)
def test_a_half_drawn_dialog_above_the_composer_refuses_both_looks(rows):
    assert not composer_holds(_over_composer(rows), "Postgres, please")
    assert not composer_ready(_over_composer(rows, "\x1b[39m❯\xa0"))


def test_a_question_asked_in_prose_above_the_composer_is_fine():
    # How Claude asks in a reply, which is what a reply answers: no rule, no
    # options under it, no footer.
    rows = "⏺ I've drafted the migration.\n\n  Do you want to run it now?"
    assert composer_holds(_over_composer(rows), "Postgres, please")
    assert composer_ready(_over_composer(rows, "\x1b[39m❯\xa0"))

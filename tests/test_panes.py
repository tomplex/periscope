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

import textwrap

import pytest

from periscope.panes import (
    SPINNER_RE, ACTIVE_OP_RE, parse_pane,
    smooth_spinner, smooth_is_claude,
    note_focus, note_action, update_focus_from_windows,
    _focused_at, _acted_at, _spinner_last_seen, _claude_last_seen,
    _active_per_session,
    SPINNER_GRACE_S, CLAUDE_STICKY_S,
    list_windows,
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
    ("N11", "OK   active=True  verb=Bootstrapping  ●  Bootstrapping packages (7m 29s · ↑ 22.1k tokens · thought for 2s)", False),

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

PARSE_CASES: list[tuple[str, str, str, str | None]] = [
    # The screenshot Tom hit: multi-word phrase + trailing `…` + ↓-arrow
    # parens with garbled timing. Used to read as "waiting" because neither
    # old regex matched. Now should detect via SPINNER_RE.
    ("screenshot", textwrap.dedent(f"""\
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
    """), "working", "Wiring"),

    # Idle Claude pane — status line present, no active marker.
    ("idle", textwrap.dedent(f"""\
        Some prior assistant output.

        ✻ Brewed for 7m 16s

        ❯
        {STATUS_LINE}
    """), "idle", None),

    # No status line at all = shell pane.
    ("shell", textwrap.dedent("""\
        $ ls
        foo.py bar.py
        $ cat foo.py
        print('hello')
        $
    """), "shell", None),

    # Prose embedding the active marker mid-sentence — was the self-quote
    # false-positive that took two iterations to fix via .match() anchoring.
    ("prose-embed", textwrap.dedent(f"""\
        The previous fix added ACTIVE_OP_RE to catch the marker.

        That literal ● Bootstrapping packages (... ↑ 22.1k tokens · thought for 2s) line ended up in scrollback.

        ✻ Brewed for 3s

        ❯
        {STATUS_LINE}
    """), "idle", None),

    # Old single-word ellipsis form (the original spinner case).
    ("old-ellipsis", textwrap.dedent(f"""\
        ✻ Envisioning…

        ❯
        {STATUS_LINE}
    """), "working", "Envisioning"),

    # Parens form with ↑ arrow (the form added in iteration 2).
    ("parens-up", textwrap.dedent(f"""\
        ● Researching codebase (3m 12s · ↑ 5.2k tokens · thought for 8s)
          tool calls below the marker

        ❯
        {STATUS_LINE}
    """), "working", "Researching"),

    # IDLE_INDICATOR_RE acts as a positional "stop searching" boundary.
    # Code-block marker quotes from the assistant's own response sit ABOVE
    # the past-tense indicator and must not false-positive as working.
    # (Iteration 6 — the bug that showed `working 3…` because a stale phrase
    # starting with a digit got picked up by the old first-word fallback.)
    ("idle-with-code-block-marker", textwrap.dedent(f"""\
        Look at these Claude TUI active marker examples:

        ✻ Envisioning…
        ✶ Bootstrapping…
        ✳ Wiring resolve_pids into endpoints…(910m 2 · ↓ 14.78 tokens · thought for 10s)
        ● Bootstrapping packages (7m 29s · ↑ 22.1k tokens · thought for 2s)

        normal-0 jumped #2 → #1, e151d2dc jumped #5 → #3, both real semantic wins.

        ✻ Cooked for 3m 42s

        ❯
        {STATUS_LINE}
    """), "idle", None),

    # Past-tense lines in the bottom rows for each verb form we know about.
    ("idle-brewed", textwrap.dedent(f"""\
        ● Some prior response.
        ✻ Brewed for 31s
        ❯
        {STATUS_LINE}
    """), "idle", None),
    ("idle-thought", textwrap.dedent(f"""\
        ● Some prior response.
        ✻ Thought for 10s
        ❯
        {STATUS_LINE}
    """), "idle", None),
    ("idle-pondered", textwrap.dedent(f"""\
        ● Some prior response.
        ✻ Pondered for 2m 15s
        ❯
        {STATUS_LINE}
    """), "idle", None),

    # Active marker present BELOW an older past-tense line from a prior
    # turn — iteration hits the active marker first (it's closer to the
    # bottom), so we correctly report working.
    ("active-after-old-brewed", textwrap.dedent(f"""\
        First turn response.
        ✻ Brewed for 30s

        Second turn started.
        ✳ Working on the next task…
          ⎿  subtask 1
             subtask 2

        ❯
        {STATUS_LINE}
    """), "working", "Working"),

    # Verb fallback: an active marker with a digit-starting phrase used to
    # surface `3…` via the first-word fallback. Now falls back to "working".
    ("verb-fallback", textwrap.dedent(f"""\
        ✻ 3 items remaining…

        ❯
        {STATUS_LINE}
    """), "working", "working"),
]


# ── Ghost-text filter cases ─────────────────────────────────────────────
# Each row: (tag, content_with_ansi, expected_pending_input).
# These fixtures embed SGR escapes (`\x1b[38;5;Nm`) — what tmux capture-pane
# -e emits. parse_pane keeps `pending_input` only when the prompt line shows
# ≥2 distinct fg colors (prefix in one color, typed text in another). Ghost
# text from Claude Code shares the prefix color and must be filtered out.

GHOST_CASES: list[tuple[str, str, str | None]] = [
    # Ghost text only — single fg color across the prompt line. The
    # visible text is Claude Code's suggestion, not user input.
    ("ghost-leave-it",
     f"\x1b[38;5;239m❯ leave it\x1b[39m\n{STATUS_LINE}\n",
     None),

    # Real input — prefix is dim grey, typed text switches to bright fg.
    ("real-input",
     f"\x1b[38;5;239m❯ \x1b[38;5;231mhello world\x1b[39m\n{STATUS_LINE}\n",
     "hello world"),

    # Empty prompt — no text after `❯ `, no pending input regardless of color.
    ("empty-prompt",
     f"\x1b[38;5;239m❯ \x1b[39m\n{STATUS_LINE}\n",
     None),

    # Backward-compat: a fixture with no SGR escapes at all (existing tests
    # don't include them). Trust the visible text — can't distinguish ghost
    # from real without color info.
    ("no-ansi-trust-visible",
     f"❯ some text the user typed\n{STATUS_LINE}\n",
     "some text the user typed"),

    # Realistic shape from periscope:4 capture: bg color escape mixed in
    # with fg escapes. Only fg codes count toward the distinct-color check.
    ("real-with-bg-color",
     f"\x1b[38;5;239m\x1b[48;5;237m❯ \x1b[38;5;231mreal user input\x1b[39m\n{STATUS_LINE}\n",
     "real user input"),
]


# ── last_line filter cases ─────────────────────────────────────────────
# Each row: (tag, content, expected_last_line). These verify the card
# snippet doesn't surface TUI chrome (auto-mode footer, title bar, etc.).

LAST_LINE_CASES: list[tuple[str, str, str]] = [
    # Auto-mode footer at the bottom — must be filtered. Falls through to
    # the actual response content above.
    ("filter-auto-mode", textwrap.dedent(f"""\
        Some recent response text from the assistant.
        ✻ Brewed for 31s
        ❯
        ─────────────────────
        {STATUS_LINE}
        ⏵⏵ auto mode on (shift+tab to cycle)
    """), "✻ Brewed for 31s"),

    # Auto-mode footer with the "← for agents" suffix — same filter.
    ("filter-auto-mode-agents", textwrap.dedent(f"""\
        Some recent response text.
        ✻ Cooked for 1m 15s
        ❯
        {STATUS_LINE}
        ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
    """), "✻ Cooked for 1m 15s"),

    # Title bar at top + auto-mode at bottom — both filtered.
    ("filter-title-bar", textwrap.dedent(f"""\
        periscope | main | +3 -26 4m * | github.com/tomplex/periscope/compare/master...main
        Assistant said something useful here.
        ✻ Brewed for 5s
        ❯
        {STATUS_LINE}
        ⏵⏵ auto mode on (shift+tab to cycle)
    """), "✻ Brewed for 5s"),

    # Active spinner line shouldn't surface as snippet (duplicate of state
    # label). Falls through to whatever's below the spinner.
    ("filter-active-spinner", textwrap.dedent(f"""\
        ✳ Wiring resolve_pids into endpoints…
          ⎿  ◼ Task 2.3: subtask line
             ◻ Task 2.4: another subtask
        ❯
        {STATUS_LINE}
        ⏵⏵ auto mode on (shift+tab to cycle)
    """), "◻ Task 2.4: another subtask"),
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
            failures.append(
                f"[{tag}] match={got} want={want} via={via}  {line[:80]}"
            )
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
            failures.append(
                f"[{tag}] pending_input={got!r} (want {want_pending!r})"
            )
    assert not failures, "\n".join(failures)


def test_last_line_cases():
    failures = []
    for tag, content, want in LAST_LINE_CASES:
        result = parse_pane(content)
        got = result.get("last_line")
        if got != want:
            failures.append(f"[{tag}] last_line={got!r} (want {want!r})")
    assert not failures, "\n".join(failures)


# ── Smoothing + focus tracking + list_windows ──────────────────────────


@pytest.fixture(autouse=True)
def reset_panes_state():
    """Clear in-memory pane state between tests."""
    _focused_at.clear()
    _acted_at.clear()
    _spinner_last_seen.clear()
    _claude_last_seen.clear()
    _active_per_session.clear()
    yield
    _focused_at.clear()
    _acted_at.clear()
    _spinner_last_seen.clear()
    _claude_last_seen.clear()
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


def test_smooth_is_claude_true_passes_through():
    assert smooth_is_claude("foo:0", True) is True
    assert "foo:0" in _claude_last_seen


def test_smooth_is_claude_false_after_stickiness_expires(mocker):
    mocker.patch(
        "periscope.panes.time.time",
        side_effect=[100.0, 100.0 + CLAUDE_STICKY_S + 1.0],
    )
    smooth_is_claude("foo:0", True)
    assert smooth_is_claude("foo:0", False) is False


def test_smooth_is_claude_sticky_within_window(mocker):
    """If we just saw is_claude=True, a momentary False should still
    return True until the stickiness window expires."""
    mocker.patch("periscope.panes.time.time", side_effect=[100.0, 100.5])
    smooth_is_claude("foo:0", True)
    assert smooth_is_claude("foo:0", False) is True


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
    sample = (
        "main\t0\tshell\t1\t/home/tom/dev/foo\t1234abcd\t%5\n"
        "main\t1\tclaude\t0\t/home/tom/dev/bar\t1235abcd\t%6\n"
    )
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


def test_list_windows_filters_usage_scrape_sessions(mocker):
    sample = (
        "main\t0\tshell\t1\t/home/tom\t\t%5\n"
        "periscope-usage-abc12345\t0\tclaude\t1\t/tmp\t\t%6\n"
    )
    mocker.patch("periscope.panes.tmux", return_value=sample)
    out = list_windows()
    # The periscope-usage-* session should be filtered out.
    assert len(out) == 1
    assert out[0]["session"] == "main"

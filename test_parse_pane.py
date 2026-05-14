# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn[standard]", "anthropic", "python-dotenv"]
# ///
"""Regression tests for parse_pane() — primarily spinner / active-op detection.

Run: `uv run test_parse_pane.py`

This file exists in a "no test suite" repo because spinner detection has needed
five iterations and each new Claude TUI variation risked silently regressing
prior ones. A flat case-matrix is the smallest thing that keeps the iteration
count from climbing.

When a new variation shows up:
  - Real active marker that didn't match     → add to REGEX_CASES with True
  - Prose / scrollback that false-positives  → add to REGEX_CASES with False
  - End-to-end pane behavior                 → add to PARSE_CASES
"""
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import server


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
    """), "waiting", None),

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
    """), "waiting", None),

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
    """), "waiting", None),

    # Past-tense lines in the bottom rows for each verb form we know about.
    ("idle-brewed", textwrap.dedent(f"""\
        ● Some prior response.
        ✻ Brewed for 31s
        ❯
        {STATUS_LINE}
    """), "waiting", None),
    ("idle-thought", textwrap.dedent(f"""\
        ● Some prior response.
        ✻ Thought for 10s
        ❯
        {STATUS_LINE}
    """), "waiting", None),
    ("idle-pondered", textwrap.dedent(f"""\
        ● Some prior response.
        ✻ Pondered for 2m 15s
        ❯
        {STATUS_LINE}
    """), "waiting", None),

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


# ── Runner ──────────────────────────────────────────────────────────────

def run_regex_cases() -> int:
    print("── regex cases ─────────────────────────────────────────────")
    failures = 0
    for tag, line, want in REGEX_CASES:
        s_m = server.SPINNER_RE.match(line)
        a_m = server.ACTIVE_OP_RE.match(line)
        got = bool(s_m or a_m)
        via = "SPINNER" if s_m else "ACTIVE" if a_m else "-"
        if got == want:
            print(f"  OK   [{tag:>3}] match={got!s:5} via={via:7}  {line[:80]}")
        else:
            failures += 1
            print(f"  FAIL [{tag:>3}] match={got!s:5} want={want!s:5} via={via:7}  {line[:80]}")
    return failures


def run_parse_cases() -> int:
    print("\n── parse_pane end-to-end ───────────────────────────────────")
    failures = 0
    for tag, content, want_state, want_spinner in PARSE_CASES:
        result = server.parse_pane(content)
        ok_state = result["state"] == want_state
        ok_spinner = result["spinner"] == want_spinner
        if ok_state and ok_spinner:
            print(f"  OK   [{tag:>13}] state={result['state']!r:11} spinner={result['spinner']!r}")
        else:
            failures += 1
            print(
                f"  FAIL [{tag:>13}] "
                f"state={result['state']!r} (want {want_state!r})  "
                f"spinner={result['spinner']!r} (want {want_spinner!r})"
            )
    return failures


def main() -> int:
    fails = run_regex_cases() + run_parse_cases()
    total = len(REGEX_CASES) + len(PARSE_CASES)
    print()
    if fails:
        print(f"=== {fails}/{total} FAIL ===")
        return 1
    print(f"=== all {total} cases pass ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Auto-rename via the Anthropic SDK.

Used by the /api/auto-rename-session and /api/auto-rename-window route
handlers in periscope/routes/auto_rename.py.

`get_anthropic` lazily constructs the SDK client (so the dashboard can boot
without an API key as long as nothing triggers an auto-rename); the cached
client is reused for subsequent calls.
"""

import json
import os
from collections import deque
from pathlib import Path

_anthropic_client = None


# Tool calls Claude makes whose `input` carries a file path worth
# surfacing in the rename prompt. Each maps to the field name we look
# for, or "*command" for Bash (a one-line summary of the command).
_TOOL_PATH_FIELD = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
    "Glob": "pattern",
    "Grep": "pattern",
}


# Shared rename taste rules — spliced into build_rename_prompt AND the
# narrator prompt (periscope/narrator.py) so the two can't drift. A list
# of lines, not a joined string: both builders assemble line-lists and
# apply their own indentation.
RENAME_RULES = [
    "- 1-3 words, lowercase-with-dashes preferred (e.g. 'fs-build', 'cohort-inv')",
    "- Max 25 characters",
    "- Prefer the CONCEPT being worked on over the mechanism. e.g. if recent",
    "  prompts talk about 'feature store liveness' and tool calls touch",
    "  files in anthology/liveness/, name it 'fs-liveness' — not 'edit-py'.",
    "- Bad: 'claude', 'shell', 'zsh', 'work', generic verbs.",
    "- Good: 'postcode-ingestion', 'monitoring-cert', 'rust-port', 'fs-liveness'",
    "- If the existing name still captures the work accurately, KEEP IT.",
    "  Don't change names just to feel like progress.",
    "- Never echo the tab's branch / worktree / track name (or an abbreviation",
    "  of it): the dashboard already shows that label on the tab's group",
    "  header. e.g. on branch 'tc/attr-worker-phase2', 'drift-detection' is a",
    "  good name; 'attr-worker-phase2' is dead weight.",
    "- If a track goal is provided, don't repeat it; pick the name that sets",
    "  this tab apart from its siblings.",
]


def get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it in your shell "
                "(e.g. add to ~/.zshenv) before starting the dashboard."
            )
        _anthropic_client = Anthropic()
    return _anthropic_client


def claude_complete(prompt: str, model: str = "claude-haiku-4-5") -> str:
    """Single-shot completion via the Anthropic SDK. Much faster than the
    claude CLI (no MCP / hooks / settings load — just an HTTP round-trip)."""
    client = get_anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    # Concatenate all text blocks (Haiku usually returns just one)
    return "".join(b.text for b in msg.content if b.type == "text")


def transcript_summary(session: str, index: int, *,
                       n_user: int = 3, n_tools: int = 8) -> dict:
    """Pull rename-relevant signals from the pane's Claude JSONL.

    Returns a dict that callers fold into the per-window context:
      - recent_user_prompts: the last `n_user` user-turn texts, each
        truncated to 240 chars. Highest-signal field for "what is the
        user asking the assistant to do."
      - recent_tool_calls: the last `n_tools` tool invocations as one-
        line strings (e.g. "Edit src/foo.py", "Bash bin/test -k slow",
        "Grep 'TODO: rename'"). Concrete; reveals scope at a glance.
      - files_touched: unique recent file paths from Edit/Write/Read.
        Bounded to the same `n_tools` window so the prompt isn't long.
    Empty dict when the pane has no Claude transcript (shell pane,
    channel never connected, etc.). Catches all exceptions — a malformed
    JSONL must not break the rename request.
    """
    try:
        from periscope.turns import get_turns_for_pane

        turns = get_turns_for_pane(session, index)
        if not turns or not turns.get("messages"):
            return {}
        return _collect_signals(turns["messages"], n_user=n_user, n_tools=n_tools)
    except Exception:
        return {}


def _collect_signals(messages: list[dict], *, n_user: int, n_tools: int) -> dict:
    """Shared signal-collection walk over {role, text, tool_uses} messages.
    Used by both transcript_summary variants so they can't drift."""
    # Walk newest-first; collect until we have what we need.
    user_prompts: list[str] = []
    tool_calls: list[str] = []
    files: list[str] = []
    seen_files: set[str] = set()
    for msg in reversed(messages):
        role = msg.get("role")
        if role == "user" and len(user_prompts) < n_user:
            text = (msg.get("text") or "").strip()
            if text:
                user_prompts.append(text[:240])
        elif role == "assistant":
            for tu in reversed(msg.get("tool_uses") or []):
                if len(tool_calls) >= n_tools:
                    break
                name = tu.get("name") or ""
                inp = tu.get("input") or {}
                summary = _summarize_tool_call(name, inp)
                if summary:
                    tool_calls.append(summary)
                field = _TOOL_PATH_FIELD.get(name)
                if field == "file_path" or field == "notebook_path":
                    p = inp.get(field)
                    if p and p not in seen_files and len(files) < n_tools:
                        seen_files.add(p)
                        files.append(p)
        if len(user_prompts) >= n_user and len(tool_calls) >= n_tools:
            break

    # Newest-first iteration above gave us reverse order; flip so the
    # prompt reads in chronological order (older → newer).
    return {
        "recent_user_prompts": list(reversed(user_prompts)),
        "recent_tool_calls": list(reversed(tool_calls)),
        "files_touched": files,
    }


def transcript_summary_from_path(jsonl_path: Path, *, n_user: int = 3,
                                 n_tools: int = 8, tail_lines: int = 500) -> dict:
    """Same return shape as transcript_summary, but from an already-resolved
    JSONL path with a bounded tail read — transcripts run tens of MB and the
    narrator calls this per regenerating pane per worker tick. Does NOT use
    history.search.messages_from_jsonl (full-file two-pass with tool-result
    back-patching the narrator doesn't need). Skips isMeta/isSidechain raw
    entries so slash-command/skill expansions never look like user intent
    (same filter messages_from_jsonl applies)."""
    try:
        with open(jsonl_path) as fh:
            tail = deque(fh, maxlen=tail_lines)
    except OSError:
        return {}
    messages: list[dict] = []
    for line in tail:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("isMeta") is True or d.get("isSidechain") is True:
            continue
        content = (d.get("message") or {}).get("content")
        if d.get("type") == "user":
            # Typed prompts are strings; tool-result/image turns are block
            # lists — only the former is user intent.
            if isinstance(content, str) and content.strip():
                messages.append({"role": "user", "text": content})
        elif d.get("type") == "assistant":
            tool_uses = ([b for b in content
                          if isinstance(b, dict) and b.get("type") == "tool_use"]
                         if isinstance(content, list) else [])
            messages.append({"role": "assistant", "tool_uses": tool_uses})
    return _collect_signals(messages, n_user=n_user, n_tools=n_tools)


def _summarize_tool_call(name: str, inp: dict) -> str:
    """One-line representation of a single tool_use: tool + the field of
    its input that's worth seeing in a rename prompt."""
    if not name:
        return ""
    field = _TOOL_PATH_FIELD.get(name)
    if field:
        val = inp.get(field) or ""
        if val:
            return f"{name} {val}"
    if name == "Bash":
        cmd = (inp.get("command") or "").strip().split("\n", 1)[0]
        if cmd:
            return f"Bash {cmd[:100]}"
    if name == "TodoWrite":
        # The set of todos is a great signal of what the user is asking
        # for. Surface the first todo's content as the summary.
        todos = inp.get("todos") or []
        if todos and isinstance(todos[0], dict):
            content = (todos[0].get("content") or "").strip()
            if content:
                return f"TodoWrite {content[:100]}"
    return name


def build_rename_prompt(windows: list[dict]) -> str:
    lines = [
        "You are renaming tmux windows in a senior developer's terminal session.",
        "",
        "For each window below, suggest a SHORT name that captures the SEMANTIC focus",
        "of the work — what is the developer actually trying to accomplish? Constraints:",
        *[f"  {r}" for r in RENAME_RULES],
        "  - Signal priority (highest first):",
        "    1. recent_user_prompts — what the developer is asking for right now",
        "    2. recent_tool_calls + files_touched — what's actually being done",
        "    3. branch / PR — long-term context",
        "    4. recap / recent_excerpt — fallback if the above is thin",
        "",
        "Windows in this session:",
    ]
    for w in windows:
        lines.append("")
        lines.append(f"[index {w['index']}] current_name='{w['current_name']}'")
        if w.get("branch"):
            pr = f", PR #{w['pr']}" if w.get("pr") else ""
            lines.append(f"  branch: {w['branch']}{pr}")
        prompts = w.get("recent_user_prompts") or []
        if prompts:
            lines.append("  recent user prompts (oldest→newest):")
            for i, p in enumerate(prompts, 1):
                lines.append(f"    {i}. {p}")
        tool_calls = w.get("recent_tool_calls") or []
        if tool_calls:
            lines.append("  recent tool calls (oldest→newest):")
            lines.extend(f"    - {tc}" for tc in tool_calls)
        files = w.get("files_touched") or []
        if files:
            lines.append(f"  files touched: {', '.join(files)}")
        if w.get("recap"):
            lines.append(f"  recap: {w['recap'][:300]}")
        if w.get("pending_input"):
            lines.append(f"  pending input: {w['pending_input'][:120]}")
        snippet = w.get("recent_excerpt", "")
        # Only show the terminal excerpt as a fallback signal when the
        # transcript was unavailable — otherwise it's noise vs. the
        # structured tool-call view above.
        if snippet and not prompts and not tool_calls:
            lines.append(f"  recent terminal excerpt:\n    {snippet}")
    lines.append("")
    lines.append(
        'Return ONLY a JSON object mapping window index (as a string) to the new name. '
        'Example: {"1": "fs-build", "2": "cohort-inv"}. '
        "No markdown fences, no commentary, just the JSON object."
    )
    return "\n".join(lines)

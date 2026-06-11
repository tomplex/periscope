"""POST /api/auto-rename-session and /api/auto-rename-window.

Both routes capture per-window context (git branch/PR, parsed pane state,
recent output snippet) and feed it to Claude via build_rename_prompt +
claude_complete, then apply the returned names via tmux rename-window.
"""

import json
import re
import time

from fastapi import APIRouter, HTTPException

from periscope.activity import stamp_pane_rename
from periscope.git_pr import cached_git_state, cached_pr_state
from periscope.panes import list_windows, parse_pane
from periscope.pids import _attach_git_then_resolve_pids
from periscope.rename_ai import build_rename_prompt, claude_complete, transcript_summary
from periscope.tmux import capture, pane_meta, tmux

router = APIRouter()


@router.post("/api/auto-rename-session")
def auto_rename_session(session: str):
    all_windows = list_windows()
    target_windows = [w for w in all_windows if w["session"] == session]
    _attach_git_then_resolve_pids(target_windows)
    if not target_windows:
        raise HTTPException(404, f"session {session!r} not found")

    # Build per-window context
    context = []
    for w in target_windows:
        target = f"{w['session']}:{w['index']}"
        try:
            content = capture(target, lines=80)
            parsed = parse_pane(content)
        except Exception:
            content, parsed = "", {}
        # Strip ANSI from snippet so the prompt isn't full of escape codes
        plain = re.sub(r"\x1b\[[\d;]*m", "", content)
        snippet_lines = [ln for ln in plain.split("\n") if ln.strip()][-20:]
        snippet = "\n    ".join(snippet_lines)[-1200:]
        # branch/pr no longer live in parse_pane output — they're derived
        # from the pane's cwd via git/gh. Fetching here (cached) gives the
        # prompt actually-useful context.
        git = cached_git_state(w.get("cwd", "")) or {}
        pr = cached_pr_state(w.get("cwd", ""), git.get("branch")) or {}
        entry = {
            "index": w["index"],
            "current_name": w["name"],
            "branch": git.get("branch"),
            "pr": pr.get("pr"),
            "recap": parsed.get("recap"),
            "pending_input": parsed.get("pending_input"),
            "recent_excerpt": snippet,
        }
        # Fold in JSONL-derived signals (recent prompts, tool calls, files).
        # No-op for shell panes; safe for Claude panes that recently
        # restarted (channel hasn't recorded a session id yet).
        entry.update(transcript_summary(w["session"], w["index"]))
        context.append(entry)

    prompt = build_rename_prompt(context)
    try:
        result = claude_complete(prompt)
    except Exception as e:
        raise HTTPException(500, str(e))

    # Claude sometimes wraps JSON in code fences despite instructions; strip.
    cleaned = result.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE)
    try:
        new_names = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"claude returned invalid JSON: {e}")

    applied = []
    for index_str, new_name in new_names.items():
        try:
            index = int(index_str)
        except ValueError:
            continue
        new_name = (new_name or "").strip()
        if not new_name:
            continue
        old = next((w["name"] for w in target_windows if w["index"] == index), None)
        if old is None or new_name == old:
            continue
        target = f"{session}:{index}"
        tmux("rename-window", "-t", target, new_name)
        # AI renames start the narrator cooldown too — same contract as a
        # manual rename. pane_id comes from the list_windows dicts.
        pane_id = next((x.get("pane_id") for x in target_windows
                        if x["index"] == index), None)
        if pane_id:
            stamp_pane_rename(pane_id, name=new_name, at=int(time.time()))
        applied.append({"index": index, "old": old, "new": new_name})

    return {"ok": True, "applied": applied, "session": session}


@router.post("/api/auto-rename-window")
def auto_rename_window(session: str, index: int):
    """Single-window variant of auto_rename_session. Same prompt machinery, but
    scoped to one window so the user can refresh a single card's name without
    perturbing siblings."""
    target = f"{session}:{index}"
    try:
        current_name, cwd = pane_meta(target)
    except Exception as e:
        raise HTTPException(500, str(e))

    # Single-window pid resolution: build a one-element list and reuse the
    # batch helper so `last_seen` stays current for this window too.
    one = [{"session": session, "index": index, "name": current_name, "active": False, "cwd": cwd, "pid_raw": ""}]
    _attach_git_then_resolve_pids(one)
    pid = one[0].get("pid")
    if not current_name:
        raise HTTPException(404, f"target {target!r} not found")

    try:
        content = capture(target, lines=80)
        parsed = parse_pane(content)
    except Exception as e:
        raise HTTPException(500, str(e))
    plain = re.sub(r"\x1b\[[\d;]*m", "", content)
    snippet_lines = [ln for ln in plain.split("\n") if ln.strip()][-20:]
    snippet = "\n    ".join(snippet_lines)[-1200:]
    git = cached_git_state(cwd) or {}
    pr = cached_pr_state(cwd, git.get("branch")) or {}

    entry = {
        "index": index,
        "current_name": current_name,
        "branch": git.get("branch"),
        "pr": pr.get("pr"),
        "recap": parsed.get("recap"),
        "pending_input": parsed.get("pending_input"),
        "recent_excerpt": snippet,
    }
    entry.update(transcript_summary(session, index))
    ctx = [entry]
    prompt = build_rename_prompt(ctx)
    try:
        result = claude_complete(prompt)
    except Exception as e:
        raise HTTPException(500, str(e))
    cleaned = result.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE)
    try:
        new_names = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"claude returned invalid JSON: {e}")
    new_name = (new_names.get(str(index)) or "").strip()
    if not new_name:
        raise HTTPException(500, "claude returned empty name")
    if new_name == current_name:
        return {"ok": True, "applied": False, "old": current_name, "new": current_name, "pid": pid}
    tmux("rename-window", "-t", target, new_name)
    # Same cooldown contract as /api/rename; this route has no list_windows
    # dict, so resolve the pane id the same way that route does.
    pane_id = tmux("display-message", "-t", target, "-p", "#{pane_id}").strip()
    if pane_id:
        stamp_pane_rename(pane_id, name=new_name, at=int(time.time()))
    return {"ok": True, "applied": True, "old": current_name, "new": new_name, "pid": pid}

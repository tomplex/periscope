"""Safe filesystem access for periscope's file-preview overlay + reveal.

Sole filesystem-access seam for the client (invariant): every new route
that touches a file goes through here. Resolves user-supplied paths
against a cwd, refuses anything outside a small set of safe roots, caps
file size, and surfaces clean HTTPException codes.

Tmux-resolving variants (`_for_pane`) are thin wrappers below — kept
separate so unit tests of the pure resolution logic don't have to mock
tmux subprocess calls.
"""
import os
import subprocess
from pathlib import Path

from fastapi import HTTPException

from periscope.tmux import tmux


_MAX_BYTES_DEFAULT = 1_000_000


def _safe_roots(cwd: Path) -> list[Path]:
    """Roots a resolved path is allowed to live under.

    - cwd (and descendants)
    - the cwd's git repo root, if any
    - $HOME (~)
    - /tmp, /var/tmp — Tom occasionally pastes build-artifact paths

    Anything else → 403.
    """
    roots = [cwd]
    repo = _git_repo_root(cwd)
    if repo:
        roots.append(repo)
    home = Path(os.path.expanduser("~"))
    if home.exists():
        roots.append(home)
    for extra in ("/tmp", "/var/tmp"):
        p = Path(extra)
        if p.exists():
            roots.append(p)
    return [r.resolve() for r in roots]


def _git_repo_root(start: Path) -> Path | None:
    """Walk up from `start` looking for a .git dir. None if not found."""
    p = start.resolve()
    for ancestor in [p, *p.parents]:
        if (ancestor / ".git").exists():
            return ancestor
    return None


def _inside_any(resolved: Path, roots: list[Path]) -> bool:
    """True if `resolved` is `r` or a descendant of any `r` in roots.
    commonpath-based to defeat /foo vs /foobar prefix confusion."""
    rstr = str(resolved)
    for r in roots:
        try:
            if os.path.commonpath([rstr, str(r)]) == str(r):
                return True
        except ValueError:
            continue
    return False


def safe_read(cwd: str, raw_path: str,
              max_bytes: int = _MAX_BYTES_DEFAULT) -> tuple[str, str]:
    """Resolve `raw_path` against `cwd`, enforce safe roots, read as UTF-8.

    Returns (resolved_abs_path, contents).

    Raises HTTPException with:
      400 — empty path.
      403 — resolved path escapes the safe roots.
      404 — file missing.
      413 — file exceeds max_bytes.
      415 — file is not UTF-8 decodable (binary).
    """
    if not raw_path or not raw_path.strip():
        raise HTTPException(status_code=400, detail="empty path")

    cwd_p = Path(cwd).resolve()
    if not cwd_p.exists():
        raise HTTPException(status_code=400, detail=f"cwd does not exist: {cwd}")

    # Strip a trailing ":NN" line suffix if present; we don't read it here
    # but callers' regex may include it.
    candidate = raw_path
    if ":" in candidate and candidate.rsplit(":", 1)[1].isdigit():
        candidate = candidate.rsplit(":", 1)[0]

    expanded = os.path.expanduser(candidate)
    if os.path.isabs(expanded):
        target = Path(expanded)
    else:
        target = cwd_p / expanded
    try:
        resolved = target.resolve()
    except OSError:
        raise HTTPException(status_code=404, detail=f"path not resolvable: {candidate}")

    roots = _safe_roots(cwd_p)
    if not _inside_any(resolved, roots):
        raise HTTPException(status_code=403, detail="path outside safe roots")

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"no such file: {resolved}")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail="not a regular file")

    size = resolved.stat().st_size
    if size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"file too large ({size} > {max_bytes} bytes)",
        )

    blob = resolved.read_bytes()
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="binary file")

    return (str(resolved), text)


def safe_reveal(cwd: str, raw_path: str) -> None:
    """Resolve `raw_path` against `cwd`, enforce safe roots, `open -R`.

    Same gating as safe_read; on success runs macOS Finder reveal.
    """
    if not raw_path or not raw_path.strip():
        raise HTTPException(status_code=400, detail="empty path")
    cwd_p = Path(cwd).resolve()
    candidate = raw_path
    if ":" in candidate and candidate.rsplit(":", 1)[1].isdigit():
        candidate = candidate.rsplit(":", 1)[0]
    expanded = os.path.expanduser(candidate)
    target = Path(expanded) if os.path.isabs(expanded) else cwd_p / expanded
    try:
        resolved = target.resolve()
    except OSError:
        raise HTTPException(status_code=404, detail=f"path not resolvable: {candidate}")
    if not _inside_any(resolved, _safe_roots(cwd_p)):
        raise HTTPException(status_code=403, detail="path outside safe roots")
    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"no such file: {resolved}")
    # Best-effort; don't surface non-zero exits as 500 (Finder may be
    # closed, etc.). Logging level is debug because this is user-visible
    # and the failure mode is benign.
    subprocess.run(["open", "-R", str(resolved)], check=False)


def _cwd_for_target(target: str) -> str:
    """Resolve the pane's cwd via `tmux display-message`. Same one-shot
    pattern as periscope/turns.py:get_turns_for_pane."""
    try:
        out = tmux(
            "display-message", "-t", target, "-p", "#{pane_current_path}"
        ).strip()
    except Exception:
        raise HTTPException(status_code=404, detail=f"unknown pane: {target}")
    if not out:
        raise HTTPException(status_code=404, detail=f"pane has no cwd: {target}")
    return out


def safe_read_for_pane(target: str, raw_path: str,
                       max_bytes: int = _MAX_BYTES_DEFAULT) -> tuple[str, str]:
    """tmux-resolves cwd from `target`, then calls safe_read."""
    return safe_read(_cwd_for_target(target), raw_path, max_bytes)


def safe_reveal_for_pane(target: str, raw_path: str) -> None:
    """tmux-resolves cwd from `target`, then calls safe_reveal."""
    safe_reveal(_cwd_for_target(target), raw_path)

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


def _resolve_existing(cwd: str, raw_path: str) -> Path:
    """Shared gate: resolve `raw_path` against `cwd` and prove it is an
    existing path inside the safe roots.

    The single place traversal is refused. Every public entry point below
    routes through here rather than re-deriving the check — four near-copies
    of security-critical resolution is how one of them ends up subtly weaker
    than the others.

    Raises 400 (empty path / missing cwd), 403 (escapes roots), 404 (no such
    path).
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
    target = Path(expanded) if os.path.isabs(expanded) else cwd_p / expanded
    try:
        resolved = target.resolve()
    except OSError:
        raise HTTPException(status_code=404, detail=f"path not resolvable: {candidate}") from None

    if not _inside_any(resolved, _safe_roots(cwd_p)):
        raise HTTPException(status_code=403, detail="path outside safe roots")
    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"no such file: {resolved}")
    return resolved


def safe_resolve(cwd: str, raw_path: str) -> Path:
    """Resolve `raw_path` against `cwd`, enforce safe roots, return the
    resolved Path.

    Same gating as `safe_read` but doesn't read the file — for endpoints
    that stream the bytes themselves (FileResponse) and don't need a UTF-8
    decode. The `safe_read` size + UTF-8 caps don't apply here; callers
    enforce their own (typically larger) limit.
    """
    resolved = _resolve_existing(cwd, raw_path)
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail="not a regular file")
    return resolved


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
    resolved = safe_resolve(cwd, raw_path)
    return (str(resolved), read_text(resolved, max_bytes))


def read_text(resolved: Path, max_bytes: int = _MAX_BYTES_DEFAULT) -> str:
    """Size-capped UTF-8 read of an ALREADY-RESOLVED path.

    Split out of safe_read for callers that need the resolved Path itself
    (to stat it) and must not resolve twice — each resolution forks tmux
    for the pane's cwd, and two of them can disagree about the target.

    Raises 413 (over max_bytes) / 415 (not UTF-8).
    """
    size = resolved.stat().st_size
    if size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"file too large ({size} > {max_bytes} bytes)",
        )

    try:
        return resolved.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="binary file") from None


def safe_write(cwd: str, raw_path: str, content: str,
               base_mtime: float | None,
               max_bytes: int = _MAX_BYTES_DEFAULT) -> tuple[str, float]:
    """Overwrite an existing file with `content`. Returns (path, new mtime).

    `base_mtime` is the st_mtime the editor loaded. A file that moved since
    then 409s rather than being written: periscope's whole job is running
    Claude against these files, so "the thing you are editing changed under
    you" is the common case here, not the exotic one. Passing None is the
    deliberate overwrite path (the conflict banner's Overwrite button) — the
    only way to skip the check, and explicit at every call site.

    Creation is not supported. The tab viewer only ever opens files that
    already exist, so a path that doesn't resolve is a typo, not a new file;
    `safe_resolve` 404s it.

    Raises 400/403/404 (see `_resolve_existing`), 409 (changed on disk),
    413 (over max_bytes).
    """
    resolved = safe_resolve(cwd, raw_path)

    blob = content.encode("utf-8")
    if len(blob) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"content too large ({len(blob)} > {max_bytes} bytes)",
        )

    st = resolved.stat()
    if base_mtime is not None and st.st_mtime != base_mtime:
        raise HTTPException(
            status_code=409, detail="file changed on disk since it was loaded")

    # Write a sibling temp and rename over the target: a crash or a full
    # disk leaves the original intact instead of truncated, and no reader
    # (the preview poller, Claude, a build watcher) can observe a half-
    # written file. Sibling rather than /tmp so the rename stays within one
    # filesystem, which is what makes it atomic. os.replace swaps the inode,
    # so the original's mode has to be carried across explicitly.
    tmp = resolved.with_name(f".{resolved.name}.periscope-tmp")
    try:
        tmp.write_bytes(blob)
        os.chmod(tmp, st.st_mode & 0o7777)
        os.replace(tmp, resolved)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"write failed: {e}") from None

    return (str(resolved), resolved.stat().st_mtime)


def safe_reveal(cwd: str, raw_path: str) -> None:
    """Resolve `raw_path` against `cwd`, enforce safe roots, `open -R`.

    Same gating as safe_read; on success runs macOS Finder reveal.
    """
    resolved = _resolve_existing(cwd, raw_path)
    # Best-effort; don't surface non-zero exits as 500 (Finder may be
    # closed, etc.). The failure mode is benign and user-visible.
    subprocess.run(["open", "-R", str(resolved)], check=False)


def _cwd_for_target(target: str) -> str:
    """Resolve the pane's cwd via `tmux display-message`. Same one-shot
    pattern as periscope/turns.py:get_turns_for_pane."""
    try:
        out = tmux(
            "display-message", "-t", target, "-p", "#{pane_current_path}"
        ).strip()
    except Exception:
        raise HTTPException(status_code=404, detail=f"unknown pane: {target}") from None
    if not out:
        raise HTTPException(status_code=404, detail=f"pane has no cwd: {target}")
    return out


def safe_read_for_pane(target: str, raw_path: str,
                       max_bytes: int = _MAX_BYTES_DEFAULT) -> tuple[str, str]:
    """tmux-resolves cwd from `target`, then calls safe_read."""
    return safe_read(_cwd_for_target(target), raw_path, max_bytes)


def safe_write_for_pane(target: str, raw_path: str, content: str,
                        base_mtime: float | None) -> tuple[str, float]:
    """tmux-resolves cwd from `target`, then calls safe_write."""
    return safe_write(_cwd_for_target(target), raw_path, content, base_mtime)


def safe_reveal_for_pane(target: str, raw_path: str) -> None:
    """tmux-resolves cwd from `target`, then calls safe_reveal."""
    safe_reveal(_cwd_for_target(target), raw_path)


def safe_resolve_for_pane(target: str, raw_path: str) -> Path:
    """tmux-resolves cwd from `target`, then calls safe_resolve."""
    return safe_resolve(_cwd_for_target(target), raw_path)

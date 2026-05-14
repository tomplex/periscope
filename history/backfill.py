"""Multi-session orchestration for indexing ~/.claude/projects/*.jsonl.

Bounded concurrency via ThreadPoolExecutor (sync Anthropic client is thread-safe).
SQLite writes are serialized inside each index_one's own connection — WAL +
short transactions keep contention low at this scale.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .indexer import index_one

log = logging.getLogger(__name__)

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def find_jsonl_files(projects_dir: Path | None = None) -> list[Path]:
    """Find all JSONLs at depth-2 under projects_dir.

    If projects_dir is None (the default for backfill use), walks BOTH the
    live projects dir AND the archive dir. Archive entries are preferred
    over live entries when the same session-id exists in both."""
    from .indexer import ARCHIVE_DIR
    dirs: list[Path]
    if projects_dir is None:
        dirs = [ARCHIVE_DIR, DEFAULT_PROJECTS_DIR]  # archive first → wins on dedup
    else:
        dirs = [projects_dir]
    by_stem: dict[str, Path] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.glob("*/*.jsonl"):
            by_stem.setdefault(p.name, p)  # first-seen wins
    return sorted(by_stem.values())


def backfill(*, projects_dir: Path | None = None,
             db_path: Path | str | None = None,
             workers: int = 2,
             since: int | None = None) -> dict:
    """Index every JSONL under projects_dir (or both live + archive if None)."""
    paths = find_jsonl_files(projects_dir)
    if since is not None:
        paths = [p for p in paths if p.stat().st_mtime >= since]
    statuses: dict[str, int] = {}
    errors: list[tuple[str, str]] = []
    log.info("history.backfill: scanning %d jsonl files (workers=%d)", len(paths), workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(index_one, str(p), db_path=db_path): p for p in paths}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                result = fut.result()
                status = result.get("status", "?")
                statuses[status] = statuses.get(status, 0) + 1
            except Exception as e:
                log.warning("history.backfill: %s failed: %s", p, e)
                errors.append((str(p), str(e)))
                statuses["error"] = statuses.get("error", 0) + 1
    log.info("history.backfill: done — %s", statuses)
    return {"scanned": len(paths), "statuses": statuses, "errors": errors}

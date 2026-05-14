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


def find_jsonl_files(projects_dir: Path = DEFAULT_PROJECTS_DIR) -> list[Path]:
    if not projects_dir.is_dir():
        return []
    return sorted(projects_dir.glob("*/*.jsonl"))


def backfill(*, projects_dir: Path = DEFAULT_PROJECTS_DIR,
             db_path: Path | str | None = None,
             workers: int = 5,
             since: int | None = None) -> dict:
    """Index every JSONL under projects_dir. Idempotent."""
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

"""Shared pytest fixtures for history tests."""
import os
import sqlite3
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# Old timestamp used to back-date fixture JSONLs so the indexer's live-session
# heuristic (mtime < 60s ago) doesn't skip them. 2023-11-14, comfortably old.
OLD_MTIME = 1700000000


@pytest.fixture
def fixture_dir() -> Path:
    """Path to the JSONL fixtures directory. Back-dates every fixture's
    mtime so the indexer doesn't classify them as live sessions."""
    for p in FIXTURE_DIR.glob("*.jsonl"):
        os.utime(p, (OLD_MTIME, OLD_MTIME))
    return FIXTURE_DIR


@pytest.fixture
def in_memory_db() -> sqlite3.Connection:
    """A fresh in-memory SQLite with the history schema applied."""
    from history.db import apply_schema
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_schema(conn)
    yield conn
    conn.close()

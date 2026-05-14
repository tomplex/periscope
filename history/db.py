"""SQLite connection management + schema migration."""
import os
import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 1
MECHANICAL_VERSION = 1
DEFAULT_HAIKU_MODEL = "claude-haiku-4-5"

DEFAULT_DB_PATH = Path(os.environ.get("CLAUDE_HISTORY_DB") or
                       Path.home() / ".claude" / "history.db")

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()
_LOCK = threading.Lock()


def apply_schema(conn: sqlite3.Connection) -> None:
    """Run the schema DDL and seed meta keys. Idempotent."""
    conn.executescript(_SCHEMA_SQL)
    cur = conn.execute("SELECT key FROM meta")
    existing = {row[0] for row in cur}
    seed = {
        "schema_version":     str(SCHEMA_VERSION),
        "mechanical_version": str(MECHANICAL_VERSION),
        "haiku_model":        DEFAULT_HAIKU_MODEL,
    }
    for key, value in seed.items():
        if key not in existing:
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open the history DB in WAL mode with the schema applied.

    Threads racing on first-time creation can otherwise both try to set
    journal_mode=WAL + apply schema simultaneously before WAL takes effect,
    triggering `database is locked`. The lock serializes that one-time path."""
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = sqlite3.connect(str(path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        apply_schema(conn)
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()

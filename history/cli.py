"""CLI verb dispatch: `python -m history <verb> [args]`."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .db import connect, get_meta, set_meta


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        _print_help()
        return 2
    verb = argv.pop(0)
    handler = _VERBS.get(verb)
    if handler is None:
        # Print to stdout so tests/users see both the error and the verb list
        # in one place; help itself is informational, not an error.
        print(f"unknown verb: {verb!r}")
        _print_help()
        return 2
    return handler(argv)


def _print_help(*, file=None) -> None:
    # Default-evaluated `file=sys.stdout` binds at function-def time and would
    # bypass pytest's capsys replacement; resolve sys.stdout at call time.
    if file is None:
        file = sys.stdout
    print("usage: python -m history <verb> [args]", file=file)
    print("verbs: " + ", ".join(_VERBS), file=file)


# --- verb: hook ---------------------------------------------------------

def _cmd_hook(argv: list[str]) -> int:
    from .hook import run_hook
    return run_hook()


# --- verb: backfill -----------------------------------------------------

def _cmd_backfill(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="history backfill")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--projects-dir", default=None)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--since", default=None, help="YYYY-MM-DD or unix ts")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    from .backfill import backfill, find_jsonl_files
    from pathlib import Path
    projects = Path(args.projects_dir) if args.projects_dir else None
    since_ts = None
    if args.since:
        try:
            since_ts = int(args.since)
        except ValueError:
            from datetime import datetime
            since_ts = int(datetime.fromisoformat(args.since).timestamp())
    if args.dry_run:
        paths = find_jsonl_files(projects or None)
        print(f"would scan {len(paths)} jsonl files")
        return 0
    kwargs = {"workers": args.workers, "db_path": args.db_path, "since": since_ts}
    if projects is not None:
        kwargs["projects_dir"] = projects
    result = backfill(**kwargs)
    # Record when the last full scan happened for `stats`.
    conn = connect(args.db_path)
    try:
        import time as _t
        set_meta(conn, "last_full_scan_at", str(int(_t.time())))
    finally:
        conn.close()
    print(json.dumps(result, indent=2))
    return 0


# --- verb: search -------------------------------------------------------

def _cmd_search(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="history search")
    parser.add_argument("query", nargs="+")
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--project", default=None)
    parser.add_argument("--branch", default=None)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--include-trivial", action="store_true")
    args = parser.parse_args(argv)
    from .search import search
    results = search(
        " ".join(args.query),
        db_path=args.db_path,
        project=args.project,
        branch=args.branch,
        include_trivial=args.include_trivial,
        rerank=args.rerank,
        limit=args.limit,
    )
    for r in results:
        when = _fmt_ts(r["started_at"])
        print("\t".join([
            when,
            r["session_id"],
            r["project_path"],
            (r["summary"] or r["first_user_msg"] or "")[:200].replace("\n", " "),
        ]))
    return 0


# --- verb: stats --------------------------------------------------------

def _cmd_stats(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="history stats")
    parser.add_argument("--db-path", default=None)
    args = parser.parse_args(argv)
    conn = connect(args.db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        summarized = conn.execute("SELECT COUNT(*) FROM sessions WHERE summary IS NOT NULL AND summary_model IS NOT NULL").fetchone()[0]
        heuristic = conn.execute("SELECT COUNT(*) FROM sessions WHERE summary IS NOT NULL AND summary_model IS NULL").fetchone()[0]
        null_summary = conn.execute("SELECT COUNT(*) FROM sessions WHERE summary IS NULL").fetchone()[0]
        last_scan = get_meta(conn, "last_full_scan_at")
        model = get_meta(conn, "haiku_model")
        mech_ver = get_meta(conn, "mechanical_version")
    finally:
        conn.close()
    print(f"sessions: {total}")
    print(f"  Haiku-summarized: {summarized}")
    print(f"  heuristic (trivial): {heuristic}")
    print(f"  no summary: {null_summary}")
    print(f"  mechanical_version: {mech_ver}")
    print(f"  haiku_model: {model}")
    if last_scan:
        print(f"  last full scan: {_fmt_ts(int(last_scan))}")
    return 0


# --- verb: clean --------------------------------------------------------

def _cmd_clean(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="history clean")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    conn = connect(args.db_path)
    try:
        rows = conn.execute("SELECT session_id, jsonl_path FROM sessions").fetchall()
        orphans = [r["session_id"] for r in rows if not os.path.isfile(r["jsonl_path"])]
        if args.dry_run:
            print(f"would remove {len(orphans)} orphan rows")
            for sid in orphans:
                print(f"  - {sid}")
            return 0
        for sid in orphans:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
        conn.commit()
        print(f"removed {len(orphans)} rows")
    finally:
        conn.close()
    return 0


# --- verb: reindex ------------------------------------------------------

def _cmd_reindex(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="history reindex")
    parser.add_argument("--all", action="store_true", required=True,
                        help="re-extract every row (mechanical_version bump effect)")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--projects-dir", default=None)
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args(argv)
    # Bumping local MECHANICAL_VERSION constant happens at code-edit time;
    # this verb forces re-extraction in case rows were inserted under an older
    # version. It walks every JSONL again — Haiku is reused via hash if the
    # underlying content is stable, so this is a free pass in the common case.
    from .backfill import backfill
    from pathlib import Path
    projects = Path(args.projects_dir) if args.projects_dir else None
    kwargs = {"workers": args.workers, "db_path": args.db_path}
    if projects is not None:
        kwargs["projects_dir"] = projects
    result = backfill(**kwargs)
    print(json.dumps(result, indent=2))
    return 0


# --- verb: resummarize --------------------------------------------------

def _cmd_resummarize(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="history resummarize")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--missing", action="store_true",
                     help="only re-summarize rows where summary IS NULL")
    grp.add_argument("--all", action="store_true",
                     help="force re-summary of every row (clears summary_input_hash)")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args(argv)
    conn = connect(args.db_path)
    try:
        if args.all:
            conn.execute("UPDATE sessions SET summary_input_hash = NULL")
            conn.commit()
            print("cleared summary_input_hash for all rows")
            paths = [r["jsonl_path"] for r in conn.execute("SELECT jsonl_path FROM sessions")]
        else:
            paths = [r["jsonl_path"] for r in conn.execute(
                "SELECT jsonl_path FROM sessions WHERE summary IS NULL"
            )]
    finally:
        conn.close()
    if not paths:
        print("nothing to do")
        return 0
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .indexer import index_one
    statuses: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(index_one, p, db_path=args.db_path) for p in paths]
        for fut in as_completed(futures):
            try:
                status = fut.result().get("status", "?")
            except Exception:
                status = "error"
            statuses[status] = statuses.get(status, 0) + 1
    print(json.dumps({"scanned": len(paths), "statuses": statuses}, indent=2))
    return 0


# --- helpers ------------------------------------------------------------

def _fmt_ts(ts: int) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


_VERBS = {
    "hook":         _cmd_hook,
    "backfill":     _cmd_backfill,
    "search":       _cmd_search,
    "stats":        _cmd_stats,
    "clean":        _cmd_clean,
    "reindex":      _cmd_reindex,
    "resummarize":  _cmd_resummarize,
}

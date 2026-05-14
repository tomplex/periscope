"""CLI verb dispatch. Fleshed out in later tasks."""
import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m history <verb> [args]")
        print("verbs: backfill, hook, search, reindex, resummarize, stats, clean")
        return 2
    verb = argv.pop(0)
    print(f"verb {verb!r} not yet implemented")
    return 1

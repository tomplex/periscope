"""Claude Code conversation history indexer + search."""

__all__ = ["index_one", "search", "recent", "stats"]


def index_one(jsonl_path: str, **kwargs) -> dict:
    """Index (or re-index) one session. Returns a status dict.

    Forwards keyword arguments to history.indexer.index_one. Supported:
    db_path=Path|str|None, force=bool."""
    from .indexer import index_one as _impl
    return _impl(jsonl_path, **kwargs)


def search(query: str, **kwargs) -> list[dict]:
    """FTS5 search across indexed sessions. See history.search.search for kwargs."""
    from .search import search as _impl
    return _impl(query, **kwargs)


def recent(**kwargs) -> list[dict]:
    """Recent sessions by `started_at desc` — used when the UI has an empty
    search query. See history.search.recent for kwargs."""
    from .search import recent as _impl
    return _impl(**kwargs)


def stats(**kwargs) -> dict:
    """Index summary: total / summarized / heuristic counts, projects,
    last full scan, DB size. See history.search.stats for kwargs."""
    from .search import stats as _impl
    return _impl(**kwargs)

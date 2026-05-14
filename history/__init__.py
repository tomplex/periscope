"""Claude Code conversation history indexer + search."""

__all__ = ["index_one", "search"]


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

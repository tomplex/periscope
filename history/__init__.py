"""Claude Code conversation history indexer + search."""

__all__ = ["index_one", "search"]


def index_one(jsonl_path: str) -> dict:
    """Index (or re-index) one session. Returns a status dict."""
    from .indexer import index_one as _impl
    return _impl(jsonl_path)


def search(query: str, **kwargs) -> list[dict]:
    """FTS5 search across indexed sessions. See history.search.search for kwargs."""
    from .search import search as _impl
    return _impl(query, **kwargs)

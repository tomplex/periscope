"""Claude Code conversation history indexer + search.

Eager re-exports rather than lazy wrappers: lazy wrappers named like a
submodule (`search`) get permanently shadowed the first time their body
runs `from .search import ...`, because the submodule import re-binds
the parent attribute. Importing at module top is order-correct — Python
binds the function from the submodule into this package's namespace
AFTER the submodule import has already set the package attribute.
"""

from .indexer import index_one
from .search import recent, search, stats

__all__ = ["index_one", "search", "recent", "stats"]

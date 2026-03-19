"""Memento storage backends.

Public API
----------
The canonical exports for consumers of this package are:

* :class:`MemoryStore` — the shared Protocol (TRD §6.3)
* :class:`SearchFilters` — query filter dataclass
* :class:`MemoryResult` — result dataclass
* :class:`GraphitiStore` — Graphiti/FalkorDB-backed implementation

Note: imports are lazy-guarded so that a broken optional dependency in one
backend does not prevent the rest of the package from loading.
"""

try:
    from memento.stores.base import MemoryResult, MemoryStore, SearchFilters
    from memento.stores.graphiti_store import GraphitiStore
    from memento.stores.mem0_store import Mem0Store
    from memento.stores.session_store import SessionStore

    __all__ = [
        "GraphitiStore",
        "Mem0Store",
        "MemoryResult",
        "MemoryStore",
        "SearchFilters",
        "SessionStore",
    ]
except ImportError:
    # graphiti_core or another optional dependency is not available in this
    # environment.  Individual stores are still importable directly, e.g.:
    #   from memento.stores.session_store import SessionStore
    pass

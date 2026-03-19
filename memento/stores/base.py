"""Abstract store interface for Memento memory backends.

Defines the :class:`MemoryStore` :class:`~typing.Protocol` from TRD §6.3, plus
the shared data-transfer types :class:`SearchFilters` and :class:`MemoryResult`
that make the protocol usable without importing backend-specific code.

Imports are kept to intra-package schema types only; no third-party libraries
are required here so that other modules can import from this file without
pulling in heavy optional dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from memento.memory.schema import (
    MemoryObject,
    PromotionDecision,
    Scope,
    TrustTier,
)

__all__ = [
    "MemoryStore",
    "MemoryResult",
    "SearchFilters",
]


# ---------------------------------------------------------------------------
# SearchFilters – TRD §6.3
# ---------------------------------------------------------------------------


@dataclass
class SearchFilters:
    """Filter parameters for :meth:`MemoryStore.search`.

    All fields are optional; omitting a field means "no restriction on that
    dimension".  Multiple filters are ANDed together.

    Attributes
    ----------
    scope:
        Restrict results to PROJECT or ORG memories.
    project_id:
        Restrict results to a specific project (implies scope=PROJECT when
        set without an explicit *scope*).
    trust_tier_min:
        Only return memories whose ``trust_tier >= trust_tier_min``.
    entity_types:
        Restrict to a subset of entity type names
        (``"Incident"``, ``"Learning"``, ``"AntiPattern"``, ``"Policy"``).
    valid_at:
        Only return memories valid at this instant, i.e.
        ``valid_from <= valid_at`` and
        ``(valid_to is None or valid_to > valid_at)``.
        Must be timezone-aware if provided.
    tags:
        Only return memories that carry **all** of the listed tags.
    limit:
        Maximum number of results to return (default 10).
    """

    scope: Scope | None = None
    project_id: str | None = None
    trust_tier_min: TrustTier | None = None
    entity_types: list[str] | None = None
    valid_at: datetime | None = None
    tags: list[str] | None = None
    limit: int = 10


# ---------------------------------------------------------------------------
# MemoryResult
# ---------------------------------------------------------------------------


@dataclass
class MemoryResult:
    """A single result returned by :meth:`MemoryStore.search`.

    Attributes
    ----------
    memory:
        The matched :class:`~memento.memory.schema.MemoryObject`.
    score:
        Relevance score in the range [0.0, 1.0] produced by the backend's
        ranking algorithm.  Higher is more relevant.  The exact semantics
        depend on the backend (e.g. cosine similarity for vector search,
        RRF score for hybrid search).
    """

    memory: MemoryObject
    score: float = field(default=1.0)


# ---------------------------------------------------------------------------
# MemoryStore Protocol – TRD §6.3
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryStore(Protocol):
    """Protocol that every Memento storage backend must satisfy (TRD §6.3).

    Implementations are expected to be instantiated with their
    backend-specific configuration and then used as async context managers or
    have their lifecycle managed explicitly by the calling service.

    All methods are ``async`` so that I/O-bound backends (databases, HTTP
    APIs) can be awaited without blocking the event loop.
    """

    async def add(self, memory: MemoryObject) -> str:
        """Persist a :class:`~memento.memory.schema.MemoryObject`.

        Parameters
        ----------
        memory:
            The fully-constructed memory object to store.  The ``id`` field
            is used as the primary key; callers must ensure it is unique (UUID
            v4 is recommended).

        Returns
        -------
        str
            The stored object's ``id`` (echoed back for convenience).
        """
        ...

    async def search(self, query: str, filters: SearchFilters) -> list[MemoryResult]:
        """Perform a semantic search with optional filters.

        Parameters
        ----------
        query:
            Natural-language search query.
        filters:
            Field-level filters applied on top of the semantic ranking.

        Returns
        -------
        list[MemoryResult]
            Results ordered by descending relevance score, capped at
            ``filters.limit`` items.
        """
        ...

    async def get(self, memory_id: str) -> MemoryObject | None:
        """Retrieve a memory by its primary key.

        Parameters
        ----------
        memory_id:
            The ``id`` of the target :class:`~memento.memory.schema.MemoryObject`.

        Returns
        -------
        MemoryObject | None
            The object if found, ``None`` otherwise.
        """
        ...

    async def invalidate(self, memory_id: str, reason: str) -> None:
        """Soft-delete a memory by setting its ``valid_to`` to *now*.

        The record is kept for audit and provenance purposes but will be
        excluded from subsequent searches (unless callers pass ``valid_at``
        that falls before the invalidation time).

        Parameters
        ----------
        memory_id:
            The ``id`` of the memory to invalidate.
        reason:
            Human-readable explanation recorded alongside the invalidation.
        """
        ...

    async def update_trust_tier(
        self,
        memory_id: str,
        new_tier: TrustTier,
        decision: PromotionDecision,
    ) -> None:
        """Change the trust tier of a memory and record the audit trail.

        Parameters
        ----------
        memory_id:
            The ``id`` of the memory to promote or demote.
        new_tier:
            The target :class:`~memento.memory.schema.TrustTier`.
        decision:
            The fully-populated :class:`~memento.memory.schema.PromotionDecision`
            recording who made the decision and why.
        """
        ...

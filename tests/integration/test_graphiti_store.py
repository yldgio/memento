"""Integration tests for GraphitiStore against a real FalkorDB instance.

Skip conditions
---------------
Tests are skipped automatically when:

1. ``MEMENTO_RUN_INTEGRATION`` is not set to ``"1"``.
2. ``MEMENTO_LLM_API_KEY`` is absent (Graphiti's LLM-based extraction requires
   a real or stub key).
3. FalkorDB is not reachable at the configured host/port.

To run locally, start the compose stack and set the required environment
variables::

    docker compose up -d falkordb
    export MEMENTO_LLM_API_KEY=sk-...
    export MEMENTO_RUN_INTEGRATION=1
    pytest tests/integration/test_graphiti_store.py -v

Covered scenarios
-----------------
* ``initialize`` — indices and constraints are created without error.
* ``add`` + ``get`` — full round-trip with metadata preservation.
* ``add`` + ``search`` — semantic search returns expected memory.
* ``invalidate`` — ``valid_to`` is set; invalidated memory is excluded from
  time-filtered searches.
* ``update_trust_tier`` — tier and promotion_decisions are updated durably.
* Group-id isolation — project-A memories are not returned under project-B.
* ``close`` — driver is released without error.
"""

from __future__ import annotations

import os
import socket
import uuid
from datetime import UTC, datetime

import pytest

from memento.memory.schema import (
    Cell,
    Lifetime,
    MemoryObject,
    PromotionDecision,
    Provenance,
    Scope,
    TrustTier,
)
from memento.stores.base import SearchFilters

# ---------------------------------------------------------------------------
# Skip guards
# ---------------------------------------------------------------------------

_INTEGRATION_ENABLED = os.environ.get("MEMENTO_RUN_INTEGRATION", "").strip() == "1"
_HAS_API_KEY = bool(os.environ.get("MEMENTO_LLM_API_KEY", "").strip())


def _falkordb_reachable() -> bool:
    """Return True if FalkorDB is listening on localhost:6379."""
    host = os.environ.get("MEMENTO_FALKORDB_HOST", "localhost")
    port = int(os.environ.get("MEMENTO_FALKORDB_PORT", "6379"))
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not (_INTEGRATION_ENABLED and _HAS_API_KEY and _falkordb_reachable()),
    reason=(
        "Integration tests require MEMENTO_RUN_INTEGRATION=1, "
        "MEMENTO_LLM_API_KEY, and a running FalkorDB instance."
    ),
)

# ---------------------------------------------------------------------------
# Store factory
# ---------------------------------------------------------------------------


def _make_store() -> GraphitiStore:  # noqa: F821  (imported lazily below)
    """Construct a fully-initialised GraphitiStore pointing at local FalkorDB."""
    from graphiti_core import Graphiti
    from graphiti_core.embedder import OpenAIEmbedder
    from graphiti_core.embedder.openai import OpenAIEmbedderConfig
    from graphiti_core.llm_client import LLMConfig, OpenAIClient

    from memento.stores.graphiti_store import GraphitiStore

    host = os.environ.get("MEMENTO_FALKORDB_HOST", "localhost")
    port = int(os.environ.get("MEMENTO_FALKORDB_PORT", "6379"))
    uri = f"bolt://{host}:{port}"
    api_key = os.environ["MEMENTO_LLM_API_KEY"]
    base_url = os.environ.get("MEMENTO_LLM_BASE_URL", "https://api.openai.com/v1")
    model = os.environ.get("MEMENTO_LLM_MODEL", "gpt-4o")

    llm = OpenAIClient(LLMConfig(api_key=api_key, model=model, base_url=base_url))
    embedder = OpenAIEmbedder(
        OpenAIEmbedderConfig(api_key=api_key, base_url=base_url)
    )
    g = Graphiti(uri=uri, user="", password="", llm_client=llm, embedder=embedder)
    return GraphitiStore(g)


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _uid() -> str:
    return str(uuid.uuid4())


def _provenance() -> Provenance:
    return Provenance(
        source_session_id=_uid(),
        source_agent_id="test-agent",
        consolidation_batch_id=_uid(),
        consolidation_model="gpt-4o",
        created_by="integration-test",
    )


def _make_memory(
    project_id: str,
    content: str = "Always validate API inputs before processing.",
    scope: Scope = Scope.PROJECT,
    trust_tier: TrustTier = TrustTier.UNVERIFIED,
    tags: list[str] | None = None,
) -> MemoryObject:
    return MemoryObject(
        id=_uid(),
        content=content,
        scope=scope,
        lifetime=Lifetime.PERSISTENT,
        cell=Cell.C5,
        confidence=0.85,
        trust_tier=trust_tier,
        provenance=_provenance(),
        tags=tags or [],
        project_id=project_id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGraphitiStoreIntegration:
    """Real-DB integration tests for GraphitiStore.

    Each test method creates its own store and project partition to avoid
    cross-test interference.
    """

    @pytest.mark.asyncio
    async def test_initialize_idempotent(self) -> None:
        """initialize() can be called multiple times without error."""
        store = _make_store()
        try:
            await store.initialize()
            await store.initialize()  # second call must also succeed
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_add_and_get_round_trip(self) -> None:
        """add() followed by get() returns the original MemoryObject."""
        store = _make_store()
        await store.initialize()
        project_id = f"proj-{_uid()}"
        memory = _make_memory(project_id, content="Always use parameterised SQL queries.")

        try:
            returned_id = await store.add(memory)
            assert returned_id == memory.id

            retrieved = await store.get(memory.id)
            assert retrieved is not None
            assert retrieved.id == memory.id
            assert retrieved.content == memory.content
            assert retrieved.trust_tier == TrustTier.UNVERIFIED
            assert retrieved.scope == Scope.PROJECT
            assert retrieved.project_id == project_id
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing_id(self) -> None:
        """get() returns None for a non-existent UUID."""
        store = _make_store()
        await store.initialize()
        try:
            result = await store.get(_uid())
            assert result is None
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_search_returns_added_memory(self) -> None:
        """A memory stored with add() appears in search results for a relevant query."""
        store = _make_store()
        await store.initialize()
        project_id = f"proj-{_uid()}"
        memory = _make_memory(
            project_id,
            content="Never commit plaintext passwords to source control.",
            tags=["security"],
        )
        try:
            await store.add(memory)
            results = await store.search(
                "password security credentials",
                SearchFilters(project_id=project_id, limit=10),
            )
            found_ids = {r.memory.id for r in results}
            assert memory.id in found_ids, (
                f"Expected memory {memory.id} in search results; got {found_ids}"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_invalidate_sets_valid_to(self) -> None:
        """invalidate() sets valid_to; the memory is excluded from valid_at filtered search."""
        store = _make_store()
        await store.initialize()
        project_id = f"proj-{_uid()}"
        memory = _make_memory(project_id, content="Cache all database results aggressively.")
        try:
            await store.add(memory)

            before_invalidation = datetime.now(UTC)
            await store.invalidate(memory.id, "superseded by updated policy")

            retrieved = await store.get(memory.id)
            assert retrieved is not None, "Memory should still be retrievable after invalidation"
            assert retrieved.valid_to is not None, "valid_to must be set after invalidation"
            assert retrieved.valid_to >= before_invalidation

            # Memory should be excluded from a search filtered to the current moment
            now = datetime.now(UTC)
            results_now = await store.search(
                "cache database",
                SearchFilters(project_id=project_id, valid_at=now, limit=20),
            )
            found_ids = {r.memory.id for r in results_now}
            assert memory.id not in found_ids, (
                "Invalidated memory must not appear in valid_at-filtered search"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_update_trust_tier(self) -> None:
        """update_trust_tier() persists the new tier and appends the decision."""
        store = _make_store()
        await store.initialize()
        project_id = f"proj-{_uid()}"
        memory = _make_memory(project_id, trust_tier=TrustTier.UNVERIFIED)
        try:
            await store.add(memory)

            decision = PromotionDecision(
                from_tier=TrustTier.UNVERIFIED,
                to_tier=TrustTier.REVIEWED,
                decided_by="test-admin",
                decided_at=datetime.now(UTC),
                reason="integration test promotion",
            )
            await store.update_trust_tier(memory.id, TrustTier.REVIEWED, decision)

            updated = await store.get(memory.id)
            assert updated is not None
            assert updated.trust_tier == TrustTier.REVIEWED
            assert len(updated.provenance.promotion_decisions) == 1
            assert updated.provenance.promotion_decisions[0].decided_by == "test-admin"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_project_isolation(self) -> None:
        """Memories stored under project-A must not appear in searches for project-B."""
        store = _make_store()
        await store.initialize()
        project_a = f"proj-a-{_uid()}"
        project_b = f"proj-b-{_uid()}"
        memory_a = _make_memory(project_a, content="Use async I/O for all network calls.")
        try:
            await store.add(memory_a)

            results_b = await store.search(
                "async I/O network",
                SearchFilters(project_id=project_b, limit=20),
            )
            found_ids = {r.memory.id for r in results_b}
            assert memory_a.id not in found_ids, (
                "Project-A memory must not appear in Project-B search results"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_trust_tier_filter_in_search(self) -> None:
        """search() with trust_tier_min excludes memories below that tier."""
        store = _make_store()
        await store.initialize()
        project_id = f"proj-{_uid()}"
        unverified = _make_memory(
            project_id,
            content="Always write unit tests for new functions.",
            trust_tier=TrustTier.UNVERIFIED,
        )
        curated = _make_memory(
            project_id,
            content="Always write unit tests for every public method.",
            trust_tier=TrustTier.CURATED,
        )
        try:
            await store.add(unverified)
            await store.add(curated)

            results = await store.search(
                "unit tests",
                SearchFilters(
                    project_id=project_id,
                    trust_tier_min=TrustTier.CURATED,
                    limit=20,
                ),
            )
            returned_tiers = {r.memory.trust_tier for r in results}
            assert TrustTier.UNVERIFIED not in returned_tiers, (
                "Unverified memory must be excluded by trust_tier_min=CURATED filter"
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_close_is_safe(self) -> None:
        """close() releases resources without raising."""
        store = _make_store()
        await store.initialize()
        # Should not raise
        await store.close()


"""Integration tests for memento.stores.mem0_store.

These tests exercise the real Mem0 embedded vector store (no external service
required) using a local in-memory Qdrant instance.

Skip conditions
---------------
Tests are skipped automatically when:
1. ``MEMENTO_LLM_API_KEY`` is not set in the environment (no real LLM key).
2. The ``mem0`` package is not importable.
3. Mem0 cannot initialise its embedded vector store (e.g. qdrant-client absent).

Set ``MEMENTO_LLM_API_KEY=any-key`` and ``MEMENTO_RUN_INTEGRATION=1`` in the
environment to force the tests to run (they still work with a stub/offline key
for vector-only operations since ``infer=False`` bypasses LLM calls).

Covered scenarios
-----------------
* add() + get(): round-trip with full metadata preservation
* add() + search(): semantic search returns expected memory
* invalidate(): deleted memory not returned by get()
* update_trust_tier(): trust tier updated persistently
* Namespace isolation: project-A memories not visible under project-B
"""

from __future__ import annotations

import os
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
from memento.stores.mem0_store import Mem0Store, SearchFilters

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

_INTEGRATION_ENABLED = os.environ.get("MEMENTO_RUN_INTEGRATION", "").strip() == "1"
_HAS_API_KEY = bool(os.environ.get("MEMENTO_LLM_API_KEY", "").strip())

pytestmark = pytest.mark.skipif(
    not (_INTEGRATION_ENABLED and _HAS_API_KEY),
    reason=(
        "Integration tests skipped: set MEMENTO_RUN_INTEGRATION=1 and "
        "MEMENTO_LLM_API_KEY=<key> to enable."
    ),
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_provenance() -> Provenance:
    return Provenance(
        source_session_id=str(uuid.uuid4()),
        source_agent_id="integration-agent",
        consolidation_batch_id=str(uuid.uuid4()),
        consolidation_model="gpt-4o",
        created_by="integration-test",
    )


def _make_memory(
    content: str = "Always write tests before committing to main.",
    trust_tier: TrustTier = TrustTier.REVIEWED,
    project_id: str = "integ-proj",
    **kwargs: object,
) -> MemoryObject:
    return MemoryObject(
        id=str(uuid.uuid4()),
        content=content,
        scope=Scope.PROJECT,
        lifetime=Lifetime.PERSISTENT,
        cell=Cell.C5,
        confidence=0.88,
        trust_tier=trust_tier,
        provenance=_make_provenance(),
        tags=["testing", "ci"],
        project_id=project_id,
        created_at=datetime.now(UTC),
        valid_from=datetime.now(UTC),
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> object:
    """Build a minimal real Settings instance wired to env vars."""
    # conftest._clean_env already clears MEMENTO_* vars; re-inject what we need.
    monkeypatch.setenv("MEMENTO_LLM_API_KEY", os.environ["MEMENTO_LLM_API_KEY"])
    monkeypatch.setenv("MEMENTO_LLM_MODEL", os.environ.get("MEMENTO_LLM_MODEL", "gpt-4o"))
    from memento.config import Settings

    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def store(settings: object) -> Mem0Store:  # type: ignore[misc]
    """Create a real Mem0Store with an embedded (in-process) vector store."""
    from memento.config import Settings

    assert isinstance(settings, Settings)
    return Mem0Store(settings)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAddGet:
    @pytest.mark.asyncio
    async def test_roundtrip_preserves_metadata(self, store: Mem0Store) -> None:
        mem = _make_memory(trust_tier=TrustTier.CURATED)
        mem0_id = await store.add(mem)

        assert mem0_id, "add() must return a non-empty ID"

        retrieved = await store.get(mem0_id)
        assert retrieved is not None, f"get({mem0_id!r}) returned None"
        assert retrieved.id == mem.id
        assert retrieved.content == mem.content
        assert retrieved.trust_tier == TrustTier.CURATED
        assert retrieved.confidence == pytest.approx(0.88)
        assert "testing" in retrieved.tags
        assert retrieved.project_id == "integ-proj"
        assert retrieved.provenance.source_agent_id == "integration-agent"

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, store: Mem0Store) -> None:
        result = await store.get(str(uuid.uuid4()))
        assert result is None


class TestSearch:
    @pytest.mark.asyncio
    async def test_semantic_search_finds_memory(self, store: Mem0Store) -> None:
        mem = _make_memory(
            content="Use pytest fixtures for shared test setup to avoid duplication.",
            project_id="search-proj",
        )
        await store.add(mem)

        results = await store.search(
            "pytest test setup best practices",
            SearchFilters(project_id="search-proj", limit=5),
        )
        assert len(results) > 0
        ids = [r.memory.id for r in results]
        assert mem.id in ids

    @pytest.mark.asyncio
    async def test_trust_tier_filter_excludes_low_tier(self, store: Mem0Store) -> None:
        unverified = _make_memory(
            content="Unverified tip: use global state sparingly.",
            trust_tier=TrustTier.UNVERIFIED,
            project_id="tier-filter-proj",
        )
        curated = _make_memory(
            content="Curated tip: prefer dependency injection over global state.",
            trust_tier=TrustTier.CURATED,
            project_id="tier-filter-proj",
        )
        await store.add(unverified)
        await store.add(curated)

        results = await store.search(
            "global state",
            SearchFilters(
                project_id="tier-filter-proj",
                trust_tier_min=TrustTier.CURATED,
                limit=10,
            ),
        )
        ids = [r.memory.id for r in results]
        assert curated.id in ids
        assert unverified.id not in ids

    @pytest.mark.asyncio
    async def test_namespace_isolation(self, store: Mem0Store) -> None:
        """Memories in project-A must not appear in project-B search results."""
        mem_a = _make_memory(
            content="Project Alpha secret: use blue-green deployments.",
            project_id="proj-alpha",
        )
        await store.add(mem_a)

        results_b = await store.search(
            "blue-green deployments",
            SearchFilters(project_id="proj-beta", limit=10),
        )
        ids_b = [r.memory.id for r in results_b]
        assert mem_a.id not in ids_b


class TestInvalidate:
    @pytest.mark.asyncio
    async def test_deleted_memory_not_retrievable(self, store: Mem0Store) -> None:
        mem = _make_memory(content="Temporary learning — will be invalidated.")
        mem0_id = await store.add(mem)

        await store.invalidate(mem0_id, reason="superseded")

        result = await store.get(mem0_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_idempotent_invalidate(self, store: Mem0Store) -> None:
        """Double invalidate should not raise."""
        mem = _make_memory(content="Memory for idempotent delete test.")
        mem0_id = await store.add(mem)

        await store.invalidate(mem0_id, reason="first")
        await store.invalidate(mem0_id, reason="second")  # must not raise


class TestUpdateTrustTier:
    @pytest.mark.asyncio
    async def test_trust_tier_updated(self, store: Mem0Store) -> None:
        mem = _make_memory(
            content="Trust-tier update integration test memory.",
            trust_tier=TrustTier.UNVERIFIED,
            project_id="tier-update-proj",
        )
        mem0_id = await store.add(mem)

        decision = PromotionDecision(
            from_tier=TrustTier.UNVERIFIED,
            to_tier=TrustTier.REVIEWED,
            decided_by="integration-test-admin",
            decided_at=datetime.now(UTC),
            reason="Integration test promotion",
        )
        await store.update_trust_tier(mem0_id, TrustTier.REVIEWED, decision)

        updated = await store.get(mem0_id)
        assert updated is not None
        assert updated.trust_tier == TrustTier.REVIEWED

    @pytest.mark.asyncio
    async def test_update_nonexistent_is_noop(self, store: Mem0Store) -> None:
        """update_trust_tier on a missing ID must not raise."""
        decision = PromotionDecision(
            from_tier=TrustTier.UNVERIFIED,
            to_tier=TrustTier.REVIEWED,
            decided_by="test",
            decided_at=datetime.now(UTC),
            reason="noop test",
        )
        await store.update_trust_tier(str(uuid.uuid4()), TrustTier.REVIEWED, decision)

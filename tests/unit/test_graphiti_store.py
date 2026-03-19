"""Unit tests for memento.stores.graphiti_store.

All external calls (Graphiti driver / LLM / embedder) are mocked so no live
services are required.

Test classes
------------
TestMemoryStoreProtocol     -- isinstance checks
TestSearchFilters           -- dataclass construction and defaults
TestMemoryResult            -- dataclass construction
TestPassesFilters           -- _passes_filters helper
TestGraphitiStoreInitialize -- initialize / close lifecycle
TestGraphitiStoreAdd        -- add() delegation
TestGraphitiStoreGet        -- get() delegation
TestGraphitiStoreSearch     -- search() using mocked search_() + SearchResults
TestGraphitiStoreInvalidate -- invalidate() updates episode
TestGraphitiStoreUpdateTrustTier -- update_trust_tier() updates episode
TestRegisteredTypes         -- constant correctness
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memento.memory.schema import (
    AntiPattern,
    AppliesTo,
    CausedBy,
    Cell,
    Incident,
    Learning,
    Lifetime,
    MemoryObject,
    Policy,
    PromotionDecision,
    Provenance,
    Scope,
    Supersedes,
    TrustTier,
    edge_type_map,
)
from memento.stores.base import MemoryResult, MemoryStore, SearchFilters
from memento.stores.graphiti_store import (
    _EDGE_TYPE_MAP,
    _EDGE_TYPES,
    _ENTITY_TYPES,
    GraphitiStore,
    _passes_filters,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory(
    *,
    scope: Scope = Scope.PROJECT,
    project_id: str | None = "proj-1",
    trust_tier: TrustTier = TrustTier.UNVERIFIED,
    tags: list[str] | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> MemoryObject:
    now = valid_from or datetime(2024, 1, 1, tzinfo=UTC)
    prov = Provenance(
        source_session_id="sess-1",
        source_agent_id="agent-1",
        consolidation_batch_id="batch-1",
        consolidation_model="gpt-4o",
        created_by="consolidation-job",
    )
    return MemoryObject(
        id=str(uuid.uuid4()),
        cell=Cell.C5,
        lifetime=Lifetime.PERSISTENT,
        confidence=0.8,
        content="test content",
        scope=scope,
        project_id=project_id,
        trust_tier=trust_tier,
        tags=tags or [],
        valid_from=now,
        valid_to=valid_to,
        provenance=prov,
    )


def _make_mock_graphiti() -> MagicMock:
    """Return a MagicMock that mimics the Graphiti interface used by GraphitiStore."""
    g = MagicMock()
    g.driver = MagicMock()
    g.build_indices_and_constraints = AsyncMock()
    g.add_episode = AsyncMock()
    g.close = AsyncMock()
    mock_sr = MagicMock()
    mock_sr.episodes = []
    mock_sr.edges = []
    g.search_ = AsyncMock(return_value=mock_sr)
    return g


def _episode_mock(memory: MemoryObject) -> MagicMock:
    """Return a mock EpisodicNode whose .content is the serialised memory."""
    ep = MagicMock()
    ep.uuid = memory.id
    ep.content = memory.model_dump_json()
    ep.save = AsyncMock()
    return ep


def _mock_search_results(
    episodes: list[Any],
    edges: list[Any] | None = None,
    scores: list[float] | None = None,
) -> MagicMock:
    """Build a mock SearchResults object."""
    sr = MagicMock()
    sr.episodes = episodes
    sr.edges = edges or []
    sr.episode_reranker_scores = scores or []
    return sr


async def _async_get_ep(ep: Any) -> Any:
    """Async coroutine factory for patching EpisodicNode.get_by_uuid."""
    return ep


# ---------------------------------------------------------------------------
# TestMemoryStoreProtocol
# ---------------------------------------------------------------------------


class TestMemoryStoreProtocol:
    def test_graphiti_store_satisfies_protocol(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        assert isinstance(store, MemoryStore)

    def test_protocol_methods_present(self) -> None:
        for method in (
            "initialize",
            "close",
            "add",
            "search",
            "get",
            "invalidate",
            "update_trust_tier",
        ):
            assert hasattr(GraphitiStore, method), f"Missing method: {method}"


# ---------------------------------------------------------------------------
# TestSearchFilters
# ---------------------------------------------------------------------------


class TestSearchFilters:
    def test_defaults(self) -> None:
        f = SearchFilters()
        assert f.limit == 10
        assert f.scope is None
        assert f.project_id is None
        assert f.trust_tier_min is None
        assert f.tags is None
        assert f.valid_at is None
        assert f.entity_types is None

    def test_custom_values(self) -> None:
        f = SearchFilters(
            limit=5,
            scope=Scope.ORG,
            trust_tier_min=TrustTier.REVIEWED,
            tags=["incident"],
        )
        assert f.limit == 5
        assert f.scope == Scope.ORG
        assert f.trust_tier_min == TrustTier.REVIEWED
        assert f.tags == ["incident"]


# ---------------------------------------------------------------------------
# TestMemoryResult
# ---------------------------------------------------------------------------


class TestMemoryResult:
    def test_construction(self) -> None:
        m = _make_memory()
        r = MemoryResult(memory=m, score=0.9)
        assert r.memory is m
        assert r.score == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# TestPassesFilters
# ---------------------------------------------------------------------------


class TestPassesFilters:
    def test_no_filters_passes(self) -> None:
        m = _make_memory()
        assert _passes_filters(m, SearchFilters()) is True

    def test_scope_match(self) -> None:
        m = _make_memory(scope=Scope.PROJECT)
        assert _passes_filters(m, SearchFilters(scope=Scope.PROJECT)) is True

    def test_scope_mismatch(self) -> None:
        m = _make_memory(scope=Scope.PROJECT)
        assert _passes_filters(m, SearchFilters(scope=Scope.ORG)) is False

    def test_project_id_match(self) -> None:
        m = _make_memory(project_id="proj-1")
        assert _passes_filters(m, SearchFilters(project_id="proj-1")) is True

    def test_project_id_mismatch(self) -> None:
        m = _make_memory(project_id="proj-1")
        assert _passes_filters(m, SearchFilters(project_id="proj-2")) is False

    def test_trust_tier_min_pass(self) -> None:
        m = _make_memory(trust_tier=TrustTier.REVIEWED)
        assert _passes_filters(m, SearchFilters(trust_tier_min=TrustTier.REVIEWED)) is True

    def test_trust_tier_min_fail(self) -> None:
        m = _make_memory(trust_tier=TrustTier.UNVERIFIED)
        assert _passes_filters(m, SearchFilters(trust_tier_min=TrustTier.REVIEWED)) is False

    def test_trust_tier_unverified_less_than_reviewed(self) -> None:
        assert TrustTier.UNVERIFIED < TrustTier.REVIEWED

    def test_tag_match(self) -> None:
        m = _make_memory(tags=["incident", "critical"])
        assert _passes_filters(m, SearchFilters(tags=["incident"])) is True

    def test_tag_missing(self) -> None:
        m = _make_memory(tags=["incident"])
        assert _passes_filters(m, SearchFilters(tags=["learning"])) is False

    def test_multiple_tags_all_present(self) -> None:
        m = _make_memory(tags=["a", "b", "c"])
        assert _passes_filters(m, SearchFilters(tags=["a", "c"])) is True

    def test_multiple_tags_one_missing(self) -> None:
        m = _make_memory(tags=["a"])
        assert _passes_filters(m, SearchFilters(tags=["a", "b"])) is False

    def test_valid_at_within_window(self) -> None:
        now = datetime(2024, 6, 1, tzinfo=UTC)
        m = _make_memory(
            valid_from=datetime(2024, 1, 1, tzinfo=UTC),
            valid_to=datetime(2024, 12, 31, tzinfo=UTC),
        )
        assert _passes_filters(m, SearchFilters(valid_at=now)) is True

    def test_valid_at_before_valid_from(self) -> None:
        m = _make_memory(valid_from=datetime(2024, 6, 1, tzinfo=UTC))
        assert _passes_filters(m, SearchFilters(valid_at=datetime(2024, 1, 1, tzinfo=UTC))) is False

    def test_valid_at_after_valid_to(self) -> None:
        m = _make_memory(
            valid_from=datetime(2024, 1, 1, tzinfo=UTC),
            valid_to=datetime(2024, 3, 1, tzinfo=UTC),
        )
        assert _passes_filters(m, SearchFilters(valid_at=datetime(2024, 6, 1, tzinfo=UTC))) is False

    def test_valid_at_no_valid_to(self) -> None:
        m = _make_memory(valid_from=datetime(2024, 1, 1, tzinfo=UTC), valid_to=None)
        assert _passes_filters(m, SearchFilters(valid_at=datetime(2025, 1, 1, tzinfo=UTC))) is True


# ---------------------------------------------------------------------------
# TestGraphitiStoreInitialize
# ---------------------------------------------------------------------------


class TestGraphitiStoreInitialize:
    @pytest.mark.asyncio
    async def test_initialize_calls_build_indices(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        await store.initialize()
        g.build_indices_and_constraints.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_calls_graphiti_close(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        await store.close()
        g.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# TestGraphitiStoreAdd
# ---------------------------------------------------------------------------


class TestGraphitiStoreAdd:
    @pytest.mark.asyncio
    async def test_add_returns_memory_id(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory()
        result = await store.add(m)
        assert result == m.id

    @pytest.mark.asyncio
    async def test_add_calls_add_episode(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory()
        await store.add(m)
        g.add_episode.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_add_passes_uuid(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory()
        await store.add(m)
        _, kwargs = g.add_episode.call_args
        assert kwargs["uuid"] == m.id

    @pytest.mark.asyncio
    async def test_add_passes_entity_types(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory()
        await store.add(m)
        _, kwargs = g.add_episode.call_args
        assert kwargs["entity_types"] == _ENTITY_TYPES

    @pytest.mark.asyncio
    async def test_add_passes_edge_types(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory()
        await store.add(m)
        _, kwargs = g.add_episode.call_args
        assert kwargs["edge_types"] == _EDGE_TYPES

    @pytest.mark.asyncio
    async def test_add_passes_edge_type_map(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory()
        await store.add(m)
        _, kwargs = g.add_episode.call_args
        assert kwargs["edge_type_map"] == _EDGE_TYPE_MAP

    @pytest.mark.asyncio
    async def test_add_org_scope_uses_org_group_id(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory(scope=Scope.ORG)
        await store.add(m)
        _, kwargs = g.add_episode.call_args
        assert kwargs["group_id"] == "org"

    @pytest.mark.asyncio
    async def test_add_project_scope_uses_project_id(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory(scope=Scope.PROJECT, project_id="my-project")
        await store.add(m)
        _, kwargs = g.add_episode.call_args
        assert kwargs["group_id"] == "my-project"

    @pytest.mark.asyncio
    async def test_add_project_scope_no_project_id_defaults(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory(scope=Scope.PROJECT, project_id=None)
        await store.add(m)
        _, kwargs = g.add_episode.call_args
        assert kwargs["group_id"] == "default"

    @pytest.mark.asyncio
    async def test_add_body_is_json(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory()
        await store.add(m)
        _, kwargs = g.add_episode.call_args
        parsed = MemoryObject.model_validate_json(kwargs["episode_body"])
        assert parsed.id == m.id


# ---------------------------------------------------------------------------
# TestGraphitiStoreGet
# ---------------------------------------------------------------------------


class TestGraphitiStoreGet:
    @pytest.mark.asyncio
    async def test_get_returns_memory(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory()
        ep = _episode_mock(m)

        async def _get_ep(*a: Any, **kw: Any) -> Any:
            return ep

        with patch(
            "memento.stores.graphiti_store.EpisodicNode.get_by_uuid",
            side_effect=_get_ep,
        ):
            result = await store.get(m.id)
        assert result is not None
        assert result.id == m.id

    @pytest.mark.asyncio
    async def test_get_returns_none_when_not_found(self) -> None:
        from graphiti_core.errors import NodeNotFoundError

        g = _make_mock_graphiti()
        store = GraphitiStore(g)

        with patch(
            "memento.stores.graphiti_store.EpisodicNode.get_by_uuid",
            side_effect=NodeNotFoundError("x"),
        ):
            result = await store.get("missing-uuid")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_none_for_non_memory_episode(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        ep = MagicMock()
        ep.uuid = "ep-1"
        ep.content = "not valid json for a MemoryObject"

        async def _get_ep(*a: Any, **kw: Any) -> Any:
            return ep

        with patch(
            "memento.stores.graphiti_store.EpisodicNode.get_by_uuid",
            side_effect=_get_ep,
        ):
            result = await store.get("ep-1")
        assert result is None


# ---------------------------------------------------------------------------
# TestGraphitiStoreSearch
# ---------------------------------------------------------------------------


class TestGraphitiStoreSearch:
    @pytest.mark.asyncio
    async def test_search_returns_empty_list_on_no_results(self) -> None:
        g = _make_mock_graphiti()
        g.search_.return_value = _mock_search_results([], [])
        store = GraphitiStore(g)
        results = await store.search("query", SearchFilters())
        assert results == []

    @pytest.mark.asyncio
    async def test_search_calls_search_underscore(self) -> None:
        g = _make_mock_graphiti()
        g.search_.return_value = _mock_search_results([], [])
        store = GraphitiStore(g)
        await store.search("find me", SearchFilters())
        g.search_.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_passes_query(self) -> None:
        g = _make_mock_graphiti()
        g.search_.return_value = _mock_search_results([], [])
        store = GraphitiStore(g)
        await store.search("my query", SearchFilters())
        _, kwargs = g.search_.call_args
        assert kwargs["query"] == "my query"

    @pytest.mark.asyncio
    async def test_search_direct_episodes_parsed(self) -> None:
        g = _make_mock_graphiti()
        m = _make_memory()
        ep = _episode_mock(m)
        g.search_.return_value = _mock_search_results([ep], [])
        store = GraphitiStore(g)
        results = await store.search("q", SearchFilters())
        assert len(results) == 1
        assert results[0].memory.id == m.id

    @pytest.mark.asyncio
    async def test_search_score_first_result_is_1(self) -> None:
        g = _make_mock_graphiti()
        m = _make_memory()
        ep = _episode_mock(m)
        g.search_.return_value = _mock_search_results([ep], [])
        store = GraphitiStore(g)
        results = await store.search("q", SearchFilters())
        assert results[0].score == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_search_score_decreases_with_rank(self) -> None:
        g = _make_mock_graphiti()
        m1 = _make_memory()
        m2 = _make_memory()
        ep1 = _episode_mock(m1)
        ep2 = _episode_mock(m2)
        g.search_.return_value = _mock_search_results([ep1, ep2], [], scores=[0.9, 0.5])
        store = GraphitiStore(g)
        results = await store.search("q", SearchFilters())
        assert results[0].score > results[1].score

    @pytest.mark.asyncio
    async def test_search_respects_limit(self) -> None:
        g = _make_mock_graphiti()
        memories = [_make_memory() for _ in range(10)]
        episodes = [_episode_mock(m) for m in memories]
        g.search_.return_value = _mock_search_results(episodes, [])
        store = GraphitiStore(g)
        results = await store.search("q", SearchFilters(limit=3))
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_search_filters_by_scope(self) -> None:
        g = _make_mock_graphiti()
        proj_mem = _make_memory(scope=Scope.PROJECT)
        org_mem = _make_memory(scope=Scope.ORG)
        ep1 = _episode_mock(proj_mem)
        ep2 = _episode_mock(org_mem)
        g.search_.return_value = _mock_search_results([ep1, ep2], [])
        store = GraphitiStore(g)
        results = await store.search("q", SearchFilters(scope=Scope.PROJECT))
        assert all(r.memory.scope == Scope.PROJECT for r in results)

    @pytest.mark.asyncio
    async def test_search_filters_by_trust_tier(self) -> None:
        g = _make_mock_graphiti()
        low = _make_memory(trust_tier=TrustTier.UNVERIFIED)
        high = _make_memory(trust_tier=TrustTier.REVIEWED)
        ep1 = _episode_mock(low)
        ep2 = _episode_mock(high)
        g.search_.return_value = _mock_search_results([ep1, ep2], [])
        store = GraphitiStore(g)
        results = await store.search("q", SearchFilters(trust_tier_min=TrustTier.REVIEWED))
        assert all(r.memory.trust_tier >= TrustTier.REVIEWED for r in results)

    @pytest.mark.asyncio
    async def test_search_filters_by_tag(self) -> None:
        g = _make_mock_graphiti()
        tagged = _make_memory(tags=["critical"])
        untagged = _make_memory(tags=[])
        ep1 = _episode_mock(tagged)
        ep2 = _episode_mock(untagged)
        g.search_.return_value = _mock_search_results([ep1, ep2], [])
        store = GraphitiStore(g)
        results = await store.search("q", SearchFilters(tags=["critical"]))
        assert len(results) == 1
        assert "critical" in results[0].memory.tags

    @pytest.mark.asyncio
    async def test_search_filters_by_project_id(self) -> None:
        g = _make_mock_graphiti()
        m_match = _make_memory(project_id="proj-1")
        m_other = _make_memory(project_id="proj-2")
        ep1 = _episode_mock(m_match)
        ep2 = _episode_mock(m_other)
        g.search_.return_value = _mock_search_results([ep1, ep2], [])
        store = GraphitiStore(g)
        results = await store.search("q", SearchFilters(project_id="proj-1"))
        assert all(r.memory.project_id == "proj-1" for r in results)

    @pytest.mark.asyncio
    async def test_search_edge_derived_episodes_fetched(self) -> None:
        """Episodes referenced through edges (not in raw.episodes) are fetched."""
        g = _make_mock_graphiti()
        m = _make_memory()
        edge_mock = MagicMock()
        edge_mock.episodes = [m.id]
        g.search_.return_value = _mock_search_results([], [edge_mock])
        ep = _episode_mock(m)

        async def _get_ep(*a: Any, **kw: Any) -> Any:
            return ep

        with patch(
            "memento.stores.graphiti_store.EpisodicNode.get_by_uuid",
            side_effect=_get_ep,
        ):
            store = GraphitiStore(g)
            results = await store.search("q", SearchFilters())
        assert len(results) == 1
        assert results[0].memory.id == m.id

    @pytest.mark.asyncio
    async def test_search_deduplicates_episode_uuids(self) -> None:
        """An episode in both raw.episodes and raw.edges is not duplicated."""
        g = _make_mock_graphiti()
        m = _make_memory()
        ep = _episode_mock(m)
        edge_mock = MagicMock()
        edge_mock.episodes = [m.id]
        g.search_.return_value = _mock_search_results([ep], [edge_mock])
        store = GraphitiStore(g)
        results = await store.search("q", SearchFilters())
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_search_org_scope_passes_org_group_id(self) -> None:
        g = _make_mock_graphiti()
        g.search_.return_value = _mock_search_results([], [])
        store = GraphitiStore(g)
        await store.search("q", SearchFilters(scope=Scope.ORG))
        _, kwargs = g.search_.call_args
        assert kwargs["group_ids"] == ["org"]

    @pytest.mark.asyncio
    async def test_search_project_id_filter_passes_group_ids(self) -> None:
        g = _make_mock_graphiti()
        g.search_.return_value = _mock_search_results([], [])
        store = GraphitiStore(g)
        await store.search("q", SearchFilters(project_id="p1"))
        _, kwargs = g.search_.call_args
        assert kwargs["group_ids"] == ["p1"]

    @pytest.mark.asyncio
    async def test_search_no_scope_passes_none_group_ids(self) -> None:
        g = _make_mock_graphiti()
        g.search_.return_value = _mock_search_results([], [])
        store = GraphitiStore(g)
        await store.search("q", SearchFilters())
        _, kwargs = g.search_.call_args
        assert kwargs["group_ids"] is None

    @pytest.mark.asyncio
    async def test_search_valid_at_filters_expired(self) -> None:
        g = _make_mock_graphiti()
        expired = _make_memory(
            valid_from=datetime(2020, 1, 1, tzinfo=UTC),
            valid_to=datetime(2021, 1, 1, tzinfo=UTC),
        )
        active = _make_memory(valid_from=datetime(2020, 1, 1, tzinfo=UTC), valid_to=None)
        ep1 = _episode_mock(expired)
        ep2 = _episode_mock(active)
        g.search_.return_value = _mock_search_results([ep1, ep2], [])
        store = GraphitiStore(g)
        results = await store.search("q", SearchFilters(valid_at=datetime(2024, 6, 1, tzinfo=UTC)))
        assert len(results) == 1
        assert results[0].memory.id == active.id

    @pytest.mark.asyncio
    async def test_search_skips_non_memory_episodes(self) -> None:
        g = _make_mock_graphiti()
        bad_ep = MagicMock()
        bad_ep.uuid = "bad-ep"
        bad_ep.content = "not-a-memory-object"
        m = _make_memory()
        good_ep = _episode_mock(m)
        g.search_.return_value = _mock_search_results([bad_ep, good_ep], [])
        store = GraphitiStore(g)
        results = await store.search("q", SearchFilters())
        assert len(results) == 1
        assert results[0].memory.id == m.id

    @pytest.mark.asyncio
    async def test_search_config_limit_overridden(self) -> None:
        """search_ must receive a config with over-fetch limit, not the global constant."""
        from memento.stores.graphiti_store import _COMBINED_SEARCH_CONFIG

        g = _make_mock_graphiti()
        g.search_.return_value = _mock_search_results([], [])
        store = GraphitiStore(g)
        await store.search("q", SearchFilters(limit=5))
        _, kwargs = g.search_.call_args
        config = kwargs["config"]
        # max(5*3, 30) = 30
        assert config.limit == 30
        # Must be a copy, not the global constant
        assert config is not _COMBINED_SEARCH_CONFIG


# ---------------------------------------------------------------------------
# TestGraphitiStoreInvalidate
# ---------------------------------------------------------------------------


class TestGraphitiStoreInvalidate:
    @pytest.mark.asyncio
    async def test_invalidate_sets_valid_to(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory()
        ep = _episode_mock(m)

        async def _get_ep(*a: Any, **kw: Any) -> Any:
            return ep

        with patch(
            "memento.stores.graphiti_store.EpisodicNode.get_by_uuid",
            side_effect=_get_ep,
        ):
            await store.invalidate(m.id, "test reason")

        updated = MemoryObject.model_validate_json(ep.content)
        assert updated.valid_to is not None

    @pytest.mark.asyncio
    async def test_invalidate_calls_save(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory()
        ep = _episode_mock(m)

        async def _get_ep(*a: Any, **kw: Any) -> Any:
            return ep

        with patch(
            "memento.stores.graphiti_store.EpisodicNode.get_by_uuid",
            side_effect=_get_ep,
        ):
            await store.invalidate(m.id, "reason")

        ep.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalidate_noop_when_not_found(self) -> None:
        from graphiti_core.errors import NodeNotFoundError

        g = _make_mock_graphiti()
        store = GraphitiStore(g)

        with patch(
            "memento.stores.graphiti_store.EpisodicNode.get_by_uuid",
            side_effect=NodeNotFoundError("x"),
        ):
            await store.invalidate("missing", "reason")

    @pytest.mark.asyncio
    async def test_invalidate_valid_to_is_recent(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory()
        ep = _episode_mock(m)
        before = datetime.now(UTC)

        async def _get_ep(*a: Any, **kw: Any) -> Any:
            return ep

        with patch(
            "memento.stores.graphiti_store.EpisodicNode.get_by_uuid",
            side_effect=_get_ep,
        ):
            await store.invalidate(m.id, "reason")

        after = datetime.now(UTC)
        updated = MemoryObject.model_validate_json(ep.content)
        assert updated.valid_to is not None
        assert before <= updated.valid_to <= after + timedelta(seconds=1)


# ---------------------------------------------------------------------------
# TestGraphitiStoreUpdateTrustTier
# ---------------------------------------------------------------------------


class TestGraphitiStoreUpdateTrustTier:
    def _make_decision(self) -> PromotionDecision:
        return PromotionDecision(
            from_tier=TrustTier.UNVERIFIED,
            to_tier=TrustTier.REVIEWED,
            reason="looks good",
            decided_by="test-agent",
            decided_at=datetime.now(UTC),
        )

    @pytest.mark.asyncio
    async def test_update_trust_tier_changes_tier(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory(trust_tier=TrustTier.UNVERIFIED)
        ep = _episode_mock(m)

        async def _get_ep(*a: Any, **kw: Any) -> Any:
            return ep

        with patch(
            "memento.stores.graphiti_store.EpisodicNode.get_by_uuid",
            side_effect=_get_ep,
        ):
            await store.update_trust_tier(m.id, TrustTier.REVIEWED, self._make_decision())

        updated = MemoryObject.model_validate_json(ep.content)
        assert updated.trust_tier == TrustTier.REVIEWED

    @pytest.mark.asyncio
    async def test_update_trust_tier_appends_decision(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory()
        ep = _episode_mock(m)
        decision = self._make_decision()

        async def _get_ep(*a: Any, **kw: Any) -> Any:
            return ep

        with patch(
            "memento.stores.graphiti_store.EpisodicNode.get_by_uuid",
            side_effect=_get_ep,
        ):
            await store.update_trust_tier(m.id, TrustTier.REVIEWED, decision)

        updated = MemoryObject.model_validate_json(ep.content)
        assert len(updated.provenance.promotion_decisions) == 1
        assert updated.provenance.promotion_decisions[0].reason == "looks good"

    @pytest.mark.asyncio
    async def test_update_trust_tier_calls_save(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory()
        ep = _episode_mock(m)

        async def _get_ep(*a: Any, **kw: Any) -> Any:
            return ep

        with patch(
            "memento.stores.graphiti_store.EpisodicNode.get_by_uuid",
            side_effect=_get_ep,
        ):
            await store.update_trust_tier(m.id, TrustTier.REVIEWED, self._make_decision())

        ep.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_trust_tier_noop_when_not_found(self) -> None:
        from graphiti_core.errors import NodeNotFoundError

        g = _make_mock_graphiti()
        store = GraphitiStore(g)

        with patch(
            "memento.stores.graphiti_store.EpisodicNode.get_by_uuid",
            side_effect=NodeNotFoundError("x"),
        ):
            await store.update_trust_tier("missing", TrustTier.REVIEWED, self._make_decision())

    @pytest.mark.asyncio
    async def test_update_trust_tier_multiple_decisions_accumulate(self) -> None:
        g = _make_mock_graphiti()
        store = GraphitiStore(g)
        m = _make_memory()
        ep = _episode_mock(m)

        d1 = self._make_decision()
        d2 = PromotionDecision(
            from_tier=TrustTier.REVIEWED,
            to_tier=TrustTier.CURATED,
            reason="also good",
            decided_by="other-agent",
            decided_at=datetime.now(UTC),
        )

        async def _get_ep(*a: Any, **kw: Any) -> Any:
            return ep

        with patch(
            "memento.stores.graphiti_store.EpisodicNode.get_by_uuid",
            side_effect=_get_ep,
        ):
            await store.update_trust_tier(m.id, TrustTier.REVIEWED, d1)

        with patch(
            "memento.stores.graphiti_store.EpisodicNode.get_by_uuid",
            side_effect=_get_ep,
        ):
            await store.update_trust_tier(m.id, TrustTier.CURATED, d2)

        updated = MemoryObject.model_validate_json(ep.content)
        assert len(updated.provenance.promotion_decisions) == 2


# ---------------------------------------------------------------------------
# TestRegisteredTypes
# ---------------------------------------------------------------------------


class TestRegisteredTypes:
    def test_entity_types_keys(self) -> None:
        assert set(_ENTITY_TYPES.keys()) == {"Incident", "Learning", "AntiPattern", "Policy"}

    def test_entity_types_values(self) -> None:
        assert _ENTITY_TYPES["Incident"] is Incident
        assert _ENTITY_TYPES["Learning"] is Learning
        assert _ENTITY_TYPES["AntiPattern"] is AntiPattern
        assert _ENTITY_TYPES["Policy"] is Policy

    def test_edge_types_keys(self) -> None:
        assert set(_EDGE_TYPES.keys()) == {"Supersedes", "CausedBy", "AppliesTo"}

    def test_edge_types_values(self) -> None:
        assert _EDGE_TYPES["Supersedes"] is Supersedes
        assert _EDGE_TYPES["CausedBy"] is CausedBy
        assert _EDGE_TYPES["AppliesTo"] is AppliesTo

    def test_edge_type_map_matches_schema(self) -> None:
        assert _EDGE_TYPE_MAP == edge_type_map

    def test_edge_type_map_non_empty(self) -> None:
        assert len(_EDGE_TYPE_MAP) > 0

    def test_all_edge_map_values_are_lists(self) -> None:
        for key, val in _EDGE_TYPE_MAP.items():
            assert isinstance(val, list), f"Expected list for key {key}, got {type(val)}"


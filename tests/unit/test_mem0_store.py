"""Unit tests for memento.stores.mem0_store.

Tests every requirement from TRD §6.3:
- add()          – namespace projection, metadata serialisation, infer=False
- search()       – query delegation, post-retrieval filter matrix, score passthrough
- get()          – hit/miss, corrupt-payload handling
- invalidate()   – delegates to Mem0 delete()
- update_trust_tier() – metadata-level update via _update_memory, audit trail
- Helpers        – _namespace, _build_mem0_config, _to_mem0_metadata / _from_mem0_result

All I/O is mocked via AsyncMock (the implementation awaits _mem methods directly).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
from memento.stores.mem0_store import (
    Mem0Store,
    _build_mem0_config,
    _from_mem0_result,
    _namespace,
    _to_mem0_metadata,
)

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_provenance(**overrides: Any) -> Provenance:
    return Provenance(
        source_session_id=overrides.get("source_session_id", "sess-001"),
        source_agent_id=overrides.get("source_agent_id", "agent-001"),
        consolidation_batch_id=overrides.get("consolidation_batch_id", "batch-001"),
        consolidation_model=overrides.get("consolidation_model", "gpt-4o"),
        created_by=overrides.get("created_by", "consolidation-job"),
    )


def _make_memory(**overrides: Any) -> MemoryObject:
    defaults: dict[str, Any] = {
        "id": "mem-001",
        "content": "Use async context managers for SQLite tests.",
        "scope": Scope.PROJECT,
        "lifetime": Lifetime.PERSISTENT,
        "cell": Cell.C5,
        "confidence": 0.9,
        "trust_tier": TrustTier.REVIEWED,
        "provenance": _make_provenance(),
        "tags": ["python", "testing"],
        "project_id": "proj-alpha",
        "created_at": datetime(2024, 1, 1, tzinfo=UTC),
        "valid_from": datetime(2024, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return MemoryObject(**defaults)


def _make_settings(**overrides: Any) -> MagicMock:
    settings = MagicMock()
    settings.llm_base_url = overrides.get("llm_base_url", "https://api.openai.com/v1")
    settings.llm_model = overrides.get("llm_model", "gpt-4o")
    settings.data_dir = Path(overrides.get("data_dir", r"C:\temp\memento-test"))
    api_key = MagicMock()
    api_key.get_secret_value.return_value = overrides.get("api_key", "test-key")
    settings.llm_api_key = api_key
    return settings


def _mem0_item(memory: MemoryObject, score: float = 0.85) -> dict[str, Any]:
    """Build a fake Mem0 result dict from a MemoryObject."""
    return {
        "id": f"mem0-{memory.id}",
        "memory": memory.content,
        "score": score,
        "metadata": _to_mem0_metadata(memory),
    }


def _make_store() -> Mem0Store:
    """Return a Mem0Store with _mem replaced by a fresh AsyncMock."""
    store = Mem0Store(_make_settings())
    store._mem = AsyncMock()
    return store


# ---------------------------------------------------------------------------
# TestHelpers
# ---------------------------------------------------------------------------


class TestHelpers:
    # ------ _namespace -------------------------------------------------------

    def test_namespace_formats_correctly(self) -> None:
        assert _namespace("proj-abc") == "project:proj-abc"

    def test_namespace_preserves_special_chars(self) -> None:
        assert _namespace("my.org/sub") == "project:my.org/sub"

    # ------ _build_mem0_config -----------------------------------------------

    def test_build_config_openai(self) -> None:
        config = _build_mem0_config(_make_settings())
        assert config["llm"]["provider"] == "openai"
        assert config["llm"]["config"]["api_key"] == "test-key"
        assert config["llm"]["config"]["model"] == "gpt-4o"

    def test_build_config_vector_store_path(self) -> None:
        config = _build_mem0_config(_make_settings())
        path: str = config["vector_store"]["config"]["path"]
        # Accept both POSIX and Windows separators
        assert "memento-test" in path and path.endswith("mem0")

    def test_build_config_ollama_provider(self) -> None:
        config = _build_mem0_config(_make_settings(llm_base_url="http://localhost:11434/v1"))
        assert config["llm"]["provider"] == "ollama"

    def test_build_config_azure_provider(self) -> None:
        config = _build_mem0_config(
            _make_settings(llm_base_url="https://myresource.openai.azure.com")
        )
        assert config["llm"]["provider"] == "azure_openai"

    def test_build_config_non_openai_sets_base_url(self) -> None:
        config = _build_mem0_config(_make_settings(llm_base_url="http://localhost:11434/v1"))
        assert "openai_base_url" in config["llm"]["config"]

    # ------ _to_mem0_metadata / _from_mem0_result ----------------------------

    def test_to_mem0_metadata_contains_all_fields(self) -> None:
        memory = _make_memory()
        meta = _to_mem0_metadata(memory)
        for key in (
            "memento_id",
            "scope",
            "lifetime",
            "cell",
            "confidence",
            "trust_tier",
            "tags",
            "project_id",
            "created_at_iso",
            "valid_from_iso",
            "prov_source_session_id",
            "prov_source_agent_id",
            "prov_batch_id",
            "prov_model",
            "prov_created_by",
            "original_content",
        ):
            assert key in meta, f"Missing key: {key}"

    def test_round_trip_preserves_core_fields(self) -> None:
        memory = _make_memory()
        result = _from_mem0_result(_mem0_item(memory))
        assert result.id == memory.id
        assert result.content == memory.content
        assert result.trust_tier == memory.trust_tier
        assert result.scope == memory.scope
        assert result.tags == memory.tags

    def test_round_trip_preserves_provenance(self) -> None:
        memory = _make_memory()
        result = _from_mem0_result(_mem0_item(memory))
        assert result.provenance.source_session_id == memory.provenance.source_session_id
        assert result.provenance.consolidation_model == memory.provenance.consolidation_model

    def test_from_mem0_result_handles_missing_optional_fields(self) -> None:
        """Valid_to and superseded_by may be absent; should not raise."""
        memory = _make_memory(valid_to=None, superseded_by=None)
        item = _mem0_item(memory)
        # remove optional keys to simulate stripped metadata
        item["metadata"].pop("valid_to_iso", None)
        item["metadata"].pop("superseded_by", None)
        result = _from_mem0_result(item)
        assert result.valid_to is None
        assert result.superseded_by is None


# ---------------------------------------------------------------------------
# TestAdd
# ---------------------------------------------------------------------------


class TestAdd:
    @pytest.mark.asyncio
    async def test_add_uses_namespace_as_user_id(self) -> None:
        store = _make_store()
        memory = _make_memory(project_id="team-rocket")
        store._mem.add = AsyncMock(return_value={"results": [{"id": "mem0-123"}]})

        await store.add(memory)

        kwargs = store._mem.add.call_args.kwargs
        assert kwargs["user_id"] == "project:team-rocket"

    @pytest.mark.asyncio
    async def test_add_sets_infer_false(self) -> None:
        store = _make_store()
        memory = _make_memory()
        store._mem.add = AsyncMock(return_value={"results": [{"id": "mem0-abc"}]})

        await store.add(memory)

        assert store._mem.add.call_args.kwargs["infer"] is False

    @pytest.mark.asyncio
    async def test_add_includes_trust_tier_in_metadata(self) -> None:
        store = _make_store()
        memory = _make_memory(trust_tier=TrustTier.CURATED)
        store._mem.add = AsyncMock(return_value={"results": [{"id": "mem0-xyz"}]})

        await store.add(memory)

        meta = store._mem.add.call_args.kwargs["metadata"]
        assert meta["trust_tier"] == TrustTier.CURATED.value

    @pytest.mark.asyncio
    async def test_add_returns_mem0_id_from_results(self) -> None:
        store = _make_store()
        memory = _make_memory()
        store._mem.add = AsyncMock(return_value={"results": [{"id": "mem0-returned"}]})

        mem0_id = await store.add(memory)

        assert mem0_id == "mem0-returned"

    @pytest.mark.asyncio
    async def test_add_default_project_id_fallback(self) -> None:
        """project_id=None should use 'default' namespace."""
        store = _make_store()
        memory = _make_memory(project_id=None)
        store._mem.add = AsyncMock(return_value={"results": [{"id": "mem0-1"}]})

        await store.add(memory)

        assert store._mem.add.call_args.kwargs["user_id"] == "project:default"

    @pytest.mark.asyncio
    async def test_add_fallback_to_memory_id_when_results_empty(self) -> None:
        store = _make_store()
        memory = _make_memory(id="fallback-id")
        store._mem.add = AsyncMock(return_value={"results": []})

        mem0_id = await store.add(memory)

        assert mem0_id == "fallback-id"


# ---------------------------------------------------------------------------
# TestSearch
# ---------------------------------------------------------------------------


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_returns_memory_results(self) -> None:
        store = _make_store()
        memory = _make_memory()
        store._mem.search = AsyncMock(return_value={"results": [_mem0_item(memory, 0.75)]})

        results = await store.search("sqlite", SearchFilters(project_id="proj-alpha"))

        assert len(results) == 1
        assert results[0].memory.id == memory.id
        assert results[0].score == pytest.approx(0.75)

    @pytest.mark.asyncio
    async def test_search_score_passthrough(self) -> None:
        store = _make_store()
        mem_a = _make_memory(id="a")
        mem_b = _make_memory(id="b")
        store._mem.search = AsyncMock(
            return_value={"results": [_mem0_item(mem_a, 0.9), _mem0_item(mem_b, 0.5)]}
        )

        results = await store.search("x", SearchFilters())

        assert results[0].score == pytest.approx(0.9)
        assert results[1].score == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_search_passes_metadata_filters_for_trust_tier(self) -> None:
        store = _make_store()
        store._mem.search = AsyncMock(return_value={"results": []})

        await store.search("q", SearchFilters(trust_tier_min=TrustTier.REVIEWED))

        kwargs = store._mem.search.call_args.kwargs
        assert kwargs["metadata_filters"] == {"trust_tier": {"gte": TrustTier.REVIEWED.value}}

    @pytest.mark.asyncio
    async def test_search_no_metadata_filters_when_no_trust_tier(self) -> None:
        store = _make_store()
        store._mem.search = AsyncMock(return_value={"results": []})

        await store.search("q", SearchFilters())

        kwargs = store._mem.search.call_args.kwargs
        assert kwargs["metadata_filters"] is None

    @pytest.mark.asyncio
    async def test_search_limit_passed_to_mem0(self) -> None:
        store = _make_store()
        store._mem.search = AsyncMock(return_value={"results": []})

        await store.search("q", SearchFilters(limit=42))

        assert store._mem.search.call_args.kwargs["limit"] == 42

    @pytest.mark.asyncio
    async def test_search_scope_filter(self) -> None:
        store = _make_store()
        proj_mem = _make_memory(scope=Scope.PROJECT)
        org_mem = _make_memory(id="org-1", scope=Scope.ORG)
        store._mem.search = AsyncMock(
            return_value={"results": [_mem0_item(proj_mem), _mem0_item(org_mem)]}
        )

        results = await store.search("x", SearchFilters(scope=Scope.PROJECT))

        assert all(r.memory.scope == Scope.PROJECT for r in results)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_search_project_id_filter(self) -> None:
        store = _make_store()
        match = _make_memory(project_id="proj-a")
        no_match = _make_memory(id="mem-002", project_id="proj-b")
        store._mem.search = AsyncMock(
            return_value={"results": [_mem0_item(match), _mem0_item(no_match)]}
        )

        results = await store.search("x", SearchFilters(project_id="proj-a"))

        assert [r.memory.project_id for r in results] == ["proj-a"]

    @pytest.mark.asyncio
    async def test_search_trust_tier_min_post_filter(self) -> None:
        """Server-side metadata_filters may not cover all results; Python re-checks."""
        store = _make_store()
        good = _make_memory(trust_tier=TrustTier.CURATED)
        bad = _make_memory(id="mem-002", trust_tier=TrustTier.UNVERIFIED)
        store._mem.search = AsyncMock(
            return_value={"results": [_mem0_item(good), _mem0_item(bad)]}
        )

        results = await store.search("x", SearchFilters(trust_tier_min=TrustTier.REVIEWED))

        assert all(r.memory.trust_tier >= TrustTier.REVIEWED for r in results)

    @pytest.mark.asyncio
    async def test_search_tags_all_semantics(self) -> None:
        """A memory must carry ALL requested tags, not just one."""
        store = _make_store()
        both_tags = _make_memory(tags=["security", "auth"])
        one_tag = _make_memory(id="mem-002", tags=["security"])
        store._mem.search = AsyncMock(
            return_value={"results": [_mem0_item(both_tags), _mem0_item(one_tag)]}
        )

        results = await store.search("x", SearchFilters(tags=["security", "auth"]))

        assert len(results) == 1
        assert results[0].memory.id == both_tags.id

    @pytest.mark.asyncio
    async def test_search_valid_at_filter_excludes_future_valid_from(self) -> None:
        ref_time = datetime(2024, 6, 1, tzinfo=UTC)
        store = _make_store()
        too_early = _make_memory(valid_from=datetime(2024, 7, 1, tzinfo=UTC))  # not yet valid
        in_range = _make_memory(id="m2", valid_from=datetime(2024, 1, 1, tzinfo=UTC))
        store._mem.search = AsyncMock(
            return_value={"results": [_mem0_item(too_early), _mem0_item(in_range)]}
        )

        results = await store.search("x", SearchFilters(valid_at=ref_time))

        assert len(results) == 1
        assert results[0].memory.id == in_range.id

    @pytest.mark.asyncio
    async def test_search_valid_at_filter_excludes_expired(self) -> None:
        ref_time = datetime(2024, 6, 1, tzinfo=UTC)
        store = _make_store()
        expired = _make_memory(
            valid_from=datetime(2024, 1, 1, tzinfo=UTC),
            valid_to=datetime(2024, 5, 1, tzinfo=UTC),  # expired before ref_time
        )
        active = _make_memory(id="m2", valid_from=datetime(2024, 1, 1, tzinfo=UTC))
        store._mem.search = AsyncMock(
            return_value={"results": [_mem0_item(expired), _mem0_item(active)]}
        )

        results = await store.search("x", SearchFilters(valid_at=ref_time))

        assert len(results) == 1
        assert results[0].memory.id == active.id

    @pytest.mark.asyncio
    async def test_search_skips_malformed_results(self) -> None:
        store = _make_store()
        good = _make_memory()
        bad_item = {"id": "bad", "memory": "broken", "metadata": {"trust_tier": 999}}
        store._mem.search = AsyncMock(
            return_value={"results": [bad_item, _mem0_item(good)]}
        )

        results = await store.search("x", SearchFilters())

        # Only the good item should come back
        assert len(results) == 1
        assert results[0].memory.id == good.id


# ---------------------------------------------------------------------------
# TestGet
# ---------------------------------------------------------------------------


class TestGet:
    @pytest.mark.asyncio
    async def test_get_returns_memory_object_for_known_id(self) -> None:
        store = _make_store()
        memory = _make_memory()
        store._mem.get = AsyncMock(return_value=_mem0_item(memory))

        result = await store.get("mem0-001")

        assert result is not None
        assert result.id == memory.id

    @pytest.mark.asyncio
    async def test_get_passes_id_to_mem0(self) -> None:
        store = _make_store()
        memory = _make_memory()
        store._mem.get = AsyncMock(return_value=_mem0_item(memory))

        await store.get("the-id")

        store._mem.get.assert_called_once_with("the-id")

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing(self) -> None:
        store = _make_store()
        store._mem.get = AsyncMock(return_value=None)

        assert await store.get("missing") is None

    @pytest.mark.asyncio
    async def test_get_raises_value_error_for_bad_payload(self) -> None:
        store = _make_store()
        store._mem.get = AsyncMock(
            return_value={"id": "x", "memory": "broken", "metadata": {"trust_tier": 999}}
        )

        with pytest.raises(ValueError, match="Failed to reconstruct"):
            await store.get("x")


# ---------------------------------------------------------------------------
# TestInvalidate
# ---------------------------------------------------------------------------


class TestInvalidate:
    @pytest.mark.asyncio
    async def test_invalidate_delegates_to_mem0_delete(self) -> None:
        store = _make_store()
        store._mem.delete = AsyncMock()

        await store.invalidate("mem0-123", "superseded by newer memory")

        store._mem.delete.assert_called_once_with("mem0-123")

    @pytest.mark.asyncio
    async def test_invalidate_passes_correct_memory_id(self) -> None:
        store = _make_store()
        store._mem.delete = AsyncMock()

        await store.invalidate("target-id", "reason")

        args = store._mem.delete.call_args.args
        assert args[0] == "target-id"


# ---------------------------------------------------------------------------
# TestUpdateTrustTier
# ---------------------------------------------------------------------------

def _make_decision(
    from_tier: TrustTier = TrustTier.UNVERIFIED,
    to_tier: TrustTier = TrustTier.REVIEWED,
) -> PromotionDecision:
    return PromotionDecision(
        from_tier=from_tier,
        to_tier=to_tier,
        decided_by="admin",
        decided_at=datetime.now(UTC),
        reason="manual review",
    )


class TestUpdateTrustTier:
    @pytest.mark.asyncio
    async def test_update_trust_tier_calls_internal_update(self) -> None:
        store = _make_store()
        memory = _make_memory(trust_tier=TrustTier.UNVERIFIED)
        store._mem.get = AsyncMock(return_value=_mem0_item(memory))
        store._mem._update_memory = AsyncMock()

        await store.update_trust_tier("mem0-123", TrustTier.REVIEWED, _make_decision())

        store._mem._update_memory.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_trust_tier_persists_new_tier_in_metadata(self) -> None:
        store = _make_store()
        memory = _make_memory(trust_tier=TrustTier.UNVERIFIED)
        store._mem.get = AsyncMock(return_value=_mem0_item(memory))
        store._mem._update_memory = AsyncMock()

        await store.update_trust_tier("mem0-123", TrustTier.CURATED, _make_decision())

        # 4th positional arg is the metadata dict
        metadata = store._mem._update_memory.call_args.args[3]
        assert metadata["trust_tier"] == TrustTier.CURATED.value

    @pytest.mark.asyncio
    async def test_update_trust_tier_passes_correct_memory_id(self) -> None:
        store = _make_store()
        memory = _make_memory(trust_tier=TrustTier.UNVERIFIED)
        store._mem.get = AsyncMock(return_value=_mem0_item(memory))
        store._mem._update_memory = AsyncMock()

        await store.update_trust_tier("the-mem-id", TrustTier.REVIEWED, _make_decision())

        first_arg = store._mem._update_memory.call_args.args[0]
        assert first_arg == "the-mem-id"

    @pytest.mark.asyncio
    async def test_update_trust_tier_raises_key_error_for_missing(self) -> None:
        store = _make_store()
        store._mem.get = AsyncMock(return_value=None)

        with pytest.raises(KeyError):
            await store.update_trust_tier("missing", TrustTier.REVIEWED, _make_decision())

    @pytest.mark.asyncio
    async def test_update_trust_tier_raises_runtime_error_without_internal_api(
        self,
    ) -> None:
        store = _make_store()
        memory = _make_memory(trust_tier=TrustTier.UNVERIFIED)
        store._mem.get = AsyncMock(return_value=_mem0_item(memory))
        # Remove _update_memory to simulate an AsyncMemory that doesn't expose it
        del store._mem._update_memory

        with pytest.raises(RuntimeError, match="_update_memory"):
            await store.update_trust_tier("mem0-123", TrustTier.REVIEWED, _make_decision())

    @pytest.mark.asyncio
    async def test_update_trust_tier_raises_value_error_for_corrupt_payload(self) -> None:
        store = _make_store()
        store._mem.get = AsyncMock(
            return_value={"id": "x", "memory": "bad", "metadata": {"trust_tier": 999}}
        )

        with pytest.raises(ValueError, match="update_trust_tier"):
            await store.update_trust_tier("x", TrustTier.REVIEWED, _make_decision())

    @pytest.mark.asyncio
    async def test_update_trust_tier_valid_from_tier_unverified_to_curated(self) -> None:
        """Ensure tier can be promoted by more than one level."""
        store = _make_store()
        memory = _make_memory(trust_tier=TrustTier.UNVERIFIED)
        store._mem.get = AsyncMock(return_value=_mem0_item(memory))
        store._mem._update_memory = AsyncMock()
        decision = _make_decision(from_tier=TrustTier.UNVERIFIED, to_tier=TrustTier.CURATED)

        await store.update_trust_tier("mem0-123", TrustTier.CURATED, decision)

        metadata = store._mem._update_memory.call_args.args[3]
        assert metadata["trust_tier"] == TrustTier.CURATED.value


# ---------------------------------------------------------------------------
# TestProtocolConformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_mem0_store_satisfies_memory_store_protocol(self) -> None:
        """Mem0Store must be structurally compatible with MemoryStore protocol."""
        from memento.stores.base import MemoryStore

        store = Mem0Store(_make_settings())
        assert isinstance(store, MemoryStore)

    @pytest.mark.asyncio
    async def test_add_search_invalidate_round_trip(self) -> None:
        """Smoke-test: add → search → invalidate sequence executes without error."""
        store = _make_store()
        memory = _make_memory()
        store._mem.add = AsyncMock(return_value={"results": [{"id": "m1"}]})
        store._mem.search = AsyncMock(return_value={"results": [_mem0_item(memory)]})
        store._mem.delete = AsyncMock()

        mem_id = await store.add(memory)
        results = await store.search("sqlite", SearchFilters())
        await store.invalidate(mem_id, "test cleanup")

        assert mem_id == "m1"
        assert len(results) == 1
        store._mem.delete.assert_called_once_with("m1")

    @pytest.mark.asyncio
    async def test_valid_at_boundary_edge_case(self) -> None:
        """Memory valid exactly at valid_from must be included (valid_from <= valid_at)."""
        ref_time = datetime(2024, 6, 1, tzinfo=UTC)
        store = _make_store()
        memory = _make_memory(valid_from=ref_time)  # exactly at boundary
        store._mem.search = AsyncMock(return_value={"results": [_mem0_item(memory)]})

        results = await store.search("x", SearchFilters(valid_at=ref_time))

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_get_then_update_trust_tier(self) -> None:
        """get() followed by update_trust_tier() on same id is coherent."""
        store = _make_store()
        memory = _make_memory(trust_tier=TrustTier.UNVERIFIED)
        item = _mem0_item(memory)
        store._mem.get = AsyncMock(return_value=item)
        store._mem._update_memory = AsyncMock()

        obj = await store.get("mem0-001")
        assert obj is not None
        assert obj.trust_tier == TrustTier.UNVERIFIED

        await store.update_trust_tier(
            "mem0-001",
            TrustTier.REVIEWED,
            _make_decision(),
        )
        store._mem._update_memory.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_list_format_raw_response(self) -> None:
        """Mem0 may return a plain list instead of dict-with-results."""
        store = _make_store()
        memory = _make_memory()
        # Simulate raw list response
        store._mem.search = AsyncMock(return_value=[_mem0_item(memory)])

        results = await store.search("x", SearchFilters())

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_add_list_format_raw_response(self) -> None:
        """Mem0 may return a plain list from add(); first item should be used."""
        store = _make_store()
        memory = _make_memory()
        store._mem.add = AsyncMock(return_value=[{"id": "list-id"}])

        mem_id = await store.add(memory)

        assert mem_id == "list-id"

"""Graphiti-backed MemoryStore implementation."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from graphiti_core import Graphiti
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EpisodeType, EpisodicNode
from graphiti_core.search.search_config import SearchResults
from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_CROSS_ENCODER
from graphiti_core.search.search_filters import (
    ComparisonOperator,
    DateFilter,
)
from graphiti_core.search.search_filters import (
    SearchFilters as GraphitiSearchFilters,
)

from memento.memory.schema import (
    AntiPattern,
    AppliesTo,
    CausedBy,
    Incident,
    Learning,
    MemoryObject,
    Policy,
    PromotionDecision,
    Scope,
    Supersedes,
    TrustTier,
    edge_type_map,
)
from memento.stores.base import MemoryResult, SearchFilters

_ENTITY_TYPES = {
    "Incident": Incident,
    "Learning": Learning,
    "AntiPattern": AntiPattern,
    "Policy": Policy,
}

_EDGE_TYPES = {
    "Supersedes": Supersedes,
    "CausedBy": CausedBy,
    "AppliesTo": AppliesTo,
}

#: Canonical edge-type map; assigned as a module-level name for testability.
_EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = edge_type_map

#: Base combined hybrid search config; model_copy'd per search call to set limit.
_COMBINED_SEARCH_CONFIG = COMBINED_HYBRID_SEARCH_CROSS_ENCODER

logger = logging.getLogger(__name__)


def _group_id_for_memory(memory: MemoryObject) -> str:
    if memory.scope == Scope.ORG:
        return "org"
    return memory.project_id or "default"


def _group_ids_for_filters(filters: SearchFilters) -> list[str] | None:
    if filters.project_id is not None:
        return [filters.project_id]
    if filters.scope == Scope.ORG:
        return ["org"]
    return None


def _native_search_filters(filters: SearchFilters) -> GraphitiSearchFilters | None:
    node_labels = filters.entity_types or None
    valid_at = None
    if filters.valid_at is not None:
        valid_at = [
            [
                DateFilter(
                    date=filters.valid_at,
                    comparison_operator=ComparisonOperator.less_than_equal,
                )
            ]
        ]

    if node_labels is None and valid_at is None:
        return None

    return GraphitiSearchFilters(
        node_labels=node_labels,
        valid_at=valid_at,
    )


def _load_memory_from_episode(episode: EpisodicNode) -> MemoryObject:
    try:
        payload = json.loads(episode.content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Episode {episode.uuid!r} does not contain valid JSON") from exc

    try:
        return MemoryObject.model_validate(payload)
    except Exception as exc:  # pragma: no cover - pydantic-specific failure details
        raise ValueError(
            f"Episode {episode.uuid!r} does not contain a valid MemoryObject payload"
        ) from exc


def _passes_filters(memory: MemoryObject, filters: SearchFilters) -> bool:
    if filters.scope is not None and memory.scope != filters.scope:
        return False
    if filters.project_id is not None and memory.project_id != filters.project_id:
        return False
    if filters.trust_tier_min is not None and memory.trust_tier < filters.trust_tier_min:
        return False
    if filters.tags and not all(tag in memory.tags for tag in filters.tags):
        return False
    if filters.valid_at is not None:
        if memory.valid_from > filters.valid_at:
            return False
        if memory.valid_to is not None and memory.valid_to <= filters.valid_at:
            return False
    return True


class GraphitiStore:
    """Async facade over Graphiti."""

    def __init__(
        self,
        graphiti: Graphiti | None = None,
        *,
        host: str | None = None,
        port: int = 6379,
        user: str = "",
        password: str = "",
        llm_client: Any | None = None,
        embedder: Any | None = None,
    ) -> None:
        if graphiti is None:
            if host is None or llm_client is None or embedder is None:
                raise ValueError(
                    "GraphitiStore requires either a Graphiti instance or host, llm_client, "
                    "and embedder parameters."
                )
            uri = f"bolt://{host}:{port}"
            graphiti = Graphiti(
                uri=uri,
                user=user,
                password=password,
                llm_client=llm_client,
                embedder=embedder,
            )
        self._graphiti = graphiti

    async def initialize(self) -> None:
        await self._graphiti.build_indices_and_constraints()

    async def close(self) -> None:
        await self._graphiti.close()  # type: ignore[no-untyped-call]

    async def add(self, memory: MemoryObject) -> str:
        await self._graphiti.add_episode(
            name=f"memory:{memory.id}",
            episode_body=memory.model_dump_json(),
            source_description="memento-memory",
            reference_time=memory.created_at,
            source=EpisodeType.json,
            group_id=_group_id_for_memory(memory),
            uuid=memory.id,
            entity_types=_ENTITY_TYPES,  # type: ignore[arg-type]
            edge_types=_EDGE_TYPES,  # type: ignore[arg-type]
            edge_type_map=edge_type_map,
        )
        return memory.id

    async def search(self, query: str, filters: SearchFilters) -> list[MemoryResult]:
        fetch_limit = max(filters.limit * 3, 30)
        config = _COMBINED_SEARCH_CONFIG.model_copy(update={"limit": fetch_limit})
        results = await self._graphiti.search_(
            query=query,
            config=config,
            group_ids=_group_ids_for_filters(filters),
            search_filter=_native_search_filters(filters),
        )
        return await _results_to_memory_results(results, filters, self._graphiti.driver)

    async def get(self, memory_id: str) -> MemoryObject | None:
        try:
            episode = await EpisodicNode.get_by_uuid(self._graphiti.driver, memory_id)
        except NodeNotFoundError:
            return None
        try:
            return _load_memory_from_episode(episode)
        except ValueError:
            return None

    async def invalidate(self, memory_id: str, reason: str) -> None:
        try:
            episode = await EpisodicNode.get_by_uuid(self._graphiti.driver, memory_id)
        except NodeNotFoundError:
            logger.warning("invalidate: memory %s not found", memory_id)
            return

        memory = _load_memory_from_episode(episode)
        memory.valid_to = datetime.now(UTC)
        memory.tags = [*memory.tags, f"invalidation:{reason}"]
        episode.content = memory.model_dump_json()
        await episode.save(self._graphiti.driver)

    async def update_trust_tier(
        self,
        memory_id: str,
        new_tier: TrustTier,
        decision: PromotionDecision,
    ) -> None:
        try:
            episode = await EpisodicNode.get_by_uuid(self._graphiti.driver, memory_id)
        except NodeNotFoundError:
            logger.warning("update_trust_tier: memory %s not found", memory_id)
            return

        memory = _load_memory_from_episode(episode)
        memory.trust_tier = new_tier
        memory.provenance.promotion_decisions.append(decision)
        episode.content = memory.model_dump_json()
        await episode.save(self._graphiti.driver)


async def _results_to_memory_results(
    results: SearchResults,
    filters: SearchFilters,
    driver: Any,
) -> list[MemoryResult]:
    scored_results: list[MemoryResult] = []
    seen_ids: set[str] = set()

    # Direct episodes — already EpisodicNode objects with content in-memory.
    for idx, episode in enumerate(results.episodes):
        try:
            memory = _load_memory_from_episode(episode)
        except ValueError:
            continue
        if not _passes_filters(memory, filters) or memory.id in seen_ids:
            continue
        seen_ids.add(memory.id)
        score = (
            results.episode_reranker_scores[idx]
            if idx < len(results.episode_reranker_scores)
            else 1.0
        )
        scored_results.append(MemoryResult(memory=memory, score=float(score)))

    # Edge-derived episodes — edge.episodes is list[str] (UUIDs); fetch each.
    for edge in results.edges:
        for episode_uuid in edge.episodes:
            if episode_uuid in seen_ids:
                continue
            try:
                episode = await EpisodicNode.get_by_uuid(driver, episode_uuid)
            except NodeNotFoundError:
                continue
            try:
                memory = _load_memory_from_episode(episode)
            except ValueError:
                continue
            if not _passes_filters(memory, filters):
                continue
            seen_ids.add(memory.id)
            scored_results.append(MemoryResult(memory=memory, score=1.0))

    return scored_results[: filters.limit]


__all__ = ["GraphitiStore", "_passes_filters"]

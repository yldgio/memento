"""Mem0-backed MemoryStore implementation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from mem0 import AsyncMemory  # type: ignore[import-untyped]
from pydantic import ValidationError

from memento.config import Settings
from memento.memory.schema import (
    Cell,
    Lifetime,
    MemoryObject,
    PromotionDecision,
    Provenance,
    Scope,
    TrustTier,
)
from memento.stores.base import MemoryResult, SearchFilters

logger = logging.getLogger(__name__)

_NAMESPACE_PREFIX = "project"


def _build_mem0_config(settings: Settings) -> dict[str, Any]:
    """Map Memento settings into Mem0's config shape."""
    base_url = settings.llm_base_url
    if "ollama" in base_url or "localhost:11434" in base_url:
        provider = "ollama"
    elif "azure" in base_url:
        provider = "azure_openai"
    else:
        provider = "openai"

    api_key = settings.llm_api_key.get_secret_value()
    llm_config: dict[str, Any] = {"model": settings.llm_model, "api_key": api_key}
    if provider != "openai":
        llm_config["openai_base_url"] = base_url

    embedder_config: dict[str, Any] = {
        "provider": "openai",
        "config": {"api_key": api_key, "model": "text-embedding-3-small"},
    }
    if provider != "openai":
        embedder_config["config"]["openai_base_url"] = base_url

    return {
        "llm": {"provider": provider, "config": llm_config},
        "embedder": embedder_config,
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "memento",
                "embedding_model_dims": 1536,
                "path": str(settings.data_dir / "mem0"),
                "on_disk": True,
            },
        },
        "history_db_path": str(settings.data_dir / "mem0-history.db"),
    }


def _namespace(project_id: str) -> str:
    return f"{_NAMESPACE_PREFIX}:{project_id}"


def _to_mem0_metadata(memory: MemoryObject) -> dict[str, Any]:
    """Serialize MemoryObject fields into Mem0 metadata."""
    return {
        "memento_id": memory.id,
        "scope": memory.scope.value,
        "lifetime": memory.lifetime.value,
        "cell": memory.cell.value,
        "confidence": memory.confidence,
        "trust_tier": memory.trust_tier.value,
        "tags": memory.tags,
        "project_id": memory.project_id,
        "created_at_iso": memory.created_at.isoformat(),
        "valid_from_iso": memory.valid_from.isoformat(),
        "valid_to_iso": memory.valid_to.isoformat() if memory.valid_to else None,
        "superseded_by": memory.superseded_by,
        "session_count": memory.session_count,
        "prov_source_session_id": memory.provenance.source_session_id,
        "prov_source_agent_id": memory.provenance.source_agent_id,
        "prov_batch_id": memory.provenance.consolidation_batch_id,
        "prov_model": memory.provenance.consolidation_model,
        "prov_created_by": memory.provenance.created_by,
        "original_content": memory.content,
    }


def _from_mem0_result(result: dict[str, Any]) -> MemoryObject:
    """Reconstruct a MemoryObject from Mem0 get/search output."""
    meta: dict[str, Any] = result.get("metadata") or {}

    def _dt(key: str) -> datetime | None:
        value = meta.get(key)
        if not value:
            return None
        return datetime.fromisoformat(str(value))

    content = str(meta.get("original_content") or result.get("memory", ""))
    provenance = Provenance(
        source_session_id=str(meta.get("prov_source_session_id", "")),
        source_agent_id=str(meta.get("prov_source_agent_id", "")),
        consolidation_batch_id=str(meta.get("prov_batch_id", "")),
        consolidation_model=str(meta.get("prov_model", "")),
        created_by=str(meta.get("prov_created_by", "")),
    )
    now = datetime.now(UTC)
    return MemoryObject(
        id=str(meta.get("memento_id") or result.get("id", "")),
        content=content,
        scope=Scope(meta.get("scope", Scope.PROJECT.value)),
        lifetime=Lifetime(meta.get("lifetime", Lifetime.TEMPORAL.value)),
        cell=Cell(meta.get("cell", Cell.C2.value)),
        confidence=float(meta.get("confidence", 0.0)),
        trust_tier=TrustTier(int(meta.get("trust_tier", TrustTier.UNVERIFIED.value))),
        provenance=provenance,
        tags=list(meta.get("tags") or []),
        project_id=meta.get("project_id"),
        created_at=_dt("created_at_iso") or now,
        valid_from=_dt("valid_from_iso") or now,
        valid_to=_dt("valid_to_iso"),
        superseded_by=meta.get("superseded_by"),
        session_count=int(meta.get("session_count", 1)),
    )


def _normalise_results(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        return list(raw.get("results", []))
    if isinstance(raw, list):
        return list(raw)
    return []


class Mem0Store:
    """Async facade over Mem0."""

    def __init__(self, settings: Settings) -> None:
        self._config = _build_mem0_config(settings)
        self._mem: Any = None

    @classmethod
    async def create(cls, settings: Settings) -> Mem0Store:
        instance = cls(settings)
        instance._mem = await AsyncMemory.from_config(instance._config)
        return instance

    async def _client(self) -> Any:
        if self._mem is None:
            self._mem = await AsyncMemory.from_config(self._config)
        return self._mem

    async def add(self, memory: MemoryObject) -> str:
        project_id = memory.project_id or "default"
        mem = await self._client()
        raw = await mem.add(
            [{"role": "user", "content": memory.content}],
            user_id=_namespace(project_id),
            metadata=_to_mem0_metadata(memory),
            infer=False,
        )
        results = _normalise_results(raw)
        if results:
            return str(results[0].get("id", memory.id))
        logger.warning("Mem0 add() returned unexpected format: %r", raw)
        return memory.id

    async def search(self, query: str, filters: SearchFilters) -> list[MemoryResult]:
        mem = await self._client()
        metadata_filters: dict[str, Any] | None = None
        if filters.trust_tier_min is not None:
            metadata_filters = {"trust_tier": {"gte": filters.trust_tier_min.value}}

        raw = await mem.search(
            query,
            user_id=_namespace(filters.project_id or "default"),
            limit=filters.limit,
            metadata_filters=metadata_filters,
        )
        items = _normalise_results(raw)
        results: list[MemoryResult] = []
        for item in items:
            try:
                obj = _from_mem0_result(item)
            except (ValidationError, TypeError, ValueError):
                logger.warning("Skipping malformed Mem0 result: %r", item, exc_info=True)
                continue

            if filters.scope is not None and obj.scope != filters.scope:
                continue
            if filters.project_id is not None and obj.project_id != filters.project_id:
                continue
            if filters.trust_tier_min is not None and obj.trust_tier < filters.trust_tier_min:
                continue
            if filters.tags and not all(tag in obj.tags for tag in filters.tags):
                continue
            if filters.valid_at is not None:
                if obj.valid_from > filters.valid_at:
                    continue
                if obj.valid_to is not None and obj.valid_to <= filters.valid_at:
                    continue

            results.append(MemoryResult(memory=obj, score=float(item.get("score", 0.0))))
        return results

    async def get(self, memory_id: str) -> MemoryObject | None:
        mem = await self._client()
        result = await mem.get(memory_id)
        if result is None:
            return None
        try:
            return _from_mem0_result(result)
        except (ValidationError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Failed to reconstruct MemoryObject from Mem0 get({memory_id!r})"
            ) from exc

    async def invalidate(self, memory_id: str, reason: str) -> None:
        logger.info("Invalidating Mem0 memory %r: %s", memory_id, reason)
        mem = await self._client()
        await mem.delete(memory_id)

    async def update_trust_tier(
        self,
        memory_id: str,
        new_tier: TrustTier,
        decision: PromotionDecision,
    ) -> None:
        mem = await self._client()
        raw = await mem.get(memory_id)
        if raw is None:
            raise KeyError(memory_id)

        try:
            existing = _from_mem0_result(raw)
        except (ValidationError, TypeError, ValueError) as exc:
            raise ValueError(
                f"update_trust_tier: failed to reconstruct MemoryObject for {memory_id!r}"
            ) from exc

        updated = existing.model_copy(
            update={
                "trust_tier": new_tier,
                "provenance": existing.provenance.model_copy(
                    update={
                        "promotion_decisions": [
                            *existing.provenance.promotion_decisions,
                            decision,
                        ]
                    }
                ),
            }
        )
        update_memory = getattr(mem, "_update_memory", None)
        if update_memory is None:
            raise RuntimeError(
                "Mem0 AsyncMemory instance does not expose _update_memory; "
                "cannot persist trust-tier metadata updates."
            )

        await update_memory(
            memory_id,
            existing.content,
            {},
            _to_mem0_metadata(updated),
        )
        logger.info(
            "Updated trust tier for %r: %s -> %s",
            memory_id,
            existing.trust_tier.name,
            new_tier.name,
        )

"""MCP server for Memento — exposes memory tools over Model Context Protocol.

Implements FR-MCP-01 through FR-MCP-04 and FR-MCP-06 from TRD §6.1:
- ``memento_context_assemble``: assemble relevant context for an agent task
- ``memento_session_log``: append an observation to an active session
- ``memento_session_end``: close a session and optionally enqueue consolidation

The server is created via :func:`create_mcp_server`, which accepts store
instances for dependency injection. Tool schemas are auto-generated from
type hints via ``@mcp.tool()`` decorators.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from memento.config import Settings

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from memento.memory.schema import (
    Observation,
    Scope,
    SessionStatus,
    TrustTier,
    _utc_now,
)
from memento.stores.base import MemoryResult, SearchFilters
from memento.stores.session_store import (
    SessionNotActiveError,
    SessionNotFoundError,
    SessionStore,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Response models (structured MCP tool return types — TRD §6.1)
# ---------------------------------------------------------------------------


class MemoryItem(BaseModel):
    """A single memory entry returned in context assembly."""

    memory_id: str
    content: str
    confidence: float
    trust_tier: str
    source: str
    entity_type: str | None = None


class PolicyItem(BaseModel):
    """A policy document from AGENTS.md or similar source (TRD §6.1)."""

    source: str
    section: str
    content: str


class ContextBlock(BaseModel):
    """Grouped context memories with source attribution."""

    project_memories: list[MemoryItem] = Field(default_factory=list)
    org_memories: list[MemoryItem] = Field(default_factory=list)
    policies: list[PolicyItem] = Field(default_factory=list)


class ContextMetadata(BaseModel):
    """Assembly metadata for a context response."""

    total_memories: int
    assembly_time_ms: int


class ContextResponse(BaseModel):
    """Response from ``memento_context_assemble`` (TRD §6.1)."""

    session_id: str
    project_id: str
    context: ContextBlock
    metadata: ContextMetadata


class LogResponse(BaseModel):
    """Response from ``memento_session_log`` (TRD §6.1)."""

    session_id: str
    observation_index: int
    timestamp: str
    status: str = "logged"


class EndResponse(BaseModel):
    """Response from ``memento_session_end`` (TRD §6.1)."""

    session_id: str
    status: str
    observation_count: int
    started_at: str
    ended_at: str
    consolidation_queued: bool
    consolidation_enqueue_id: str | None = None


# ---------------------------------------------------------------------------
# Store protocol stubs for typing (avoids importing heavy backends)
# ---------------------------------------------------------------------------

# We use a lightweight protocol approach: the MCP server depends on concrete
# store interfaces (SessionStore for sessions, and MemoryStore-like search
# for Mem0/Graphiti).  These are injected at construction time.


class _MemorySearchable(Protocol):
    """Structural type for stores that support ``search()``."""

    async def search(self, query: str, filters: SearchFilters) -> list[MemoryResult]: ...


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_mcp_server(
    *,
    session_store: SessionStore,
    mem0_store: _MemorySearchable | None = None,
    graphiti_store: _MemorySearchable | None = None,
    name: str = "memento",
) -> FastMCP:
    """Create and return a configured :class:`FastMCP` server.

    Parameters
    ----------
    session_store:
        Opened :class:`~memento.stores.session_store.SessionStore` instance.
    mem0_store:
        Optional Mem0 store implementing ``search(query, filters)``.
    graphiti_store:
        Optional Graphiti store implementing ``search(query, filters)``.
    name:
        MCP server name reported to clients.

    Returns
    -------
    FastMCP
        The fully-configured MCP server, ready to ``run()`` or to be
        mounted as a Starlette sub-application.
    """
    mcp = FastMCP(name)

    # ------------------------------------------------------------------
    # Tool: memento_context_assemble  (FR-MCP-02, FR-CTX-01, FR-CTX-06)
    # ------------------------------------------------------------------

    @mcp.tool()
    async def memento_context_assemble(  # noqa: N802
        project: str,
        task: str,
        agent_id: str,
        max_memories: int = 20,
        include_policies: bool = True,
    ) -> dict[str, Any]:
        """Assemble relevant context for an agent task.

        Queries project memory (Mem0) and org-wide memory (Graphiti)
        for knowledge relevant to the given task. Returns a structured
        context blob with source attribution.

        A new session is created and its ID is returned.
        The agent_id is required for provenance tracking.
        """
        if max_memories < 1:
            raise ValueError("max_memories must be >= 1")

        start_ms = _now_ms()

        # --- Query stores (trust_tier >= REVIEWED) ---
        filters_project = SearchFilters(
            project_id=project,
            trust_tier_min=TrustTier.REVIEWED,
            valid_at=_utc_now(),
            limit=max_memories,
        )
        filters_org = SearchFilters(
            scope=Scope.ORG,
            trust_tier_min=TrustTier.REVIEWED,
            valid_at=_utc_now(),
            limit=max_memories,
        )

        project_results: list[MemoryResult] = []
        org_results: list[MemoryResult] = []

        if mem0_store is not None:
            project_results = await mem0_store.search(task, filters_project)

        if graphiti_store is not None:
            org_results = await graphiti_store.search(task, filters_org)

        # --- Deduplicate by memory_id ---
        seen_ids: set[str] = set()
        project_items: list[MemoryItem] = []
        for r in project_results:
            if r.memory.id not in seen_ids:
                seen_ids.add(r.memory.id)
                project_items.append(
                    MemoryItem(
                        memory_id=r.memory.id,
                        content=r.memory.content,
                        confidence=r.memory.confidence,
                        trust_tier=r.memory.trust_tier.name.lower(),
                        source="mem0",
                    )
                )

        org_items: list[MemoryItem] = []
        for r in org_results:
            if r.memory.id not in seen_ids:
                seen_ids.add(r.memory.id)
                org_items.append(
                    MemoryItem(
                        memory_id=r.memory.id,
                        content=r.memory.content,
                        confidence=r.memory.confidence,
                        trust_tier=r.memory.trust_tier.name.lower(),
                        source="graphiti",
                    )
                )

        total = len(project_items) + len(org_items)
        elapsed_ms = _now_ms() - start_ms

        session = await session_store.create_session(
            agent_id=agent_id,
            project_id=project,
            task_description=task,
        )

        # Phase 0 stub: policy loading from AGENTS.md deferred to P1 (FR-CTX-03).
        policies: list[PolicyItem] = []
        if include_policies:
            logger.debug(
                "include_policies=True but policy loading deferred to P1; "
                "returning empty policies list"
            )

        response = ContextResponse(
            session_id=session.session_id,
            project_id=project,
            context=ContextBlock(
                project_memories=project_items,
                org_memories=org_items,
                policies=policies,
            ),
            metadata=ContextMetadata(
                total_memories=total,
                assembly_time_ms=elapsed_ms,
            ),
        )
        return response.model_dump()

    # ------------------------------------------------------------------
    # Tool: memento_session_log  (FR-MCP-03)
    # ------------------------------------------------------------------

    @mcp.tool()
    async def memento_session_log(  # noqa: N802
        session_id: str,
        observation: str,
        agent_id: str,
        tags: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Log an observation to the current session.

        Observations are append-only. Each observation is timestamped
        and attributed to the given agent_id. The session must be ACTIVE.

        Raises an MCP tool error if the session_id is invalid or not active.
        """
        # Validate session exists and is ACTIVE — surface as MCP error
        try:
            session = await session_store.get_session(session_id)
        except Exception as exc:
            raise ValueError(f"Failed to retrieve session {session_id!r}: {exc}") from exc

        if session is None:
            raise ValueError(f"Session not found: {session_id!r}")
        if session.agent_id != agent_id:
            raise ValueError(
                f"Session {session_id!r} belongs to agent {session.agent_id!r}, "
                f"not {agent_id!r}"
            )
        if session.status != SessionStatus.ACTIVE:
            raise ValueError(
                f"Session {session_id!r} is {session.status.value}, expected ACTIVE"
            )

        now = _utc_now()
        obs = Observation(
            timestamp=now,
            content=observation,
            tags=tags or [],
            context=context,
        )

        try:
            obs_index = await session_store.append_observation(session_id, obs)
        except SessionNotFoundError:
            raise ValueError(f"Session not found: {session_id!r}")
        except SessionNotActiveError as exc:
            raise ValueError(str(exc)) from exc

        response = LogResponse(
            session_id=session_id,
            observation_index=obs_index,
            timestamp=now.isoformat(),
        )
        return response.model_dump()

    # ------------------------------------------------------------------
    # Tool: memento_session_end  (FR-MCP-04)
    # ------------------------------------------------------------------

    @mcp.tool()
    async def memento_session_end(  # noqa: N802
        session_id: str,
        summary: str | None = None,
        trigger_consolidation: bool = True,
    ) -> dict[str, Any]:
        """End a session and optionally trigger consolidation.

        The session is marked as ENDED. If trigger_consolidation is True,
        the consolidation job is enqueued for this session.

        Returns a summary with observation count, timestamps, status
        transition, and consolidation enqueue ID.
        """
        try:
            ended_session = await session_store.end_session(session_id)
        except SessionNotFoundError:
            raise ValueError(f"Session not found: {session_id!r}")
        except SessionNotActiveError as exc:
            raise ValueError(str(exc)) from exc

        # Log optional summary as a final observation (if provided and
        # session was still active — end_session already transitioned it,
        # so we just note it in the log).
        if summary:
            logger.info(
                "Session %s ended with summary: %s", session_id, summary[:200]
            )

        # Phase 0 enqueue mechanism: ending the session is the durable handoff
        # that the scheduler will scan in P0-T10. Expose that transition with a
        # deterministic token rather than claiming an external queue job exists.
        consolidation_enqueue_id: str | None = None
        if trigger_consolidation:
            consolidation_enqueue_id = f"session:{ended_session.session_id}"
            logger.info(
                "Consolidation queued for session %s via ended-session lifecycle "
                "(enqueue_id=%s)",
                session_id,
                consolidation_enqueue_id,
            )

        response = EndResponse(
            session_id=session_id,
            status=ended_session.status.value,
            observation_count=len(ended_session.observations),
            started_at=ended_session.started_at.isoformat(),
            ended_at=ended_session.ended_at.isoformat() if ended_session.ended_at else "",
            consolidation_queued=trigger_consolidation,
            consolidation_enqueue_id=consolidation_enqueue_id,
        )
        return response.model_dump()

    return mcp


# ---------------------------------------------------------------------------
# Store bootstrap helpers (used by main.py lifespan and stdio entrypoint)
# ---------------------------------------------------------------------------


async def try_open_mem0(settings: Settings) -> _MemorySearchable | None:
    """Attempt to create and initialise a :class:`Mem0Store`.

    Returns the opened store, or *None* if the backend is unavailable
    (missing dependency, invalid config, or connection error).
    """
    try:
        from memento.stores.mem0_store import Mem0Store

        return await Mem0Store.create(settings)
    except Exception:
        logger.warning("Mem0Store unavailable; skipping project memories", exc_info=True)
        return None


async def try_open_graphiti(settings: Settings) -> _MemorySearchable | None:
    """Attempt to create and initialise a :class:`GraphitiStore`.

    Returns the opened store, or *None* if the backend is unavailable
    (missing dependency, FalkorDB unreachable, or invalid config).
    """
    store: _MemorySearchable | None = None
    try:
        from graphiti_core import Graphiti
        from graphiti_core.embedder import OpenAIEmbedder
        from graphiti_core.embedder.openai import OpenAIEmbedderConfig
        from graphiti_core.llm_client import LLMConfig, OpenAIClient

        from memento.stores.graphiti_store import GraphitiStore

        api_key = settings.llm_api_key.get_secret_value()
        llm = OpenAIClient(
            LLMConfig(
                api_key=api_key,
                model=settings.llm_model,
                base_url=settings.llm_base_url,
            )
        )
        embedder = OpenAIEmbedder(
            OpenAIEmbedderConfig(api_key=api_key, base_url=settings.llm_base_url)
        )
        graphiti = Graphiti(
            uri=f"bolt://{settings.falkordb_host}:{settings.falkordb_port}",
            user="",
            password="",
            llm_client=llm,
            embedder=embedder,
        )
        store = GraphitiStore(graphiti)
        await store.initialize()
        return store
    except Exception:
        if hasattr(store, "close"):
            try:
                await store.close()  # type: ignore[union-attr]
            except Exception:
                logger.warning(
                    "GraphitiStore cleanup failed after initialization error",
                    exc_info=True,
                )
        logger.warning("GraphitiStore unavailable; skipping org memories", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Transport runners
# ---------------------------------------------------------------------------


async def run_stdio(
    *,
    session_store: SessionStore,
    mem0_store: _MemorySearchable | None = None,
    graphiti_store: _MemorySearchable | None = None,
) -> None:
    """Create the MCP server and run it on the **stdio** transport.

    This is the programmatic entry-point used by the ``__main__`` block
    (``python -m memento.mcp.server``) and can also be called directly
    from test harnesses.
    """
    mcp = create_mcp_server(
        session_store=session_store,
        mem0_store=mem0_store,
        graphiti_store=graphiti_store,
    )
    await mcp.run_stdio_async()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """Return current time in milliseconds (monotonic clock)."""
    return int(time.monotonic() * 1000)


# ---------------------------------------------------------------------------
# stdio entry-point: ``python -m memento.mcp.server``
# ---------------------------------------------------------------------------


async def _stdio_main() -> None:
    """Bootstrap stores and run the MCP server on stdio."""
    from memento.config import get_settings

    settings = get_settings()

    store = SessionStore()
    await store.open()
    graphiti: _MemorySearchable | None = None
    try:
        mem0 = await try_open_mem0(settings)
        graphiti = await try_open_graphiti(settings)
        await run_stdio(
            session_store=store,
            mem0_store=mem0,
            graphiti_store=graphiti,
        )
    finally:
        if hasattr(graphiti, "close"):
            await graphiti.close()  # type: ignore[union-attr]
        await store.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(_stdio_main())

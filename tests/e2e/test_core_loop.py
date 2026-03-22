"""End-to-end test: the complete Memento core loop.

Demonstrates the full Phase 0 acceptance criterion:

  observe → consolidate → retrieve

The complete sequence tested:

1. Agent calls ``memento_context_assemble`` → gets *empty* context (no memories yet).
2. Agent logs an observation about JWT token expiry.
3. Agent ends the session.
4. Consolidation job extracts learnings from the session (LLM mocked or real).
5. Agent calls ``memento_context_assemble`` for a related task → context now
   **includes** the JWT expiry learning from step 2.
6. Assert the returned context contains a memory referencing "JWT" and "15-min expiry".

Run modes
---------
* **Default (mocked LLM)**: ``python -m pytest tests/e2e/test_core_loop.py -v``
* **Real LLM**: ``MEMENTO_E2E_REAL_LLM=1 python -m pytest tests/e2e/ -v``
  (requires valid ``MEMENTO_LLM_*`` env vars pointing to a live model endpoint)
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from memento.jobs.consolidation import run_consolidation
from memento.mcp.server import ContextResponse, create_mcp_server
from memento.memory.schema import MemoryObject, PromotionDecision, TrustTier
from memento.stores.base import MemoryResult, SearchFilters
from memento.stores.session_store import SessionStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_tool_result(raw: Any) -> dict[str, Any]:
    """Extract the dict payload from an MCP ``call_tool`` result.

    FastMCP's ``call_tool`` returns either:

    * ``(list[TextContent], dict)`` — structured output when the tool returns a
      dict (the payload is the second element).
    * ``list[TextContent]`` — unstructured; parse JSON from the first block.

    This helper normalises both shapes into a plain Python dict.
    """
    if isinstance(raw, tuple):
        return dict(raw[1])
    content_list: list[Any] = raw
    return json.loads(content_list[0].text)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Fake in-memory MemoryStore
# ---------------------------------------------------------------------------


class FakeMemoryStore:
    """Lightweight in-memory :class:`~memento.stores.base.MemoryStore`.

    Stores :class:`~memento.memory.schema.MemoryObject` instances in a plain
    dict and returns them from ``search()`` with basic filter support.  This
    allows consolidation tests to write and then retrieve memories without
    spinning up Mem0/Qdrant or FalkorDB.

    Filtering in ``search()`` mirrors the subset of :class:`SearchFilters`
    actually used by :func:`~memento.mcp.server.memento_context_assemble`:

    * ``trust_tier_min`` — minimum trust tier (inclusive)
    * ``project_id`` — exact project match
    * ``scope`` — exact scope match

    Similarity scoring is not implemented; every matching memory gets a
    fixed score of 1.0 so that duplicate-detection (≥ 0.9 threshold) works.
    """

    def __init__(self) -> None:
        self._memories: dict[str, MemoryObject] = {}

    async def add(self, memory: MemoryObject) -> str:
        """Store *memory* under its ``id`` (overwrites if id already exists)."""
        self._memories[memory.id] = memory
        return memory.id

    async def search(self, query: str, filters: SearchFilters) -> list[MemoryResult]:
        """Return memories that pass *filters*, capped at ``filters.limit``."""
        results: list[MemoryResult] = []
        for mem in self._memories.values():
            if (
                filters.trust_tier_min is not None
                and mem.trust_tier < filters.trust_tier_min
            ):
                continue
            if filters.project_id is not None and mem.project_id != filters.project_id:
                continue
            if filters.scope is not None and mem.scope != filters.scope:
                continue
            results.append(MemoryResult(memory=mem, score=1.0))
        return results[: filters.limit]

    async def get(self, memory_id: str) -> MemoryObject | None:
        """Retrieve a memory by its primary key."""
        return self._memories.get(memory_id)

    async def invalidate(self, memory_id: str, reason: str) -> None:
        """Remove *memory_id* from the store (soft-delete not required here)."""
        self._memories.pop(memory_id, None)

    async def update_trust_tier(
        self,
        memory_id: str,
        new_tier: TrustTier,
        decision: PromotionDecision,
    ) -> None:
        """Update trust tier in-place via :meth:`~pydantic.BaseModel.model_copy`."""
        if memory_id in self._memories:
            self._memories[memory_id] = self._memories[memory_id].model_copy(
                update={"trust_tier": new_tier}
            )


# ---------------------------------------------------------------------------
# Core loop test — mocked LLM (default, fast, deterministic)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_core_loop_with_mocked_llm(
    session_store: SessionStore,
    mock_llm_client: AsyncMock,
) -> None:
    """Full core loop: observe → consolidate → retrieve (mocked LLM).

    Uses a deterministic fake LLM response that returns a single JWT expiry
    learning so the test is fully self-contained and requires no external
    services.
    """
    # Use a unique project ID to prevent cross-test pollution.
    project_id = f"test-{uuid.uuid4().hex[:8]}"
    agent_id = "test-agent"

    # Fresh in-memory stores for this test run.
    fake_mem0 = FakeMemoryStore()
    fake_graphiti = FakeMemoryStore()

    mcp = create_mcp_server(
        session_store=session_store,
        mem0_store=fake_mem0,
        graphiti_store=fake_graphiti,
    )

    # ------------------------------------------------------------------
    # Step 1: Context assemble — no prior memories → empty context
    # ------------------------------------------------------------------
    result1 = await mcp.call_tool(
        "memento_context_assemble",
        {
            "project": project_id,
            "task": "implement auth",
            "agent_id": agent_id,
        },
    )
    data1 = _extract_tool_result(result1)
    resp1 = ContextResponse.model_validate(data1)

    session_id: str = resp1.session_id

    assert resp1.project_id == project_id
    assert resp1.context.project_memories == [], (
        "Expected empty project memories on first call"
    )
    assert resp1.context.org_memories == [], (
        "Expected empty org memories on first call"
    )
    assert resp1.metadata.total_memories == 0

    # ------------------------------------------------------------------
    # Step 2: Log observation — JWT expiry learning
    # ------------------------------------------------------------------
    result2 = await mcp.call_tool(
        "memento_session_log",
        {
            "session_id": session_id,
            "observation": "JWT tokens need 15-min expiry for this API",
            "agent_id": agent_id,
        },
    )
    data2 = _extract_tool_result(result2)

    assert data2["status"] == "logged"
    assert data2["session_id"] == session_id
    assert data2["observation_index"] == 1

    # ------------------------------------------------------------------
    # Step 3: End session
    # ------------------------------------------------------------------
    result3 = await mcp.call_tool(
        "memento_session_end",
        {"session_id": session_id},
    )
    data3 = _extract_tool_result(result3)

    assert data3["status"] == "ENDED"
    assert data3["observation_count"] == 1
    assert data3["consolidation_queued"] is True

    # ------------------------------------------------------------------
    # Step 4: Run consolidation (LLM mocked — returns JWT candidate)
    # ------------------------------------------------------------------
    session_log = await session_store.get_session(session_id)
    assert session_log is not None, "Session must exist in store after end_session"

    consol_result = await run_consolidation(
        session_log,
        fake_mem0,
        fake_graphiti,
        session_store,
        http_client=mock_llm_client,
    )

    assert consol_result.errors == [], f"Consolidation errors: {consol_result.errors}"
    assert consol_result.promoted >= 1, (
        f"Expected ≥1 REVIEWED memory promoted, "
        f"got promoted={consol_result.promoted}, "
        f"unverified={consol_result.unverified}, "
        f"skipped_injection={consol_result.skipped_injection}"
    )

    # Verify the memory was stored in mem0 (project-scoped candidate).
    assert len(fake_mem0._memories) >= 1, (
        "Expected at least one memory stored in FakeMemoryStore after consolidation"
    )

    # ------------------------------------------------------------------
    # Step 5: New context assemble for a related task
    # ------------------------------------------------------------------
    result5 = await mcp.call_tool(
        "memento_context_assemble",
        {
            "project": project_id,
            "task": "fix auth bug",
            "agent_id": agent_id,
        },
    )
    data5 = _extract_tool_result(result5)
    resp5 = ContextResponse.model_validate(data5)

    # ------------------------------------------------------------------
    # Step 6: Assert context contains the JWT expiry learning
    # ------------------------------------------------------------------
    all_memories = resp5.context.project_memories + resp5.context.org_memories

    assert resp5.metadata.total_memories >= 1, (
        f"Expected ≥1 memory in context, got 0. "
        f"Store contents: {[m.content for m in fake_mem0._memories.values()]}"
    )

    jwt_memories = [
        m
        for m in all_memories
        if "jwt" in m.content.lower() and "15-min" in m.content.lower()
    ]
    assert len(jwt_memories) >= 1, (
        f"Expected a memory referencing 'JWT' and '15-min expiry' in context.\n"
        f"All memory contents: {[m.content for m in all_memories]}"
    )

    # Confirm trust tier is REVIEWED (confidence 0.9 ≥ 0.6 threshold).
    assert jwt_memories[0].trust_tier == "reviewed", (
        f"Expected trust_tier='reviewed', got {jwt_memories[0].trust_tier!r}"
    )


# ---------------------------------------------------------------------------
# Core loop test — real LLM (opt-in, requires MEMENTO_E2E_REAL_LLM=1)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.skipif(
    not os.getenv("MEMENTO_E2E_REAL_LLM"),
    reason=(
        "Real-LLM e2e test skipped. "
        "Set MEMENTO_E2E_REAL_LLM=1 and configure MEMENTO_LLM_* env vars to run."
    ),
)
async def test_core_loop_with_real_llm(
    real_llm_env: None,
    session_store: SessionStore,
) -> None:
    """Full core loop: observe → consolidate → retrieve (real LLM).

    Identical flow to the mocked test but uses the actual LLM endpoint
    configured via ``MEMENTO_LLM_*`` environment variables.  The assertion
    on JWT content is relaxed to a substring check since LLM wording varies.

    Requires:
    * ``MEMENTO_E2E_REAL_LLM=1``
    * ``MEMENTO_LLM_API_KEY`` — valid API key
    * ``MEMENTO_LLM_BASE_URL`` — reachable LLM endpoint
    * ``MEMENTO_LLM_MODEL`` — available model on that endpoint
    """
    project_id = f"test-real-{uuid.uuid4().hex[:8]}"
    agent_id = "test-agent-real"

    fake_mem0 = FakeMemoryStore()
    fake_graphiti = FakeMemoryStore()

    mcp = create_mcp_server(
        session_store=session_store,
        mem0_store=fake_mem0,
        graphiti_store=fake_graphiti,
    )

    # Step 1: empty context
    result1 = await mcp.call_tool(
        "memento_context_assemble",
        {"project": project_id, "task": "implement auth", "agent_id": agent_id},
    )
    data1 = _extract_tool_result(result1)
    resp1 = ContextResponse.model_validate(data1)
    session_id = resp1.session_id
    assert resp1.metadata.total_memories == 0

    # Step 2: log JWT observation
    await mcp.call_tool(
        "memento_session_log",
        {
            "session_id": session_id,
            "observation": "JWT tokens need 15-min expiry for this API",
            "agent_id": agent_id,
        },
    )

    # Step 3: end session
    await mcp.call_tool("memento_session_end", {"session_id": session_id})

    # Step 4: consolidate with real LLM (no mock client)
    session_log = await session_store.get_session(session_id)
    assert session_log is not None
    consol_result = await run_consolidation(
        session_log,
        fake_mem0,
        fake_graphiti,
        session_store,
        http_client=None,  # uses real httpx.AsyncClient
    )
    assert consol_result.errors == [], f"Consolidation errors: {consol_result.errors}"

    # Step 5: retrieve with new session
    result5 = await mcp.call_tool(
        "memento_context_assemble",
        {"project": project_id, "task": "fix auth bug", "agent_id": agent_id},
    )
    data5 = _extract_tool_result(result5)
    resp5 = ContextResponse.model_validate(data5)

    # Step 6: verify something about auth/JWT made it into memory
    all_memories = resp5.context.project_memories + resp5.context.org_memories
    assert resp5.metadata.total_memories >= 1, (
        f"Expected ≥1 memory in context after real-LLM consolidation. "
        f"consol_result={consol_result}"
    )
    # At least one memory should reference JWT or token expiry concepts.
    jwt_related = [
        m
        for m in all_memories
        if any(kw in m.content.lower() for kw in ("jwt", "token", "expiry", "auth"))
    ]
    assert len(jwt_related) >= 1, (
        f"Expected a JWT/auth-related memory in context.\n"
        f"All memory contents: {[m.content for m in all_memories]}"
    )

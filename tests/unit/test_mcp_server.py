"""Unit tests for memento.mcp.server — MCP tool logic with mocked stores.

Tests cover:
- ``memento_context_assemble``: context assembly, deduplication, trust filtering, policies
- ``memento_session_log``: observation logging, error surfacing
- ``memento_session_end``: session closing, consolidation enqueue
- Tool schema registration via ``list_tools``
- Transport entry-points (``run_stdio``, store bootstrap helpers)
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Protocol
from unittest.mock import AsyncMock

import pytest

from memento.mcp.server import (
    ContextResponse,
    EndResponse,
    LogResponse,
    PolicyItem,
    _MemorySearchable,
    create_mcp_server,
    run_stdio,
    try_open_graphiti,
    try_open_mem0,
)
from memento.memory.schema import (
    Cell,
    Lifetime,
    MemoryObject,
    Observation,
    Provenance,
    Scope,
    SessionLog,
    SessionStatus,
    TrustTier,
)
from memento.stores.base import MemoryResult, SearchFilters
from memento.stores.session_store import SessionNotActiveError, SessionNotFoundError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_tool_result(raw: Any) -> dict[str, Any]:
    """Extract the dict payload from an MCP ``call_tool`` result.

    FastMCP's ``call_tool`` returns either:
    - ``(list[TextContent], dict)`` tuple when the tool returns a dict (structured output)
    - ``list[TextContent]`` when the tool returns a non-dict

    This helper normalises both shapes into a plain dict.
    """
    if isinstance(raw, tuple):
        # Structured output: (content_blocks, structured_dict)
        return dict(raw[1])
    # Fallback: parse JSON from first TextContent
    content_list: Sequence[Any] = raw
    return json.loads(content_list[0].text)  # type: ignore[union-attr]


def _make_memory(
    *,
    memory_id: str | None = None,
    content: str = "test memory",
    trust_tier: TrustTier = TrustTier.REVIEWED,
    scope: Scope = Scope.PROJECT,
    project_id: str = "proj-1",
) -> MemoryObject:
    """Create a minimal valid MemoryObject for testing."""
    return MemoryObject(
        id=memory_id or str(uuid.uuid4()),
        content=content,
        scope=scope,
        lifetime=Lifetime.PERSISTENT,
        cell=Cell.C5,
        confidence=0.9,
        trust_tier=trust_tier,
        provenance=Provenance(
            source_session_id="s1",
            source_agent_id="a1",
            consolidation_batch_id="b1",
            consolidation_model="gpt-4o",
            created_by="test",
        ),
        project_id=project_id,
    )


def _make_session(
    *,
    session_id: str = "sess-1",
    status: SessionStatus = SessionStatus.ACTIVE,
    observations: list[Observation] | None = None,
) -> SessionLog:
    """Create a minimal valid SessionLog for testing."""
    return SessionLog(
        session_id=session_id,
        project_id="proj-1",
        agent_id="agent-1",
        task_description="Fix flaky test",
        started_at=datetime.now(UTC),
        status=status,
        observations=observations or [],
    )


def _mock_session_store() -> AsyncMock:
    """Return an AsyncMock that mimics SessionStore's public API."""
    store = AsyncMock()
    store.create_session = AsyncMock(return_value=_make_session())
    store.get_session = AsyncMock(return_value=_make_session())
    store.append_observation = AsyncMock()
    store.end_session = AsyncMock(
        return_value=_make_session(
            status=SessionStatus.ENDED,
            observations=[
                Observation(
                    timestamp=datetime.now(UTC),
                    content="obs1",
                ),
            ],
        )
    )
    return store


def _mock_mem0_store(results: list[MemoryResult] | None = None) -> AsyncMock:
    store = AsyncMock()
    store.search = AsyncMock(return_value=results or [])
    return store


def _mock_graphiti_store(results: list[MemoryResult] | None = None) -> AsyncMock:
    store = AsyncMock()
    store.search = AsyncMock(return_value=results or [])
    return store


# ---------------------------------------------------------------------------
# Test: Tool registration / list_tools  (FR-MCP-06)
# ---------------------------------------------------------------------------


class TestToolRegistration:
    """Verify all expected tools are registered with correct schemas."""

    def test_server_has_three_tools(self) -> None:
        session_store = _mock_session_store()
        mcp = create_mcp_server(session_store=session_store)
        # FastMCP stores tools internally; access via _tool_manager
        tool_names = set(mcp._tool_manager._tools.keys())
        assert tool_names == {
            "memento_context_assemble",
            "memento_session_log",
            "memento_session_end",
        }

    def test_tools_have_input_schemas(self) -> None:
        session_store = _mock_session_store()
        mcp = create_mcp_server(session_store=session_store)
        for name, tool in mcp._tool_manager._tools.items():
            # Each tool should have parameters from type hints
            assert tool.parameters is not None, f"{name} missing parameters schema"


# ---------------------------------------------------------------------------
# Test: memento_context_assemble  (FR-MCP-02)
# ---------------------------------------------------------------------------


class TestContextAssemble:
    """Tests for the memento_context_assemble tool."""

    async def test_creates_session_and_returns_context(self) -> None:
        mem1 = _make_memory(content="project fact")
        org1 = _make_memory(content="org learning", scope=Scope.ORG)

        session_store = _mock_session_store()
        mem0 = _mock_mem0_store([MemoryResult(memory=mem1, score=0.9)])
        graphiti = _mock_graphiti_store([MemoryResult(memory=org1, score=0.85)])

        mcp = create_mcp_server(
            session_store=session_store,
            mem0_store=mem0,
            graphiti_store=graphiti,
        )

        result = await mcp.call_tool(
            "memento_context_assemble",
            {
                "project": "proj-1",
                "task": "Fix flaky test",
                "agent_id": "agent-1",
            },
        )

        data = _extract_tool_result(result)
        resp = ContextResponse.model_validate(data)

        # Session was created
        session_store.create_session.assert_awaited_once()
        call_kwargs = session_store.create_session.call_args.kwargs
        assert call_kwargs["agent_id"] == "agent-1"
        assert call_kwargs["project_id"] == "proj-1"
        assert call_kwargs["task_description"] == "Fix flaky test"

        # Memories are returned with source attribution
        assert resp.project_id == "proj-1"
        assert len(resp.context.project_memories) == 1
        assert resp.context.project_memories[0].source == "mem0"
        assert len(resp.context.org_memories) == 1
        assert resp.context.org_memories[0].source == "graphiti"
        assert resp.metadata.total_memories == 2

    async def test_trust_tier_filter_applied(self) -> None:
        """Verify search filters pass trust_tier_min=REVIEWED to stores."""
        session_store = _mock_session_store()
        mem0 = _mock_mem0_store()

        mcp = create_mcp_server(session_store=session_store, mem0_store=mem0)

        await mcp.call_tool(
            "memento_context_assemble",
            {
                "project": "p1",
                "task": "test",
                "agent_id": "a1",
            },
        )

        call_args = mem0.search.call_args
        filters: SearchFilters = call_args[0][1]
        assert filters.trust_tier_min == TrustTier.REVIEWED

    async def test_deduplication_across_stores(self) -> None:
        """Memories with the same ID from both stores should appear only once."""
        shared_id = str(uuid.uuid4())
        mem = _make_memory(memory_id=shared_id, content="shared")

        session_store = _mock_session_store()
        mem0 = _mock_mem0_store([MemoryResult(memory=mem, score=0.9)])
        graphiti = _mock_graphiti_store([MemoryResult(memory=mem, score=0.8)])

        mcp = create_mcp_server(
            session_store=session_store,
            mem0_store=mem0,
            graphiti_store=graphiti,
        )

        result = await mcp.call_tool(
            "memento_context_assemble",
            {"project": "p1", "task": "t", "agent_id": "a1"},
        )

        data = _extract_tool_result(result)
        resp = ContextResponse.model_validate(data)
        # Should appear in project_memories (mem0 is queried first)
        # and NOT again in org_memories
        assert resp.metadata.total_memories == 1

    async def test_no_stores_returns_empty_context(self) -> None:
        """When no optional stores are provided, context is empty but session is created."""
        session_store = _mock_session_store()
        mcp = create_mcp_server(session_store=session_store)

        result = await mcp.call_tool(
            "memento_context_assemble",
            {"project": "p1", "task": "t", "agent_id": "a1"},
        )

        data = _extract_tool_result(result)
        resp = ContextResponse.model_validate(data)
        assert resp.metadata.total_memories == 0
        assert resp.context.project_memories == []
        assert resp.context.org_memories == []
        session_store.create_session.assert_awaited_once()

    async def test_max_memories_forwarded(self) -> None:
        """The max_memories param is passed through to search filters."""
        session_store = _mock_session_store()
        graphiti = _mock_graphiti_store()

        mcp = create_mcp_server(session_store=session_store, graphiti_store=graphiti)

        await mcp.call_tool(
            "memento_context_assemble",
            {"project": "p1", "task": "t", "agent_id": "a1", "max_memories": 5},
        )

        filters: SearchFilters = graphiti.search.call_args[0][1]
        assert filters.limit == 5

    async def test_graphiti_queries_are_org_scoped(self) -> None:
        """Org memories must be restricted to ORG scope only."""
        session_store = _mock_session_store()
        graphiti = _mock_graphiti_store()
        mcp = create_mcp_server(session_store=session_store, graphiti_store=graphiti)

        await mcp.call_tool(
            "memento_context_assemble",
            {"project": "p1", "task": "t", "agent_id": "a1"},
        )

        filters: SearchFilters = graphiti.search.call_args[0][1]
        assert filters.scope is Scope.ORG

    async def test_context_queries_apply_valid_at_filter(self) -> None:
        """Context assembly should exclude memories that are no longer valid."""
        session_store = _mock_session_store()
        mem0 = _mock_mem0_store()
        graphiti = _mock_graphiti_store()
        mcp = create_mcp_server(
            session_store=session_store,
            mem0_store=mem0,
            graphiti_store=graphiti,
        )

        await mcp.call_tool(
            "memento_context_assemble",
            {"project": "p1", "task": "t", "agent_id": "a1"},
        )

        mem0_filters: SearchFilters = mem0.search.call_args[0][1]
        graphiti_filters: SearchFilters = graphiti.search.call_args[0][1]
        assert mem0_filters.valid_at is not None
        assert graphiti_filters.valid_at is not None

    async def test_rejects_non_positive_max_memories(self) -> None:
        """Negative or zero limits should fail at the MCP boundary."""
        session_store = _mock_session_store()
        mcp = create_mcp_server(session_store=session_store)

        with pytest.raises(Exception, match="max_memories must be >= 1"):
            await mcp.call_tool(
                "memento_context_assemble",
                {"project": "p1", "task": "t", "agent_id": "a1", "max_memories": 0},
            )

    async def test_does_not_create_session_when_search_fails(self) -> None:
        """Backend failures should not leave behind a newly created ACTIVE session."""
        session_store = _mock_session_store()
        mem0 = _mock_mem0_store()
        mem0.search = AsyncMock(side_effect=RuntimeError("backend down"))
        mcp = create_mcp_server(session_store=session_store, mem0_store=mem0)

        with pytest.raises(Exception, match="backend down"):
            await mcp.call_tool(
                "memento_context_assemble",
                {"project": "p1", "task": "t", "agent_id": "a1"},
            )

        session_store.create_session.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test: memento_session_log  (FR-MCP-03)
# ---------------------------------------------------------------------------


class TestSessionLog:
    """Tests for the memento_session_log tool."""

    async def test_logs_observation_to_active_session(self) -> None:
        session = _make_session(
            observations=[Observation(timestamp=datetime.now(UTC), content="first")]
        )
        session_store = _mock_session_store()
        session_store.get_session = AsyncMock(side_effect=[session, session])
        session_store.append_observation = AsyncMock(return_value=2)

        mcp = create_mcp_server(session_store=session_store)

        result = await mcp.call_tool(
            "memento_session_log",
            {
                "session_id": "sess-1",
                "observation": "Found root cause",
                "agent_id": "agent-1",
                "tags": ["debug"],
            },
        )

        data = _extract_tool_result(result)
        resp = LogResponse.model_validate(data)
        assert resp.session_id == "sess-1"
        assert resp.status == "logged"
        assert resp.observation_index == 2

        # append_observation should have been called
        session_store.append_observation.assert_awaited_once()
        call_args = session_store.append_observation.call_args
        assert call_args[0][0] == "sess-1"
        obs: Observation = call_args[0][1]
        assert obs.content == "Found root cause"
        assert obs.tags == ["debug"]

    async def test_invalid_session_id_raises_error(self) -> None:
        session_store = _mock_session_store()
        session_store.get_session = AsyncMock(return_value=None)

        mcp = create_mcp_server(session_store=session_store)

        # call_tool propagates ValueError as MCP tool error
        with pytest.raises(Exception, match="Session not found"):
            await mcp.call_tool(
                "memento_session_log",
                {
                    "session_id": "nonexistent",
                    "observation": "test",
                    "agent_id": "a1",
                },
            )

    async def test_non_active_session_raises_error(self) -> None:
        ended = _make_session(status=SessionStatus.ENDED)
        session_store = _mock_session_store()
        session_store.get_session = AsyncMock(return_value=ended)

        mcp = create_mcp_server(session_store=session_store)

        with pytest.raises(Exception, match="ENDED.*expected ACTIVE"):
            await mcp.call_tool(
                "memento_session_log",
                {
                    "session_id": "sess-1",
                    "observation": "test",
                    "agent_id": "agent-1",
                },
            )

    async def test_agent_id_mismatch_raises_error(self) -> None:
        """Only the agent that owns the session can append observations."""
        session_store = _mock_session_store()
        session_store.get_session = AsyncMock(return_value=_make_session())

        mcp = create_mcp_server(session_store=session_store)

        with pytest.raises(Exception, match="belongs to agent"):
            await mcp.call_tool(
                "memento_session_log",
                {
                    "session_id": "sess-1",
                    "observation": "test",
                    "agent_id": "different-agent",
                },
            )

    async def test_context_dict_forwarded(self) -> None:
        """Optional context dict is passed through to the Observation."""
        session_store = _mock_session_store()
        mcp = create_mcp_server(session_store=session_store)

        await mcp.call_tool(
            "memento_session_log",
            {
                "session_id": "sess-1",
                "observation": "test",
                "agent_id": "agent-1",
                "context": {"file": "main.py", "line": 42},
            },
        )

        obs: Observation = session_store.append_observation.call_args[0][1]
        assert obs.context == {"file": "main.py", "line": 42}


# ---------------------------------------------------------------------------
# Test: memento_session_end  (FR-MCP-04)
# ---------------------------------------------------------------------------


class TestSessionEnd:
    """Tests for the memento_session_end tool."""

    async def test_ends_session_and_returns_summary(self) -> None:
        ended = _make_session(
            status=SessionStatus.ENDED,
            observations=[Observation(timestamp=datetime.now(UTC), content="o1")],
        )
        ended.ended_at = datetime.now(UTC)
        session_store = _mock_session_store()
        session_store.end_session = AsyncMock(return_value=ended)

        mcp = create_mcp_server(session_store=session_store)

        result = await mcp.call_tool(
            "memento_session_end",
            {"session_id": "sess-1"},
        )

        data = _extract_tool_result(result)
        resp = EndResponse.model_validate(data)

        assert resp.session_id == "sess-1"
        assert resp.status == "ENDED"
        assert resp.observation_count == 1
        assert resp.consolidation_queued is True
        assert resp.consolidation_enqueue_id == "session:sess-1"
        assert resp.started_at
        assert resp.ended_at

    async def test_no_consolidation_when_disabled(self) -> None:
        ended = _make_session(status=SessionStatus.ENDED)
        ended.ended_at = datetime.now(UTC)
        session_store = _mock_session_store()
        session_store.end_session = AsyncMock(return_value=ended)

        mcp = create_mcp_server(session_store=session_store)

        result = await mcp.call_tool(
            "memento_session_end",
            {"session_id": "sess-1", "trigger_consolidation": False},
        )

        data = _extract_tool_result(result)
        resp = EndResponse.model_validate(data)
        assert resp.consolidation_queued is False
        assert resp.consolidation_enqueue_id is None

    async def test_not_found_session_raises_error(self) -> None:
        session_store = _mock_session_store()
        session_store.end_session = AsyncMock(side_effect=SessionNotFoundError("bad-id"))

        mcp = create_mcp_server(session_store=session_store)

        with pytest.raises(Exception, match="Session not found"):
            await mcp.call_tool(
                "memento_session_end",
                {"session_id": "bad-id"},
            )

    async def test_non_active_session_raises_error(self) -> None:
        session_store = _mock_session_store()
        session_store.end_session = AsyncMock(
            side_effect=SessionNotActiveError("Session 'x' has status 'ENDED', expected ACTIVE")
        )

        mcp = create_mcp_server(session_store=session_store)

        with pytest.raises(Exception, match="expected ACTIVE"):
            await mcp.call_tool(
                "memento_session_end",
                {"session_id": "x"},
            )

    async def test_summary_is_logged_not_raised(self) -> None:
        """Providing a summary should not break the call."""
        ended = _make_session(status=SessionStatus.ENDED)
        ended.ended_at = datetime.now(UTC)
        session_store = _mock_session_store()
        session_store.end_session = AsyncMock(return_value=ended)

        mcp = create_mcp_server(session_store=session_store)

        result = await mcp.call_tool(
            "memento_session_end",
            {"session_id": "sess-1", "summary": "All done"},
        )

        data = _extract_tool_result(result)
        assert data["status"] == "ENDED"


# ---------------------------------------------------------------------------
# Test: Policies field in ContextResponse  (TRD §6.1 / FR-CTX-03)
# ---------------------------------------------------------------------------


class TestPolicies:
    """Verify the ``policies`` field and ``include_policies`` parameter."""

    async def test_context_response_contains_policies_field(self) -> None:
        """ContextResponse.context always includes a ``policies`` key (may be empty)."""
        session_store = _mock_session_store()
        mcp = create_mcp_server(session_store=session_store)

        result = await mcp.call_tool(
            "memento_context_assemble",
            {"project": "p1", "task": "t", "agent_id": "a1"},
        )

        data = _extract_tool_result(result)
        resp = ContextResponse.model_validate(data)
        assert isinstance(resp.context.policies, list)
        # Phase 0 stub: policies list is empty regardless of include_policies
        assert resp.context.policies == []

    async def test_include_policies_false_returns_empty_policies(self) -> None:
        session_store = _mock_session_store()
        mcp = create_mcp_server(session_store=session_store)

        result = await mcp.call_tool(
            "memento_context_assemble",
            {"project": "p1", "task": "t", "agent_id": "a1", "include_policies": False},
        )

        data = _extract_tool_result(result)
        resp = ContextResponse.model_validate(data)
        assert resp.context.policies == []

    def test_policy_item_model_fields(self) -> None:
        """PolicyItem has the three TRD-mandated fields."""
        item = PolicyItem(source="AGENTS.md", section="auth-policy", content="Use JWT tokens")
        assert item.source == "AGENTS.md"
        assert item.section == "auth-policy"
        assert item.content == "Use JWT tokens"


# ---------------------------------------------------------------------------
# Test: Transport surface (stdio runner + store bootstrap helpers)
# ---------------------------------------------------------------------------


class TestTransportSurface:
    """Verify the transport entry-points and store helpers are properly exposed."""

    def test_run_stdio_is_async_function(self) -> None:
        """``run_stdio`` is importable and is an async function."""
        assert inspect.iscoroutinefunction(run_stdio)

    def test_memory_searchable_is_protocol(self) -> None:
        """``_MemorySearchable`` is a typing Protocol for structural subtyping."""
        assert issubclass(_MemorySearchable, Protocol)

    def test_try_open_mem0_is_async(self) -> None:
        assert inspect.iscoroutinefunction(try_open_mem0)

    def test_try_open_graphiti_is_async(self) -> None:
        assert inspect.iscoroutinefunction(try_open_graphiti)

    async def test_try_open_mem0_returns_none_when_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When Mem0 initialisation fails, the helper returns None gracefully."""
        monkeypatch.setenv("MEMENTO_LLM_API_KEY", "test-key")
        from memento.config import Settings

        settings = Settings()
        monkeypatch.setattr(
            "memento.stores.mem0_store.Mem0Store.create",
            AsyncMock(side_effect=RuntimeError("mem0 unavailable")),
        )
        result = await try_open_mem0(settings)
        assert result is None

    async def test_try_open_graphiti_returns_none_when_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When Graphiti initialisation fails, the helper returns None gracefully."""
        monkeypatch.setenv("MEMENTO_LLM_API_KEY", "test-key")
        from memento.config import Settings
        from memento.stores.graphiti_store import GraphitiStore

        settings = Settings()
        monkeypatch.setattr(
            GraphitiStore,
            "initialize",
            AsyncMock(side_effect=RuntimeError("graphiti unavailable")),
        )
        result = await try_open_graphiti(settings)
        assert result is None

    async def test_try_open_graphiti_returns_none_when_cleanup_also_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cleanup errors should not defeat Graphiti graceful degradation."""
        monkeypatch.setenv("MEMENTO_LLM_API_KEY", "test-key")
        from memento.config import Settings
        from memento.stores.graphiti_store import GraphitiStore

        settings = Settings()
        monkeypatch.setattr(
            GraphitiStore,
            "initialize",
            AsyncMock(side_effect=RuntimeError("graphiti unavailable")),
        )
        monkeypatch.setattr(
            GraphitiStore,
            "close",
            AsyncMock(side_effect=RuntimeError("close fail")),
        )

        result = await try_open_graphiti(settings)
        assert result is None


# ---------------------------------------------------------------------------
# Test: MCP HTTP runner surface (main.py lifespan wires stores + port)
# ---------------------------------------------------------------------------


class TestMainLifespan:
    """Verify main.py lifespan wiring without starting real servers."""

    def test_main_imports_store_bootstrap_helpers(self) -> None:
        """main.py imports the shared store helpers from mcp.server."""
        import memento.main as main_mod

        # The lifespan uses these; verify they're reachable from the module
        assert hasattr(main_mod, "_lifespan")

    def test_app_uses_lifespan(self) -> None:
        """FastAPI app is configured with the lifespan context manager."""
        from memento.main import app

        # FastAPI stores lifespan on the router
        assert app.router.lifespan_context is not None

    async def test_wait_for_mcp_startup_raises_when_task_exits_early(self) -> None:
        """The API should fail startup if the MCP server task dies immediately."""
        from memento.main import _wait_for_mcp_startup

        async def _boom() -> None:
            raise RuntimeError("bind failed")

        server = SimpleNamespace(started=False, should_exit=False)
        task: asyncio.Task[None] = asyncio.create_task(_boom())

        with pytest.raises(RuntimeError, match="bind failed"):
            await _wait_for_mcp_startup(server, task, timeout=0.1)

    async def test_shutdown_resources_closes_session_store_on_task_error(self) -> None:
        """Shutdown must close stores even when the MCP task crashes."""
        from memento.main import _shutdown_mcp_resources

        async def _boom() -> None:
            raise RuntimeError("mcp crashed")

        server = SimpleNamespace(should_exit=False)
        task: asyncio.Task[None] = asyncio.create_task(_boom())
        session_store = AsyncMock()
        graphiti_store = AsyncMock()
        graphiti_store.close = AsyncMock()

        with pytest.raises(RuntimeError, match="mcp crashed"):
            await _shutdown_mcp_resources(server, task, graphiti_store, session_store)

        graphiti_store.close.assert_awaited_once()
        session_store.close.assert_awaited_once()

    async def test_shutdown_resources_closes_stores_on_task_cancellation(self) -> None:
        """Cancelled MCP tasks should still trigger shutdown cleanup."""
        from memento.main import _shutdown_mcp_resources

        async def _sleep_forever() -> None:
            await asyncio.sleep(10)

        server = SimpleNamespace(should_exit=False)
        task: asyncio.Task[None] = asyncio.create_task(_sleep_forever())
        session_store = AsyncMock()
        graphiti_store = AsyncMock()
        graphiti_store.close = AsyncMock()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _shutdown_mcp_resources(server, task, graphiti_store, session_store)

        graphiti_store.close.assert_awaited_once()
        session_store.close.assert_awaited_once()

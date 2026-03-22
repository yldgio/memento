"""Integration tests: context assembly retrieves memories with source attribution.

Verifies that ``memento_context_assemble`` returns:
- ``project_memories`` sourced from Mem0 (``source == "mem0"``)
- ``org_memories`` sourced from Graphiti (``source == "graphiti"``)
- A ``session_id`` suitable for subsequent ``session_log`` / ``session_end`` calls
- Correct ``metadata`` fields (``total_memories``, ``assembly_time_ms``)

For a freshly-started stack with no pre-seeded memories, the lists will be
empty but the response structure must still be valid.  Tests that verify
actual memory content (round-trip) are covered in ``test_consolidation.py``
and the store-specific integration tests.

Skip condition
--------------
Tests are skipped when ``MEMENTO_RUN_INTEGRATION != "1"`` or the MCP server
is not reachable on port 8081.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.integration.conftest import (
    _INTEGRATION_ENABLED,
    mcp_reachable,
)

pytestmark = pytest.mark.skipif(
    not (_INTEGRATION_ENABLED and mcp_reachable()),
    reason=(
        "Context assembly integration tests require MEMENTO_RUN_INTEGRATION=1 "
        "and a running MCP server on port 8081."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_content(content: list[Any]) -> dict[str, Any]:
    """Extract JSON dict from MCP tool result content."""
    assert content, "Tool response must be non-empty"
    text: str = getattr(content[0], "text", None) or str(content[0])
    return json.loads(text)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Response structure
# ---------------------------------------------------------------------------


class TestContextAssemblyStructure:
    """Verify the shape of ``memento_context_assemble`` responses."""

    @pytest.mark.asyncio
    async def test_response_has_all_required_top_level_keys(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """Response contains ``session_id``, ``project_id``, ``context``, ``metadata``."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "memento_context_assemble",
                    {
                        "project": unique_project_id,
                        "task": "Review authentication module for security issues",
                        "agent_id": unique_agent_id,
                    },
                )

        assert not result.isError, f"Tool error: {result.content}"
        data = _parse_content(result.content)

        for key in ("session_id", "project_id", "context", "metadata"):
            assert key in data, f"Missing key {key!r} in response: {list(data.keys())}"

    @pytest.mark.asyncio
    async def test_project_id_matches_request(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """``project_id`` in the response matches the requested project."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "memento_context_assemble",
                    {
                        "project": unique_project_id,
                        "task": "Test project_id echo",
                        "agent_id": unique_agent_id,
                    },
                )

        data = _parse_content(result.content)
        assert data["project_id"] == unique_project_id

    @pytest.mark.asyncio
    async def test_context_block_structure(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """``context`` block contains ``project_memories``, ``org_memories``,
        and ``policies`` lists."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "memento_context_assemble",
                    {
                        "project": unique_project_id,
                        "task": "Test context block structure",
                        "agent_id": unique_agent_id,
                    },
                )

        data = _parse_content(result.content)
        ctx = data["context"]

        assert isinstance(ctx.get("project_memories"), list), (
            "project_memories must be a list"
        )
        assert isinstance(ctx.get("org_memories"), list), (
            "org_memories must be a list"
        )
        assert isinstance(ctx.get("policies"), list), "policies must be a list"

    @pytest.mark.asyncio
    async def test_metadata_values_are_non_negative(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """``metadata.total_memories`` and ``metadata.assembly_time_ms`` are >= 0."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "memento_context_assemble",
                    {
                        "project": unique_project_id,
                        "task": "Test metadata numeric values",
                        "agent_id": unique_agent_id,
                    },
                )

        data = _parse_content(result.content)
        meta = data["metadata"]

        assert isinstance(meta.get("total_memories"), int)
        assert meta["total_memories"] >= 0

        assert isinstance(meta.get("assembly_time_ms"), int)
        assert meta["assembly_time_ms"] >= 0


# ---------------------------------------------------------------------------
# Source attribution
# ---------------------------------------------------------------------------


class TestContextAssemblySourceAttribution:
    """Verify that memory items carry correct ``source`` attribution."""

    @pytest.mark.asyncio
    async def test_project_memories_have_mem0_source(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """Any returned ``project_memories`` must carry ``source == "mem0"``."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "memento_context_assemble",
                    {
                        "project": unique_project_id,
                        "task": "Verify mem0 source attribution",
                        "agent_id": unique_agent_id,
                    },
                )

        data = _parse_content(result.content)
        project_memories: list[dict[str, Any]] = data["context"]["project_memories"]

        for item in project_memories:
            assert item.get("source") == "mem0", (
                f"project_memory item has unexpected source: {item.get('source')!r}"
            )

    @pytest.mark.asyncio
    async def test_org_memories_have_graphiti_source(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """Any returned ``org_memories`` must carry ``source == "graphiti"``."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "memento_context_assemble",
                    {
                        "project": unique_project_id,
                        "task": "Verify graphiti source attribution",
                        "agent_id": unique_agent_id,
                    },
                )

        data = _parse_content(result.content)
        org_memories: list[dict[str, Any]] = data["context"]["org_memories"]

        for item in org_memories:
            assert item.get("source") == "graphiti", (
                f"org_memory item has unexpected source: {item.get('source')!r}"
            )

    @pytest.mark.asyncio
    async def test_memory_items_have_required_fields(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """Each memory item (if any) contains all required MemoryItem fields."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "memento_context_assemble",
                    {
                        "project": unique_project_id,
                        "task": "Verify memory item field completeness",
                        "agent_id": unique_agent_id,
                        "max_memories": 5,
                    },
                )

        data = _parse_content(result.content)
        all_memories: list[dict[str, Any]] = (
            data["context"]["project_memories"] + data["context"]["org_memories"]
        )
        required_fields = {"memory_id", "content", "confidence", "trust_tier", "source"}

        for item in all_memories:
            missing = required_fields - set(item.keys())
            assert not missing, (
                f"Memory item missing fields {missing}: {item}"
            )
            assert isinstance(item["content"], str) and item["content"], (
                "content must be a non-empty string"
            )
            assert 0.0 <= item["confidence"] <= 1.0, (
                f"confidence out of range [0,1]: {item['confidence']}"
            )


# ---------------------------------------------------------------------------
# Session usability after assembly
# ---------------------------------------------------------------------------


class TestSessionUsabilityAfterAssembly:
    """Session returned by context_assemble must support log and end operations."""

    @pytest.mark.asyncio
    async def test_returned_session_id_is_usable(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """Session ID from context_assemble can be used for log and end calls."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Assemble and capture session_id
                assemble = await session.call_tool(
                    "memento_context_assemble",
                    {
                        "project": unique_project_id,
                        "task": "Validate session usability post-assembly",
                        "agent_id": unique_agent_id,
                    },
                )
                session_id = _parse_content(assemble.content)["session_id"]

                # Log should succeed
                log_result = await session.call_tool(
                    "memento_session_log",
                    {
                        "session_id": session_id,
                        "observation": "Context assembled successfully.",
                        "agent_id": unique_agent_id,
                    },
                )
                assert not log_result.isError, (
                    f"session_log failed on session created by context_assemble: "
                    f"{log_result.content}"
                )

                # End should succeed
                end_result = await session.call_tool(
                    "memento_session_end",
                    {"session_id": session_id, "trigger_consolidation": False},
                )
                assert not end_result.isError, (
                    f"session_end failed on session created by context_assemble: "
                    f"{end_result.content}"
                )

        end_data = _parse_content(end_result.content)
        assert end_data["status"] == "ENDED"

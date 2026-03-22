"""Integration tests: MCP client connects, discovers tools, each tool responds.

Tests run against the live MCP streamable-HTTP server (port 8081) started by
the Docker Compose stack.  They verify:

- The MCP handshake succeeds (``session.initialize()``).
- All three expected tools are advertised (``list_tools``).
- Each tool accepts valid input and returns a non-error response.
- Tool input schemas are present and non-empty.

Skip condition
--------------
Tests are skipped when ``MEMENTO_RUN_INTEGRATION != "1"`` or the MCP server
is not reachable on port 8081.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from tests.integration.conftest import (
    _INTEGRATION_ENABLED,
    mcp_reachable,
)

pytestmark = pytest.mark.skipif(
    not (_INTEGRATION_ENABLED and mcp_reachable()),
    reason=(
        "MCP tool integration tests require MEMENTO_RUN_INTEGRATION=1 "
        "and a running MCP server on port 8081."
    ),
)

# Expected tool names (TRD §6.1)
_EXPECTED_TOOLS = frozenset(
    {
        "memento_context_assemble",
        "memento_session_log",
        "memento_session_end",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_tool_content(content: list[Any]) -> dict[str, Any]:
    """Extract the dict payload from an MCP ``call_tool`` content list."""
    assert content, "Tool response content must be non-empty"
    item = content[0]
    # TextContent has a ``text`` attribute containing JSON
    text: str = getattr(item, "text", None) or str(item)
    return json.loads(text)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------


class TestMcpToolDiscovery:
    """Verify the MCP server advertises the expected tools."""

    @pytest.mark.asyncio
    async def test_client_handshake_succeeds(self, mcp_endpoint: str) -> None:
        """MCP ``initialize`` handshake completes without error."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # If we reach here, handshake succeeded

    @pytest.mark.asyncio
    async def test_list_tools_returns_expected_names(self, mcp_endpoint: str) -> None:
        """``list_tools`` returns exactly the three Memento tools."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()

        tool_names = {t.name for t in result.tools}
        assert _EXPECTED_TOOLS <= tool_names, (
            f"Missing tools: {_EXPECTED_TOOLS - tool_names}. "
            f"Found: {tool_names}"
        )

    @pytest.mark.asyncio
    async def test_all_tools_have_input_schemas(self, mcp_endpoint: str) -> None:
        """Every tool exposes a non-empty ``inputSchema``."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()

        for tool in result.tools:
            if tool.name not in _EXPECTED_TOOLS:
                continue
            schema = tool.inputSchema
            assert schema is not None, f"Tool {tool.name!r} missing inputSchema"
            # Schema must define at least one property
            props = getattr(schema, "properties", None) or {}
            assert props, f"Tool {tool.name!r} has empty inputSchema properties"


# ---------------------------------------------------------------------------
# memento_context_assemble
# ---------------------------------------------------------------------------


class TestContextAssembleTool:
    """Verify ``memento_context_assemble`` call succeeds and returns well-formed data."""

    @pytest.mark.asyncio
    async def test_tool_returns_session_id(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """``memento_context_assemble`` creates a session and returns its ID."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "memento_context_assemble",
                    {
                        "project": unique_project_id,
                        "task": "Integration test: verify tool response shape",
                        "agent_id": unique_agent_id,
                    },
                )

        assert not result.isError, f"Tool returned error: {result.content}"
        data = _parse_tool_content(result.content)

        assert "session_id" in data, f"Missing 'session_id' in response: {data}"
        assert data["project_id"] == unique_project_id
        assert isinstance(data["session_id"], str) and data["session_id"]

    @pytest.mark.asyncio
    async def test_tool_response_has_context_structure(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """Response contains ``context`` block with project/org memory lists."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "memento_context_assemble",
                    {
                        "project": unique_project_id,
                        "task": "Test context structure",
                        "agent_id": unique_agent_id,
                    },
                )

        data = _parse_tool_content(result.content)
        context = data.get("context", {})
        assert "project_memories" in context, "Missing 'project_memories' in context"
        assert "org_memories" in context, "Missing 'org_memories' in context"
        assert "policies" in context, "Missing 'policies' in context"
        assert isinstance(context["project_memories"], list)
        assert isinstance(context["org_memories"], list)

    @pytest.mark.asyncio
    async def test_tool_returns_metadata(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """Response ``metadata`` block contains timing and total count."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "memento_context_assemble",
                    {
                        "project": unique_project_id,
                        "task": "Test metadata fields",
                        "agent_id": unique_agent_id,
                    },
                )

        data = _parse_tool_content(result.content)
        meta = data.get("metadata", {})
        assert "total_memories" in meta
        assert "assembly_time_ms" in meta
        assert isinstance(meta["total_memories"], int)
        assert meta["assembly_time_ms"] >= 0


# ---------------------------------------------------------------------------
# memento_session_log
# ---------------------------------------------------------------------------


class TestSessionLogTool:
    """Verify ``memento_session_log`` appends observations correctly."""

    @pytest.mark.asyncio
    async def test_tool_returns_log_response(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """``memento_session_log`` returns observation index and timestamp."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Step 1: create a session
                assemble = await session.call_tool(
                    "memento_context_assemble",
                    {
                        "project": unique_project_id,
                        "task": "Test session log",
                        "agent_id": unique_agent_id,
                    },
                )
                assemble_data = _parse_tool_content(assemble.content)
                session_id: str = assemble_data["session_id"]

                # Step 2: log an observation
                log_result = await session.call_tool(
                    "memento_session_log",
                    {
                        "session_id": session_id,
                        "observation": "Observed: type hints improve IDE support.",
                        "agent_id": unique_agent_id,
                        "tags": ["pattern"],
                    },
                )

        assert not log_result.isError, f"Tool error: {log_result.content}"
        log_data = _parse_tool_content(log_result.content)

        assert log_data["session_id"] == session_id
        assert "observation_index" in log_data
        assert log_data["observation_index"] >= 1
        assert "timestamp" in log_data
        assert log_data["status"] == "logged"

    @pytest.mark.asyncio
    async def test_tool_rejects_invalid_session(
        self,
        mcp_endpoint: str,
        unique_agent_id: str,
    ) -> None:
        """``memento_session_log`` returns an error for a non-existent session."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        bogus_session_id = str(uuid.uuid4())

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "memento_session_log",
                    {
                        "session_id": bogus_session_id,
                        "observation": "This should fail.",
                        "agent_id": unique_agent_id,
                    },
                )

        assert result.isError, (
            "Expected an error response for invalid session_id, got success"
        )


# ---------------------------------------------------------------------------
# memento_session_end
# ---------------------------------------------------------------------------


class TestSessionEndTool:
    """Verify ``memento_session_end`` closes the session correctly."""

    @pytest.mark.asyncio
    async def test_tool_returns_end_response(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """``memento_session_end`` returns ENDED status and observation count."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                assemble = await session.call_tool(
                    "memento_context_assemble",
                    {
                        "project": unique_project_id,
                        "task": "Test session end",
                        "agent_id": unique_agent_id,
                    },
                )
                session_id: str = _parse_tool_content(assemble.content)["session_id"]

                end_result = await session.call_tool(
                    "memento_session_end",
                    {
                        "session_id": session_id,
                        "trigger_consolidation": False,
                    },
                )

        assert not end_result.isError, f"Tool error: {end_result.content}"
        end_data = _parse_tool_content(end_result.content)

        assert end_data["session_id"] == session_id
        assert end_data["status"] == "ENDED"
        assert "observation_count" in end_data
        assert "started_at" in end_data
        assert "ended_at" in end_data
        assert end_data["consolidation_queued"] is False

    @pytest.mark.asyncio
    async def test_tool_rejects_invalid_session(
        self,
        mcp_endpoint: str,
    ) -> None:
        """``memento_session_end`` returns an error for a non-existent session."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        bogus_session_id = str(uuid.uuid4())

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "memento_session_end",
                    {"session_id": bogus_session_id},
                )

        assert result.isError, (
            "Expected an error response for invalid session_id, got success"
        )

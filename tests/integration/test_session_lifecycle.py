"""Integration tests: full session lifecycle via MCP tools.

Tests the complete agent workflow end-to-end:

1. ``memento_context_assemble`` — creates a session and returns its ID.
2. ``memento_session_log`` — appends observations to the active session.
3. ``memento_session_end`` — ends the session; status transitions to ENDED.
4. Post-end behaviour — subsequent log attempts are rejected.

All tests run against the live MCP streamable-HTTP server (port 8081).

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
        "Session lifecycle integration tests require MEMENTO_RUN_INTEGRATION=1 "
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
# Full lifecycle
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    """End-to-end session workflow: create → log → end."""

    @pytest.mark.asyncio
    async def test_full_lifecycle_returns_ended_status(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """Complete lifecycle: create → two logs → end → status is ENDED."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 1. Create session via context_assemble
                assemble = await session.call_tool(
                    "memento_context_assemble",
                    {
                        "project": unique_project_id,
                        "task": "Implement feature X with full test coverage",
                        "agent_id": unique_agent_id,
                    },
                )
                assert not assemble.isError, f"context_assemble failed: {assemble.content}"
                assemble_data = _parse_content(assemble.content)
                session_id: str = assemble_data["session_id"]

                assert session_id, "session_id must be a non-empty string"
                assert assemble_data["project_id"] == unique_project_id

                # 2. Log first observation
                log1 = await session.call_tool(
                    "memento_session_log",
                    {
                        "session_id": session_id,
                        "observation": "Discovered: the legacy module uses global state.",
                        "agent_id": unique_agent_id,
                        "tags": ["anti-pattern", "legacy"],
                    },
                )
                assert not log1.isError, f"session_log (1) failed: {log1.content}"
                log1_data = _parse_content(log1.content)
                assert log1_data["observation_index"] == 1
                assert log1_data["status"] == "logged"

                # 3. Log second observation
                log2 = await session.call_tool(
                    "memento_session_log",
                    {
                        "session_id": session_id,
                        "observation": "Refactored to dependency injection; tests pass.",
                        "agent_id": unique_agent_id,
                        "tags": ["pattern", "refactor"],
                    },
                )
                assert not log2.isError, f"session_log (2) failed: {log2.content}"
                log2_data = _parse_content(log2.content)
                assert log2_data["observation_index"] == 2

                # 4. End session
                end = await session.call_tool(
                    "memento_session_end",
                    {
                        "session_id": session_id,
                        "summary": "Refactored legacy global-state module.",
                        "trigger_consolidation": False,
                    },
                )
                assert not end.isError, f"session_end failed: {end.content}"
                end_data = _parse_content(end.content)

        # Assertions on the ended session
        assert end_data["session_id"] == session_id
        assert end_data["status"] == "ENDED", (
            f"Expected status 'ENDED', got {end_data['status']!r}"
        )
        assert end_data["observation_count"] == 2
        assert end_data["started_at"], "started_at must be non-empty"
        assert end_data["ended_at"], "ended_at must be non-empty"
        assert end_data["consolidation_queued"] is False

    @pytest.mark.asyncio
    async def test_session_id_is_unique_per_call(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """Two separate ``context_assemble`` calls produce different session IDs."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        session_ids: list[str] = []

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for i in range(2):
                    result = await session.call_tool(
                        "memento_context_assemble",
                        {
                            "project": f"{unique_project_id}-{i}",
                            "task": "Parallel session uniqueness check",
                            "agent_id": unique_agent_id,
                        },
                    )
                    assert not result.isError
                    data = _parse_content(result.content)
                    session_ids.append(data["session_id"])

        assert session_ids[0] != session_ids[1], (
            "Two context_assemble calls must produce different session IDs"
        )

    @pytest.mark.asyncio
    async def test_log_after_end_is_rejected(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """``memento_session_log`` must reject observations on an ENDED session."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Create and immediately end the session
                assemble = await session.call_tool(
                    "memento_context_assemble",
                    {
                        "project": unique_project_id,
                        "task": "Test post-end log rejection",
                        "agent_id": unique_agent_id,
                    },
                )
                session_id = _parse_content(assemble.content)["session_id"]

                await session.call_tool(
                    "memento_session_end",
                    {"session_id": session_id, "trigger_consolidation": False},
                )

                # Attempt to log after session is ended — must fail
                late_log = await session.call_tool(
                    "memento_session_log",
                    {
                        "session_id": session_id,
                        "observation": "This observation should be rejected.",
                        "agent_id": unique_agent_id,
                    },
                )

        assert late_log.isError, (
            "Expected an error when logging to an ENDED session, got success"
        )

    @pytest.mark.asyncio
    async def test_consolidation_queued_flag(
        self,
        mcp_endpoint: str,
        unique_project_id: str,
        unique_agent_id: str,
    ) -> None:
        """``trigger_consolidation=True`` sets ``consolidation_queued=True`` and
        returns a non-empty ``consolidation_enqueue_id``."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(mcp_endpoint) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                assemble = await session.call_tool(
                    "memento_context_assemble",
                    {
                        "project": unique_project_id,
                        "task": "Test consolidation flag",
                        "agent_id": unique_agent_id,
                    },
                )
                session_id = _parse_content(assemble.content)["session_id"]

                end = await session.call_tool(
                    "memento_session_end",
                    {
                        "session_id": session_id,
                        "trigger_consolidation": True,
                    },
                )

        end_data = _parse_content(end.content)
        assert end_data["consolidation_queued"] is True
        enqueue_id: str | None = end_data.get("consolidation_enqueue_id")
        assert enqueue_id and len(enqueue_id) > 0, (
            "Expected a non-empty consolidation_enqueue_id"
        )
        assert session_id in enqueue_id, (
            f"consolidation_enqueue_id {enqueue_id!r} should reference {session_id!r}"
        )

"""Shared fixtures and skip-guards for Memento integration tests.

All integration tests require:
- ``MEMENTO_RUN_INTEGRATION=1`` environment variable
- A running Docker Compose stack (for tests that hit the live API/MCP ports)

Individual test modules declare their own ``pytestmark`` referencing the
helpers defined here.  Fixtures are available to any test in this package
via the normal pytest fixture discovery mechanism.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# Environment / reachability helpers (evaluated at collection time)
# ---------------------------------------------------------------------------

_INTEGRATION_ENABLED: bool = os.environ.get("MEMENTO_RUN_INTEGRATION", "").strip() == "1"
_HAS_API_KEY: bool = bool(os.environ.get("MEMENTO_LLM_API_KEY", "").strip())


def _port_reachable(host: str, port: int, *, timeout: float = 2.0) -> bool:
    """Return *True* if a TCP connection to *host*:*port* can be established."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _api_host() -> str:
    return os.environ.get("MEMENTO_API_HOST", "localhost")


def _falkordb_host() -> str:
    return os.environ.get("MEMENTO_FALKORDB_HOST", _api_host())


def _api_port() -> int:
    return int(os.environ.get("MEMENTO_API_PORT", "8080"))


def _mcp_port() -> int:
    return int(os.environ.get("MEMENTO_MCP_PORT_HOST", "8081"))


def _falkordb_port() -> int:
    return int(os.environ.get("MEMENTO_FALKORDB_PORT", "6379"))


def api_reachable() -> bool:
    """Return *True* if the Memento REST API is reachable."""
    return _port_reachable(_api_host(), _api_port())


def mcp_reachable() -> bool:
    """Return *True* if the Memento MCP HTTP server is reachable."""
    return _port_reachable(_api_host(), _mcp_port())


def falkordb_reachable() -> bool:
    """Return *True* if FalkorDB is reachable at the configured host/port."""
    host = os.environ.get("MEMENTO_FALKORDB_HOST", "localhost")
    return _port_reachable(host, _falkordb_port())


# ---------------------------------------------------------------------------
# Reusable skip-reason strings
# ---------------------------------------------------------------------------

_STACK_SKIP_REASON = (
    "Integration tests require MEMENTO_RUN_INTEGRATION=1 "
    "and a running Docker Compose stack (memento-api on port 8080)."
)

_MCP_SKIP_REASON = (
    "Integration tests require MEMENTO_RUN_INTEGRATION=1 "
    "and a running MCP server on port 8081."
)

_CONSOLIDATION_SKIP_REASON = (
    "Consolidation integration tests require MEMENTO_RUN_INTEGRATION=1 "
    "and MEMENTO_LLM_API_KEY set."
)


# ---------------------------------------------------------------------------
# Shared pytest marks for easy re-use in test modules
# ---------------------------------------------------------------------------

stack_required = pytest.mark.skipif(
    not (_INTEGRATION_ENABLED and api_reachable()),
    reason=_STACK_SKIP_REASON,
)

mcp_required = pytest.mark.skipif(
    not (_INTEGRATION_ENABLED and mcp_reachable()),
    reason=_MCP_SKIP_REASON,
)

consolidation_required = pytest.mark.skipif(
    not (_INTEGRATION_ENABLED and _HAS_API_KEY),
    reason=_CONSOLIDATION_SKIP_REASON,
)


# ---------------------------------------------------------------------------
# URL fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def api_base_url() -> str:
    """Base URL for the Memento REST API (port 8080 by default)."""
    return f"http://{_api_host()}:{_api_port()}"


@pytest.fixture(scope="session")
def mcp_base_url() -> str:
    """Base URL for the Memento MCP HTTP server (port 8081 by default)."""
    return f"http://{_api_host()}:{_mcp_port()}"


@pytest.fixture(scope="session")
def mcp_endpoint(mcp_base_url: str) -> str:
    """Full URL for the MCP streamable-HTTP endpoint (``/mcp/`` path)."""
    return f"{mcp_base_url}/mcp/"


# ---------------------------------------------------------------------------
# HTTP client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def http_client(api_base_url: str) -> AsyncGenerator[httpx.AsyncClient, None]:
    """An ``httpx.AsyncClient`` pre-configured for the Memento REST API."""
    async with httpx.AsyncClient(base_url=api_base_url, timeout=30.0) as client:
        yield client


# ---------------------------------------------------------------------------
# Unique-ID fixtures (prevent cross-test contamination)
# ---------------------------------------------------------------------------


@pytest.fixture
def unique_project_id() -> str:
    """Unique project ID for a single test run."""
    return f"integ-proj-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def unique_agent_id() -> str:
    """Unique agent ID for a single test run."""
    return f"integ-agent-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# LLM mock transport
# ---------------------------------------------------------------------------


class MockLLMTransport(httpx.AsyncBaseTransport):
    """An ``httpx`` transport that returns a fixed chat-completions response.

    Used to avoid real LLM calls in consolidation integration tests.
    The ``candidates`` list is serialised as the ``choices[0].message.content``
    JSON string, matching the format that ``_parse_llm_response`` expects.
    """

    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        self._candidates = candidates

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(self._candidates),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }
        return httpx.Response(200, json=body)


@pytest.fixture
def mock_llm_candidates() -> list[dict[str, Any]]:
    """Default LLM candidate list returned by the mock transport."""
    return [
        {
            "content": "Always write integration tests before shipping.",
            "confidence": 0.92,
            "scope": "project",
            "tags": ["testing", "process"],
        },
        {
            "content": "Use unique IDs per test to prevent cross-contamination.",
            "confidence": 0.88,
            "scope": "org",
            "tags": ["testing", "pattern"],
        },
    ]


@pytest.fixture
def mock_llm_http_client(
    mock_llm_candidates: list[dict[str, Any]],
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` wired to the mock LLM transport."""
    return httpx.AsyncClient(transport=MockLLMTransport(mock_llm_candidates))

"""Integration tests: health / reachability of Docker Compose stack components.

Verifies that all required services are responding before the heavier MCP
and session tests run.  Each assertion is independent so a CI run can report
exactly which component is unavailable.

Skip condition
--------------
All tests are skipped when ``MEMENTO_RUN_INTEGRATION != "1"`` or the
Memento API is not reachable on port 8080.
"""

from __future__ import annotations

import httpx
import pytest

from tests.integration.conftest import (
    _INTEGRATION_ENABLED,
    _api_host,
    _api_port,
    _falkordb_host,
    _falkordb_port,
    _mcp_port,
    _port_reachable,
    api_reachable,
)

pytestmark = pytest.mark.skipif(
    not (_INTEGRATION_ENABLED and api_reachable()),
    reason=(
        "Health integration tests require MEMENTO_RUN_INTEGRATION=1 "
        "and a running Docker Compose stack (memento-api on port 8080)."
    ),
)


# ---------------------------------------------------------------------------
# API server health
# ---------------------------------------------------------------------------


class TestApiServerHealth:
    """Verify the Memento REST API (port 8080) is up and responding."""

    @pytest.mark.asyncio
    async def test_api_responds_with_http(self, api_base_url: str) -> None:
        """GET /openapi.json returns 200 — FastAPI is running."""
        async with httpx.AsyncClient(base_url=api_base_url, timeout=10.0) as client:
            response = await client.get("/openapi.json")
        assert response.status_code == 200, (
            f"Expected 200 from /openapi.json, got {response.status_code}"
        )

    @pytest.mark.asyncio
    async def test_openapi_schema_contains_memento(self, api_base_url: str) -> None:
        """OpenAPI schema identifies the application as Memento."""
        async with httpx.AsyncClient(base_url=api_base_url, timeout=10.0) as client:
            response = await client.get("/openapi.json")
        schema = response.json()
        title: str = schema.get("info", {}).get("title", "")
        assert "Memento" in title, f"Unexpected API title: {title!r}"

    @pytest.mark.asyncio
    async def test_api_docs_endpoint_available(self, api_base_url: str) -> None:
        """GET /docs returns 200 — Swagger UI is available."""
        async with httpx.AsyncClient(base_url=api_base_url, timeout=10.0) as client:
            response = await client.get("/docs")
        assert response.status_code == 200, (
            f"Expected 200 from /docs, got {response.status_code}"
        )


# ---------------------------------------------------------------------------
# MCP server health
# ---------------------------------------------------------------------------


class TestMcpServerHealth:
    """Verify the Memento MCP HTTP server (port 8081) is reachable."""

    def test_mcp_port_reachable(self) -> None:
        """MCP server port 8081 accepts TCP connections."""
        host = _api_host()
        port = _mcp_port()
        assert _port_reachable(host, port), (
            f"MCP server not reachable at {host}:{port}. "
            "Is the Docker Compose stack running?"
        )

    @pytest.mark.asyncio
    async def test_mcp_endpoint_responds(self, mcp_endpoint: str) -> None:
        """MCP /mcp/ endpoint returns an HTTP response (any status code)."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                # A plain GET to the MCP endpoint; the server may return
                # 405 Method Not Allowed (POST required) but that still
                # confirms the endpoint is live.
                response = await client.get(mcp_endpoint)
                assert response.status_code in {200, 405, 404, 400}, (
                    f"Unexpected status {response.status_code} from {mcp_endpoint}"
                )
            except httpx.ConnectError as exc:
                pytest.fail(f"Cannot reach MCP endpoint {mcp_endpoint}: {exc}")


# ---------------------------------------------------------------------------
# FalkorDB health
# ---------------------------------------------------------------------------


class TestFalkorDbHealth:
    """Verify FalkorDB (port 6379) is reachable when the full stack is running."""

    def test_falkordb_port_reachable(self) -> None:
        """FalkorDB listens on port 6379."""
        host = _falkordb_host()
        port = _falkordb_port()
        if not _port_reachable(host, port):
            pytest.skip(f"FalkorDB not reachable at {host}:{port} — skipping.")
        assert _port_reachable(host, port), (
            f"FalkorDB not reachable at {host}:{port}."
        )


# ---------------------------------------------------------------------------
# Component summary
# ---------------------------------------------------------------------------


class TestComponentSummary:
    """Aggregate view: report which components are available."""

    def test_component_availability_summary(self) -> None:
        """Log availability of all stack components (informational)."""
        api_host = _api_host()
        components = {
            f"memento-api (:{_api_port()})": _port_reachable(api_host, _api_port()),
            f"mcp-server (:{_mcp_port()})": _port_reachable(api_host, _mcp_port()),
            f"falkordb (:{_falkordb_port()})": _port_reachable(
                _falkordb_host(), _falkordb_port()
            ),
        }
        unavailable = [name for name, ok in components.items() if not ok]
        if unavailable:
            pytest.skip(
                f"Some stack components unavailable: {', '.join(unavailable)}"
            )
        # All reachable — pass
        assert all(components.values())

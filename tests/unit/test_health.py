"""Unit tests for GET /health endpoint (FR-API-08).

Covers:
- ``_ping_graphiti``: ok, None store, missing driver, driver failure
- ``_ping_mem0``: ok, None store, uninitialised client
- ``_ping_llm``: ok, empty URL, connection error
- ``_component_status``: ok, raises, timeout (via mock)
- ``GET /health`` route: all-ok, graphiti-degraded, mem0-degraded,
  llm-degraded, all-error
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from memento.main import (
    HealthComponents,
    HealthResponse,
    _component_status,
    _ping_graphiti,
    _ping_llm,
    _ping_mem0,
    app,
)

# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------


def _make_graphiti_store(*, fail: bool = False) -> MagicMock:
    """Return a mock GraphitiStore with a working (or failing) FalkorDB driver."""
    store = MagicMock()
    driver = AsyncMock()
    if fail:
        driver.verify_connectivity = AsyncMock(side_effect=OSError("connection refused"))
    else:
        driver.verify_connectivity = AsyncMock(return_value=None)
    store._graphiti = MagicMock()
    store._graphiti.driver = driver
    return store


def _make_mem0_store(*, initialised: bool = True) -> MagicMock:
    """Return a mock Mem0Store with an initialised (or None) ``_mem`` client."""
    store = MagicMock()
    store._mem = MagicMock() if initialised else None
    return store


def _make_settings(*, llm_base_url: str = "http://test-llm/v1") -> MagicMock:
    """Return a mock Settings object."""
    settings = MagicMock()
    settings.llm_base_url = llm_base_url
    return settings


def _mock_httpx_ok(mock_cls: Any) -> None:
    """Configure mock httpx.AsyncClient to return a 200 response."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)


# ---------------------------------------------------------------------------
# _ping_graphiti
# ---------------------------------------------------------------------------


async def test_ping_graphiti_ok() -> None:
    """Returns 'ok' when driver.verify_connectivity() succeeds."""
    store = _make_graphiti_store()
    result = await _ping_graphiti(store)
    assert result == "ok"
    store._graphiti.driver.verify_connectivity.assert_awaited_once()


async def test_ping_graphiti_none_store_raises() -> None:
    """Raises RuntimeError when store is None."""
    with pytest.raises(RuntimeError, match="not initialised"):
        await _ping_graphiti(None)


async def test_ping_graphiti_missing_driver_raises() -> None:
    """Raises RuntimeError when the graphiti driver attribute is None."""
    store = MagicMock()
    store._graphiti = MagicMock()
    store._graphiti.driver = None
    with pytest.raises(RuntimeError, match="driver not available"):
        await _ping_graphiti(store)


async def test_ping_graphiti_driver_error_propagates() -> None:
    """Propagates exceptions from driver.verify_connectivity()."""
    store = _make_graphiti_store(fail=True)
    with pytest.raises(OSError, match="connection refused"):
        await _ping_graphiti(store)


# ---------------------------------------------------------------------------
# _ping_mem0
# ---------------------------------------------------------------------------


async def test_ping_mem0_ok() -> None:
    """Returns 'ok' when _mem is initialised."""
    store = _make_mem0_store(initialised=True)
    result = await _ping_mem0(store)
    assert result == "ok"


async def test_ping_mem0_none_store_raises() -> None:
    """Raises RuntimeError when store is None."""
    with pytest.raises(RuntimeError, match="not initialised"):
        await _ping_mem0(None)


async def test_ping_mem0_uninitialised_client_raises() -> None:
    """Raises RuntimeError when _mem is None (client not yet initialised)."""
    store = _make_mem0_store(initialised=False)
    with pytest.raises(RuntimeError, match="client not initialised"):
        await _ping_mem0(store)


# ---------------------------------------------------------------------------
# _ping_llm
# ---------------------------------------------------------------------------


async def test_ping_llm_ok() -> None:
    """Returns 'ok' when the HTTP GET succeeds."""
    with patch("memento.main.httpx.AsyncClient") as mock_cls:
        _mock_httpx_ok(mock_cls)
        result = await _ping_llm("http://test-llm/v1")
    assert result == "ok"


async def test_ping_llm_any_http_status_is_ok() -> None:
    """Even a 401 response counts as reachable (server is up)."""
    with patch("memento.main.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=MagicMock(status_code=401))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await _ping_llm("http://test-llm/v1")
    assert result == "ok"


async def test_ping_llm_empty_url_raises() -> None:
    """Raises RuntimeError when base_url is empty."""
    with pytest.raises(RuntimeError, match="not configured"):
        await _ping_llm("")


async def test_ping_llm_connection_error_propagates() -> None:
    """Propagates OS-level connection errors."""
    with patch("memento.main.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=OSError("connection refused"))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(OSError):
            await _ping_llm("http://test-llm/v1")


# ---------------------------------------------------------------------------
# _component_status
# ---------------------------------------------------------------------------


async def test_component_status_ok() -> None:
    """Returns 'ok' when the coroutine completes successfully."""

    async def _succeed() -> str:
        return "ok"

    assert await _component_status(_succeed()) == "ok"


async def test_component_status_raises_returns_error() -> None:
    """Returns 'error' when the coroutine raises."""

    async def _fail() -> str:
        raise RuntimeError("boom")

    assert await _component_status(_fail()) == "error"


async def test_component_status_timeout_returns_error() -> None:
    """Returns 'error' when asyncio.wait_for raises TimeoutError."""

    async def _succeed() -> str:  # pragma: no cover
        return "ok"

    coro = _succeed()
    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        result = await _component_status(coro)
    # Ensure the coroutine is closed so it is not left unawaited.
    coro.close()
    assert result == "error"


# ---------------------------------------------------------------------------
# GET /health route (via TestClient — lifespan NOT started)
# ---------------------------------------------------------------------------


def _setup_state(
    *,
    graphiti_store: Any = None,
    mem0_store: Any = None,
    settings: Any = None,
) -> None:
    """Inject mock objects directly into shared app state (bypasses lifespan)."""
    app.state.graphiti_store = graphiti_store
    app.state.mem0_store = mem0_store
    app.state.settings = settings


def test_health_all_ok() -> None:
    """All components healthy → HTTP 200, status 'ok'."""
    _setup_state(
        graphiti_store=_make_graphiti_store(),
        mem0_store=_make_mem0_store(),
        settings=_make_settings(),
    )
    with patch("memento.main.httpx.AsyncClient") as mock_cls:
        _mock_httpx_ok(mock_cls)
        response = TestClient(app).get("/health")

    assert response.status_code == 200
    body = HealthResponse.model_validate(response.json())
    assert body.status == "ok"
    assert body.components == HealthComponents(graphiti="ok", mem0="ok", llm="ok")


def test_health_graphiti_error_is_degraded() -> None:
    """Graphiti store None → status 'degraded', graphiti component 'error'."""
    _setup_state(
        graphiti_store=None,
        mem0_store=_make_mem0_store(),
        settings=_make_settings(),
    )
    with patch("memento.main.httpx.AsyncClient") as mock_cls:
        _mock_httpx_ok(mock_cls)
        response = TestClient(app).get("/health")

    assert response.status_code == 200
    body = HealthResponse.model_validate(response.json())
    assert body.status == "degraded"
    assert body.components.graphiti == "error"
    assert body.components.mem0 == "ok"
    assert body.components.llm == "ok"


def test_health_mem0_error_is_degraded() -> None:
    """Mem0 store None → status 'degraded', mem0 component 'error'."""
    _setup_state(
        graphiti_store=_make_graphiti_store(),
        mem0_store=None,
        settings=_make_settings(),
    )
    with patch("memento.main.httpx.AsyncClient") as mock_cls:
        _mock_httpx_ok(mock_cls)
        response = TestClient(app).get("/health")

    assert response.status_code == 200
    body = HealthResponse.model_validate(response.json())
    assert body.status == "degraded"
    assert body.components.graphiti == "ok"
    assert body.components.mem0 == "error"
    assert body.components.llm == "ok"


def test_health_llm_error_is_degraded() -> None:
    """Empty LLM base URL → status 'degraded', llm component 'error'."""
    _setup_state(
        graphiti_store=_make_graphiti_store(),
        mem0_store=_make_mem0_store(),
        settings=_make_settings(llm_base_url=""),
    )
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    body = HealthResponse.model_validate(response.json())
    assert body.status == "degraded"
    assert body.components.graphiti == "ok"
    assert body.components.mem0 == "ok"
    assert body.components.llm == "error"


def test_health_all_error_is_degraded() -> None:
    """All stores None, no settings → status 'degraded', all components 'error'."""
    _setup_state(graphiti_store=None, mem0_store=None, settings=None)
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    body = HealthResponse.model_validate(response.json())
    assert body.status == "degraded"
    assert body.components == HealthComponents(graphiti="error", mem0="error", llm="error")


def test_health_response_schema() -> None:
    """Response JSON matches the documented schema shape."""
    _setup_state(graphiti_store=None, mem0_store=None, settings=None)
    response = TestClient(app).get("/health")

    data = response.json()
    assert set(data.keys()) == {"status", "components"}
    assert set(data["components"].keys()) == {"graphiti", "mem0", "llm"}
    assert data["status"] in {"ok", "degraded"}
    assert all(v in {"ok", "error"} for v in data["components"].values())

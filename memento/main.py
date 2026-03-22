"""FastAPI application entrypoint for Memento REST API and MCP server.

The MCP streamable-HTTP transport is served on a **separate port**
(``settings.mcp_port``, default 8081) so that agents connect to a
dedicated endpoint while the REST API remains on ``settings.api_port``
(default 8080).  The stdio transport is started via
``python -m memento.mcp.server``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Literal

import httpx
import uvicorn
from fastapi import FastAPI, Request
from pydantic import BaseModel

from memento.mcp.server import (
    create_mcp_server,
    try_open_graphiti,
    try_open_mem0,
)
from memento.stores.session_store import SessionStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health check models (FR-API-08)
# ---------------------------------------------------------------------------


class HealthComponents(BaseModel):
    """Per-component health statuses."""

    graphiti: Literal["ok", "error"]
    mem0: Literal["ok", "error"]
    llm: Literal["ok", "error"]


class HealthResponse(BaseModel):
    """Top-level health response returned by ``GET /health`` (FR-API-08)."""

    status: Literal["ok", "degraded"]
    components: HealthComponents


# ---------------------------------------------------------------------------
# Health check helpers
# ---------------------------------------------------------------------------


async def _ping_graphiti(store: object | None) -> str:
    """Attempt FalkorDB connectivity via the graphiti driver.

    Raises :exc:`RuntimeError` (or any driver exception) on failure.
    """
    if store is None:
        raise RuntimeError("graphiti store not initialised")
    graphiti_obj = getattr(store, "_graphiti", None)
    driver = getattr(graphiti_obj, "driver", None) if graphiti_obj is not None else None
    if driver is None:
        raise RuntimeError("graphiti driver not available")
    await driver.verify_connectivity()
    return "ok"


async def _ping_mem0(store: object | None) -> str:
    """Check that the Mem0 async client is initialised.

    Raises :exc:`RuntimeError` if the store or its internal client is absent.
    """
    if store is None:
        raise RuntimeError("mem0 store not initialised")
    if getattr(store, "_mem", None) is None:
        raise RuntimeError("mem0 client not initialised")
    return "ok"


async def _ping_llm(base_url: str) -> str:
    """Verify the LLM base URL is network-reachable.

    Any HTTP response (including 4xx/5xx) counts as reachable.
    Raises on connection error or empty URL.
    """
    if not base_url:
        raise RuntimeError("LLM base URL not configured")
    async with httpx.AsyncClient() as client:
        await client.get(base_url)
    return "ok"


async def _component_status(coro: Awaitable[str]) -> Literal["ok", "error"]:
    """Await *coro* with a 5-second timeout.

    Returns ``'ok'`` on success, ``'error'`` on any exception or timeout.
    """
    try:
        await asyncio.wait_for(coro, timeout=5.0)
        return "ok"
    except Exception:
        return "error"


async def _wait_for_mcp_startup(
    mcp_http: uvicorn.Server,
    mcp_task: asyncio.Task[None],
    *,
    timeout: float = 5.0,
) -> None:
    """Fail fast if the MCP HTTP server does not bind successfully."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not mcp_http.started:
        if mcp_task.done():
            await mcp_task
            raise RuntimeError("MCP HTTP server exited before startup completed")
        if loop.time() >= deadline:
            mcp_http.should_exit = True
            raise TimeoutError("MCP HTTP server did not start within the startup timeout")
        await asyncio.sleep(0.01)


async def _shutdown_mcp_resources(
    mcp_http: uvicorn.Server,
    mcp_task: asyncio.Task[None],
    graphiti_store: object | None,
    session_store: SessionStore,
) -> None:
    """Shut down MCP resources and re-raise task failures after cleanup."""
    mcp_error: BaseException | None = None
    if not mcp_task.done():
        mcp_http.should_exit = True
    try:
        await mcp_task
    except BaseException as exc:  # pragma: no cover - exercised via unit tests
        mcp_error = exc

    try:
        if hasattr(graphiti_store, "close"):
            await graphiti_store.close()
    finally:
        await session_store.close()

    if mcp_error is not None:
        raise mcp_error


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Manage application-scoped resources (stores, MCP HTTP server)."""
    from memento.config import get_settings

    settings = get_settings()

    # --- Open stores --------------------------------------------------
    session_store = SessionStore()
    await session_store.open()

    mem0_store = await try_open_mem0(settings)
    graphiti_store = await try_open_graphiti(settings)

    # --- Create MCP server with all available stores -------------------
    mcp_server = create_mcp_server(
        session_store=session_store,
        mem0_store=mem0_store,
        graphiti_store=graphiti_store,
    )

    # --- Start MCP streamable-HTTP on its own port (TRD §9.4) ---------
    mcp_app = mcp_server.streamable_http_app()
    mcp_config = uvicorn.Config(
        mcp_app,
        host="0.0.0.0",
        port=settings.mcp_port,
        log_level=settings.log_level.lower(),
    )
    mcp_http = uvicorn.Server(mcp_config)
    # Prevent the inner uvicorn server from handling process signals that belong
    # to the outer API server process.
    setattr(mcp_http, "install_signal_handlers", lambda: None)
    mcp_task = asyncio.create_task(mcp_http.serve(), name="mcp-http")
    logger.info("MCP streamable-HTTP server starting on port %d", settings.mcp_port)

    # --- Expose stores and settings for the health endpoint --------------
    application.state.graphiti_store = graphiti_store
    application.state.mem0_store = mem0_store
    application.state.settings = settings

    try:
        await _wait_for_mcp_startup(mcp_http, mcp_task)
        yield
    finally:
        # --- Graceful shutdown ----------------------------------------
        await _shutdown_mcp_resources(
            mcp_http,
            mcp_task,
            graphiti_store,
            session_store,
        )
        logger.info("MCP streamable-HTTP server stopped")


app = FastAPI(
    title="Memento",
    description="Agent Memory Platform — LLM-agnostic memory system for AI coding agents",
    version="0.1.0",
    lifespan=_lifespan,
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Return health status for all backend components (FR-API-08).

    Always returns HTTP 200; top-level ``status`` is ``'degraded'`` when any
    component is unhealthy so callers can act without treating partial failures
    as hard errors.
    """
    graphiti_store: object | None = getattr(request.app.state, "graphiti_store", None)
    mem0_store: object | None = getattr(request.app.state, "mem0_store", None)
    settings: object | None = getattr(request.app.state, "settings", None)
    llm_base_url: str = (
        getattr(settings, "llm_base_url", "") if settings is not None else ""
    )

    g_status, m_status, l_status = await asyncio.gather(
        _component_status(_ping_graphiti(graphiti_store)),
        _component_status(_ping_mem0(mem0_store)),
        _component_status(_ping_llm(llm_base_url)),
    )

    components = HealthComponents(graphiti=g_status, mem0=m_status, llm=l_status)
    overall: Literal["ok", "degraded"] = (
        "ok" if g_status == m_status == l_status == "ok" else "degraded"
    )
    return HealthResponse(status=overall, components=components)


def main() -> None:
    """Start the Memento API server."""
    from memento.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "memento.main:app",
        host="0.0.0.0",
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()

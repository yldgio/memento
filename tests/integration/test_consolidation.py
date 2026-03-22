"""Integration tests: consolidation pipeline (Mem0 and Graphiti paths).

Tests the ``run_consolidation`` entry-point end-to-end using:
- Real in-process ``SessionStore`` (SQLite, temp directory)
- Real in-process ``Mem0Store`` (embedded Qdrant, temp directory)
- Real in-process ``GraphitiStore`` **only** when FalkorDB is reachable
- Mocked LLM HTTP calls (``MockLLMTransport`` from conftest)

No Docker stack is required for the Mem0 path; only the Graphiti path
requires a running FalkorDB instance.

Skip conditions
---------------
- All tests: ``MEMENTO_RUN_INTEGRATION != "1"`` or ``MEMENTO_LLM_API_KEY`` unset.
- Graphiti path tests: additionally require FalkorDB reachable on port 6379.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.conftest import (
    _HAS_API_KEY,
    _INTEGRATION_ENABLED,
    MockLLMTransport,
    falkordb_reachable,
)

pytestmark = pytest.mark.skipif(
    not (_INTEGRATION_ENABLED and _HAS_API_KEY),
    reason=(
        "Consolidation integration tests require MEMENTO_RUN_INTEGRATION=1 "
        "and MEMENTO_LLM_API_KEY set."
    ),
)

# ---------------------------------------------------------------------------
# Candidate fixtures
# ---------------------------------------------------------------------------

_PROJECT_CANDIDATE: dict[str, Any] = {
    "content": "Always validate HTTP response status before parsing JSON body.",
    "confidence": 0.91,
    "scope": "project",
    "tags": ["pattern", "http"],
}

_ORG_CANDIDATE: dict[str, Any] = {
    "content": "Use structured logging with correlation IDs across all services.",
    "confidence": 0.87,
    "scope": "org",
    "tags": ["pattern", "observability"],
}

_LOW_CONFIDENCE_CANDIDATE: dict[str, Any] = {
    "content": "Consider caching query results.",
    "confidence": 0.45,  # Below default threshold 0.6 → UNVERIFIED
    "scope": "project",
    "tags": ["performance"],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Inject required MEMENTO_* env vars for in-process store creation."""
    api_key = os.environ.get("MEMENTO_LLM_API_KEY", "integration-test-key")
    monkeypatch.setenv("MEMENTO_LLM_API_KEY", api_key)
    monkeypatch.setenv("MEMENTO_LLM_MODEL", os.environ.get("MEMENTO_LLM_MODEL", "gpt-4o"))
    # Point data_dir at tmp_path so embedded stores are isolated per test
    monkeypatch.setenv("MEMENTO_DATA_DIR", str(tmp_path))
    # Clear the settings singleton so it picks up new env
    from memento.config import get_settings

    get_settings.cache_clear()


@pytest.fixture
async def session_store(tmp_path: Path) -> Any:
    """Open an isolated SQLite SessionStore in a temp directory."""
    from memento.stores.session_store import SessionStore

    store = SessionStore(db_path=tmp_path / "sessions.db")
    await store.open()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def mem0_store(_env: None) -> Any:
    """Create a real Mem0Store using an embedded Qdrant vector store."""
    from memento.config import get_settings
    from memento.stores.mem0_store import Mem0Store

    settings = get_settings()
    return Mem0Store(settings)


@pytest.fixture
async def project_llm_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Mock HTTP client returning a single project-scope candidate."""
    client = httpx.AsyncClient(transport=MockLLMTransport([_PROJECT_CANDIDATE]))
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def org_llm_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Mock HTTP client returning a single org-scope candidate."""
    client = httpx.AsyncClient(transport=MockLLMTransport([_ORG_CANDIDATE]))
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def multi_llm_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Mock HTTP client returning project + org + low-confidence candidates."""
    client = httpx.AsyncClient(
        transport=MockLLMTransport(
            [_PROJECT_CANDIDATE, _ORG_CANDIDATE, _LOW_CONFIDENCE_CANDIDATE]
        )
    )
    try:
        yield client
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_ended_session(
    session_store: Any,
    *,
    project_id: str,
    agent_id: str,
    observations: list[str] | None = None,
) -> Any:
    """Create an ACTIVE session, optionally append observations, then end it."""
    from memento.memory.schema import Observation

    session = await session_store.create_session(
        agent_id=agent_id,
        project_id=project_id,
        task_description="Integration test consolidation task",
    )
    for text in (observations or ["Observed that tests pass consistently."]):
        obs = Observation(timestamp=datetime.now(UTC), content=text)
        await session_store.append_observation(session.session_id, obs)

    return await session_store.end_session(session.session_id)


# ---------------------------------------------------------------------------
# Mem0 (project scope) path
# ---------------------------------------------------------------------------


class TestConsolidationMem0Path:
    """Consolidation pipeline stores project-scope memories in Mem0."""

    @pytest.mark.asyncio
    async def test_project_memory_promoted_to_mem0(
        self,
        session_store: Any,
        mem0_store: Any,
        project_llm_client: httpx.AsyncClient,
        unique_project_id: str,
        unique_agent_id: str,
        _env: None,
    ) -> None:
        """A project-scope candidate at high confidence is stored in Mem0."""
        from unittest.mock import AsyncMock

        from memento.jobs.consolidation import run_consolidation
        from memento.stores.base import SearchFilters

        # Use a real session store; mock graphiti (FalkorDB not required here)
        graphiti_mock = AsyncMock()
        graphiti_mock.search = AsyncMock(return_value=[])
        graphiti_mock.add = AsyncMock(return_value="mock-id")

        session_log = await _create_ended_session(
            session_store,
            project_id=unique_project_id,
            agent_id=unique_agent_id,
            observations=[
                "HTTP response validation prevents unexpected JSON parse errors.",
                "Added status check before .json() call; all tests green.",
            ],
        )

        result = await run_consolidation(
            session_log,
            mem0_store=mem0_store,
            graphiti_store=graphiti_mock,  # type: ignore[arg-type]
            session_store=session_store,
            http_client=project_llm_client,
        )

        assert result.session_id == session_log.session_id
        assert result.promoted >= 1, (
            f"Expected at least 1 promoted memory, got {result.promoted}. "
            f"Errors: {result.errors}"
        )
        assert not result.errors, f"Consolidation errors: {result.errors}"

        # Verify the memory is retrievable from Mem0
        search_results = await mem0_store.search(
            "HTTP response validation",
            SearchFilters(project_id=unique_project_id, limit=10),
        )
        contents = [r.memory.content for r in search_results]
        assert any("HTTP" in c or "response" in c.lower() for c in contents), (
            f"Expected memory about HTTP validation in Mem0; found: {contents}"
        )

    @pytest.mark.asyncio
    async def test_session_marked_consolidated_after_run(
        self,
        session_store: Any,
        mem0_store: Any,
        project_llm_client: httpx.AsyncClient,
        unique_project_id: str,
        unique_agent_id: str,
        _env: None,
    ) -> None:
        """Session status transitions to CONSOLIDATED after ``run_consolidation``."""
        from unittest.mock import AsyncMock

        from memento.jobs.consolidation import run_consolidation
        from memento.memory.schema import SessionStatus

        graphiti_mock = AsyncMock()
        graphiti_mock.search = AsyncMock(return_value=[])
        graphiti_mock.add = AsyncMock(return_value="mock-id")

        session_log = await _create_ended_session(
            session_store,
            project_id=unique_project_id,
            agent_id=unique_agent_id,
        )

        await run_consolidation(
            session_log,
            mem0_store=mem0_store,
            graphiti_store=graphiti_mock,  # type: ignore[arg-type]
            session_store=session_store,
            http_client=project_llm_client,
        )

        updated = await session_store.get_session(session_log.session_id)
        assert updated is not None
        assert updated.status == SessionStatus.CONSOLIDATED, (
            f"Expected CONSOLIDATED status, got {updated.status!r}"
        )

    @pytest.mark.asyncio
    async def test_low_confidence_candidate_stored_as_unverified(
        self,
        session_store: Any,
        mem0_store: Any,
        unique_project_id: str,
        unique_agent_id: str,
        _env: None,
    ) -> None:
        """A candidate below the confidence threshold is stored as UNVERIFIED."""
        from unittest.mock import AsyncMock

        from memento.jobs.consolidation import run_consolidation

        async with httpx.AsyncClient(
            transport=MockLLMTransport([_LOW_CONFIDENCE_CANDIDATE])
        ) as low_conf_client:
            graphiti_mock = AsyncMock()
            graphiti_mock.search = AsyncMock(return_value=[])
            graphiti_mock.add = AsyncMock(return_value="mock-id")

            session_log = await _create_ended_session(
                session_store,
                project_id=unique_project_id,
                agent_id=unique_agent_id,
            )

            result = await run_consolidation(
                session_log,
                mem0_store=mem0_store,
                graphiti_store=graphiti_mock,  # type: ignore[arg-type]
                session_store=session_store,
                http_client=low_conf_client,
            )

            # Low-confidence candidate counts as unverified, not promoted
            assert result.unverified >= 1, (
                f"Expected at least 1 unverified memory; got {result.unverified}"
            )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestConsolidationIdempotency:
    """Running consolidation twice on the same session is a no-op."""

    @pytest.mark.asyncio
    async def test_already_consolidated_session_is_skipped(
        self,
        session_store: Any,
        mem0_store: Any,
        project_llm_client: httpx.AsyncClient,
        unique_project_id: str,
        unique_agent_id: str,
        _env: None,
    ) -> None:
        """Second call on a CONSOLIDATED session returns empty result."""
        from unittest.mock import AsyncMock

        from memento.jobs.consolidation import run_consolidation
        from memento.memory.schema import SessionStatus

        graphiti_mock = AsyncMock()
        graphiti_mock.search = AsyncMock(return_value=[])
        graphiti_mock.add = AsyncMock(return_value="mock-id")

        session_log = await _create_ended_session(
            session_store,
            project_id=unique_project_id,
            agent_id=unique_agent_id,
        )

        # First run — should consolidate
        first = await run_consolidation(
            session_log,
            mem0_store=mem0_store,
            graphiti_store=graphiti_mock,  # type: ignore[arg-type]
            session_store=session_store,
            http_client=project_llm_client,
        )
        assert first.promoted + first.unverified >= 0  # sanity check

        # Reload session to get CONSOLIDATED status
        consolidated_log = await session_store.get_session(session_log.session_id)
        assert consolidated_log is not None
        assert consolidated_log.status == SessionStatus.CONSOLIDATED

        # Second run — must be a no-op (zero promoted/unverified/errors)
        second = await run_consolidation(
            consolidated_log,
            mem0_store=mem0_store,
            graphiti_store=graphiti_mock,  # type: ignore[arg-type]
            session_store=session_store,
            http_client=project_llm_client,
        )
        assert second.promoted == 0
        assert second.unverified == 0
        assert not second.errors


# ---------------------------------------------------------------------------
# Graphiti (org scope) path — requires FalkorDB
# ---------------------------------------------------------------------------


class TestConsolidationGraphitiPath:
    """Consolidation pipeline stores org-scope memories in GraphitiStore.

    These tests are additionally skipped when FalkorDB is not reachable.
    """

    @pytest.mark.asyncio
    async def test_org_memory_stored_in_graphiti(
        self,
        session_store: Any,
        mem0_store: Any,
        org_llm_client: httpx.AsyncClient,
        unique_project_id: str,
        unique_agent_id: str,
        _env: None,
    ) -> None:
        """An org-scope candidate is stored in GraphitiStore (real FalkorDB)."""
        if not falkordb_reachable():
            pytest.skip("FalkorDB not reachable — skipping Graphiti consolidation test.")

        from graphiti_core import Graphiti
        from graphiti_core.embedder import OpenAIEmbedder
        from graphiti_core.embedder.openai import OpenAIEmbedderConfig
        from graphiti_core.llm_client import LLMConfig, OpenAIClient

        from memento.config import get_settings
        from memento.jobs.consolidation import run_consolidation
        from memento.stores.base import SearchFilters
        from memento.stores.graphiti_store import GraphitiStore

        settings = get_settings()
        api_key = settings.llm_api_key.get_secret_value()
        host = os.environ.get("MEMENTO_FALKORDB_HOST", "localhost")
        port = int(os.environ.get("MEMENTO_FALKORDB_PORT", "6379"))

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
        graphiti_client = Graphiti(
            uri=f"bolt://{host}:{port}",
            user="",
            password="",
            llm_client=llm,
            embedder=embedder,
        )
        graphiti_store = GraphitiStore(graphiti_client)
        await graphiti_store.initialize()

        try:
            session_log = await _create_ended_session(
                session_store,
                project_id=unique_project_id,
                agent_id=unique_agent_id,
                observations=[
                    "Structured logging with correlation IDs improved trace analysis.",
                    "Rolled out correlation ID headers to all services.",
                ],
            )

            result = await run_consolidation(
                session_log,
                mem0_store=mem0_store,
                graphiti_store=graphiti_store,
                session_store=session_store,
                http_client=org_llm_client,
            )

            assert result.promoted >= 1, (
                f"Expected >= 1 promoted org memory; got {result.promoted}. "
                f"Errors: {result.errors}"
            )
            assert not result.errors, f"Consolidation errors: {result.errors}"

            # Verify org memory is searchable in Graphiti
            search = await graphiti_store.search(
                "structured logging correlation IDs",
                SearchFilters(limit=10),
            )
            contents = [r.memory.content for r in search]
            assert any(
                "log" in c.lower() or "correlation" in c.lower() for c in contents
            ), f"Expected org memory about logging; found: {contents}"
        finally:
            await graphiti_store.close()

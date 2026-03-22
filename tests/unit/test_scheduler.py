"""Unit tests for memento.scheduler.

Covers
------
* Cron interval parsing (``*/N`` extraction, edge cases)
* ENDED sessions are picked up and processed by the scheduler loop
* Errors in consolidation for one session do not crash the loop
* Clean shutdown via event
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from memento.jobs.consolidation import ConsolidationResult
from memento.memory.schema import (
    Observation,
    SessionLog,
    SessionStatus,
)
from memento.scheduler import (
    parse_interval_minutes,
    process_ended_sessions,
    scheduler_loop,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session(
    session_id: str = "sess-1",
    status: SessionStatus = SessionStatus.ENDED,
) -> SessionLog:
    return SessionLog(
        session_id=session_id,
        project_id="proj-1",
        agent_id="agent-1",
        task_description="Fix bug",
        started_at=datetime.now(UTC),
        status=status,
        observations=[
            Observation(
                timestamp=datetime.now(UTC),
                content="Found issue in auth module",
            )
        ],
    )


_DEFAULT_ENV = {
    "MEMENTO_LLM_API_KEY": "test-key-not-real",
    "MEMENTO_LLM_BASE_URL": "http://localhost:11434",
    "MEMENTO_LLM_MODEL": "test-model",
    "MEMENTO_CONFIDENCE_THRESHOLD": "0.6",
}


# ---------------------------------------------------------------------------
# Cron parsing
# ---------------------------------------------------------------------------


class TestParseIntervalMinutes:
    """Test the Phase 0 cron string parser."""

    def test_standard_step(self) -> None:
        assert parse_interval_minutes("*/30 * * * *") == 30

    def test_every_5_minutes(self) -> None:
        assert parse_interval_minutes("*/5 * * * *") == 5

    def test_every_minute_star(self) -> None:
        assert parse_interval_minutes("* * * * *") == 1

    def test_fallback_fixed_minute(self) -> None:
        # "15 * * * *" = at minute 15 → fallback to 30
        assert parse_interval_minutes("15 * * * *") == 30

    def test_empty_string_fallback(self) -> None:
        assert parse_interval_minutes("") == 30

    def test_minimum_is_one(self) -> None:
        assert parse_interval_minutes("*/0 * * * *") == 1


# ---------------------------------------------------------------------------
# process_ended_sessions
# ---------------------------------------------------------------------------


class TestProcessEndedSessions:
    """Verify the session discovery + consolidation dispatch."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _DEFAULT_ENV.items():
            monkeypatch.setenv(k, v)

    async def test_ended_sessions_are_processed(self) -> None:
        """Sessions with ENDED status are picked up and consolidated."""
        session_store = AsyncMock()
        session_store.list_sessions = AsyncMock(
            return_value=[_session("s1"), _session("s2")]
        )

        mock_result = ConsolidationResult(
            batch_id="b1", session_id="s1", promoted=2
        )

        with patch(
            "memento.scheduler.run_consolidation",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_consolidate:
            mem0 = AsyncMock()
            graphiti = AsyncMock()

            total = await process_ended_sessions(
                session_store, mem0, graphiti
            )

        assert mock_consolidate.call_count == 2
        assert total == 4  # 2 promoted × 2 sessions

    async def test_error_in_one_session_continues_to_next(
        self,
    ) -> None:
        """If consolidation fails for one session, the next is tried."""
        session_store = AsyncMock()
        session_store.list_sessions = AsyncMock(
            return_value=[_session("s1"), _session("s2")]
        )

        call_count = 0

        async def _side_effect(
            *args: object, **kwargs: object
        ) -> ConsolidationResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("LLM unreachable")
            return ConsolidationResult(
                batch_id="b2", session_id="s2", promoted=1
            )

        with patch(
            "memento.scheduler.run_consolidation",
            new_callable=AsyncMock,
            side_effect=_side_effect,
        ):
            mem0 = AsyncMock()
            graphiti = AsyncMock()

            total = await process_ended_sessions(
                session_store, mem0, graphiti
            )

        assert call_count == 2  # Both sessions attempted
        assert total == 1  # Only s2 contributed

    async def test_no_ended_sessions(self) -> None:
        """When there are no ENDED sessions, nothing happens."""
        session_store = AsyncMock()
        session_store.list_sessions = AsyncMock(return_value=[])

        with patch(
            "memento.scheduler.run_consolidation",
            new_callable=AsyncMock,
        ) as mock_consolidate:
            total = await process_ended_sessions(
                session_store, AsyncMock(), AsyncMock()
            )

        mock_consolidate.assert_not_called()
        assert total == 0


# ---------------------------------------------------------------------------
# scheduler_loop
# ---------------------------------------------------------------------------


class TestSchedulerLoop:
    """Verify the main event loop behaviour."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in _DEFAULT_ENV.items():
            monkeypatch.setenv(k, v)

    async def test_loop_processes_then_stops(self) -> None:
        """The loop processes sessions, then exits on shutdown event."""
        session_store = AsyncMock()
        session_store.list_sessions = AsyncMock(
            return_value=[_session()]
        )

        mock_result = ConsolidationResult(
            batch_id="b1", session_id="s1", promoted=1
        )

        shutdown = asyncio.Event()

        tick_count = 0

        async def _mock_consolidate(
            *args: object, **kwargs: object
        ) -> ConsolidationResult:
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 2:
                shutdown.set()
            return mock_result

        with patch(
            "memento.scheduler.run_consolidation",
            new_callable=AsyncMock,
            side_effect=_mock_consolidate,
        ):
            await scheduler_loop(
                session_store,
                AsyncMock(),
                AsyncMock(),
                interval_seconds=0.01,
                shutdown_event=shutdown,
            )

        assert tick_count >= 2

    async def test_loop_survives_tick_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An error in one tick doesn't kill the loop."""
        session_store = AsyncMock()
        session_store.list_sessions = AsyncMock(
            side_effect=RuntimeError("DB gone")
        )

        shutdown = asyncio.Event()
        tick_count = 0

        async def _counting_process(
            *args: object, **kwargs: object
        ) -> int:
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 2:
                shutdown.set()
            raise RuntimeError("DB gone")

        with patch(
            "memento.scheduler.process_ended_sessions",
            side_effect=_counting_process,
        ):
            with caplog.at_level(logging.ERROR):
                await scheduler_loop(
                    session_store,
                    AsyncMock(),
                    AsyncMock(),
                    interval_seconds=0.01,
                    shutdown_event=shutdown,
                )

        assert tick_count >= 2
        assert "Unhandled error" in caplog.text

"""Unit tests for memento.stores.session_store.

Covers
------
* Session lifecycle (create → append → end → get → list)
* Invalid-append guard (non-ACTIVE session)
* Timeout expiry (ACTIVE sessions past the threshold → TIMED_OUT)
* Metadata immutability (agent_id / project_id / task_description are read-only)
* Concurrent access (multiple coroutines writing simultaneously)
* Settings integration (data_dir drives the default db path)
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memento.memory.schema import Observation, SessionLog, SessionStatus
from memento.stores.session_store import (
    SessionNotActiveError,
    SessionNotFoundError,
    SessionStore,
)

# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------


def _obs(content: str = "test observation", **kwargs: object) -> Observation:
    """Build a minimal valid Observation."""
    return Observation(timestamp=datetime.now(UTC), content=content, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[SessionStore, None]:
    """Open a fresh in-memory-style SessionStore backed by a temp-dir file."""
    async with SessionStore(db_path=tmp_path / "sessions.db", session_timeout=3600) as s:
        yield s


@pytest.fixture
async def active_session(store: SessionStore) -> SessionLog:
    """Create one ACTIVE session and return it."""
    return await store.create_session(
        agent_id="agent-001",
        project_id="proj-001",
        task_description="Write a failing test",
    )


# ---------------------------------------------------------------------------
# 1. Session lifecycle
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    """Full create → append → end → get → list round-trip."""

    async def test_create_returns_active_session_log(
        self, store: SessionStore
    ) -> None:
        log = await store.create_session(
            agent_id="agent-1",
            project_id="proj-x",
            task_description="Do something",
        )
        assert isinstance(log, SessionLog)
        assert log.status is SessionStatus.ACTIVE
        assert log.agent_id == "agent-1"
        assert log.project_id == "proj-x"
        assert log.task_description == "Do something"
        assert log.ended_at is None
        assert log.observations == []

    async def test_create_assigns_uuid_session_id(self, store: SessionStore) -> None:
        log1 = await store.create_session(
            agent_id="a", project_id="p", task_description="t"
        )
        log2 = await store.create_session(
            agent_id="a", project_id="p", task_description="t"
        )
        assert log1.session_id != log2.session_id

    async def test_append_observation_persists(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        obs = _obs("Something happened", tags=["tag1"])
        await store.append_observation(active_session.session_id, obs)

        loaded = await store.get_session(active_session.session_id)
        assert loaded is not None
        assert len(loaded.observations) == 1
        assert loaded.observations[0].content == "Something happened"
        assert loaded.observations[0].tags == ["tag1"]

    async def test_append_multiple_observations_preserves_order(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        contents = ["first", "second", "third"]
        for c in contents:
            await store.append_observation(active_session.session_id, _obs(c))

        loaded = await store.get_session(active_session.session_id)
        assert loaded is not None
        assert [o.content for o in loaded.observations] == contents

    async def test_observation_context_roundtrip(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        obs = _obs("context test", context={"key": "value", "num": 42})
        await store.append_observation(active_session.session_id, obs)

        loaded = await store.get_session(active_session.session_id)
        assert loaded is not None
        assert loaded.observations[0].context == {"key": "value", "num": 42}

    async def test_observation_null_context_roundtrip(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        await store.append_observation(active_session.session_id, _obs("no context"))
        loaded = await store.get_session(active_session.session_id)
        assert loaded is not None
        assert loaded.observations[0].context is None

    async def test_end_session_sets_ended_status(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        ended = await store.end_session(active_session.session_id)
        assert ended.status is SessionStatus.ENDED
        assert ended.ended_at is not None

    async def test_end_session_preserves_observations(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        await store.append_observation(active_session.session_id, _obs("pre-end"))
        ended = await store.end_session(active_session.session_id)
        assert len(ended.observations) == 1
        assert ended.observations[0].content == "pre-end"

    async def test_get_session_unknown_id_returns_none(
        self, store: SessionStore
    ) -> None:
        result = await store.get_session("no-such-id")
        assert result is None

    async def test_list_sessions_empty(self, store: SessionStore) -> None:
        assert await store.list_sessions() == []

    async def test_list_sessions_returns_all(self, store: SessionStore) -> None:
        await store.create_session(agent_id="a", project_id="p1", task_description="t")
        await store.create_session(agent_id="b", project_id="p2", task_description="t")
        sessions = await store.list_sessions()
        assert len(sessions) == 2

    async def test_list_sessions_filter_by_project(
        self, store: SessionStore
    ) -> None:
        await store.create_session(agent_id="a", project_id="proj-A", task_description="t")
        await store.create_session(agent_id="b", project_id="proj-B", task_description="t")
        result = await store.list_sessions(project_id="proj-A")
        assert len(result) == 1
        assert result[0].project_id == "proj-A"

    async def test_list_sessions_filter_by_status(
        self, store: SessionStore
    ) -> None:
        log = await store.create_session(
            agent_id="a", project_id="p", task_description="t"
        )
        await store.create_session(agent_id="b", project_id="p", task_description="t")
        await store.end_session(log.session_id)

        active = await store.list_sessions(status=SessionStatus.ACTIVE)
        ended = await store.list_sessions(status=SessionStatus.ENDED)
        assert len(active) == 1
        assert len(ended) == 1

    async def test_list_sessions_filter_by_project_and_status(
        self, store: SessionStore
    ) -> None:
        log = await store.create_session(
            agent_id="a", project_id="proj-A", task_description="t"
        )
        await store.create_session(
            agent_id="b", project_id="proj-B", task_description="t"
        )
        await store.end_session(log.session_id)

        result = await store.list_sessions(project_id="proj-A", status=SessionStatus.ENDED)
        assert len(result) == 1
        assert result[0].session_id == log.session_id

    async def test_started_at_is_utc_aware(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        loaded = await store.get_session(active_session.session_id)
        assert loaded is not None
        assert loaded.started_at.tzinfo is not None


# ---------------------------------------------------------------------------
# 2. Invalid-append guard
# ---------------------------------------------------------------------------


class TestInvalidAppend:
    """Appending observations to non-ACTIVE sessions must raise."""

    async def test_append_to_ended_session_raises(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        await store.end_session(active_session.session_id)
        with pytest.raises(SessionNotActiveError):
            await store.append_observation(active_session.session_id, _obs())

    async def test_append_to_timed_out_session_raises(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        # Force the session into TIMED_OUT via expire (timeout=0 → immediate)
        store._session_timeout = 0
        await store.expire_timed_out_sessions()

        with pytest.raises(SessionNotActiveError):
            await store.append_observation(active_session.session_id, _obs())

    async def test_append_to_unknown_session_raises(
        self, store: SessionStore
    ) -> None:
        with pytest.raises(SessionNotFoundError):
            await store.append_observation("ghost-session-id", _obs())

    async def test_end_already_ended_session_raises(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        await store.end_session(active_session.session_id)
        with pytest.raises(SessionNotActiveError):
            await store.end_session(active_session.session_id)

    async def test_end_unknown_session_raises(self, store: SessionStore) -> None:
        with pytest.raises(SessionNotFoundError):
            await store.end_session("ghost-session-id")


# ---------------------------------------------------------------------------
# 3. Timeout expiry
# ---------------------------------------------------------------------------


class TestTimeoutExpiry:
    """expire_timed_out_sessions correctly moves old sessions to TIMED_OUT."""

    async def test_no_sessions_expire_when_within_timeout(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        store._session_timeout = 9999  # very long
        expired = await store.expire_timed_out_sessions()
        assert expired == []

        loaded = await store.get_session(active_session.session_id)
        assert loaded is not None
        assert loaded.status is SessionStatus.ACTIVE

    async def test_session_expires_when_timeout_exceeded(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        store._session_timeout = 0  # everything is expired
        expired = await store.expire_timed_out_sessions()
        assert active_session.session_id in expired

        loaded = await store.get_session(active_session.session_id)
        assert loaded is not None
        assert loaded.status is SessionStatus.TIMED_OUT
        assert loaded.ended_at is not None

    async def test_only_active_sessions_are_considered(
        self, store: SessionStore
    ) -> None:
        """Already-ended sessions must not appear in the expired list."""
        log = await store.create_session(
            agent_id="a", project_id="p", task_description="t"
        )
        await store.end_session(log.session_id)
        store._session_timeout = 0
        expired = await store.expire_timed_out_sessions()
        assert log.session_id not in expired

    async def test_expiry_idempotent_on_second_call(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        store._session_timeout = 0
        expired1 = await store.expire_timed_out_sessions()
        expired2 = await store.expire_timed_out_sessions()
        assert active_session.session_id in expired1
        assert active_session.session_id not in expired2  # already TIMED_OUT

    async def test_ended_at_set_on_expiry(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        store._session_timeout = 0
        await store.expire_timed_out_sessions()
        loaded = await store.get_session(active_session.session_id)
        assert loaded is not None
        assert loaded.ended_at is not None
        assert loaded.ended_at.tzinfo is not None

    async def test_multiple_sessions_all_expired_at_once(
        self, store: SessionStore
    ) -> None:
        logs = [
            await store.create_session(agent_id="a", project_id="p", task_description="t")
            for _ in range(5)
        ]
        store._session_timeout = 0
        expired = await store.expire_timed_out_sessions()
        assert set(expired) == {log.session_id for log in logs}


# ---------------------------------------------------------------------------
# 4. Metadata immutability
# ---------------------------------------------------------------------------


class TestMetadataImmutability:
    """agent_id, project_id, task_description cannot be changed post-creation.

    Since SessionStore deliberately provides no 'update_metadata' method,
    immutability is enforced by the public API contract.  These tests verify
    that the stored values are unchanged after end_session and expiry.
    """

    async def test_metadata_preserved_after_end_session(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        ended = await store.end_session(active_session.session_id)
        assert ended.agent_id == active_session.agent_id
        assert ended.project_id == active_session.project_id
        assert ended.task_description == active_session.task_description

    async def test_metadata_preserved_after_expiry(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        store._session_timeout = 0
        await store.expire_timed_out_sessions()
        loaded = await store.get_session(active_session.session_id)
        assert loaded is not None
        assert loaded.agent_id == active_session.agent_id
        assert loaded.project_id == active_session.project_id
        assert loaded.task_description == active_session.task_description

    async def test_no_update_metadata_method_exists(self) -> None:
        """The SessionStore API must not expose any metadata-update method."""
        update_names = [
            name
            for name in dir(SessionStore)
            if "update" in name.lower() and not name.startswith("_")
        ]
        assert update_names == [], (
            f"Unexpected update methods on SessionStore: {update_names}"
        )

    async def test_observations_cannot_be_deleted_via_api(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        """There is no delete_observation method on the public API."""
        delete_names = [
            name
            for name in dir(SessionStore)
            if "delete" in name.lower() and not name.startswith("_")
        ]
        assert delete_names == [], (
            f"Unexpected delete methods on SessionStore: {delete_names}"
        )


# ---------------------------------------------------------------------------
# 5. Concurrent access
# ---------------------------------------------------------------------------


class TestConcurrentAccess:
    """Multiple coroutines may use the same store concurrently."""

    async def test_concurrent_session_creation(self, store: SessionStore) -> None:
        """Creating many sessions concurrently produces unique session IDs."""
        tasks = [
            store.create_session(
                agent_id=f"agent-{i}",
                project_id="proj-concurrent",
                task_description=f"task-{i}",
            )
            for i in range(20)
        ]
        logs = await asyncio.gather(*tasks)
        ids = {log.session_id for log in logs}
        assert len(ids) == 20, "All session IDs must be unique"

    async def test_concurrent_observation_append(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        """Concurrent appends to the same session all persist."""
        tasks = [
            store.append_observation(active_session.session_id, _obs(f"obs-{i}"))
            for i in range(10)
        ]
        await asyncio.gather(*tasks)
        loaded = await store.get_session(active_session.session_id)
        assert loaded is not None
        assert len(loaded.observations) == 10

    async def test_concurrent_reads_while_writing(
        self, store: SessionStore, active_session: SessionLog
    ) -> None:
        """Reading while writing does not raise errors (WAL mode)."""

        async def writer() -> None:
            for i in range(5):
                await store.append_observation(
                    active_session.session_id, _obs(f"write-{i}")
                )

        async def reader() -> SessionLog | None:
            return await store.get_session(active_session.session_id)

        write_task = asyncio.create_task(writer())
        read_results = await asyncio.gather(
            reader(), reader(), reader(), reader(), reader()
        )
        await write_task
        # All reads should complete without error
        for result in read_results:
            assert result is None or isinstance(result, SessionLog)

    async def test_concurrent_list_and_create(self, store: SessionStore) -> None:
        """list_sessions and create_session can interleave without error."""

        async def create() -> None:
            for i in range(5):
                await store.create_session(
                    agent_id="a", project_id="p", task_description=f"t{i}"
                )

        async def list_all() -> list[SessionLog]:
            return await store.list_sessions()

        results = await asyncio.gather(
            create(),
            list_all(),
            list_all(),
        )
        # Second and third results are list[SessionLog]; no exception must be raised
        for item in results[1:]:
            assert isinstance(item, list)


# ---------------------------------------------------------------------------
# 6. Settings integration
# ---------------------------------------------------------------------------


class TestSettingsIntegration:
    """Verify Settings.data_dir drives the default db path."""

    def test_data_dir_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default data_dir should be /data."""
        monkeypatch.setenv("MEMENTO_LLM_API_KEY", "test-key")
        from memento.config import Settings, get_settings

        get_settings.cache_clear()
        settings = Settings()  # type: ignore[call-arg]
        assert settings.data_dir == Path("/data")
        get_settings.cache_clear()

    def test_data_dir_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MEMENTO_DATA_DIR env var overrides the default."""
        monkeypatch.setenv("MEMENTO_LLM_API_KEY", "test-key")
        monkeypatch.setenv("MEMENTO_DATA_DIR", "/tmp/memento-test")
        from memento.config import Settings, get_settings

        get_settings.cache_clear()
        settings = Settings()  # type: ignore[call-arg]
        assert settings.data_dir == Path("/tmp/memento-test")
        get_settings.cache_clear()

    async def test_store_creates_db_under_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When db_path is omitted, the store uses settings.data_dir."""
        monkeypatch.setenv("MEMENTO_LLM_API_KEY", "test-key")
        monkeypatch.setenv("MEMENTO_DATA_DIR", str(tmp_path / "data"))
        from memento.config import get_settings

        get_settings.cache_clear()

        async with SessionStore() as store:
            log = await store.create_session(
                agent_id="a", project_id="p", task_description="t"
            )
            assert (tmp_path / "data" / "sessions.db").exists()
            result = await store.get_session(log.session_id)
            assert result is not None

        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 7. Context-manager and open/close guard
# ---------------------------------------------------------------------------


class TestStoreGuards:
    async def test_operation_before_open_raises(self, tmp_path: Path) -> None:
        store = SessionStore(db_path=tmp_path / "sessions.db")
        with pytest.raises(RuntimeError, match="not open"):
            await store.create_session(agent_id="a", project_id="p", task_description="t")

    async def test_close_without_open_is_safe(self, tmp_path: Path) -> None:
        store = SessionStore(db_path=tmp_path / "sessions.db")
        await store.close()  # must not raise

    async def test_context_manager_closes_connection(
        self, tmp_path: Path
    ) -> None:
        async with SessionStore(db_path=tmp_path / "sessions.db") as store:
            await store.create_session(agent_id="a", project_id="p", task_description="t")
        # After context manager exits, _conn should be None
        assert store._conn is None

    async def test_expiry_task_lifecycle(self, tmp_path: Path) -> None:
        store = SessionStore(
            db_path=tmp_path / "sessions.db",
            session_timeout=9999,
            expiry_interval=9999.0,
        )
        await store.open()
        try:
            await store.start_expiry_task()
            assert store._expiry_task is not None
            assert not store._expiry_task.done()
            # Starting again is a no-op
            await store.start_expiry_task()
        finally:
            await store.close()
        # Task must be cancelled after close
        assert store._expiry_task is None

    async def test_expiry_task_propagates_non_db_errors(
        self, tmp_path: Path
    ) -> None:
        """Non-sqlite3.Error exceptions must NOT be swallowed by the expiry loop.

        If the store connection is closed while the task is running, aiosqlite
        raises either RuntimeError (if _conn is None) or ValueError (if _conn
        exists but its underlying thread has stopped).  Either way the task
        should fail visibly rather than looping silently.
        """
        store = SessionStore(
            db_path=tmp_path / "sessions.db",
            session_timeout=0,
            expiry_interval=0.01,  # fire almost immediately
        )
        await store.open()
        await store.create_session(agent_id="a", project_id="p", task_description="t")
        await store.start_expiry_task()
        assert store._expiry_task is not None

        # Force the connection closed so the next expiry attempt hits an error
        # that is NOT a sqlite3.Error (aiosqlite raises ValueError here).
        conn = store._conn
        assert conn is not None
        await conn.close()
        store._conn = None

        # Give the loop time to fire and fail
        await asyncio.sleep(0.15)

        task = store._expiry_task
        assert task is not None
        assert task.done(), "Task should have terminated after a non-DB error"
        exc = task.exception()
        # The exception must have propagated — it must NOT be a sqlite3.Error
        assert exc is not None
        assert not isinstance(exc, sqlite3.Error), (
            f"sqlite3.Error should be caught and retried, not propagated; got {exc!r}"
        )

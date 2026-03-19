"""SQLite-backed async SessionStore for Memento.

Manages :class:`~memento.memory.schema.SessionLog` objects in a persistent
SQLite database located at ``MEMENTO_DATA_DIR/sessions.db``.

Design notes
------------
* Uses :mod:`aiosqlite` for non-blocking database access inside an asyncio
  event loop.  Every public method is ``async``.
* WAL journal mode is enabled on first open so that concurrent readers never
  block a writer and vice-versa.
* Observations are append-only: the store never issues UPDATE/DELETE on the
  ``observations`` table.
* Session metadata (``agent_id``, ``project_id``, ``task_description``) is
  written once at creation and never exposed via an update path.
* An optional background task polls active sessions and sets their status to
  ``TIMED_OUT`` once they exceed ``MEMENTO_SESSION_TIMEOUT`` seconds.

Usage example::

    async with SessionStore(db_path=Path("/data/sessions.db")) as store:
        log = await store.create_session(
            agent_id="agent-1",
            project_id="proj-abc",
            task_description="Fix flaky test",
        )
        await store.append_observation(log.session_id, obs)
        await store.end_session(log.session_id)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from memento.memory.schema import Observation, SessionLog, SessionStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    agent_id         TEXT NOT NULL,
    project_id       TEXT NOT NULL,
    task_description TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    status           TEXT NOT NULL DEFAULT 'ACTIVE'
)
"""

_DDL_OBSERVATIONS = """
CREATE TABLE IF NOT EXISTS observations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    timestamp  TEXT NOT NULL,
    content    TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '[]',
    context    TEXT
)
"""

_DDL_OBS_IDX = """
CREATE INDEX IF NOT EXISTS obs_session_idx ON observations (session_id)
"""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SessionNotFoundError(KeyError):
    """Raised when a requested session_id does not exist in the store."""


class SessionNotActiveError(ValueError):
    """Raised when an operation requires an ACTIVE session but it is not."""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class SessionStore:
    """Async SQLite-backed store for :class:`~memento.memory.schema.SessionLog`.

    Parameters
    ----------
    db_path:
        Path to the SQLite file.  When *None* the path is derived from
        ``settings.data_dir / "sessions.db"``; this lazy lookup is deferred
        until :meth:`open` is called so that tests can patch the environment
        before instantiation.
    session_timeout:
        Number of seconds after which an ACTIVE session is considered timed
        out.  When *None* the value is read from
        ``settings.session_timeout`` each time :meth:`expire_timed_out_sessions`
        is called.
    expiry_interval:
        How often (seconds) the background expiry task polls the database.
        Default is 60 s.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        session_timeout: int | None = None,
        *,
        expiry_interval: float = 60.0,
    ) -> None:
        self._db_path_override = db_path
        self._session_timeout = session_timeout
        self._expiry_interval = expiry_interval
        self._conn: aiosqlite.Connection | None = None
        self._expiry_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Open (or create) the database and initialise schema + WAL mode."""
        db_path = self._resolve_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(db_path)
        self._conn.row_factory = aiosqlite.Row  # row["column"] access
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute(_DDL_SESSIONS)
        await self._conn.execute(_DDL_OBSERVATIONS)
        await self._conn.execute(_DDL_OBS_IDX)
        await self._conn.commit()

    async def close(self) -> None:
        """Stop the background task and close the database connection."""
        await self.stop_expiry_task()
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> SessionStore:
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Public CRUD API
    # ------------------------------------------------------------------

    async def create_session(
        self,
        *,
        agent_id: str,
        project_id: str,
        task_description: str,
    ) -> SessionLog:
        """Create and persist a new ACTIVE session.

        Returns
        -------
        SessionLog
            The newly created session with status ``ACTIVE``.
        """
        conn = self._require_conn()
        session_id = str(uuid.uuid4())
        started_at = datetime.now(UTC)
        await conn.execute(
            """
            INSERT INTO sessions
                (session_id, agent_id, project_id, task_description, started_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                agent_id,
                project_id,
                task_description,
                started_at.isoformat(),
                str(SessionStatus.ACTIVE),
            ),
        )
        await conn.commit()
        return SessionLog(
            session_id=session_id,
            agent_id=agent_id,
            project_id=project_id,
            task_description=task_description,
            started_at=started_at,
            status=SessionStatus.ACTIVE,
        )

    async def append_observation(
        self,
        session_id: str,
        observation: Observation,
    ) -> None:
        """Append *observation* to the given session.

        Observations are append-only: this method never modifies or removes
        existing observations.

        Raises
        ------
        SessionNotFoundError
            If *session_id* does not exist.
        SessionNotActiveError
            If the session is not currently ACTIVE.
        """
        conn = self._require_conn()
        await self._assert_active(conn, session_id)
        await conn.execute(
            """
            INSERT INTO observations (session_id, timestamp, content, tags, context)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_id,
                observation.timestamp.isoformat(),
                observation.content,
                json.dumps(observation.tags),
                json.dumps(observation.context) if observation.context is not None else None,
            ),
        )
        await conn.commit()

    async def end_session(self, session_id: str) -> SessionLog:
        """Transition an ACTIVE session to ENDED.

        Raises
        ------
        SessionNotFoundError
            If *session_id* does not exist.
        SessionNotActiveError
            If the session is not currently ACTIVE.

        Returns
        -------
        SessionLog
            The updated session with status ``ENDED`` and ``ended_at`` set.
        """
        conn = self._require_conn()
        await self._assert_active(conn, session_id)
        ended_at = datetime.now(UTC)
        await conn.execute(
            "UPDATE sessions SET status = ?, ended_at = ? WHERE session_id = ?",
            (str(SessionStatus.ENDED), ended_at.isoformat(), session_id),
        )
        await conn.commit()
        result = await self.get_session(session_id)
        assert result is not None  # just ended it, can't be missing
        return result

    async def get_session(self, session_id: str) -> SessionLog | None:
        """Return the :class:`SessionLog` for *session_id*, or *None* if absent."""
        conn = self._require_conn()
        async with conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        observations = await self._load_observations(conn, session_id)
        return _row_to_session_log(row, observations)

    async def list_sessions(
        self,
        *,
        project_id: str | None = None,
        status: SessionStatus | None = None,
    ) -> list[SessionLog]:
        """Return all sessions, optionally filtered by *project_id* and/or *status*.

        Parameters
        ----------
        project_id:
            When set, only sessions for this project are returned.
        status:
            When set, only sessions with this status are returned.
        """
        conn = self._require_conn()
        query = "SELECT * FROM sessions WHERE 1=1"
        params: list[Any] = []
        if project_id is not None:
            query += " AND project_id = ?"
            params.append(project_id)
        if status is not None:
            query += " AND status = ?"
            params.append(str(status))
        async with conn.execute(query, params) as cur:
            rows = await cur.fetchall()
        result: list[SessionLog] = []
        for row in rows:
            obs = await self._load_observations(conn, row["session_id"])
            result.append(_row_to_session_log(row, obs))
        return result

    # ------------------------------------------------------------------
    # Timeout expiry
    # ------------------------------------------------------------------

    async def expire_timed_out_sessions(self) -> list[str]:
        """Expire ACTIVE sessions that have exceeded the configured timeout.

        Each expired session has its status set to ``TIMED_OUT`` and its
        ``ended_at`` timestamp recorded.

        The timeout value is taken from the *session_timeout* constructor
        argument, falling back to ``settings.session_timeout`` if that was
        not supplied.

        Returns
        -------
        list[str]
            Session IDs that were expired during this call.
        """
        conn = self._require_conn()
        timeout_secs = self._effective_timeout()
        now = datetime.now(UTC)

        async with conn.execute(
            "SELECT session_id, started_at FROM sessions WHERE status = ?",
            (str(SessionStatus.ACTIVE),),
        ) as cur:
            active_rows = await cur.fetchall()

        expired: list[str] = []
        for row in active_rows:
            started_at = datetime.fromisoformat(row["started_at"])
            elapsed = (now - started_at).total_seconds()
            if elapsed >= timeout_secs:
                expired.append(row["session_id"])

        if expired:
            placeholders = ",".join("?" * len(expired))
            await conn.execute(
                f"UPDATE sessions SET status = ?, ended_at = ?"
                f" WHERE session_id IN ({placeholders})",
                [str(SessionStatus.TIMED_OUT), now.isoformat(), *expired],
            )
            await conn.commit()
            for sid in expired:
                logger.info("Session %s set to TIMED_OUT", sid)

        return expired

    async def start_expiry_task(self) -> None:
        """Start a background asyncio task that periodically calls
        :meth:`expire_timed_out_sessions`.

        Calling this when the task is already running is a no-op.
        """
        if self._expiry_task is not None and not self._expiry_task.done():
            return

        async def _loop() -> None:
            while True:
                await asyncio.sleep(self._expiry_interval)
                try:
                    expired = await self.expire_timed_out_sessions()
                    if expired:
                        logger.debug("Expiry task timed out %d session(s)", len(expired))
                except sqlite3.Error:
                    # Transient database error — log and retry on next interval.
                    # Non-DB exceptions (e.g. RuntimeError, programming errors)
                    # are intentionally allowed to propagate so the task fails
                    # visibly rather than looping silently in a broken state.
                    logger.exception("Database error in session expiry task; will retry")

        self._expiry_task = asyncio.create_task(_loop(), name="session-expiry")

    async def stop_expiry_task(self) -> None:
        """Cancel and await the background expiry task if running."""
        if self._expiry_task is not None:
            self._expiry_task.cancel()
            try:
                await self._expiry_task
            except asyncio.CancelledError:
                pass
            self._expiry_task = None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_db_path(self) -> Path:
        if self._db_path_override is not None:
            return self._db_path_override
        # Deferred import to avoid forcing env vars at import time
        from memento.config import get_settings

        return get_settings().data_dir / "sessions.db"

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError(
                "SessionStore is not open. "
                "Call await store.open() or use 'async with SessionStore(...) as store'."
            )
        return self._conn

    def _effective_timeout(self) -> int:
        if self._session_timeout is not None:
            return self._session_timeout
        from memento.config import get_settings

        return get_settings().session_timeout

    @staticmethod
    async def _assert_active(conn: aiosqlite.Connection, session_id: str) -> None:
        async with conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise SessionNotFoundError(session_id)
        if row["status"] != str(SessionStatus.ACTIVE):
            raise SessionNotActiveError(
                f"Session {session_id!r} has status {row['status']!r}, expected ACTIVE"
            )

    @staticmethod
    async def _load_observations(
        conn: aiosqlite.Connection, session_id: str
    ) -> list[Observation]:
        async with conn.execute(
            "SELECT timestamp, content, tags, context"
            " FROM observations WHERE session_id = ? ORDER BY id",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        result: list[Observation] = []
        for row in rows:
            context: dict[str, Any] | None = (
                json.loads(row["context"]) if row["context"] is not None else None
            )
            result.append(
                Observation(
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    content=row["content"],
                    tags=json.loads(row["tags"]),
                    context=context,
                )
            )
        return result


# ---------------------------------------------------------------------------
# Module-level helper (pure function — no I/O)
# ---------------------------------------------------------------------------


def _row_to_session_log(
    row: aiosqlite.Row,
    observations: list[Observation],
) -> SessionLog:
    ended_at: datetime | None = (
        datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None
    )
    return SessionLog(
        session_id=row["session_id"],
        agent_id=row["agent_id"],
        project_id=row["project_id"],
        task_description=row["task_description"],
        started_at=datetime.fromisoformat(row["started_at"]),
        ended_at=ended_at,
        observations=observations,
        status=SessionStatus(row["status"]),
    )

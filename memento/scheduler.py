"""Scheduler entrypoint for consolidation cron jobs.

Runs as a standalone process: ``python -m memento.scheduler``

Reads ``MEMENTO_CONSOLIDATION_SCHEDULE`` (cron string, default
``*/30 * * * *``) and executes
:func:`~memento.jobs.consolidation.run_consolidation` on every session
with status ``ENDED``.

Design notes
------------
* Phase 0 keeps it simple: parse the ``*/N`` minute interval from
  the first field of the cron string and sleep that many minutes.
* Uses a single ``asyncio`` event loop; no APScheduler dependency.
* Responds to ``SIGINT`` / ``SIGTERM`` / ``KeyboardInterrupt`` for
  clean shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import re
import signal
import time
from types import FrameType

from memento.config import get_settings
from memento.jobs.consolidation import run_consolidation
from memento.memory.schema import SessionStatus
from memento.stores.base import MemoryStore
from memento.stores.session_store import SessionStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cron parsing (Phase 0 — minute-field only)
# ---------------------------------------------------------------------------

_STEP_RE = re.compile(r"^\*/(\d+)$")


def parse_interval_minutes(cron: str) -> int:
    """Extract the interval in minutes from a ``*/N * * * *`` cron string.

    Falls back to 30 minutes if the first field cannot be parsed.
    """
    stripped = cron.strip()
    if not stripped:
        return 30
    first_field = stripped.split()[0]
    match = _STEP_RE.match(first_field)
    if match:
        return max(1, int(match.group(1)))
    # Not a step expression — treat as "every minute" if *, else 30
    if first_field == "*":
        return 1
    return 30


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------


async def process_ended_sessions(
    session_store: SessionStore,
    mem0_store: MemoryStore,
    graphiti_store: MemoryStore,
) -> int:
    """Find ENDED sessions and consolidate each one.

    Returns the total number of memories promoted + stored as
    unverified.
    """
    sessions = await session_store.list_sessions(
        status=SessionStatus.ENDED
    )
    total_memories = 0

    for session_log in sessions:
        start = time.monotonic()
        try:
            result = await run_consolidation(
                session_log,
                mem0_store,
                graphiti_store,
                session_store,
            )
            elapsed = time.monotonic() - start
            memories = result.promoted + result.unverified
            total_memories += memories
            logger.info(
                "Consolidated session %s in %.1fs: "
                "%d memories (%d promoted, %d unverified, "
                "%d duplicates)",
                session_log.session_id,
                elapsed,
                memories,
                result.promoted,
                result.unverified,
                result.duplicates,
            )
        except Exception:
            elapsed = time.monotonic() - start
            logger.exception(
                "Consolidation failed for session %s after %.1fs",
                session_log.session_id,
                elapsed,
            )
            # Continue to next session — do not crash the loop

    return total_memories


async def scheduler_loop(
    session_store: SessionStore,
    mem0_store: MemoryStore,
    graphiti_store: MemoryStore,
    *,
    interval_seconds: float,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Run the consolidation loop until *shutdown_event* is set.

    Parameters
    ----------
    session_store:
        SQLite-backed session store.
    mem0_store:
        Project-scoped memory backend.
    graphiti_store:
        Org-scoped memory backend.
    interval_seconds:
        How many seconds to sleep between runs.
    shutdown_event:
        An :class:`asyncio.Event` that, when set, causes the loop to
        exit cleanly.  If *None*, a new event is created and attached
        to SIGINT/SIGTERM handlers.
    """
    stop = shutdown_event or asyncio.Event()

    logger.info(
        "Scheduler loop started (interval=%.0fs)", interval_seconds
    )
    while not stop.is_set():
        try:
            total = await process_ended_sessions(
                session_store, mem0_store, graphiti_store
            )
            if total:
                logger.info(
                    "Scheduler tick complete: %d memories extracted",
                    total,
                )
        except Exception:
            logger.exception("Unhandled error in scheduler tick")

        # Sleep interruptibly
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=interval_seconds
            )
        except TimeoutError:
            pass  # Normal: timeout means we loop again

    logger.info("Scheduler loop stopped.")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the Memento scheduler for consolidation jobs."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings = get_settings()
    interval_minutes = parse_interval_minutes(
        settings.consolidation_schedule
    )
    interval_seconds = interval_minutes * 60.0
    logger.info(
        "Memento scheduler starting (schedule=%s → %dm interval)",
        settings.consolidation_schedule,
        interval_minutes,
    )

    async def _run() -> None:
        stop = asyncio.Event()

        # Wire up OS signals for graceful shutdown
        loop = asyncio.get_running_loop()

        def _signal_handler(
            sig: int, _frame: FrameType | None
        ) -> None:
            logger.info("Received signal %s — shutting down", sig)
            loop.call_soon_threadsafe(stop.set)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _signal_handler)
            except (OSError, ValueError):
                pass  # Not available on this platform

        session_store = SessionStore()
        await session_store.open()
        try:
            # Phase 0: stores are created but not fully initialised
            # (they connect to their backends lazily).
            from memento.stores.graphiti_store import (
                GraphitiStore,
            )
            from memento.stores.mem0_store import Mem0Store

            mem0: MemoryStore = Mem0Store(settings)
            graphiti: MemoryStore = GraphitiStore(
                host=settings.falkordb_host,
                port=settings.falkordb_port,
            )

            await scheduler_loop(
                session_store,
                mem0,
                graphiti,
                interval_seconds=interval_seconds,
                shutdown_event=stop,
            )
        finally:
            try:
                if hasattr(graphiti, "close"):
                    await graphiti.close()
            finally:
                await session_store.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Scheduler interrupted by user.")


if __name__ == "__main__":
    main()

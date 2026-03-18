"""Scheduler entrypoint for consolidation and analytics cron jobs."""

import logging

logger = logging.getLogger(__name__)


def main() -> None:
    """Start the Memento scheduler for consolidation and analytics jobs."""
    logging.basicConfig(level=logging.INFO)
    logger.info("Memento scheduler starting...")
    # Scheduler implementation will be added in P0-T10.
    # For now, just log and exit cleanly.
    logger.info("Memento scheduler: no jobs configured yet. Exiting.")


if __name__ == "__main__":
    main()

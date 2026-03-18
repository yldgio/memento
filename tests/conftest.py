"""Shared test fixtures for Memento test suite."""

import os

import pytest

from memento.config import get_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests don't leak environment variables or cached settings.

    Removes all MEMENTO_* env vars and clears the settings singleton
    cache before each test so that tests start from a known clean state.
    """
    get_settings.cache_clear()
    for key in list(os.environ.keys()):
        if key.startswith("MEMENTO_"):
            monkeypatch.delenv(key, raising=False)

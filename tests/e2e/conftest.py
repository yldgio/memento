"""Shared fixtures for end-to-end tests.

These fixtures create real infrastructure (SQLite session store, in-memory
fake memory stores) and mock LLM HTTP clients so that the complete core loop
can be tested without external services.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from memento.config import get_settings
from memento.stores.session_store import SessionStore

# ---------------------------------------------------------------------------
# Capture real MEMENTO_* env vars at module-load time (before autouse
# _clean_env fixture strips them).  Used by the real-LLM test variant.
# ---------------------------------------------------------------------------
_PRE_TEST_MEMENTO_ENV: dict[str, str] = {
    k: v for k, v in os.environ.items() if k.startswith("MEMENTO_")
}


# ---------------------------------------------------------------------------
# Env setup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _e2e_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set deterministic MEMENTO_* env vars for every e2e test.

    This fixture runs *after* the root-level ``_clean_env`` fixture removes
    all MEMENTO_* variables, so it re-populates only what the e2e tests need.
    The LLM API key is a dummy value — the mocked-LLM path never sends a
    real HTTP request.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("MEMENTO_LLM_API_KEY", "test-key-e2e-not-real")
    monkeypatch.setenv("MEMENTO_LLM_BASE_URL", "http://test-llm-e2e")
    monkeypatch.setenv("MEMENTO_LLM_MODEL", "test-model")
    monkeypatch.setenv("MEMENTO_CONFIDENCE_THRESHOLD", "0.6")


# ---------------------------------------------------------------------------
# SessionStore fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_store(tmp_path: Path) -> AsyncGenerator[SessionStore, None]:
    """Real SQLite-backed :class:`SessionStore` in a pytest temp directory.

    The database is discarded after each test.  No expiry background task
    is started (``expiry_interval`` set very high).
    """
    store = SessionStore(
        db_path=tmp_path / "e2e-sessions.db",
        expiry_interval=86_400.0,  # 1 day — prevents noise in short tests
    )
    await store.open()
    try:
        yield store
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Mock LLM client helpers
# ---------------------------------------------------------------------------


def _build_llm_response(candidates: list[dict[str, Any]]) -> httpx.Response:
    """Build a fake OpenAI-compatible chat completions response."""
    body: dict[str, Any] = {
        "choices": [
            {"message": {"content": json.dumps(candidates)}}
        ]
    }
    return httpx.Response(
        status_code=200,
        json=body,
        request=httpx.Request("POST", "http://test-llm-e2e/v1/chat/completions"),
    )


@pytest.fixture
def mock_llm_client() -> AsyncMock:
    """Mock ``httpx.AsyncClient`` that returns a deterministic LLM response.

    The response contains a single project-scoped candidate about JWT token
    expiry with confidence 0.9 (above the 0.6 threshold → REVIEWED tier).
    """
    candidates: list[dict[str, Any]] = [
        {
            "content": "JWT tokens need 15-min expiry for this API",
            "confidence": 0.9,
            "scope": "project",
            "tags": ["security", "pattern"],
        }
    ]
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(return_value=_build_llm_response(candidates))
    return client


# ---------------------------------------------------------------------------
# Real-LLM env restore fixture (opt-in via MEMENTO_E2E_REAL_LLM=1)
# ---------------------------------------------------------------------------


@pytest.fixture
def real_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the pre-test MEMENTO_* env vars for real-LLM test variants.

    The root ``_clean_env`` fixture strips all MEMENTO_* variables before each
    test.  This fixture reads the values captured at module import time
    (before ``_clean_env`` ran) and re-installs them so that the real LLM
    path can use actual credentials from the developer's environment.
    """
    get_settings.cache_clear()
    for key, value in _PRE_TEST_MEMENTO_ENV.items():
        monkeypatch.setenv(key, value)

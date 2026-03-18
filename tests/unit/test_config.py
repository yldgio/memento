"""Unit tests for memento.config module."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memento.config import Settings, get_settings


class TestSettingsDefaults:
    """Verify all default values match TRD §10 specification."""

    def test_defaults_match_trd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All default values should match the TRD specification table."""
        monkeypatch.setenv("MEMENTO_LLM_API_KEY", "test-key")
        settings = Settings()  # type: ignore[call-arg]

        assert settings.llm_base_url == "https://api.openai.com/v1"
        assert settings.llm_model == "gpt-4o"
        assert settings.llm_api_key.get_secret_value() == "test-key"
        assert settings.falkordb_host == "localhost"
        assert settings.falkordb_port == 6379
        assert settings.confidence_threshold == 0.6
        assert settings.session_timeout == 3600
        assert settings.max_context_tokens == 4000
        assert settings.max_memories_per_query == 20
        assert settings.consolidation_schedule == "*/30 * * * *"
        assert settings.analytics_schedule == "0 2 * * 0"
        assert settings.api_port == 8080
        assert settings.mcp_port == 8081
        assert settings.log_level == "INFO"
        assert settings.api_key is None
        assert settings.mcp_token is None
        assert settings.org_promotion_min_sessions == 2


class TestSettingsRequired:
    """Verify required field enforcement."""

    def test_missing_llm_api_key_raises(self) -> None:
        """Missing MEMENTO_LLM_API_KEY must raise ValidationError."""
        # The _clean_env fixture removes all MEMENTO_* vars
        with pytest.raises(ValidationError) as exc_info:
            Settings()  # type: ignore[call-arg]

        errors = exc_info.value.errors()
        field_names = [e["loc"][0] for e in errors]
        assert "llm_api_key" in field_names


class TestSettingsEnvOverride:
    """Verify environment variable overrides work."""

    def test_env_override_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """String env vars should override defaults."""
        monkeypatch.setenv("MEMENTO_LLM_API_KEY", "sk-test-123")
        monkeypatch.setenv("MEMENTO_LLM_BASE_URL", "https://custom.api/v1")
        monkeypatch.setenv("MEMENTO_LLM_MODEL", "gpt-3.5-turbo")

        settings = Settings()  # type: ignore[call-arg]

        assert settings.llm_api_key.get_secret_value() == "sk-test-123"
        assert settings.llm_base_url == "https://custom.api/v1"
        assert settings.llm_model == "gpt-3.5-turbo"

    def test_env_override_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Integer env vars should be coerced from strings."""
        monkeypatch.setenv("MEMENTO_LLM_API_KEY", "test-key")
        monkeypatch.setenv("MEMENTO_FALKORDB_PORT", "7379")
        monkeypatch.setenv("MEMENTO_SESSION_TIMEOUT", "7200")
        monkeypatch.setenv("MEMENTO_API_PORT", "9090")

        settings = Settings()  # type: ignore[call-arg]

        assert settings.falkordb_port == 7379
        assert settings.session_timeout == 7200
        assert settings.api_port == 9090

    def test_env_override_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Float env vars should be coerced from strings."""
        monkeypatch.setenv("MEMENTO_LLM_API_KEY", "test-key")
        monkeypatch.setenv("MEMENTO_CONFIDENCE_THRESHOLD", "0.85")

        settings = Settings()  # type: ignore[call-arg]

        assert settings.confidence_threshold == 0.85

    def test_env_override_optional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Optional fields should accept values from env."""
        monkeypatch.setenv("MEMENTO_LLM_API_KEY", "test-key")
        monkeypatch.setenv("MEMENTO_API_KEY", "my-api-key")
        monkeypatch.setenv("MEMENTO_MCP_TOKEN", "my-mcp-token")

        settings = Settings()  # type: ignore[call-arg]

        assert settings.api_key.get_secret_value() == "my-api-key"
        assert settings.mcp_token.get_secret_value() == "my-mcp-token"

    def test_case_insensitive_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env var prefix matching should be case-insensitive."""
        monkeypatch.setenv("MEMENTO_LLM_API_KEY", "test-key")
        monkeypatch.setenv("MEMENTO_LOG_LEVEL", "DEBUG")

        settings = Settings()  # type: ignore[call-arg]

        assert settings.log_level == "DEBUG"


class TestGetSettingsSingleton:
    """Verify the get_settings() caching behaviour."""

    def test_get_settings_returns_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_settings() should return a Settings instance."""
        monkeypatch.setenv("MEMENTO_LLM_API_KEY", "test-key")
        # Clear the lru_cache before testing
        get_settings.cache_clear()

        settings = get_settings()

        assert isinstance(settings, Settings)
        assert settings.llm_api_key.get_secret_value() == "test-key"

    def test_get_settings_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Repeated calls should return the same object."""
        monkeypatch.setenv("MEMENTO_LLM_API_KEY", "test-key")
        get_settings.cache_clear()

        s1 = get_settings()
        s2 = get_settings()

        assert s1 is s2

    def test_cache_clear_allows_new_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clearing cache should allow loading new settings."""
        monkeypatch.setenv("MEMENTO_LLM_API_KEY", "key-1")
        get_settings.cache_clear()
        s1 = get_settings()

        monkeypatch.setenv("MEMENTO_LLM_API_KEY", "key-2")
        get_settings.cache_clear()
        s2 = get_settings()

        assert s1.llm_api_key.get_secret_value() == "key-1"
        assert s2.llm_api_key.get_secret_value() == "key-2"
        assert s1 is not s2

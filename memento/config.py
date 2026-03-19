"""Application configuration via environment variables.

Uses pydantic-settings to parse MEMENTO_* environment variables
into a typed Settings object. Access via ``get_settings()`` singleton.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Memento application settings.

    All fields are mapped to environment variables with the ``MEMENTO_`` prefix.
    For example, ``llm_api_key`` is read from ``MEMENTO_LLM_API_KEY``.
    """

    model_config = SettingsConfigDict(
        env_prefix="MEMENTO_",
        case_sensitive=False,
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # --- LLM Provider ---
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o"
    llm_api_key: SecretStr  # Required — no default, masked in logs

    # --- FalkorDB ---
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379

    # --- Memory Settings ---
    confidence_threshold: float = 0.6
    session_timeout: int = 3600
    max_context_tokens: int = 4000
    max_memories_per_query: int = 20

    # --- Scheduling ---
    consolidation_schedule: str = "*/30 * * * *"
    analytics_schedule: str = "0 2 * * 0"

    # --- Server Ports ---
    api_port: int = 8080
    mcp_port: int = 8081

    # --- Logging ---
    log_level: str = "INFO"

    # --- Authentication (optional) ---
    api_key: SecretStr | None = None
    mcp_token: SecretStr | None = None

    # --- Promotion ---
    org_promotion_min_sessions: int = 2

    # --- Data Storage ---
    data_dir: Path = Path("/data")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings singleton.

    Settings are read from environment variables on first call and
    cached for the lifetime of the process.
    """
    return Settings()  # type: ignore[call-arg]

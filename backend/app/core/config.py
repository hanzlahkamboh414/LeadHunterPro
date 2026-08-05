"""Application configuration using Pydantic Settings.

This module defines the application settings loaded from environment
variables and/or a ``.env`` file.  All settings are typed and validated
at startup.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Top-level application settings."""

    APP_NAME: str = "LeadHunter Pro AI"
    APP_VERSION: str = "0.1.0"

    DATABASE_URL: str

    API_PREFIX: str = "/api"

    LOG_LEVEL: str = "INFO"

    OLLAMA_URL: str = "http://localhost:11434"

    AI_PROVIDER: str = "openai"

    OPENAI_API_KEY: str = ""

    OPENAI_BASE_URL: str = "https://api.openai.com/v1"

    OPENAI_MODEL: str = "gpt-5.5"

    # Search Provider Configuration
    SEARXNG_URL: str = ""  # Self-hosted SearXNG instance URL
    BRAVE_SEARCH_API_KEY: str = ""  # Brave Search API key (optional)

    model_config = SettingsConfigDict(
        env_file=_ENV_PATH,
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()

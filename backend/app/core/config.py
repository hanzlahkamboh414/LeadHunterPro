"""Application configuration using Pydantic Settings.

This module defines the application settings loaded from environment
variables and/or a `.env` file. All settings are typed and validated
at startup.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"

# Every credential field below is declared with ``repr=False``. This is a real
# leak fix, not tidiness. Pydantic's generated ``__repr__`` prints EVERY field
# value, and pytest embeds that repr into an AttributeError message -- so on
# 2026-08-19 a single unrelated test failure splattered the live AI and Tavily
# keys across the terminal dozens of times, and from there into a pasted chat
# log. The leak path is any accidental repr of ``settings``: an exception, a
# logged object, a debugger frame. Excluding the field from the repr closes that
# whole class of accident at the source, while normal reads
# (``settings.AI_API_KEY``) keep working unchanged.
#
# Deliberately NOT using ``SecretStr``: it is stronger (masks even a direct
# print) but forces every read site to call ``.get_secret_value()``. Worth
# revisiting if a credential ever needs to survive being logged deliberately.
#
# Each field gets its OWN ``Field(...)`` call rather than a shared module-level
# instance: pydantic attaches per-field metadata to the FieldInfo object, so
# reusing one instance across fields is not safe.


class Settings(BaseSettings):
    """Top-level application settings."""

    APP_NAME: str = "LeadHunter Pro AI"
    APP_VERSION: str = "0.1.0"

    # Accuracy-first execution mode (CLAUDE.md §1):
    #   live — real sources only, fixtures COMPLETELY excluded
    #   demo — fixtures allowed and clearly labeled as bridge data
    #   test — fixtures allowed (automated tests / offline dev)
    # Default is live; a run may never silently fall back to fixtures.
    LEADHUNTER_ENV: Literal["live", "demo", "test"] = "live"

    DATABASE_URL: str

    API_PREFIX: str = "/api"

    LOG_LEVEL: str = "INFO"

    # ------------------------------------------------------------------
    # AI transport — vendor-neutral by design.
    #
    # These four settings are the ONLY place an AI vendor is named, and
    # backend/.env is the only file that changes to switch vendor. Nothing under
    # app/ mentions a vendor at all: RouterProvider speaks the OpenAI-compatible
    # protocol that every major vendor and router exposes, so OpenAI /
    # NaraRouter / OmniRoute / OpenRouter / Groq / Ollama are each reachable
    # through the same three values (CLAUDE.md sections 4 and 8 — providers are
    # replaceable infrastructure, never the architecture).
    #
    # AI_PROVIDER selects a registered provider class by name; "router" is the
    # generic OpenAI-compatible one. An unregistered name raises at startup
    # instead of degrading into a stub (CLAUDE.md section 12).
    # ------------------------------------------------------------------
    AI_PROVIDER: str = "router"

    # Defaults describe the transport proven live on 2026-08-19. The real key is
    # loaded at runtime from the gitignored backend/.env; never hardcode one.
    AI_BASE_URL: str = "https://router.bynara.id/v1"
    AI_MODEL: str = "agnes-2.0-flash"
    AI_API_KEY: str = Field(default="", repr=False)  # secret — see note above

    # Search Provider Configuration
    SEARXNG_URL: str = ""
    BRAVE_SEARCH_API_KEY: str = Field(default="", repr=False)  # secret
    TAVILY_SEARCH_API_KEY: str = Field(default="", repr=False)  # secret

    model_config = SettingsConfigDict(
        env_file=_ENV_PATH,
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
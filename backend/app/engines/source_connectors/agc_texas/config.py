"""AGC Texas Connector configuration.

Defines configuration constants for the Associated General Contractors
of Texas (AGC Texas) member directory connector.
"""

from __future__ import annotations

from app.engines.source_connectors.sdk import ConnectorConfig


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class AgcTexasConfig(ConnectorConfig):
    """Configuration for the AGC Texas connector."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(
            name="agc_texas",
            timeout=30,
            max_retries=3,
            user_agent="LeadHunterPro/1.0 (contact@leadhunterpro.ai)",
            rate_limit=0.5,  # Rate limit to 2 requests per second
            **kwargs,  # type: ignore[arg-type]
        )

        # AGC Texas specific settings
        self.base_url: str = "https://www.agctexas.org"
        self.membership_api: str = "/members/directory"
        self.search_endpoint: str = "/members/search"


# Default configuration instance
DEFAULT_CONFIG = AgcTexasConfig()

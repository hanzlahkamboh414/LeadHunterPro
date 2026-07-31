"""Leadership discovery service."""

import logging

from app.discovery.leadership_discovery import LeadershipDiscovery

logger = logging.getLogger(__name__)


class LeadershipService:
    """High-level leadership/personnel discovery orchestration."""

    def __init__(self) -> None:
        self._discovery_engine = LeadershipDiscovery()

    def discover(self, website: str) -> list[dict]:
        """Discover leadership contacts from a company website.

        Args:
            website: Base URL of the company.

        Returns:
            List of candidate person dictionaries.
        """
        return self._discovery_engine.discover(website)

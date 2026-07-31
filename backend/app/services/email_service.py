"""Email discovery service."""

import logging

from app.email.email_discovery import EmailDiscovery

logger = logging.getLogger(__name__)


class EmailService:
    """High-level email discovery orchestration."""

    def __init__(self) -> None:
        self._discovery = EmailDiscovery()

    def discover(self, website: str) -> dict:
        """Discover email addresses from a company website.

        Args:
            website: Base URL of the company.

        Returns:
            Dictionary containing found emails and count.
        """
        return self._discovery.discover(website)

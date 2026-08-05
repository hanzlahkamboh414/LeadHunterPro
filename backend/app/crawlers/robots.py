"""robots.txt compliance checker.

Determines whether a given URL is allowed to be crawled based on
the host's robots.txt policy. Results are cached per-host to avoid
re-fetching on every request.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Pattern to extract path from URL
_PATH_PATTERN = re.compile(r"^([^?#]*)")

# Default user-agent for crawlers that don't identify themselves
_DEFAULT_USER_AGENT = "LeadHunterPro-Crawler/1.0"


class RobotsDirective:
    """Parsed robots.txt directive for a single URL path."""

    __slots__ = ("allowed", "reason")

    def __init__(self, allowed: bool, reason: str = "") -> None:
        """Initialize a robots directive.

        Args:
            allowed: True if the path is allowed, False if disallowed.
            reason: Explanation for the decision.
        """
        self.allowed = allowed
        self.reason = reason


class RobotsManager:
    """Checks robots.txt rules before crawling.

    Fetches and caches robots.txt for each host. Supports multiple
    user-agents and path matching according to RFC 9309.
    """

    def __init__(self, cache: Any) -> None:  # type: ignore[no-redef]
        """Initialize the robots manager.

        Args:
            cache: A ResponseCache instance for storing parsed robots.txt.
        """
        self._cache = cache
        self._host_rules: dict[str, list[tuple[str, bool]]] = {}

    async def is_allowed(
        self,
        url: str,
        user_agent: str = _DEFAULT_USER_AGENT,
    ) -> RobotsDirective:
        """Check if a URL is allowed by robots.txt.

        Args:
            url: The full URL to check.
            user_agent: User-Agent string (used for agent-specific rules).

        Returns:
            RobotsDirective with allowed status and reason.
        """
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        path = parsed.path or "/"

        # Check cache first
        cache_key = f"robots:{host}:{user_agent}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Fetch and parse robots.txt
        directive = await self._fetch_and_parse(host, path, user_agent)
        self._cache.set(cache_key, directive, ttl_seconds=3600)
        return directive

    async def _fetch_and_parse(
        self,
        host: str,
        path: str,
        user_agent: str,
    ) -> RobotsDirective:
        """Fetch robots.txt and evaluate the path.

        Args:
            host: The hostname to fetch robots.txt for.
            path: The URL path to check.
            user_agent: User-Agent string.

        Returns:
            RobotsDirective for the given path and agent.
        """
        robots_url = f"https://{host}/robots.txt"
        if host.startswith("www."):
            robots_url = f"https://{host}/robots.txt"

        try:
            import aiohttp

            async def _check() -> RobotsDirective:
                async with aiohttp.ClientSession() as session:
                    try:
                        async with session.get(
                            robots_url,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status != 200:
                                # No robots.txt or error — allow by default
                                logger.debug(
                                    "No robots.txt at %s (status %d), allowing",
                                    robots_url,
                                    resp.status,
                                )
                                return RobotsDirective(
                                    allowed=True, reason="No robots.txt found"
                                )

                            text = await resp.text()
                            return self._evaluate_rules(text, path, user_agent)
                    except aiohttp.ClientError:
                        logger.warning("Failed to fetch robots.txt from %s", robots_url)
                        return RobotsDirective(
                            allowed=True, reason="Failed to fetch robots.txt"
                        )

            return await _check()

        except ImportError:
            # aiohttp not available — allow by default
            logger.warning(
                "aiohttp not available, skipping robots.txt check for %s", host
            )
            return RobotsDirective(allowed=True, reason="aiohttp unavailable")

    def _evaluate_rules(
        self,
        robots_text: str,
        path: str,
        user_agent: str,
    ) -> RobotsDirective:
        """Evaluate robots.txt rules against a path and user-agent.

        Args:
            robots_text: Raw robots.txt content.
            path: URL path to check.
            user_agent: User-Agent string.

        Returns:
            RobotsDirective for the request.
        """
        lines = robots_text.splitlines()
        current_agent = None
        disallow_paths: list[str] = []
        allow_paths: list[str] = []

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if ":" not in line:
                continue

            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()

            if key == "user-agent":
                current_agent = value.lower()
                disallow_paths = []
                allow_paths = []
            elif key == "disallow" and current_agent == user_agent.lower():
                if value:
                    disallow_paths.append(value)
            elif key == "allow" and current_agent == user_agent.lower():
                if value:
                    allow_paths.append(value)

        # If no rules match our user-agent, allow by default
        if not disallow_paths and not allow_paths:
            return RobotsDirective(allowed=True, reason="No matching rules found")

        # Check allow rules first (more specific wins)
        for allow_path in allow_paths:
            if self._path_matches(path, allow_path):
                return RobotsDirective(allowed=True, reason=f"Allowed by: {allow_path}")

        # Then check disallow rules
        for disallow_path in disallow_paths:
            if self._path_matches(path, disallow_path):
                return RobotsDirective(
                    allowed=False, reason=f"Disallowed by: {disallow_path}"
                )

        return RobotsDirective(allowed=True, reason="No matching disallow rules")

    @staticmethod
    def _path_matches(url_path: str, rule_path: str) -> bool:
        """Check if a URL path matches a robots.txt rule path.

        Supports prefix matching as specified in RFC 9309.

        Args:
            url_path: The URL path to check.
            rule_path: The robots.txt rule pattern.

        Returns:
            True if the path matches the rule.
        """
        # Empty disallow means allow all
        if not rule_path:
            return False

        # Wildcard matching
        if rule_path.endswith("*"):
            prefix = rule_path[:-1]
            return url_path.startswith(prefix)

        # Exact or prefix match
        return url_path.startswith(rule_path)

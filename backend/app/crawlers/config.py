"""Centralized crawler configuration.

All crawler behaviour is governed by this dataclass. Connectors
receive a CrawlerConfig instance and the crawler reads only from it.
No magic numbers exist anywhere in the crawler package.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CrawlerConfig:
    """Configuration for the Universal Crawler.

    All fields have sensible defaults. Override only what differs
    per connector or per deployment environment.

    Attributes:
        default_timeout: HTTP request timeout in seconds.
        max_retries: Maximum retry attempts on transient failure.
        retry_backoff_base: Base delay in seconds for exponential backoff.
        retry_backoff_max: Maximum delay cap in seconds.
        rate_limit_per_host_seconds: Minimum interval between requests to
            the same host.
        rate_limit_global_seconds: Minimum interval between any two requests.
        user_agent_pool: Rotating User-Agent strings to reduce detection.
        respect_robots_txt: Whether to enforce robots.txt rules.
        robots_cache_ttl: Seconds to cache parsed robots.txt per host.
        session_timeout: Internal aiohttp client timeout.
    """

    default_timeout: int = 30
    max_retries: int = 3
    retry_backoff_base: float = 2.0
    retry_backoff_max: float = 30.0
    rate_limit_per_host_seconds: float = 0.5
    rate_limit_global_seconds: float = 0.1
    user_agent_pool: list[str] = field(
        default_factory=lambda: [
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
            (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
            (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
        ]
    )
    respect_robots_txt: bool = True
    robots_cache_ttl: int = 3600
    session_timeout: int = 35

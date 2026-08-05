"""Universal Crawler Infrastructure for LeadHunter Pro.

Provides reusable HTTP fetching, parsing, and compliance tools
for all source connectors. This package is a leaf dependency --
nothing above it may import from it, and it imports nothing above it.

Components:
    exceptions     - Crawler-specific exception hierarchy
    config         - Centralized configuration dataclass
    response       - Standardized CrawlResponse dataclass
    cache          - TTL-based in-memory cache (Redis-ready interface)
    robots         - robots.txt compliance checker
    rate_limiter   - Token-bucket rate limiter (per-host + global)
    retry          - Exponential backoff retry engine
    session_manager - Low-level aiohttp session management
    html_parser    - BeautifulSoup HTML extraction wrapper
    base           - BaseCrawler ABC with CrawlRequest model
    http_crawler   - Main async crawler orchestrating all components
"""

from __future__ import annotations

from app.crawlers.base import BaseCrawler, CrawlRequest
from app.crawlers.cache import ResponseCache
from app.crawlers.config import CrawlerConfig
from app.crawlers.exceptions import (
    CrawlerError,
    CrawlerHTTPError,
    CrawlerRetryExceeded,
    CrawlerRobotsBlocked,
    CrawlerTimeout,
)
from app.crawlers.html_parser import HTMLParser, ParsedPage
from app.crawlers.http_crawler import HTTPCrawler
from app.crawlers.rate_limiter import RateLimiter
from app.crawlers.response import CrawlResponse
from app.crawlers.retry import RetryEngine, connector_retry  # noqa: F401
from app.crawlers.robots import RobotsDirective, RobotsManager
from app.crawlers.session_manager import SessionManager

__all__ = [
    "BaseCrawler",
    "CrawlRequest",
    "CrawlResponse",
    "CrawlerConfig",
    "CrawlerError",
    "CrawlerHTTPError",
    "CrawlerRetryExceeded",
    "CrawlerRobotsBlocked",
    "CrawlerTimeout",
    "HTMLParser",
    "HTTPCrawler",
    "ParsedPage",
    "RateLimiter",
    "ResponseCache",
    "RetriedEngine",
    "RobotsDirective",
    "RobotsManager",
    "SessionManager",
    "connector_retry",
]

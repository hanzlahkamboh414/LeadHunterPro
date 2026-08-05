"""Shared HTTP helper for discovery sources.

Provides a single fetch function that maps low-level network outcomes
onto the discovery SourceStatus vocabulary so every source degrades
gracefully and identically:

  - DNS failure / connection refused / timeout  -> UNAVAILABLE
  - HTTP 2xx                                    -> (caller parses)
  - HTTP 4xx/5xx                                -> ERROR
  - any other exception                         -> ERROR

No source should ever raise a network exception to the orchestrator.
This helper guarantees that by catching everything and returning a
structured result.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.discovery.sources.status import SourceStatus

logger = logging.getLogger(__name__)

# Default browser-like headers. Public government sites often reject
# requests with no User-Agent.
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class FetchResult:
    """Outcome of an HTTP fetch, classified into a SourceStatus."""

    status: SourceStatus
    http_status: int | None = None
    text: str = ""
    final_url: str = ""
    elapsed_ms: float = 0.0
    error: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when the response is a usable 2xx payload."""
        return self.http_status is not None and 200 <= self.http_status < 300


def fetch(
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> FetchResult:
    """Fetch a URL and classify the outcome into a SourceStatus.

    This function NEVER raises. Network problems become UNAVAILABLE,
    HTTP errors become ERROR, and successful responses carry the body
    for the caller to parse.

    Args:
        url: Target URL.
        method: HTTP method ("GET" or "POST").
        params: Query-string parameters.
        data: Form body for POST requests.
        headers: Extra headers merged over the browser defaults.
        timeout: Per-request timeout in seconds.

    Returns:
        A :class:`FetchResult`.
    """
    merged_headers = {**_DEFAULT_HEADERS, **(headers or {})}
    start = time.monotonic()

    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests is a hard dep
        return FetchResult(
            status=SourceStatus.ERROR,
            error=f"requests_not_installed: {exc}",
        )

    try:
        resp = requests.request(
            method.upper(),
            url,
            params=params,
            data=data,
            headers=merged_headers,
            timeout=timeout,
            allow_redirects=True,
        )
        elapsed_ms = (time.monotonic() - start) * 1000

        if 200 <= resp.status_code < 300:
            status = SourceStatus.SUCCESS
        else:
            status = SourceStatus.ERROR

        return FetchResult(
            status=status,
            http_status=resp.status_code,
            text=resp.text,
            final_url=resp.url,
            elapsed_ms=round(elapsed_ms, 1),
            error="" if status == SourceStatus.SUCCESS else f"http_{resp.status_code}",
            headers=dict(resp.headers),
        )

    except requests.exceptions.ConnectionError as exc:
        # DNS failure, connection refused, network unreachable — the
        # source is configured but cannot be reached right now.
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info("HTTP unavailable for %s: %s", url, exc)
        return FetchResult(
            status=SourceStatus.UNAVAILABLE,
            elapsed_ms=round(elapsed_ms, 1),
            error=f"connection_error: {_short(exc)}",
        )
    except requests.exceptions.Timeout as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info("HTTP timeout for %s: %s", url, exc)
        return FetchResult(
            status=SourceStatus.UNAVAILABLE,
            elapsed_ms=round(elapsed_ms, 1),
            error=f"timeout: {_short(exc)}",
        )
    except Exception as exc:  # noqa: BLE001 - never propagate to orchestrator
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.warning("HTTP unexpected error for %s: %s", url, exc)
        return FetchResult(
            status=SourceStatus.ERROR,
            elapsed_ms=round(elapsed_ms, 1),
            error=f"unexpected: {_short(exc)}",
        )


def _short(exc: Exception, limit: int = 200) -> str:
    """Return a truncated string form of an exception."""
    s = str(exc)
    return s if len(s) <= limit else s[:limit] + "..."

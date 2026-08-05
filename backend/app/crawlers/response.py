"""Standardized crawl response model.

Every crawler operation returns a CrawlResponse. Consumers never
see raw aiohttp objects -- they always interact with this dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CrawlResponse:
    """Immutable result of a single crawl attempt.

    Attributes:
        url: The final URL after all redirects.
        status_code: HTTP status code of the response.
        content: Raw bytes of the response body.
        headers: Response headers as a frozen mapping.
        successful: True if status_code is 2xx.
        robots_compliant: True if robots.txt allowed this request.
        cached: True if the response was served from cache.
        error: Exception string if the request failed, else empty.
        response_time_ms: Wall-clock time in milliseconds.
    """

    url: str
    status_code: int
    content: bytes
    headers: dict[str, str]
    successful: bool
    robots_compliant: bool
    cached: bool = False
    error: str = ""
    response_time_ms: float = 0.0

    @property
    def text(self) -> str:
        """Decode content as UTF-8 text.

        Returns:
            Decoded string, or empty string on decode failure.
        """
        if not self.content:
            return ""
        try:
            return self.content.decode("utf-8")
        except UnicodeDecodeError:
            return self.content.decode("latin-1", errors="replace")

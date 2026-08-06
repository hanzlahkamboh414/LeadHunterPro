"""SearXNG-backed candidate generator.

Queries a self-hosted SearXNG metasearch instance to find construction
company websites. Requires SEARXNG_URL to be configured.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from app.discovery.website.candidates import Candidate, CandidateGenerator

if TYPE_CHECKING:
    from app.search_providers.searxng import SearXNGProvider

logger = logging.getLogger(__name__)


class SearXNGGenerator(CandidateGenerator):
    """Generate candidates via a SearXNG instance.

    Wraps SearXNGProvider as a CandidateGenerator. Requires a base URL,
    read from the constructor or the SEARXNG_URL environment variable.
    If no base URL is configured, generate() returns an empty list.
    """

    name = "searxng"

    def __init__(self, *, base_url: str | None = None) -> None:
        """Initialize the SearXNG generator.

        Args:
            base_url: SearXNG instance URL. Defaults to SEARXNG_URL env var.
        """
        self._base_url = base_url or os.environ.get("SEARXNG_URL", "")
        self._provider: SearXNGProvider | None = None

    def _ensure_provider(self) -> None:
        """Lazily construct the provider once a base URL is known."""
        if self._provider is None and self._base_url:
            from app.search_providers.searxng import SearXNGProvider

            self._provider = SearXNGProvider(base_url=self._base_url)

    def generate(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> list[Candidate]:
        """Propose candidates via SearXNG.

        Args:
            industry: Industry keyword (e.g. "Roofing").
            location: Geographic location (e.g. "Dallas Texas").
            limit: Maximum candidates to return.

        Returns:
            List of candidates from search results, possibly empty.
        """
        if not self._base_url:
            logger.info(
                "SearXNGGenerator: no base URL configured "
                "(set SEARXNG_URL), returning empty"
            )
            return []

        from app.search_providers.models import SearchQuery

        self._ensure_provider()
        assert self._provider is not None  # guaranteed by base_url check

        query_text = f"{industry} contractor {location}".strip()
        query = SearchQuery(keywords=query_text, num_results=limit)

        logger.debug(
            "SearXNGGenerator: querying %r (limit=%d)",
            query_text,
            limit,
        )

        try:
            response = asyncio.run(self._provider.search(query))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "SearXNGGenerator: search failed — %s: %s",
                type(exc).__name__,
                exc,
            )
            return []

        if response.error:
            logger.warning(
                "SearXNGGenerator: provider error — %s",
                response.error,
            )
            return []

        if not response.results:
            logger.debug("SearXNGGenerator: no results")
            return []

        candidates: list[Candidate] = []
        for result in response.results[:limit]:
            try:
                candidate = Candidate(
                    url=result.url,
                    generator=self.generator_name,
                    attributes={
                        "title": result.title,
                        "snippet": result.snippet,
                        "query": query_text,
                        "position": str(result.position),
                    },
                )
                candidates.append(candidate)
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "SearXNGGenerator: skipping %r — %s",
                    result.url,
                    exc,
                )
                continue

        logger.info(
            "SearXNGGenerator: %d/%d candidates accepted",
            len(candidates),
            len(response.results),
        )

        return candidates

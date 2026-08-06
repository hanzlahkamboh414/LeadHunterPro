"""Brave Search-backed candidate generator.

Queries the Brave Search API to find construction company websites
matching an industry and location.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from app.discovery.website.candidates import Candidate, CandidateGenerator

if TYPE_CHECKING:
    from app.search_providers.brave import BraveSearchProvider
    from app.search_providers.models import SearchQuery

logger = logging.getLogger(__name__)


class BraveSearchGenerator(CandidateGenerator):
    """Generate candidates via Brave Search API.

    Wraps BraveSearchProvider as a CandidateGenerator, mapping search
    results to Candidate objects. Requires BRAVE_SEARCH_API_KEY.
    """

    name = "brave_search"

    def __init__(self, *, api_key: str | None = None) -> None:
        """Initialize the Brave Search generator.

        Args:
            api_key: Optional API key override. Defaults to environment.
        """
        from app.search_providers.brave import BraveSearchProvider

        self._provider: BraveSearchProvider = BraveSearchProvider(api_key=api_key)

    def generate(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> list[Candidate]:
        """Propose candidates via Brave Search.

        Args:
            industry: Industry keyword (e.g. "Roofing").
            location: Geographic location (e.g. "Dallas Texas").
            limit: Maximum candidates to return.

        Returns:
            List of candidates from search results, possibly empty.
        """
        from app.search_providers.models import SearchQuery

        # Build search query
        query_text = f"{industry} contractor {location}".strip()
        query = SearchQuery(keywords=query_text, num_results=limit)

        logger.debug(
            "BraveSearchGenerator: querying %r (limit=%d)",
            query_text,
            limit,
        )

        # Execute search (async bridge)
        try:
            response = asyncio.run(self._provider.search(query))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "BraveSearchGenerator: search failed — %s: %s",
                type(exc).__name__,
                exc,
            )
            return []

        # Handle provider errors
        if response.error:
            logger.warning(
                "BraveSearchGenerator: provider error — %s",
                response.error,
            )
            return []

        if not response.results:
            logger.debug("BraveSearchGenerator: no results")
            return []

        # Map SearchResult → Candidate
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
                # URL normalization failed or invalid input
                logger.warning(
                    "BraveSearchGenerator: skipping %r — %s",
                    result.url,
                    exc,
                )
                continue

        logger.info(
            "BraveSearchGenerator: %d/%d candidates accepted",
            len(candidates),
            len(response.results),
        )

        return candidates

"""Fixture-backed candidate generator — emergency bridge only.

Emits candidate URLs from the curated fixture dataset so the discovery
pipeline is exercisable end-to-end when no live provider is available.

This is NOT a primary source. Per CLAUDE.md §1, every candidate it emits
carries explicit fixture provenance so bridge usage is never hidden.
It reuses FixtureSource for loading and matching (CLAUDE.md §14) — it does
NOT re-parse the fixture file.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.discovery.website.candidates import Candidate, CandidateGenerator

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class FixtureGenerator(CandidateGenerator):
    """Generate candidate URLs from the fixture dataset (bridge only).

    Reuses FixtureSource's loading and matching logic. Every candidate
    is tagged with data_source="fixture" and a fallback_reason so
    provenance survives into the final company record.
    """

    name = "fixture"

    def __init__(self, fixture_path: Path | None = None) -> None:
        """Initialize the fixture generator.

        Args:
            fixture_path: Optional override forwarded to FixtureSource.
        """
        from app.discovery.sources.fixture_source import FixtureSource

        self._source = FixtureSource(fixture_path=fixture_path)

    def generate(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> list[Candidate]:
        """Propose candidate URLs from fixture data.

        Args:
            industry: Industry keyword (e.g. "Roofing").
            location: Geographic location (e.g. "Dallas Texas").
            limit: Maximum candidates to return.

        Returns:
            List of candidates from matched fixture companies, possibly empty.
        """
        # Reuse FixtureSource matching — no re-parsing of the fixture file.
        _status, companies, meta = self._source.discover(
            industry=industry, location=location, limit=limit
        )

        fallback_reason = meta.get("fallback_reason", "fixture_bridge")

        candidates: list[Candidate] = []
        for company in companies[:limit]:
            website = company.get("website", "")
            if not website:
                continue
            try:
                candidate = Candidate(
                    url=website,
                    generator=self.generator_name,
                    attributes={
                        "title": company.get("company_name", ""),
                        "data_source": "fixture",
                        "fallback_reason": fallback_reason,
                        "query": f"{industry} {location}".strip(),
                    },
                )
                candidates.append(candidate)
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "FixtureGenerator: skipping %r — %s",
                    website,
                    exc,
                )
                continue

        logger.warning(
            "FixtureGenerator: %d candidates (BRIDGE DATA — "
            "fixture source, not live discovery)",
            len(candidates),
        )

        return candidates

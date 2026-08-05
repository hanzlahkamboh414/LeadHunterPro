"""Connector manager — orchestrates discovery across multiple connectors.

Provides a unified interface for listing connectors, running discovery
on one or all of them, and aggregating results.
"""

from __future__ import annotations

import logging
from typing import Any

from app.engines.source_connectors.deduplicator import Deduplicator
from app.engines.source_connectors.error_handler import ErrorHandler
from app.engines.source_connectors.normalizer import Normalizer
from app.engines.source_connectors.sdk import CompanyResult, ConnectorRegistry

logger = logging.getLogger(__name__)


class ConnectorManager:
    """Manages discovery operations across registered connectors.

    The manager coordinates multi-connector discovery runs, applying
    normalisation and deduplication across results from different sources.
    """

    def __init__(
        self,
        *,
        normalizer: Normalizer | None = None,
        deduplicator: Deduplicator | None = None,
    ) -> None:
        """Initialize the connector manager.

        Args:
            normalizer: Optional normalizer instance. A new one is created
                if not provided.
            deduplicator: Optional deduplicator instance. A new one is
                created if not provided.
        """
        self._normalizer = normalizer or Normalizer()
        self._deduplicator = deduplicator or Deduplicator()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_connectors(self) -> list[dict[str, str]]:
        """Return metadata for all registered connectors.

        Returns:
            List of dicts with ``name`` and ``description`` keys.
        """
        connectors = ConnectorRegistry.list_all()
        return [
            {"name": c.name, "description": c.description}
            for c in connectors
        ]

    def get_connector(self, name: str) -> Any | None:
        """Retrieve a connector by name.

        Args:
            name: Connector identifier.

        Returns:
            The connector instance, or ``None`` if not found.
        """
        return ConnectorRegistry.get(name)

    def discover(
        self,
        *,
        connector_name: str | None = None,
        state: str | None = None,
        city: str | None = None,
        industry: str = "Construction Estimating",
        limit: int = 50,
    ) -> tuple[list[CompanyResult], dict[str, Any]]:
        """Run discovery across one or all registered connectors.

        If *connector_name* is given, only that connector is queried.
        Otherwise, all registered connectors are queried and results
        are merged, normalised, and deduplicated.

        Args:
            connector_name: Specific connector to query (optional).
            state: US state code filter.
            city: City name filter.
            industry: Industry keyword filter.
            limit: Maximum total results across all connectors.

        Returns:
            Tuple of (deduplicated results, aggregate metadata).
        """
        error_handler = ErrorHandler("connector_manager")
        all_results: list[CompanyResult] = []
        connector_metadata: list[dict[str, Any]] = []

        if connector_name:
            targets = [(connector_name, ConnectorRegistry.get(connector_name))]
        else:
            targets = [
                (c.name, c) for c in ConnectorRegistry.list_all()
            ]

        for name, connector in targets:
            if connector is None or not connector.is_available():
                error_handler.add_warning(f"Connector '{name}' unavailable, skipping")
                continue

            try:
                results, meta = connector.discover(
                    state=state,
                    city=city,
                    industry=industry,
                    limit=limit,
                )
                connector_metadata.append({
                    "connector": name,
                    "raw_count": meta.get("returned_count", len(results)),
                })
                all_results.extend(results)
            except Exception as exc:
                error_handler.add_error(f"discover({name})", str(exc))
                logger.error("Connector '%s' discovery failed: %s", name, exc, exc_info=True)

        # Normalise (in case any connectors return raw dicts)
        # All connectors now return CompanyResult, so this is a pass-through.
        # But we keep it for backward compatibility.
        normalised: list[CompanyResult] = []
        for r in all_results:
            if isinstance(r, CompanyResult):
                normalised.append(r)
            else:
                norm = self._normalizer.normalize(r)  # type: ignore[arg-type]
                if norm:
                    normalised.append(norm)

        # Deduplicate across all connectors
        final = self._deduplicator.deduplicate(normalised)

        # Apply global limit
        final = final[:limit]

        metadata: dict[str, Any] = {
            "total_raw": len(all_results),
            "total_after_dedup": len(final),
            "connectors_queried": len(connector_metadata),
            "connectors": connector_metadata,
            "filters_applied": {
                "state": state,
                "city": city,
                "industry": industry,
                "limit": limit,
            },
        }
        if error_handler.has_errors:
            metadata["errors"] = error_handler.summary()

        logger.info(
            "ConnectorManager: %d raw → %d after dedup (%d connectors)",
            len(all_results),
            len(final),
            len(connector_metadata),
        )
        return final, metadata

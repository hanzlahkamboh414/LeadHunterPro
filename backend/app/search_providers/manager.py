"""Search provider manager — orchestrates multi-provider search with fallback.

The manager executes providers in priority order, aggregating results and
falling back to the next provider when one fails or returns no results.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.search_providers.models import SearchQuery, SearchResponse
from app.search_providers.registry import SearchProviderRegistry, get_registry

logger = logging.getLogger(__name__)


class SearchProviderManager:
    """Orchestrates search across multiple providers with fallback.

    Providers are executed in priority order. Results are aggregated
    and deduplicated by URL. If all providers fail, an error response
    is returned with diagnostic information.

    Usage:
        manager = SearchProviderManager()
        response = await manager.search(SearchQuery(keywords="roofing dallas"))
    """

    def __init__(self, registry: SearchProviderRegistry | None = None) -> None:
        """Initialize the search provider manager.

        Args:
            registry: Optional custom registry. Uses singleton if not provided.
        """
        self._registry = registry or get_registry()

    async def search(
        self,
        query: SearchQuery,
        *,
        fail_fast: bool = False,
        min_results: int = 1,
    ) -> SearchResponse:
        """Execute a search query across all enabled providers.

        Providers are tried in priority order. Results are aggregated
        and deduplicated. Execution stops early if fail_fast=True and
        any provider succeeds, or when min_results are collected.

        Args:
            query: The search query to execute.
            fail_fast: If True, return after first successful provider.
            min_results: Minimum results before stopping early.

        Returns:
            Aggregated SearchResponse with combined results.
        """
        start = time.monotonic()
        providers = self._registry.get_enabled()
        all_results: list[Any] = []
        errors: dict[str, str] = {}
        provider_stats: dict[str, dict[str, Any]] = {}

        logger.info(
            "SearchProviderManager.search: %d providers, query=%r",
            len(providers),
            query.keywords,
        )

        for provider in providers:
            # Circuit-breaker: a provider inside its down-window (recently timed
            # out / hard-failed) is skipped instantly — no slow retries.
            if self._registry.is_down(provider.provider_name):
                logger.debug("Skipping down provider: %s", provider.provider_name)
                continue

            provider_start = time.monotonic()

            try:
                response = await asyncio.wait_for(
                    asyncio.ensure_future(provider.search(query)),
                    timeout=getattr(provider, "timeout_s", 10.0),
                )
                provider_elapsed = (time.monotonic() - provider_start) * 1000

                provider_stats[provider.provider_name] = {
                    "status": response.status,
                    "results": len(response.results),
                    "latency_ms": round(provider_elapsed, 1),
                    "error": response.error,
                }

                if response.status == "success" and response.results:
                    all_results.extend(response.results)
                    logger.info(
                        "Provider %s returned %d results in %.1fms",
                        provider.provider_name,
                        len(response.results),
                        provider_elapsed,
                    )

                    if fail_fast:
                        break
                    if len(all_results) >= min_results:
                        break
                elif response.status == "error":
                    # A provider that answers with status=error (e.g. SearXNG
                    # swallowing its own aiohttp timeout, an auth/rate-limit
                    # rejection) will keep failing — blacklist it for a TTL so
                    # later queries skip straight to the next provider.
                    errors[provider.provider_name] = response.error
                    self._registry.mark_down(provider.provider_name)
                    logger.warning(
                        "Provider %s failed — marked down: %s",
                        provider.provider_name,
                        response.error,
                    )

            except asyncio.TimeoutError:
                # Hung provider (e.g. SearXNG waiting on dead engines) — give up
                # fast, fall through to the next provider, and blacklist it for
                # a TTL so the NEXT query does not pay the same wait again.
                provider_elapsed = (time.monotonic() - provider_start) * 1000
                name = provider.provider_name
                errors[name] = f"{name} timed out after {getattr(provider, 'timeout_s', 10.0):.1f}s"
                provider_stats[name] = {
                    "status": "error",
                    "results": 0,
                    "latency_ms": round(provider_elapsed, 1),
                    "error": errors[name],
                }
                self._registry.mark_down(name)
                logger.warning(
                    "Provider %s hung (>%.1fs) — marked down, falling back",
                    name, getattr(provider, "timeout_s", 10.0),
                )

            except Exception as exc:
                provider_elapsed = (time.monotonic() - provider_start) * 1000
                name = provider.provider_name
                errors[name] = str(exc)
                provider_stats[name] = {
                    "status": "error",
                    "results": 0,
                    "latency_ms": round(provider_elapsed, 1),
                    "error": str(exc),
                }
                self._registry.mark_down(name)
                logger.exception(
                    "Provider %s exception — marked down",
                    name,
                )

        total_latency = (time.monotonic() - start) * 1000

        # Determine overall status
        if all_results:
            status = "success"
        elif errors:
            status = "error"
        else:
            status = "partial"

        return SearchResponse(
            results=all_results,
            provider=", ".join(p.provider_name for p in providers) or "none",
            query=query.keywords,
            latency_ms=round(total_latency, 1),
            error=self._format_error(errors) if errors else "",
            status=status,
        )

    @staticmethod
    def _format_error(errors: dict[str, str]) -> str:
        """Format provider errors into a human-readable string.

        Args:
            errors: Dict mapping provider name to error message.

        Returns:
            Formatted error string.
        """
        if not errors:
            return ""
        parts = [f"{name}: {msg}" for name, msg in errors.items()]
        return "; ".join(parts)

    async def health_check(self) -> dict[str, dict[str, Any]]:
        """Check health of all registered providers.

        Returns:
            Dict mapping provider name to health status.
        """
        return await self._registry.health_check_all()

    def list_providers(self) -> list[dict[str, Any]]:
        """List all registered providers with their status.

        Returns:
            List of provider info dicts.
        """
        return [
            {
                "name": p.provider_name,
                "enabled": p.enabled,
                "priority": p.priority,
                "description": p.description,
            }
            for p in self._registry.get_all()
        ]

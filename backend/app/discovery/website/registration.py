"""Composition root for the Direct Website Discovery plugin.

This module is the one place the plugin's concrete collaborators are
wired together and handed to the framework's registration mechanism
(``get_registry().register(...)``) — the same seam the plugin design
describes as "register plugins at startup". The mirror convention in
this codebase is ``app.search_providers``, which config-gates provider
registration on import.

Two responsibilities, kept separate so each is testable:

    build_website_discovery_plugin()  →  the DI wiring: the plugin with
        its three concrete candidate generators. No registry involved.
    register_website_discovery()      →  the config gate and the
        registration call. No generator knowledge beyond "are there any".

Why the config gate exists:
    The plugin proposes candidate websites from its generators, then
    crawls and extracts them. When no live origin is configured
    (neither ``BRAVE_SEARCH_API_KEY`` nor ``SEARXNG_URL``), the only
    generator that can propose anything is the fixture bridge — and a
    website-discovery run over fixture-only candidates would make the
    bridge a primary source, which CLAUDE.md §1 forbids. The downstream
    pipeline already covers the "nothing live" case with
    :class:`~app.discovery.sources.fixture_source.FixtureSource`, so the
    plugin simply is not registered until a live origin exists. The
    skip is logged, never silent (§5).

Why idempotent:
    Registration may run from a composition root that executes per
    discovery call. Re-registering is a no-op that reports the plugin is
    already present, so a live run never rebuilds or warns.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.discovery.plugins.plugin_registry import PluginRegistry, get_registry
from app.discovery.website.generators import (
    BraveSearchGenerator,
    FixtureGenerator,
    SearXNGGenerator,
)
from app.discovery.website.plugin import DirectWebsiteDiscoveryPlugin

if TYPE_CHECKING:
    from app.discovery.plugins.plugin_config import PluginConfig
    from app.discovery.website.candidates import CandidateGenerator

logger = logging.getLogger(__name__)

#: The plugin's identity, single-sourced from the plugin itself so a
#: rename here can never drift from the registry key used at runtime.
PLUGIN_NAME = DirectWebsiteDiscoveryPlugin.name


def _live_origin_configured() -> bool:
    """Whether any live candidate origin has configuration present.

    Mirror of the search-provider auto-registration gate
    (``app.search_providers``): a generator whose origin is not
    configured cannot produce candidates. Returns ``False`` in
    environments where ``app.core.config`` cannot be loaded, which keeps
    registration a clean no-op there.
    """
    try:
        from app.core.config import settings
    except Exception as exc:  # noqa: BLE001
        # A settings module that cannot load (e.g. a missing required
        # field such as DATABASE_URL) must not take discovery down — the
        # plugin simply stays unregistered and the skip is logged.
        logger.warning(
            "Website discovery plugin: app.core.config unavailable (%s); "
            "treating as no live origin configured",
            exc,
        )
        return False
    return bool(
        getattr(settings, "BRAVE_SEARCH_API_KEY", "")
        or getattr(settings, "SEARXNG_URL", "")
    )


def build_website_discovery_plugin(
    *,
    config: PluginConfig | None = None,
    generators: list[CandidateGenerator] | None = None,
) -> DirectWebsiteDiscoveryPlugin:
    """Construct the website discovery plugin with its concrete generators.

    Dependency injection exactly as the plugin design describes: the
    plugin accepts its candidate origins through the constructor and
    holds no concrete provider itself. With no explicit ``generators``
    the full set is wired — Brave Search, SearXNG and the fixture
    bridge — so the crawl-and-extract pipeline is exercisable end to
    end, with bridge provenance carried on every fixture candidate
    (CLAUDE.md §1).

    Args:
        config: Optional runtime configuration forwarded to the plugin.
        generators: Candidate origins. Defaults to ``BraveSearchGenerator``,
            ``SearXNGGenerator`` and ``FixtureGenerator``.

    Returns:
        A configured :class:`DirectWebsiteDiscoveryPlugin`.
    """
    if generators is None:
        generators = [
            BraveSearchGenerator(),
            SearXNGGenerator(),
            FixtureGenerator(),
        ]
    return DirectWebsiteDiscoveryPlugin(config, generators=generators)


def register_website_discovery(
    *,
    registry: PluginRegistry | None = None,
) -> bool:
    """Register the website discovery plugin with the shared PluginRegistry.

    Config-gated: with no live origin configured (neither
    ``BRAVE_SEARCH_API_KEY`` nor ``SEARXNG_URL``), nothing is registered
    and ``False`` is returned with the reason logged — fixture-only
    website discovery must never be the pipeline's first and only path
    (CLAUDE.md §1, §5). Idempotent: an already-registered plugin is left
    in place.

    Args:
        registry: Registry to register into. Defaults to the shared one.

    Returns:
        True if the plugin is registered (or already was); False when it
        was deliberately not registered for lack of a live origin.
    """
    if not _live_origin_configured():
        logger.info(
            "%s: NO LIVE CANDIDATE ORIGIN CONFIGURED — website discovery "
            "plugin NOT registered. Set BRAVE_SEARCH_API_KEY or SEARXNG_URL "
            "to activate it (CLAUDE.md §5).",
            PLUGIN_NAME,
        )
        return False

    registry = registry or get_registry()
    if PLUGIN_NAME in registry:
        return True

    plugin = build_website_discovery_plugin()
    return registry.register(plugin)

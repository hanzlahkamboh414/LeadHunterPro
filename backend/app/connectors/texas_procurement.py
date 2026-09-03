"""Texas Procurement Connector — Production Implementation.

Discovers construction companies from Texas public procurement data,
trade directories, and contractor registries. Uses the Universal
Crawler infrastructure for any live web fetching.

Data flow (production pipeline):
    discover()
        ↓
    _fetch_live()           ← Uses HTTPCrawler for Tier 2 sources
        ↓
    if live results exist:  ← Primary path (Sprint 2.3+)
        use them
    else:                   ← Bridge path (Sprint 2.2)
        load fixture        ← Temporary bridge per ADR-002
        ↓
    normalize
        ↓
    validate
        ↓
    rank
        ↓
    return

The fixture dataset is a TIME-LIMITED BRIDGE per ADR-002.
It MUST be replaced with live sourcing in Sprint 2.3.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.connectors.base_connector import BaseConnector
from app.connectors.connector_result import ConnectorResult
from app.connectors.industry_expansion import expand_industry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fixture Data Location
# ---------------------------------------------------------------------------
# BRIDGE DATA: This is a temporary bridge per ADR-002.
# Must be replaced with live Tier 2 sources in Sprint 2.3.
# ---------------------------------------------------------------------------

_FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "texas_procurement.json"


def _load_fixture_data() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load fixture data from JSON file.

    Returns:
        Tuple of (companies list, metadata dict with bridge info).
    """
    if not _FIXTURE_PATH.exists():
        logger.error("Fixture file missing: %s", _FIXTURE_PATH)
        return [], {"data_source": "missing", "temporary": True}

    try:
        with open(_FIXTURE_PATH, encoding="utf-8") as f:
            data = json.load(f)

        companies = data.get("companies", [])
        meta = {
            "data_source": "fixture",
            "temporary": data.get("temporary", True),
            "version": data.get("version", "unknown"),
            "last_updated": data.get("last_updated", "unknown"),
            "description": data.get("description", ""),
        }
        logger.info(
            "Loaded %d companies from fixture: %s", len(companies), _FIXTURE_PATH
        )
        return companies, meta
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load fixture %s: %s", _FIXTURE_PATH, exc)
        return [], {"data_source": "error", "temporary": True, "error": str(exc)}


class TexasProcurementConnector(BaseConnector):
    """Discovery connector for Texas construction companies.

    Production pipeline:
    1. Attempt live fetch via _fetch_live() (uses HTTPCrawler)
    2. If no live results, fall back to curated fixtures
    3. Apply industry expansion matching
    4. Validate URLs and company data
    5. Rank by relevance
    6. Return ConnectorResults with metadata

    The fixture fallback is a TIME-LIMITED BRIDGE (ADR-002).
    Sprint 2.3 MUST replace this with live Tier 2 sources.
    """

    connector_name = "texas_procurement"
    priority = 10
    enabled = True

    def __init__(
        self,
        *,
        ai_engine: Any | None = None,
    ) -> None:
        """Initialize the Texas Procurement connector.

        Args:
            ai_engine: Optional Phase 3 Step 2 AI engine (injectable in
                tests). When ``None`` the connector lazily creates a real
                ``AIEngine`` on first use; if construction fails AI
                intelligence is skipped and deterministic acceptance is
                unaffected (AI must never break discovery).
        """
        self._companies, self._fixture_meta = _load_fixture_data()
        self._live_results: list[dict[str, Any]] = []
        self._ai_engine: Any | None = ai_engine
        self._ai_engine_resolved: bool = ai_engine is not None
        logger.info(
            "TexasProcurementConnector initialized: %d fixture records, live=%s",
            len(self._companies),
            bool(self._live_results),
        )

    def search(
        self,
        industry: str,
        location: str,
        limit: int,
    ) -> tuple[list[ConnectorResult], dict[str, Any]]:
        """Execute discovery following production pipeline.

        Uses SourceOrchestrator which:
        1. Runs registered discovery plugins first (when any are registered)
        2. Tries SearchProviderSource (live web search) next
        3. Falls back to FixtureSource when no providers are configured
           or all fail
        4. Aggregates, deduplicates, and classifies results
        5. Applies industry expansion matching
        6. Validates URLs and ranks by confidence

        Args:
            industry: Industry search term (e.g. "Roofing").
            location: Geographic location (e.g. "Dallas Texas").
            limit: Maximum number of results to return.

        Returns:
            Tuple of (list of ConnectorResult, metadata dict).
        """
        logger.info(
            "TexasProcurementConnector.search: industry=%r location=%r limit=%d",
            industry,
            location,
            limit,
        )

        # Step 1: Run SourceOrchestrator (plugins + live search + fixture fallback)
        from app.discovery.plugins.base_plugin import PluginCapability
        from app.discovery.source_orchestrator import SourceOrchestrator
        from app.discovery.sources.directory_crawl_source import DirectoryCrawlSource
        from app.discovery.sources.fixture_source import FixtureSource
        from app.discovery.sources.plugin_source import attach_plugin_source
        from app.discovery.sources.search_provider_source import SearchProviderSource
        from app.discovery.website.registration import register_website_discovery

        # Register the Direct Website Discovery plugin before the
        # orchestrator is assembled. Config-gated and idempotent: with no
        # live search origin configured it is a logged no-op, and the
        # pipeline below runs exactly as it did before (CLAUDE.md §1, §5).
        register_website_discovery()

        orchestrator = SourceOrchestrator()
        # Plugin framework, attached only when a company-discovery plugin is
        # registered. With an empty registry this is a logged no-op and the
        # pipeline below is unchanged. COMPANY_DISCOVERY is passed explicitly
        # so a future leadership/email plugin cannot leak into this stage.
        attach_plugin_source(
            orchestrator,
            capability=PluginCapability.COMPANY_DISCOVERY,
        )
        # DirectoryCrawlSource (priority 20) runs BEFORE search providers
        # (50) and the fixture bridge (999): it is the API-free primary
        # path (Blueprint §6 Phase 3). When it returns UNAVAILABLE — e.g.
        # the sandbox blocks seed-host DNS — the orchestrator simply
        # continues with the next source; it is never terminal.
        orchestrator.register(DirectoryCrawlSource())
        # PlanHolderSource (priority 25) sits between the directory crawl and
        # the search providers: it finds plan-holder-list PDFs live and feeds
        # their rows (named contact + person-bound email) into discovery.
        from app.discovery.sources.plan_holder_source import PlanHolderSource

        orchestrator.register(PlanHolderSource())
        orchestrator.register(SearchProviderSource())
        orchestrator.register(FixtureSource())
        companies, orch_metadata = orchestrator.discover(
            industry=industry,
            location=location,
            limit=limit,
        )
        data_source = orch_metadata.get("data_source", "empty")

        # Inc9: enrich location-less candidates from their own websites
        # BEFORE the Step-3 filter. Search and website-plugin sources emit
        # no location by design (accuracy-first, Phase 2 Step 3); crawling
        # each candidate's site fills city/state/address from REAL page
        # evidence so the filter and LocationVerifier act on proof, never
        # the query. Honest by construction: unreachable or no-address
        # pages stay un-enriched and are dropped downstream (§12). The
        # import is function-local to keep aiohttp out of this module's
        # import graph (same boundary DirectoryCrawlSource draws).
        from app.discovery.website.enricher import WebsiteEnricher

        companies, enrich_meta = WebsiteEnricher().enrich(
            companies, industry=industry, limit=limit
        )
        orch_metadata = {**orch_metadata, "enrichment": enrich_meta}

        # Step 2: Parse location and expand industry for matching
        city, state = _parse_location(location)
        expanded_keywords = expand_industry(industry)
        logger.debug(
            "Expanded industry %r to %d keywords", industry, len(expanded_keywords)
        )

        # Step 3: Apply local filtering (state, city, keyword match).
        # Project heterogeneous source dicts onto the connector's expected
        # keys first so no record is dropped for schema reasons: the website
        # discovery plugin emits name/services/evidence and no location,
        # while search/fixture sources emit company_name/industry_focus.
        # Location is never filled from the query (Phase 2 Step 3): the
        # parsed state/city below remain SEARCH TARGETS for this filter only,
        # and are not written into records. Records carry only a
        # source-supplied location, else empty/unknown.
        companies = [
            self._normalize_company(c, state=state, city=city) for c in companies
        ]
        matched: list[dict[str, Any]] = []
        for company in companies:
            # Inc 3 carve-out: a plan-holder PDF record carries a named
            # contact + person-bound email but NO state/city/industry signal
            # (the list states only company name + person). The location and
            # keyword filters below would drop it for that absence, killing
            # the bridge before it can surface the pre-bound person. Its value
            # is the person/email, not company-level location matching, so it
            # is passed through the filter untouched and re-verified at the
            # gate (Step 4) exactly like the fixture bridge — never a
            # live-verified lead.
            if company.get("_discovery_source") == "plan_holder":
                matched.append(company)
                continue
            if state and company.get("state", "").upper() != state.upper():
                continue
            if city and company.get("city", "").lower() != city.lower():
                continue
            text = (
                f"{company.get('company_name', '')} "
                f"{company.get('industry_focus', '')} "
                f"{company.get('trade_category', '')}"
            ).lower()
            if any(kw in text for kw in expanded_keywords):
                matched.append(company)

        logger.info(
            "Matched %d companies for industry=%r in location=%r",
            len(matched),
            industry,
            location,
        )

        # Step 4: Acceptance gate, then build results with validation and
        # ranking. Only gate-passing records become accepted ConnectorResults
        # (Phase 2 Step 4): a manufacturer/supplier/association, a record with
        # an invalid/placeholder/aggregator website, or a Tier-4 fixture is
        # never a live verified lead. Evidence gaps (missing email/phone/
        # decision maker, unknown website/location) are NOT rejections.
        is_bridge = data_source == "fixture"
        results: list[ConnectorResult] = []
        discovery_reasons: list[str] = []
        accepted_count = 0

        for company in matched[:limit]:
            gate = self._verify_company(company, location)

            # ADR-002 bridge carve-out: when ALL live discovery failed the
            # fixture bridge is surfaced as labeled bridge data (never as a
            # live-verified lead). Hard rejections are excluded even here.
            bridge_fixture = (
                is_bridge
                and company.get("_discovery_source") == "fixture_bridge"
                and not gate.hard_rejected
            )
            # Inc 3 carve-out: a plan-holder PDF record carries a named
            # contact + person-bound email but NO verification evidence
            # (derived website, empty industry/location), so the gate grades
            # it confirmed==0 / accepted=False. Like the fixture carve-out it
            # is passed through LABELED unverified — never as a verified lead
            # — so the Inc 3 bridge can surface the pre-bound person/email in
            # the pipeline. Hard rejections still drop it.
            plan_holder_carveout = (
                company.get("_discovery_source") == "plan_holder"
                and not gate.hard_rejected
            )
            if not gate.accepted and not (bridge_fixture or plan_holder_carveout):
                logger.debug(
                    "Gate did not accept %s: %s",
                    company.get("company_name", "?"),
                    "; ".join(gate.reasons),
                )
                continue
            accepted_count += 1

            # Verified location comes ONLY from LocationVerifier evidence
            # (gate.city/state) — never from the query or source claims.
            result, reason = self._build_result(
                company,
                industry,
                expanded_keywords,
                verified_city=gate.city or "",
                verified_state=gate.state or "",
            )
            # Additive verification metadata — ConnectorResult's frozen
            # dataclass contract is preserved (Phase 2 Step 4, G).
            result = replace(
                result,
                metadata={
                    **result.metadata,
                    "verification_status": gate.verification_status.value,
                    "verification_confidence": gate.verification_confidence,
                    "source_tier": gate.source_tier,
                    "gate_accepted": gate.accepted,
                    "verification": gate.to_dict(),
                },
            )
            # Phase 3 Step 2: additive AI intelligence (ai/qualification
            # namespaces only) for gate-accepted records. Rejected, unknown,
            # and bridge-free branches are never consulted — the deterministic
            # verification verdict above stays authoritative.
            result = self._attach_ai_intelligence(
                result, company, gate, industry, location
            )
            results.append(result)
            discovery_reasons.append(reason)

        logger.info(
            "Gate: %d of %d matched companies accepted",
            accepted_count,
            len(matched[:limit]),
        )

        # The orchestrator labels data_source from its raw aggregate, but
        # Step 3 can drop every live-sourced record (e.g. a crawl returned
        # companies outside the queried city/industry), leaving a result set
        # that is entirely fixture bridge data. The label must reflect what
        # is actually returned (CLAUDE.md §1): fixture-only output is never
        # "live". Per-record provenance is the orchestrator's
        # "_discovery_source" tag, applied when the aggregate was built.
        returned_companies = matched[:limit]
        live_survivors = [
            c
            for c in returned_companies
            if c.get("_discovery_source")
            and c["_discovery_source"] != "fixture_bridge"
        ]
        if returned_companies and data_source == "live" and not live_survivors:
            data_source = "fixture"
            orch_metadata = {
                **orch_metadata,
                "data_source": "fixture",
                "bridge_mode": True,
                "fallback_reason": (
                    "live_sources_returned_no_surviving_results; "
                    "returned records are fixture bridge data"
                ),
            }

        # Step 5: Compile metadata
        metadata: dict[str, Any] = {
            "connector": self.connector_name,
            "total_in_dataset": len(companies),
            "total_matched": len(matched),
            "total_returned": len(results),
            "data_source": data_source,
            "source_metadata": orch_metadata,
            "filters_applied": {
                "state": state,
                "city": city,
                "industry": industry,
                "expanded_keywords": list(expanded_keywords),
                "limit": limit,
            },
            "discovery_reasons_sample": discovery_reasons[:5],
        }

        logger.info(
            "TexasProcurementConnector: %d returned from %d matched " "(source=%s)",
            len(results),
            len(matched),
            data_source,
        )
        return results, metadata

    def _fetch_live(
        self,
        industry: str,
        location: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Fetch live company data using SearchProviderManager.

        Executes a web search via configured search providers (SearXNG,
        Brave), then classifies each result as a contractor or rejects
        it. Falls back to empty list when no providers are configured
        or all return no results.

        Args:
            industry: Industry search term (e.g. "Roofing").
            location: Geographic location (e.g. "Dallas Texas").
            limit: Max results to request.

        Returns:
            List of company dicts from live sources, or empty list.
        """

        from app.search_providers.contractor_classifier import (
            ContractorClassifier,
        )
        from app.search_providers.manager import SearchProviderManager
        from app.search_providers.models import SearchQuery
        from app.search_providers.registry import get_registry

        registry = get_registry()
        enabled = registry.get_enabled()
        logger.info(
            "[Stage 1] Registry.get_enabled() -> %d providers",
            len(enabled),
        )
        logger.info(
            "[Stage 2] Provider names: %s",
            [p.provider_name for p in enabled],
        )
        if not enabled:
            logger.info("[Stage 7] No providers registered — returning [] immediately")
            return []

        # Build intelligent search query
        city, state = _parse_location(location)
        search_query = f"{industry} contractor {city or ''} {state}".strip()
        logger.info(
            "_fetch_live: searching %r with %d providers",
            search_query,
            len(enabled),
        )

        query = SearchQuery(
            keywords=search_query,
            num_results=limit
            * 3,  # Request extra to compensate for classification filtering
        )

        manager = SearchProviderManager(registry)
        logger.info("[Stage 5] Calling SearchProviderManager.search()...")
        try:
            response = asyncio.run(manager.search(query))
            logger.info(
                "[Stage 6] SearchResponse: status=%r provider=%r "
                "results=%d error=%r",
                response.status,
                response.provider,
                len(response.results),
                response.error[:100] if response.error else "",
            )
        except Exception:
            logger.exception("_fetch_live: search failed")
            return []

        if not response.results:
            if response.error:
                logger.info(
                    "[Stage 7] Zero results because: provider error -> %r",
                    response.error[:200],
                )
            else:
                logger.info(
                    "[Stage 7] Zero results because: no search results from providers"
                )
            return []

        # Classify and filter results
        classifier = ContractorClassifier()
        companies: list[dict[str, Any]] = []

        for result in response.results:
            classification = classifier.classify(
                name=result.title,
                title=result.title,
                description=result.snippet,
                url=result.url,
                industry_hint=industry,
            )

            if not classification["accepted"]:
                logger.debug(
                    "Rejected: %s — %s", result.title, classification["reject_reason"]
                )
                continue

            companies.append(
                {
                    "company_name": result.title,
                    "website": result.url,
                    "city": city or "",
                    "state": state,
                    "country": "USA",
                    "trade_category": classification["trade_category"],
                    "industry_focus": result.snippet or result.title,
                    "revenue_tier": "",
                    "source_url": result.url,
                    "data_provenance": f"live:{result.position}",
                    "discovery_reason": classification.get("reject_reason", ""),
                }
            )

        logger.info(
            "_fetch_live: %d accepted out of %d search results",
            len(companies),
            len(response.results),
        )
        return companies

    def _normalize_company(
        self,
        company: dict[str, Any],
        *,
        state: str,
        city: str,
    ) -> dict[str, Any]:
        """Project a source company dict onto the connector's expected schema.

        The orchestrator aggregates heterogeneous dicts: search and fixture
        sources emit ``company_name``/``industry_focus``/``trade_category``
        and their own location, while the website discovery plugin emits
        ``name``/``services``/``evidence`` and no location fields. This
        adapter maps every variant onto the keys ``_build_result`` and the
        Step-3 filters consume, so plugin records are not silently dropped.

        Location is NEVER filled from the queried *state*/*city*
        (accuracy-first, Phase 2 Step 3): the search query is search intent,
        not company location evidence. Only a source-supplied
        ``state``/``city`` survives; otherwise the fields stay empty
        (unknown) and are verified deterministically downstream.
        Provenance is preserved by aliasing the plugin's ``discovered_by``
        into ``data_provenance``.

        Args:
            company: Raw company dict from any source.
            state: Query-parsed state code — a search TARGET only, never
                written into the record.
            city: Query-parsed city — a search TARGET only, never written
                into the record.

        Returns:
            A dict in the connector's expected schema (originals intact).
        """
        normalized = dict(company)
        services = company.get("services", [])
        services_text = (
            " ".join(services) if isinstance(services, list) else str(services or "")
        ).strip()
        normalized["company_name"] = company.get("company_name") or company.get(
            "name", ""
        )
        normalized["industry_focus"] = (
            company.get("industry_focus") or services_text or company.get("title", "")
        )
        normalized["trade_category"] = company.get("trade_category", "")
        # ACCURACY-FIRST: location is never filled from the query. Only a
        # source-supplied city/state survives; otherwise empty = unknown.
        normalized["state"] = company.get("state") or ""
        normalized["city"] = company.get("city") or ""
        normalized["data_provenance"] = company.get("data_provenance") or company.get(
            "discovered_by", ""
        )
        return normalized

    def _verify_company(
        self,
        company: dict[str, Any],
        query_location: str,
    ) -> "AcceptanceResult":
        """Run the deterministic Phase 2 verification stack on one company.

        Offline by design: identity verification uses a no-op fetcher so no
        network request occurs at discovery time (official-site confirmation
        is deployment-host work — Blueprint §9.4). Placeholder/invalid/
        aggregator websites still reject deterministically WITHOUT a fetch;
        every other website becomes ``unknown`` (an evidence gap, not a
        rejection). City/state on the returned gate result come ONLY from
        LocationVerifier evidence — never from the query (Phase 2 Step 3/4).

        Args:
            company: Normalized company dict (connector schema).
            query_location: Raw query location string — a SEARCH TARGET only.

        Returns:
            An ``AcceptanceResult``; never raises.
        """
        from app.engines.verification.acceptance_gate import AcceptanceGate
        from app.engines.verification.identity_verifier import (
            FetchOutcome,
            IdentityVerifier,
        )
        from app.engines.verification.industry_verifier import IndustryVerifier
        from app.engines.verification.location_verifier import LocationVerifier

        source = company.get("_discovery_source") or self.connector_name
        source_url = company.get("source_url", "") or company.get("website", "")
        company_name = company.get("company_name", "")
        website = company.get("website", "")
        title = company.get("title", "") or company_name
        description = (
            company.get("description", "") or company.get("industry_focus", "")
        )
        address = company.get("address", "")

        def _offline_fetcher(url: str) -> FetchOutcome:  # noqa: ARG001
            """No-op fetch: offline discovery-time gating (Blueprint §9.4)."""
            return FetchOutcome(ok=False, status_code=0, page_text="")

        identity = IdentityVerifier(fetcher=_offline_fetcher).verify(
            company_name=company_name,
            website=website,
            source=source,
            source_url=source_url,
        )
        industry = IndustryVerifier().verify(
            company_name=company_name,
            title=title,
            description=description,
            website=website,
            source=source,
            source_url=source_url,
        )
        evidence_texts = [t for t in (description, title, address) if t]
        location = LocationVerifier().verify(
            city=company.get("city", ""),
            state=company.get("state", ""),
            evidence_texts=evidence_texts,
            query=query_location,
            source=source,
            source_url=source_url,
        )
        return AcceptanceGate().evaluate(
            record=company,
            identity=identity,
            industry=industry,
            location=location,
        )

    def _ai_engine_for(self) -> Any | None:
        """Return the AI engine for gate-accepted reviews (lazy, once).

        Phase 3 Step 2: resolved on first use and reused for the connector's
        lifetime. If construction fails (e.g. no AI provider configured) the
        engine is permanently skipped for this connector — ``None`` — so AI
        can never break deterministic discovery.
        """
        if self._ai_engine_resolved:
            return self._ai_engine
        self._ai_engine_resolved = True
        try:
            from app.engines.ai_engine import AIEngine  # noqa: PLC0415

            self._ai_engine = AIEngine()
        except Exception as exc:  # noqa: BLE001 — AI must never break discovery
            logger.warning(
                "AI engine unavailable; AI intelligence skipped after "
                "deterministic acceptance (verdict unchanged): %s",
                exc,
            )
            self._ai_engine = None
        return self._ai_engine

    def _attach_ai_intelligence(
        self,
        result: ConnectorResult,
        company: dict[str, Any],
        gate: Any,
        industry: str,
        location: str,
    ) -> ConnectorResult:
        """Attach additive AI intelligence to a gate-accepted result (Phase 3 Step 2).

        Connects the deterministic :class:`AcceptanceGate` result to
        ``AIEngine.intelligence_for`` at the repository's only production
        junction where ``gate.accepted is True`` is known (the Step-4 loop of
        ``search``). AI runs ONLY for accepted records; rejected, unknown,
        and bridge fixture data never consult AI. Everything AI produces
        lives under its own ``ai`` / ``qualification`` metadata namespaces and
        is merged additively — the ``verification`` namespace stays
        authoritative and the frozen ``ConnectorResult`` contract is
        preserved. Returns *result* unchanged when AI is not applicable.
        """
        if not (gate is not None and gate.accepted):
            return result
        engine = self._ai_engine_for()
        if engine is None:
            return result
        try:
            intelligence = engine.intelligence_for(
                record=company,
                gate=gate,
                query={"industry": industry, "location": location},
            )
        except Exception as exc:  # noqa: BLE001 — AI must never break discovery
            logger.warning(
                "AI intelligence failed for %s; verification unchanged: %s",
                company.get("company_name", "?"),
                exc,
            )
            return result
        if not intelligence:
            return result
        return replace(result, metadata={**result.metadata, **intelligence})

    def _build_result(
        self,
        company: dict[str, Any],
        search_industry: str,
        expanded_keywords: set[str],
        *,
        verified_city: str | None = None,
        verified_state: str | None = None,
    ) -> tuple[ConnectorResult, str]:
        """Build a ConnectorResult with meaningful discovery reason.

        Args:
            company: Raw company data dictionary.
            search_industry: Original industry search term.
            expanded_keywords: Set of expanded keywords used for matching.
            verified_city: Optional LocationVerifier-verified city. When
                provided it REPLACES the record's claim on the result
                (Phase 2 Step 4: location comes only from evidence).
            verified_state: Optional LocationVerifier-verified state.

        Returns:
            Tuple of (ConnectorResult, discovery_reason_string).
        """
        trade_cat = company.get("trade_category", "")
        # Phase 2 Step 4: the result's location is the VERIFIED one when the
        # acceptance gate ran; otherwise the source-supplied value survives.
        city = verified_city if verified_city is not None else company.get("city", "")
        state = (
            verified_state if verified_state is not None else company.get("state", "")
        )
        industry_focus = company.get("industry_focus", "")
        website = company.get("website", "")

        # Generate discovery reason
        reason = self._generate_discovery_reason(
            trade_cat=trade_cat,
            city=city,
            industry_focus=industry_focus,
            search_industry=search_industry,
        )

        # Validate and clean URL
        verified_url = _verify_and_clean_url(website)
        confidence = 0.85 if verified_url else 0.60

        metadata: dict[str, Any] = {
            "industry_focus": industry_focus,
            "revenue_tier": company.get("revenue_tier", ""),
            "trade_category": trade_cat,
            "data_provenance": company.get("data_provenance", ""),
            "verified_url": bool(verified_url),
            "matched_keywords": self._find_matched_keywords(
                company, expanded_keywords
            ),
            "discovery_reason": reason,
        }
        # Inc 3: the plan-holder pre-bound person/emails ride through on the
        # result metadata so the LeadPipeline bridge can consume them without
        # re-crawling the (unverified, often derived) website.
        plan_holder = company.get("plan_holder")
        if plan_holder:
            metadata["plan_holder"] = plan_holder

        result = ConnectorResult(
            company_name=company.get("company_name", ""),
            website=verified_url or website,
            city=city,
            # Accuracy-first (Phase 2 Step 3): no TX default — state comes
            # only from the record (which is never filled from the query).
            state=state,
            country=company.get("country", "USA"),
            source=self.connector_name,
            source_url=company.get("source_url", website),
            confidence=confidence,
            metadata=metadata,
        )
        return result, reason

    def _find_matched_keywords(
        self,
        company: dict[str, Any],
        expanded_keywords: set[str],
    ) -> list[str]:
        """Find which expanded keywords matched this company.

        Args:
            company: Company data dictionary.
            expanded_keywords: Set of expanded keywords.

        Returns:
            List of matching keywords (max 3).
        """
        text = (
            f"{company.get('company_name', '')} " f"{company.get('industry_focus', '')}"
        ).lower()
        matched = [kw for kw in expanded_keywords if kw in text]
        return matched[:3]

    def _generate_discovery_reason(
        self,
        trade_cat: str,
        city: str,
        industry_focus: str,
        search_industry: str,
    ) -> str:
        """Generate a human-readable discovery reason.

        Args:
            trade_cat: Company's trade category.
            city: Company's city.
            industry_focus: Company's stated industry focus.
            search_industry: Original user search term.

        Returns:
            Human-readable reason string.
        """
        city_part = f" {city}" if city else ""
        trade_label = (
            trade_cat.replace("_", " ").title()
            if trade_cat
            else search_industry.title()
        )
        search_title = search_industry.title()

        if trade_cat:
            return f"Matched {city_part.strip()} {trade_label.lower()} contractor"

        if industry_focus:
            focus_words = set(re.findall(r"[a-z]+", industry_focus.lower()))
            search_words = set(re.findall(r"[a-z]+", search_industry.lower()))
            overlap = focus_words & search_words
            if overlap:
                best_match = max(overlap, key=len)
                return f"Matched {city_part.strip()} {best_match.title()} contractor"

        return f"Matched {city_part.strip()} {search_title.lower()} contractor"

    def health_check(self) -> bool:
        """Check if this connector can operate.

        Returns:
            True if the connector is available.
        """
        return len(self._companies) > 0

    def validate_result(self, result: ConnectorResult) -> bool:
        """Validate a single connector result.

        Args:
            result: The connector result to validate.

        Returns:
            True if the result has valid company_name and website.
        """
        if not result.company_name or len(result.company_name.strip()) < 3:
            return False
        if not result.website:
            return False
        parsed = urlparse(result.website)
        return bool(parsed.netloc)


# ---------------------------------------------------------------------------
# Location Parsing Helpers
# ---------------------------------------------------------------------------

_STATE_MAP: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}


def _parse_location(location: str) -> tuple[str | None, str]:
    """Parse location string into (city, state).

    Handles formats like:
    - "Dallas Texas" -> ("dallas", "TX")
    - "Houston, TX" -> ("houston", "TX")
    - "Dallas" -> (None, "TX")  # default to TX for Texas Procurement
    - "TX" -> (None, "TX")

    Args:
        location: Location string from API query.

    Returns:
        Tuple of (city, state) where city may be None.
    """
    location = re.sub(r"\s+", " ", location.strip()).lower()
    state = "TX"
    city_loc = location

    # Find full state name
    for state_code, state_name in _STATE_MAP.items():
        pattern = rf"\b{re.escape(state_name.lower())}\b"
        if re.search(pattern, location):
            state = state_code
            city_loc = re.sub(pattern, "", location).strip()
            break

    # Find state code abbreviation
    if state == "TX":
        for state_code in _STATE_MAP:
            pattern = r"(?:^|[\s,;])" + state_code + r"(?:$|[\s,;])"
            if re.search(pattern, location, re.IGNORECASE):
                state = state_code
                city_loc = re.sub(pattern, " ", location, flags=re.IGNORECASE).strip()
                break

    # Clean up city
    for state_name in _STATE_MAP.values():
        city_loc = re.sub(r"\b" + re.escape(state_name.lower()) + r"\b", "", city_loc)

    city = re.sub(r"^[,\s;]+|[,\s;]+$", "", city_loc).strip() or None
    return city, state


# ---------------------------------------------------------------------------
# URL Validation Helpers
# ---------------------------------------------------------------------------


def _verify_and_clean_url(url: str) -> str | None:
    """Verify and clean a URL for use in ConnectorResult.

    Returns the cleaned URL if it appears valid, otherwise None.

    Args:
        url: Raw URL string.

    Returns:
        Cleaned URL or None if invalid.
    """
    if not url:
        return None

    cleaned = url.strip()
    if not cleaned.startswith(("http://", "https://")):
        cleaned = "https://" + cleaned

    try:
        parsed = urlparse(cleaned)
        if not parsed.netloc:
            return None
        if "." not in parsed.netloc:
            return None
        return cleaned
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Auto-registration on import
# ---------------------------------------------------------------------------
from app.connectors.connector_registry import ConnectorRegistry

ConnectorRegistry.register(TexasProcurementConnector())

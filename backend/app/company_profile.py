"""The company profile — who we are, and who is (and is NOT) a target.

Why this exists
---------------
The "fiber construction" leak (and the class of off-vertical junk behind it)
had three causes: discovery searches the broad trade string with no vertical
boundary, the acceptance gate only asks "is construction?" (binary — a
fiber-optics contractor IS construction, so it sails through), and the AI gets
the *seller* identity ("The Best Estimator LLC") but never the *target*
definition. This module is the SINGLE definition of the vertical boundary
(CLAUDE.md §7 root cause, §14 one source of truth), consumed by:

* the acceptance / re-gate layer (``scoring.regate_recommendation``) — a
  dossier whose industry is explicitly OFF-vertical is hard-skipped;
* the live deep-dive gate (``agent.AILeadResearchAgent``) — an off-vertical
  lead never spends deep-research credits;
* the research prompts ('prompts.py') — the AI is told what our company is,
  what we sell, who the ideal client is, and which verticals are NOT targets,
  replacing the hardcoded "The Best Estimator LLC (Texas)".

Design (deliberate, CLAUDE.md §11):
* The gate REJECTS only on an EXPLICIT off-vertical match (fiber / telecom /
  utility-infra / road-bridge / oil-gas-pipeline / materials-only supplier).
  A company with an unknown or vague industry is NOT hard-rejected here — the
  existing construction keyword gate still applies, and re-rejecting on a
  guess would starve the funnel. Quality-first, never funnel-starving.
* "Building trades only" (the user's chosen boundary) lives as the
  ``target_trades`` allow-list used to *raise* confidence and to teach the
  discovery + self-teach layer later — not as a separate hard reject.
* The profile is configurable via ``backend/company_profile.json`` (no
  secrets; defaults match the current Best Estimator LLC identity), so the
  boundary can be tightened without touching code. Invalid JSON falls back to
  defaults with a LOGGED warning (CLAUDE.md §6 — never silent).

Files
-----
Default: ``backend/company_profile.json`` (overrides every field; missing file
or a missing field falls back to the defaults below).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def default_profile_path() -> str:
    """``backend/company_profile.json`` — the editable, commitable config file."""
    return os.path.join(os.path.dirname(__file__), "..", "company_profile.json")


#: Keyword vernaculars for the CONSTRUCTION-trade detection (kept in sync with
#: the pre-existing lists in lead_research/agent.py + scoring.py — see below).
#: ``_is_construction`` already decides "is this construction at all?"; this
#: profile decides "is it OUR vertical?" — two different questions.
_TARGET_TRADE_DEFAULTS = (
    "general contractor", "construction", "subcontractor", "builder",
    "building", "commercial", "residential", "roofing", "electrical",
    "plumbing", "hvac", "mechanical", "masonry", "concrete", "framing",
    "drywall", "painting", "flooring", "glazing", "landscaping",
    "site work", "excavat", "demolition", "steel", "structural",
)

#: Vernaculars that flag a company as OFF our vertical that is still
#: "construction" by the binary gate — the exact class of the fiber leak.
#: Fiber-optic / telecom / utility infrastructure / roads & bridges /
#: pipeline & oil-gas / materials-only suppliers-distributors.
_EXCLUDED_VERNACULAR_DEFAULTS = (
    "fiber", "fibre", "optic", "telecom", "telecommunication", "internet",
    "broadband", "utility", "utilities", "pipeline", "transmission line",
    "substation", "power line", "electrical grid", "highway", "roadway",
    "road construction", "bridge", "rail", "railway", "transit", "airport",
    "oil and gas", "midstream", "gas pipeline", "liquid natural gas", "lng",
    "wind farm", "solar farm", "material", "materials", "supply", "supplier",
    "distributor", "distribution", "wholesale", "manufacturer", "fabricator",
    "rental", "equipment dealer", "tools",
)

#: Vernaculars that mark a company — or a discovery-time lead — as NOT one of
#: our clients even though it may touch construction: A/E/C consultants and
#: engineering/architecture/design firms, trade associations & plan services,
#: software/IT, environmental/compliance, recruiting, insurance, logistics &
#: trucking, transit/mobility providers, conglomerates. Derived from the actual
#: off-vertical nurture/contact_now junk found in the live store (2026-09-08
#: audit — Jacobs/Parsons/HNTB AEC firms, Dodge software, Builders' Exchanges,
#: a dedicated DCTA transit vendor list) so the boundary is evidence-tight.
#:
#: Deliberately PHRASE-precise: single words like "engineering", "architecture"
#: or "design" are NOT here — "General Engineering Contractor" and a
#: "Design-Build" builder are legitimate clients, so only terms that can never
#: belong to an ACTIVE building-trades bidder earn a spot. The same list is
#: applied at two granularities (CLAUDE.md §11 one definition):
#:   * industry labels  — ``CompanyProfile.is_non_client`` (research + re-gate)
#:   * lead strings     — ``CompanyProfile.lead_is_non_client`` (ingestion:
#:                         company + domain + source-url host, see below)
_NON_CLIENT_TERMS_DEFAULTS = (
    "consulting", "consultancy", "consultant",
    "engineering firm", "design firm", "architecture firm",
    "engineering services", "epcm", "a/e/c", "aec consulting",
    "association", "builders exchange", "plan service", "chamber of commerce",
    "software", "it services", "technology solutions", "computer", "digital",
    "transit", "mobility", "fare", "autonomous", "shuttle", "parking",
    "tolling", "environmental", "compliance", "recruiting", "staffing",
    "insurance", "trucking", "logistics", "warehousing",
    "conglomerate", "aerospace",
    "newspaper", "media publishing", "news publication",
    "real estate", "realty", "property management",
    "junk removal", "hauling",
    "medical", "healthcare", "dental", "chiropractic",
    "legal", "law firm", "attorney",
    "funeral",
    "marketing", "advertising", "digital agency",
    "education", "publishing",
)

#: Discovery-source hosts that publish NON-construction procurement — transit /
#: mobility / maritime authorities rather than building plan lists. Leads parsed
#: from them (mobility-tech vendors, contract-ID row labels) are dropped at
#: ingestion instead of researched (root cause of the 54-lead DCTA transit junk
#: in the live cache). This is the CONFIG seed; Phase B's source-quality learner
#: extends it from live verdicts (CLAUDE.md §10 — never a hidden hardcode).
_NON_CLIENT_SOURCE_HOSTS_DEFAULTS = (
    "dcta.net", "tjpa.org", "trideltatransit.com", "sfport.com",
    "thepaperboy.news", "piercecountyjournal.news", "carmelpinecone.com",
)


#: Persona facts injected into the research prompts — replaces the hardcoded
#: "The Best Estimator LLC (Texas)" so the AI is told what we are, what we sell,
#: and who is NOT a target (CLAUDE.md §6 honest context, never silence).
_PROMPT_DEFAULTS = {
    "company_name": "The Best Estimator LLC",
    "location": "United States (nationwide)",
    "what_we_sell": (
        "preconstruction / construction estimation services: bid preparation, "
        "cost estimation, quantity takeoffs"
    ),
    "ideal_client": (
        "an active construction company (general contractor, subcontractor, or "
        "developer) that bids on buildings and projects but may lack in-house "
        "estimation capacity"
    ),
}


class CompanyProfile:
    """Immutable value object describing the company + its target boundary."""

    def __init__(
        self,
        *,
        company_name: str,
        location: str,
        what_we_sell: str,
        ideal_client: str,
        target_trades: tuple[str, ...],
        excluded_vernaculars: tuple[str, ...],
        non_client_terms: tuple[str, ...] | None = None,
        non_client_source_hosts: tuple[str, ...] | None = None,
    ) -> None:
        self.company_name = company_name.strip() or _PROMPT_DEFAULTS["company_name"]
        self.location = location.strip() or _PROMPT_DEFAULTS["location"]
        self.what_we_sell = what_we_sell.strip() or _PROMPT_DEFAULTS["what_we_sell"]
        self.ideal_client = ideal_client.strip() or _PROMPT_DEFAULTS["ideal_client"]
        self.target_trades = tuple(t.strip().lower() for t in target_trades if t.strip())
        self.excluded_vernaculars = tuple(
            v.strip().lower() for v in excluded_vernaculars if v.strip()
        )
        self.non_client_terms = tuple(
            t.strip().lower() for t in (non_client_terms or _NON_CLIENT_TERMS_DEFAULTS) if t.strip()
        )
        self.non_client_source_hosts = tuple(
            h.strip().lower() for h in (non_client_source_hosts or _NON_CLIENT_SOURCE_HOSTS_DEFAULTS)
            if h.strip()
        )

    # -- target vertical ----------------------------------------------------

    def is_off_vertical(self, industry: str) -> bool:
        """True when the company's industry is EXPLICITLY outside our vertical.

        Conservative by design: only an explicit excluded-vernacular match
        returns True. Unknown / vague / empty industries return False — they
        stay gated by the existing construction keyword check downstream, never
        by a guess here (CLAUDE.md §11 — don't starve the funnel to look tidy).
        """
        ind = (industry or "").lower().strip()
        if not ind:
            return False
        # A materials-only marker ("supply", "distribution", "wholesale",
        # "manufacturer"...) matched against the industry label is our strongest
        # signal: a supplier is not a bidding contractor, so it can never be an
        # estimation-services client regardless of the words around it.
        return any(v in ind for v in self.excluded_vernaculars)

    def is_target_trade(self, industry: str) -> bool:
        """True when the industry names one of our target building trades."""
        ind = (industry or "").lower().strip()
        return bool(ind) and any(t in ind for t in self.target_trades)

    def is_non_client(self, industry: str) -> bool:
        """True when the industry names a company that is NOT one of our clients
        even though it may touch construction (the A/E/C consultant, trade
        association, software/IT, transit/mobility class).

        Same conservative contract as :meth:`is_off_vertical`: only an EXPLICIT
        non-client-term match returns True; unknown/vague/empty industries pass
        so the funnel is never starved on a guess. The terms are PHRASES — a
        single "engineering" or "design" word would false-reject a legitimate
        'General Engineering Contractor' or a design-builder.
        """
        ind = (industry or "").lower().strip()
        if not ind:
            return False
        return any(t in ind for t in self.non_client_terms)

    def lead_is_non_client(
        self, *, company: str = "", domain: str = "", source_url: str = ""
    ) -> bool:
        """True when a DISCOVERY-TIME lead is clearly not one of our clients.

        Ingestion has no AI-derived industry yet, so the verdict uses the ONLY
        signals available: the company string, the email domain, and the
        discovery source's host. Source hosts matter because a transit/mobility
        procurement list (DCTA) yields leads whose company cell is a contract ID
        (``1305-009``) and whose domains belong to mobility vendors — neither
        carries a construction keyword. The source-host seed list is deliberate
        config (CLAUDE.md §6 honest, logged), extended by Phase B's learner.
        """
        blob = " ".join([(company or ""), (domain or "")]).lower()
        if any(t in blob for t in self.non_client_terms):
            return True
        host = (source_url or "").lower()
        return any(h in host for h in self.non_client_source_hosts)

    # -- prompt context -----------------------------------------------------

    def prompt_context(self) -> str:
        """A compact, honest persona block injected into every research prompt.

        Replaces the hardcoded "The Best Estimator LLC (Texas)" so the AI shares
        ONE current definition of what we sell, who the ideal client is, and
        which verticals are explicitly NOT targets (the fiber/telecom/utility/
        road-class that used to be scored as leads).
        """
        excluded = ", ".join(sorted(self.excluded_vernaculars)) or "none configured"
        non_client = ", ".join(sorted(self.non_client_terms)) or "none configured"
        return (
            f"Our company: {self.company_name} ({self.location}). "
            f"We sell {self.what_we_sell}.\n"
            f"Ideal client: {self.ideal_client}.\n"
            f"NOT our clients: companies in the {excluded} "
            "vertical, and materials-only suppliers/distributors.\n"
            "ALSO not our clients, even when they touch construction: "
            f"{non_client} — an active building-trades bidder is our client, a "
            "consultant/association/software/transit company is not. If the "
            "company is any of these, say so honestly — estimating services are "
            "not for them."
        )


def _coerce_list(value: Any, field: str) -> tuple[str, ...]:
    """Accept a JSON list, an internal ``*_DEFAULTS`` tuple, or a
    comma/pipe-separated string for list fields."""
    if isinstance(value, str):
        return tuple(part.strip() for part in value.replace("|", ",").split(",") if part.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    logger.warning("COMPANY PROFILE: %s ignored (expected list of strings)", field)
    return tuple()


def load_profile(data: dict[str, Any] | None = None) -> CompanyProfile:
    """Build the profile from an optional dict (the parsed company_profile.json).

    Every field falls back to a default independently, so a partially edited
    file still loads (never crashes, never silent — a missing/invalid field is
    logged). ``data=None`` means "pure defaults".
    """
    d = data or {}
    return CompanyProfile(
        company_name=d.get("company_name", _PROMPT_DEFAULTS["company_name"]),
        location=d.get("location", _PROMPT_DEFAULTS["location"]),
        what_we_sell=d.get("what_we_sell", _PROMPT_DEFAULTS["what_we_sell"]),
        ideal_client=d.get("ideal_client", _PROMPT_DEFAULTS["ideal_client"]),
        target_trades=_coerce_list(d.get("target_trades") or _TARGET_TRADE_DEFAULTS, "target_trades"),
        excluded_vernaculars=_coerce_list(
            d.get("excluded_vernaculars") or _EXCLUDED_VERNACULAR_DEFAULTS,
            "excluded_vernaculars",
        ),
        non_client_terms=_coerce_list(
            d.get("non_client_terms") or _NON_CLIENT_TERMS_DEFAULTS,
            "non_client_terms",
        ),
        non_client_source_hosts=_coerce_list(
            d.get("non_client_source_hosts") or _NON_CLIENT_SOURCE_HOSTS_DEFAULTS,
            "non_client_source_hosts",
        ),
    )


# Cached defaults — the profile has no secrets and the default boundary is a
# constant of the codebase, so a module-level default is safe and cheap.
_DEFAULT_PROFILE = load_profile(None)

_PROFILE_LOCK = threading.Lock()
_PROFILE: CompanyProfile = _DEFAULT_PROFILE
_PROFILE_LOADED = False
_PROFILE_PATH: str | None = None


def get_profile() -> CompanyProfile:
    """The current profile: defaults overlaid by ``company_profile.json``.

    Loaded once, cached. Set path explicitly (tests) via :func:`set_profile_path`.
    """
    global _PROFILE, _PROFILE_LOADED, _PROFILE_PATH
    if _PROFILE_LOADED:
        return _PROFILE
    with _PROFILE_LOCK:
        if _PROFILE_LOADED:
            return _PROFILE
        _PROFILE = load_profile(_read_config(_PROFILE_PATH or default_profile_path()))
        _PROFILE_LOADED = True
    return _PROFILE


def set_profile_path(path: str | None) -> None:
    """Point the singleton at a specific JSON file (tests / alternate deploy)."""
    global _PROFILE_PATH, _PROFILE_LOADED
    with _PROFILE_LOCK:
        _PROFILE_PATH = path
        _PROFILE_LOADED = False


def _read_config(path: str) -> dict[str, Any]:
    """Parse the profile JSON; malformed file -> defaults with a LOGGED warning."""
    try:
        p = Path(path)
        if not p.exists():
            return {}
        raw = p.read_text(encoding="utf-8")
        if not raw.strip():
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            logger.warning("COMPANY PROFILE %s: expected a JSON object; using defaults", path)
            return {}
        return parsed
    except (OSError, ValueError) as exc:
        logger.warning("COMPANY PROFILE %s: unreadable (%s); using defaults", path, exc)
        return {}
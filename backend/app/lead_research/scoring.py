"""AI Lead Research — Scoring + Recommendation (Stage 4).

Deterministic signal-based scoring — NO LLM call required.

Each lead attribute contributes a weighted signal to the final score.
The deterministic gate is the FINAL authority on recommendation.

Credit optimization: replaced LLM scoring with rule-based system
(~20 LLM calls saved per job, ~25% credit reduction).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from app.lead_research.models import (
    AIEvidence,
    CompanyProfile,
    IntentAssessment,
    LeadDossier,
    PersonFindings,
    TIMING_WINDOWS,
    TimingAssessment,
)

logger = logging.getLogger(__name__)


class AIAskFn(Protocol):
    def __call__(self, prompt: str) -> str: ...


# Construction-related industry keywords
_CONSTRUCTION_KEYWORDS = (
    "contractor", "construction", "general contractor", "subcontractor",
    "gc", "builder", "building", "estimating", "preconstruction",
    "develop", "developer", "site work", "paving", "concrete", "asphalt",
    "excavat", "earthwork", "demolition", "framing", "roofing", "electrical",
    "plumbing", "mechanical", "hvac", "masonry", "steel", "civil",
    "engineering", "grading", "landscaping", "insulation", "drywall",
    "painting", "flooring", "glazing", "waterproofing",
)


def _is_construction(industry: str) -> bool:
    """Return True if industry string indicates construction-related work."""
    ind = (industry or "").lower()
    return any(kw in ind for kw in _CONSTRUCTION_KEYWORDS)


#: The ``dossier.fit`` prefixes the PRE-SCORING gates in
#: ``app.lead_research.agent`` write when they abandon a lead — the junk-domain,
#: free-mail, dead-domain, pre-verdict and not-a-client shortcuts, every one of
#: which returns before Stage 4 and so leaves ``potential_score`` at its
#: dataclass default. :func:`has_measured_score` is the ONE reader of this list;
#: adding a new early-return gate means adding its prefix here, or
#: :func:`rescore_dossier` will score a lead that was never researched.
#:
#: ``"Generic email — "`` is written by no current code — the 60 rows carrying
#: it are legacy from a stage that has since been removed, kept because the
#: rows are still in the store.
_ABANDONED_FIT_PREFIXES = (
    "Not our client — ",
    "Free mail domain — ",
    "Junk/placeholder address — ",
    "Dead/expired domain — ",
    "Generic email — ",
)


def _is_service_area(location: str) -> bool:
    """Return True if location is in our primary service area (Texas).

    A STATE FOLD, not a substring test (fixed 2026-09-21). This used to be
    ``"texas" in loc or "tx" in loc``, which is a THIRD answer to a question
    the codebase already had two answers to — and the wrong kind of answer: it
    credited any string merely CONTAINING "tx", and it read a bare state name
    anywhere in a compound string as proof. Measured on the live store, that
    handed the Texas service-area signal to 14 dossiers whose real home is
    elsewhere ("Dallas, TX (corporate HQ: Tustin, CA)", "Amsterdam,
    Netherlands (with Dallas, TX office)").

    ``state_from_text`` is the single fold, the same one ``serve_shared`` uses
    to decide which state's run a dossier may be served to, so the score and
    the serve filter can no longer disagree about one string.

    ``location`` is the AI's Stage-1 claim, NOT verified evidence — the
    discovery path verifies location through ``LocationVerifier``, this path
    does not. The fold is honest about that: anything it cannot resolve to
    exactly one state returns "" , and "" is not Texas.
    """
    from app.engines.verification.location_verifier import state_from_text

    return state_from_text(location) == "TX"


class LeadScorer:
    """Stage 4: deterministic signal-based scoring.

    Each lead attribute contributes a weighted signal:
    - Construction industry:      +2.0
    - Person bound (reachable):   +1.5
- Person has relevant role:   +1.0
    - Needs estimation (yes):     +2.0
    - Intent signal present:      +1.0
    - Timing window reported:     +0.5   (only now/soon/later — "unknown" is
                                         the failure sentinel, see
                                         ``TIMING_WINDOWS``)
    - Service area (Texas):       +0.5
    - Has evidence facts:         +0.5
    - Non-construction industry:  -1.0

    Max possible: 9.0  |  Min possible: -1.0
    """

    def __init__(self, *, ai_ask: AIAskFn | None = None) -> None:
        # Kept for backward compatibility (tests inject ai_ask).
        # The deterministic scorer does not use it.
        self._ai_ask = ai_ask

    def score(
        self,
        email: str,
        company: CompanyProfile,
        person: PersonFindings,
        intent: IntentAssessment,
        timing: TimingAssessment,
        *,
        log: bool = True,
    ) -> tuple[str, float, str]:
        """Score the lead deterministically. Returns (fit, potential_score, recommendation).

        ``log=False`` silences the per-lead INFO line for READ-time callers
        (:func:`rescore_dossier` runs once per row of every leads page); the
        research path keeps it, because there it is progress, not noise.
        """
        signals: list[tuple[str, float]] = []

        # --- Company signals ---
        if company.name:
            if _is_construction(company.industry):
                signals.append(("construction industry", 2.0))
            elif company.industry:
                signals.append(("related industry", 0.5))
            else:
                signals.append(("company identified", 0.5))
        else:
            signals.append(("no company info", -1.0))

        # Non-construction penalty
        if company.industry and not _is_construction(company.industry):
            signals.append(("non-construction", -1.0))

        # --- Person signals ---
        if person.bound:
            signals.append(("reachable person", 1.5))
        if person.name and person.role_relevance:
            signals.append(("relevant role", 1.0))
        # Generic email backstop: a real-domain info@/admin@ address that
        # passed company research (Stage 1) but skipped person research is
        # still a valid construction company worth keeping for outreach.
        # Without this signal, the unbound person caps the score at 2.5
        # (construction 2.0 + facts 0.5), missing the 3.0 nurture threshold.
        elif company.name and not person.bound and company.industry:
            signals.append(("generic company contact", 0.5))

        # --- Intent signals ---
        if (intent.needs_estimation or "").lower() in ("yes", "likely", "probable"):
            signals.append(("needs estimation", 2.0))
        if intent.signal:
            signals.append(("intent signal", 1.0))

        # --- Timing signals ---
        # An ALLOWLIST, not a truthiness test. ``window`` is free text whose
        # default is the string "unknown" — which ``intent_timing`` writes when
        # the AI call failed or returned something unparseable — and "unknown"
        # is truthy, so ``if timing.window`` paid this bonus for a call that
        # never answered. A failure must not score better than silence.
        if (timing.window or "").strip().lower() in TIMING_WINDOWS:
            signals.append(("timing window", 0.5))

        # --- Location signal ---
        if _is_service_area(company.location):
            signals.append(("service area", 0.5))

        # --- Evidence signal ---
        if company.facts:
            signals.append(("has evidence", 0.5))

        # Calculate score
        raw_score = sum(val for _, val in signals)
        potential_score = round(max(-1.0, min(10.0, raw_score)), 1)

        # Generate fit description
        fit = self._build_fit_text(signals, company, person)

        # Deterministic gate (hard rules — construction-only, relevant role,
        # verifiable location required)
        recommendation = self._gate(person, potential_score, company.industry, company.location)

        if log:
            logger.info(
                "Scoring %s: score=%.1f rec=%s signals=%s",
                email, potential_score, recommendation,
                [s[0] for s in signals],
            )

        return fit, potential_score, recommendation

    def _build_fit_text(
        self,
        signals: list[tuple[str, float]],
        company: CompanyProfile,
        person: PersonFindings,
    ) -> str:
        """Generate a human-readable fit assessment from signals."""
        if not signals:
            return "Insufficient data to assess fit."

        pos = [s[0] for s in signals if s[1] > 0]
        neg = [s[0] for s in signals if s[1] < 0]

        parts: list[str] = []
        if company.name:
            parts.append(company.name)
        if pos:
            parts.append("identified with " + ", ".join(pos[:3]))
        if neg:
            parts.append("but " + ", ".join(neg))

        return ". ".join(parts) + "." if parts else "Partial information available."

    def _gate(
        self,
        person: PersonFindings,
        potential_score: float,
        industry: str = "",
        location: str = "",
    ) -> str:
        """Deterministic gate — final authority on recommendation.

        HARD RULES (founder):
        - A KNOWN non-construction company is NEVER a qualified lead → skip
          (The Best Estimator sells to construction — an IT company is worthless).
        - contact_now requires score >= 6.0 AND a bound person AND a relevant
          role (decision-maker who owns estimation/bidding — not an IT/support
          contact). Without a relevant role, even a high score caps at nurture.
        - contact_now also requires a non-empty, verified location — never call
          a decision-maker we cannot geolocate (nsarro@unitedcr.com bug:
          fabricated "Insurance Restoration Contractor" + no location = 9.0
          contact_now, which is wrong).
        - nurture: score >= 3.0
        - skip: below 3.0
        """
        ind = (industry or "").lower()
        # Hard reject: explicitly non-construction industry (never contact_now
        # even with a high score — the -1.0 signal penalty alone is not enough).
        if ind and not _is_construction(ind):
            return "skip"

        # Location gate: contact_now requires a verified location string.
        loc = (location or "").strip()
        if potential_score >= 6.0 and person.bound and person.role_relevance:
            if not loc:
                logger.info("Location gate: score %.1f contact_now downgraded to nurture (no verified location)", potential_score)
                return "nurture"
            return "contact_now"
        elif potential_score >= 3.0:
            return "nurture"
        else:
            return "skip"

    def _default_score(self, person: PersonFindings) -> tuple[str, float, str]:
        """Default score when scoring fails."""
        if person.bound and person.name:
            return ("Partial info — person identified but scoring unavailable", 3.0, "nurture")
        return ("Unable to assess — scoring unavailable", 0.0, "skip")


def has_measured_score(dossier: LeadDossier) -> bool:
    """True when ``potential_score`` is a MEASUREMENT, not a leftover default.

    ``LeadDossier.potential_score`` defaults to ``0.0`` and is only overwritten
    by Stage 4. Every gate that returns BEFORE Stage 4 — the junk-domain,
    free-mail, dead-domain, pre-verdict and not-a-client shortcuts in
    ``app.lead_research.agent`` — therefore persists ``0.0`` for a lead whose
    research never happened, and that ``0.0`` is indistinguishable from a real
    score by value alone (a scored dossier can legitimately total 0.0 too).

    That distinction is load-bearing for :func:`rescore_dossier`: re-deriving a
    score for an abandoned lead would compute a number from fields that were
    never researched and present it as a measurement. Measured on the live
    store 2026-09-21 — 397 of 1179 dossiers are abandoned this way, and
    re-scoring them manufactured a passing score for 43 companies the pipeline
    had explicitly decided are not our clients.

    ``fit`` is the discriminator, and it is exact, not a guess: every gate that
    abandons a lead writes its reason there, so an abandoned dossier carries
    one of these prefixes and a scored one carries ``_build_fit_text``'s
    sentence instead. Verified across all 1179 stored rows: 397 triage fits all
    at score 0.0, 782 scorer fits, ZERO rows with a triage fit and a score.
    ``"Generic email — "`` is in the list although no current code writes it —
    those 60 rows are legacy from a removed stage, which is exactly why this is
    a compatibility shim and not a permanent representation.

    NOTE (RC-8, fixed 2026-09-22): this used to add that ``agent.py``'s
    not-a-client shortcut also appended ``"scoring"`` to ``sources_checked``
    although the scorer never ran, which is why ``sources_checked`` could not
    be the marker. That false append is now removed, so the two markers agree
    — but ``fit`` remains the discriminator anyway, because the 397 rows
    written before the fix still carry the phantom ``"scoring"`` and because
    a stored field can never be re-derived, only read.
    """
    fit = (dossier.fit or "").strip()
    if not fit:
        return False
    return not fit.startswith(_ABANDONED_FIT_PREFIXES)


def rescore_dossier(dossier: LeadDossier) -> float:
    """Re-derive a STORED dossier's potential score from TODAY'S weights.

    The score is written ONCE, at research time. :func:`regate_verdict` has
    always re-applied today's hard gates at read time — but it compared them
    against that STORED number, so a formula correction reached the gate and
    never reached the number the gate compares, nor the number the leads page
    shows. Measured on the live store before the 2026-09-21 fixes: 1058 of 1179
    dossiers carry the ``"unknown"`` timing sentinel and were paid a bonus for a
    call that never answered; 27 of them sit exactly on a threshold that bogus
    +0.5 carried, and 6 change class.

    An ABANDONED dossier is returned unchanged (see
    :func:`has_measured_score`) — there is no measurement to correct, and
    inventing one is worse than a stale number would be.

    REUSE, not a second formula (CLAUDE.md §14): this calls the SAME
    ``LeadScorer`` the research path calls with the SAME five inputs
    (``agent.py`` Stage 4), so the displayed score and a fresh research of the
    same lead can never disagree. ``log=False`` because this runs on the READ
    path, once per row of every page.

    Only the score is returned: the recommendation is NOT ``LeadScorer._gate``'s
    to give here. :func:`regate_verdict` applies gates this class does not have
    (off-vertical, non-client, the identity backstop, and a role relevance
    recomputed from the role STRING rather than the stored AI-era flag), so it
    keeps owning the verdict and consumes this score.
    """
    if not has_measured_score(dossier):
        return float(dossier.potential_score or 0.0)
    _, score, _ = LeadScorer().score(
        dossier.email,
        dossier.company,
        dossier.person,
        dossier.intent,
        dossier.timing,
        log=False,
    )
    return score


def regate_verdict(dossier: LeadDossier) -> tuple[str, float]:
    """TODAY'S verdict for a STORED dossier: ``(recommendation, score)``.

    ONE computation for the two numbers a read path needs, because they must
    never disagree: the recommendation is gated on THIS score. Callers that
    want only the verdict use :func:`regate_recommendation`; callers that show
    a row use both.

    Old dossiers carry research-time recommendations AND research-time scores —
    researched before the deterministic role override and the non-construction
    hard skip existed, an AI could return contact_now for a "Sales
    Representative" or "Contact / Representative" at Ferguson. Without
    re-researching (zero credits), we re-apply the SAME gates the live scorer
    uses:

      1. Industry construction check        -> hard skip
      2. role_relevance recomputed from the ROLE STRING (the deterministic
         list is the authority, never the stored AI-era flag)
      3. bound + score thresholds

    Applied at read time in :func:`app.api.v1.leads.list_leads`, so the leads
    page is always an honest view of current rules — never a stale AI guess.

    The SCORE the thresholds compare is re-derived too, through
    :func:`rescore_dossier` (fixed 2026-09-21). Re-applying today's gates
    against a stale number is only half an honest view: a dossier sitting at
    the 6.0 contact_now line on a bonus the formula no longer grants was still
    served contact_now, and the page displayed the number that justified it.

    The score is computed up front, before the hard skips, so every return
    path can report it. It is deterministic and cheap (no AI, no network, no
    I/O) — the same arithmetic the research path already ran on this dossier.
    """
    from app.engines.lead.lead_models import role_is_plausibly_relevant

    from app.company_profile import get_profile

    # TODAY'S score, not the stored one — see the docstring.
    score = rescore_dossier(dossier)

    ind = (dossier.company.industry or "").lower()
    if ind and not _is_construction(ind):
        return "skip", score
    # Off-vertical hard skip (root-cause fix for the "fiber construction" leak):
    # the binary construction check passes any 'fiber/telecom/utility/road/
    # pipeline/materials' contractor that calls itself construction. The company
    # profile is the ONE definition of OUR vertical (building trades), and a
    # dossier whose industry explicitly matches an excluded vernacular is not a
    # lead — re-gated here so old dossiers are never served stale (same policy
    # as the _is_construction skip above). Conservative: unknown industries pass.
    if ind and get_profile().is_off_vertical(dossier.company.industry):
        return "skip", score
    # Non-client hard skip (root-cause fix for the "irrelevant data" flood): a
    # company that is an A/E/C consultant, trade association, software/IT firm,
    # transit/mobility provider, plan service, etc. is NOT a buyer even when it
    # touches construction. The profile's term list is the ONE definition, and
    # re-gating stored dossiers here (same display-time policy, zero credits)
    # is what keeps old off-service contacts from showing on the frontend —
    # the user's "data jo hamari services se match nahi karta" complaint.
    if ind and get_profile().is_non_client(dossier.company.industry):
        return "skip", score

    # --- Identity backstop (root-cause hardening for the 2026-09-08 purge) ---
    # All three industry gates above can only read the STORED industry label,
    # and the AI occasionally mislabels a non-client — a marine/heavy-civil
    # contractor, an A/E/C consultancy, a supplier, a bridge builder — as
    # "General Contractor". That mislabel sails past every industry gate, which
    # is exactly why 24 such dossiers were hand-purged from the live store.
    # This reads the IDENTITY strings (company name, refined company, email
    # domain — short, low-collision) against BOTH the excluded and non-client
    # lists, and the headline FACT against the phrase-precise non-client list
    # ONLY (never the excluded list: a sentence like "supply chain coordination"
    # must not martyre a legit GC). An explicit match hard-skips, same policy.
    _profile = get_profile()
    _identity = " ".join([
        dossier.company.name or "",
        dossier.refined_company or "",
        dossier.domain or "",
    ]).lower()
    if _identity and (
        any(v in _identity for v in _profile.excluded_vernaculars)
        or any(t in _identity for t in _profile.non_client_terms)
    ):
        return "skip", score
    if dossier.company.facts and any(
        t in (dossier.company.facts[0].claim or "").lower()
        for t in _profile.non_client_terms
    ):
        return "skip", score

    relevant = bool((dossier.person.role or "").strip()) and role_is_plausibly_relevant(
        dossier.person.role
    )
    loc = (dossier.company.location or "").strip()
    if score >= 6.0 and dossier.person.bound and relevant:
        if not loc:
            return "nurture", score
        return "contact_now", score
    if score >= 3.0:
        return "nurture", score
    return "skip", score


def regate_recommendation(dossier: LeadDossier) -> str:
    """The recommendation half of :func:`regate_verdict` — see it for the rules.

    Kept because most callers only gate visibility (:func:`_is_visible`,
    ``serve_shared``, ``clear_junk``) and never show a number.
    """
    return regate_verdict(dossier)[0]

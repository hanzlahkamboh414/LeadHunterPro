"""Event and evidence taxonomy for the Company Signal Intelligence Engine.

WHY THIS MODULE EXISTS (root cause, CLAUDE.md §7): the pre-existing
``IntentEvidenceType`` collapsed an entire procurement lifecycle into ONE
value. ``bid_award`` meant "won a bid / contract" — which is simultaneously
*bid submitted*, *apparent low bidder*, and *contract awarded*. Downstream
every consumer then read a company that merely BID as a company that WON,
and no code could tell the difference because the difference had been
destroyed at the point of storage.

``check.txt`` §10 states the rule this module enforces in code:

    bid_submitted → apparent_low_bidder → recommended_for_award
                  → contract_awarded → contract_signed

    An "apparent low bidder" must NEVER be converted into "won project".

That prohibition is not a prompt instruction here. It is a constant
(:data:`BID_AWARD_CHAIN`), an ordering function (:func:`chain_rank`), and a
guard (:func:`may_promote`) — so a caller cannot make the mistake by
accident, only by deliberately deleting the guard.

Two further hard-coded semantics, both founder-frozen 2026-09-18:

1. **A permit is project activity, never a win.** ``permit_issued`` is
   excluded from :data:`CONTRACT_AWARD_EVENTS`, so no signal or pain layer
   can derive "won a bid" from a permit.
2. **News is a source kind, not an event.** ``news`` is absent from
   :class:`EvidenceType`'s event-bearing set by construction: it is an
   evidence type whose event must be READ off the page, never assumed from
   the fact that a newspaper mentioned the company.

A third semantic was added after founder review on 2026-09-18 and it corrects
the FIRST implementation of the ``bid_award`` split:

3. **An unresolved procurement stage is its own state, not the weakest
   stage.** The first cut mapped legacy ``bid_award`` to ``bid_submitted`` on
   the reasoning that under-claiming is survivable. That reasoning was wrong:
   ``bid_submitted`` is not a weaker version of "bid/award-related evidence",
   it is a DIFFERENT assertion — and a page that merely mentions a company in
   a bid context (bid documents picked up, all bids rejected, a subcontract
   named) does not establish that the company submitted anything. Resolving
   ambiguity downward is only safe when the weaker claim is still TRUE.
   ``needs_resolution`` is the honest target, and
   :func:`may_promote` refuses to leave it without new evidence.

The enums are ``str``-based and open by design, following the established
pattern (``PluginCapability``, ``SourceStatus``): an unknown value
normalizes, compares and serializes exactly like a declared one, so adding a
procurement stage later is not a schema change.
"""

from __future__ import annotations

from enum import Enum


class EventType(str, Enum):
    """A factual occurrence observed in evidence (``check.txt`` §9).

    Deliberately NOT a judgement. "The company was awarded a contract" is an
    event; "the company is overloaded" is not — that is a signal or a pain
    hypothesis and lives in later layers.

    The five procurement stages are ordered and must not be conflated; see
    :data:`BID_AWARD_CHAIN`.
    """

    # -- procurement lifecycle, in order (check.txt §10) -------------------
    BID_SUBMITTED = "bid_submitted"
    APPARENT_LOW_BIDDER = "apparent_low_bidder"
    RECOMMENDED_FOR_AWARD = "recommended_for_award"
    CONTRACT_AWARDED = "contract_awarded"
    CONTRACT_SIGNED = "contract_signed"

    # -- project activity --------------------------------------------------
    #: A project exists / was publicised. NOT a procurement stage.
    PROJECT_ANNOUNCED = "project_announced"
    PROJECT_COMPLETED = "project_completed"
    #: A permit was filed. ``check.txt`` §21: this is PROJECT ACTIVITY
    #: evidence — it is explicitly NOT a bid win and NOT a contract award.
    PERMIT_ISSUED = "permit_issued"
    PAYMENT_RECORDED = "payment_recorded"

    # -- organisational ----------------------------------------------------
    HIRING = "hiring"
    NEW_OFFICE = "new_office"
    MARKET_EXPANSION = "market_expansion"

    # -- the non-occurrence (see the module docstring, point 3) ------------
    #: NOT a factual occurrence, and the only member of this enum that is
    #: not one. It says the evidence IS procurement-related but does NOT
    #: establish which stage — so no stage may be read off it. Consumers
    #: must handle it explicitly: it is deliberately in NEITHER
    #: :data:`ACTIVITY_ONLY_EVENTS` nor :data:`CONTRACT_AWARD_EVENTS`,
    #: because it establishes neither activity nor a win.
    #:
    #: It exists because "we cannot tell" is a real, storable finding — the
    #: event-layer twin of :data:`ResearchState.NOT_VERIFIED`. Collapsing it
    #: into a stage is the defect this whole project exists to remove.
    NEEDS_RESOLUTION = "needs_resolution"

    def __str__(self) -> str:
        """Render as the bare value, not ``EventType.X``."""
        return self.value


class EvidenceType(str, Enum):
    """What KIND of record a piece of evidence is (``check.txt`` §8).

    Distinct from :class:`EventType` on purpose: the evidence type says where
    the text came from, the event type says what it means. One government
    award page and one newspaper article can both carry a
    ``contract_awarded`` event, and they must score differently — see
    ``app.engines.verification.source_tiers``.
    """

    GOVERNMENT_AWARD = "government_award"
    PROCUREMENT_NOTICE = "procurement_notice"
    PERMIT_RECORD = "permit_record"
    JOB_POSTING = "job_posting"
    NEWS_ARTICLE = "news_article"
    COMPANY_PAGE = "company_page"
    BUSINESS_FILING = "business_filing"
    DIRECTORY_LISTING = "directory_listing"

    def __str__(self) -> str:
        """Render as the bare value."""
        return self.value


class CompanyMatch(str, Enum):
    """How strongly a piece of evidence is tied to the company (``check.txt`` §7).

    Evidence about a DIFFERENT company is not stored at all — the store
    rejects :data:`MISMATCH` rather than persisting a row that every later
    layer would have to remember to filter.
    """

    CONFIRMED = "confirmed"  # the company is named in the source itself
    INFERRED = "inferred"  # matched by domain/address, not by name
    UNKNOWN = "unknown"  # could not be established
    MISMATCH = "mismatch"  # a DIFFERENT company — never stored

    def __str__(self) -> str:
        """Render as the bare value."""
        return self.value


class ResearchState(str, Enum):
    """The honest outcome of looking for something (``check.txt`` §31).

    Founder-frozen 2026-09-18: **these four must never collapse into one
    another.** "We found nothing" and "we could not look" are different
    facts, and merging them is how a coverage hole silently becomes a
    confident negative.

    NOT_FOUND        searched; no relevant evidence exists to cite
    NOT_ACCESSIBLE   the source exists but was blocked / login-walled / errored
    NOT_VERIFIED     evidence exists but conflicts or is incomplete
    VERIFIED         evidence is sufficient and uncontradicted
    """

    NOT_FOUND = "NOT_FOUND"
    NOT_ACCESSIBLE = "NOT_ACCESSIBLE"
    NOT_VERIFIED = "NOT_VERIFIED"
    VERIFIED = "VERIFIED"

    def __str__(self) -> str:
        """Render as the bare value."""
        return self.value


#: The procurement lifecycle, weakest assertion first (``check.txt`` §10).
#: Order is semantic, not cosmetic: moving a company RIGHT along this tuple
#: asserts something stronger about it, so it may only happen with new
#: evidence (see :func:`may_promote`).
BID_AWARD_CHAIN: tuple[EventType, ...] = (
    EventType.BID_SUBMITTED,
    EventType.APPARENT_LOW_BIDDER,
    EventType.RECOMMENDED_FOR_AWARD,
    EventType.CONTRACT_AWARDED,
    EventType.CONTRACT_SIGNED,
)

#: Event types that genuinely establish a CONTRACT WAS WON.
#: ``permit_issued`` is deliberately absent (``check.txt`` §21): a $15M
#: permit shows active work, never that the company won a bid.
CONTRACT_AWARD_EVENTS: frozenset[EventType] = frozenset({
    EventType.CONTRACT_AWARDED,
    EventType.CONTRACT_SIGNED,
})

#: Event types that establish project ACTIVITY but nothing about winning.
#: Used by the signal layer so "activity is high" can never be reported as
#: "they are winning work".
#:
#: :data:`EventType.NEEDS_RESOLUTION` is deliberately ABSENT: an unresolved
#: stage is not activity either. Counting it here would let "we found a
#: procurement page we could not read" be reported as a company doing work.
ACTIVITY_ONLY_EVENTS: frozenset[EventType] = frozenset({
    EventType.PROJECT_ANNOUNCED,
    EventType.PROJECT_COMPLETED,
    EventType.PERMIT_ISSUED,
    EventType.PAYMENT_RECORDED,
    EventType.HIRING,
    EventType.NEW_OFFICE,
    EventType.MARKET_EXPANSION,
    EventType.BID_SUBMITTED,
    EventType.APPARENT_LOW_BIDDER,
    EventType.RECOMMENDED_FOR_AWARD,
})

#: Event types that assert NO stage at all. Leaving this set is a promotion
#: in the sense that matters — it asserts something previously unestablished
#: — and :func:`may_promote` therefore demands new evidence for it.
UNRESOLVED_EVENTS: frozenset[EventType] = frozenset({
    EventType.NEEDS_RESOLUTION,
})


def normalize_event_type(event: EventType | str) -> str:
    """Reduce an event type to its canonical string form.

    The single choke point every comparison passes through, which is what
    lets an undeclared event type behave like a declared one.
    """
    if isinstance(event, EventType):
        return event.value
    if isinstance(event, str):
        normalized = event.strip().lower()
        if not normalized:
            raise ValueError("Event type must not be empty")
        return normalized
    raise TypeError(
        f"Event type must be an EventType or str, got {type(event).__name__}"
    )


def chain_rank(event: EventType | str) -> int | None:
    """Position in :data:`BID_AWARD_CHAIN`, or ``None`` if not a stage.

    ``None`` (rather than -1 or a sentinel) so "not part of the procurement
    chain" can never be accidentally ordered against a real stage.
    """
    try:
        normalized = EventType(normalize_event_type(event))
    except ValueError:
        return None
    try:
        return BID_AWARD_CHAIN.index(normalized)
    except ValueError:
        return None


def is_contract_award(event: EventType | str) -> bool:
    """True only for events that establish a contract was WON.

    The machine-enforced form of ``check.txt`` §21: ``permit_issued`` is
    False here, and so is every stage before ``contract_awarded``.
    """
    try:
        normalized = EventType(normalize_event_type(event))
    except ValueError:
        return False
    return normalized in CONTRACT_AWARD_EVENTS


def is_unresolved_event(event: EventType | str | None) -> bool:
    """True when the event asserts no stage at all.

    Covers ``None`` as well as :data:`EventType.NEEDS_RESOLUTION`: an
    observation with no candidate event and one explicitly parked as
    unresolved are the same thing to every consumer — *no stage is
    established here*. Callers that need to distinguish "unread" from
    "read and unresolved" read ``event_candidate`` directly.
    """
    if event is None:
        return True
    try:
        normalized = EventType(normalize_event_type(event))
    except (ValueError, TypeError):
        return True
    return normalized in UNRESOLVED_EVENTS


def may_promote(
    old: EventType | str | None,
    new: EventType | str | None,
    *,
    new_evidence: bool = False,
) -> bool:
    """Whether ``old`` may be replaced by ``new`` without silent promotion.

    ``check.txt`` §10's prohibition, as a function. Two rules:

    **Leaving an unresolved state is a promotion.** Going from
    ``needs_resolution`` (or nothing) to *any* stage asserts something that
    was not previously established, so it requires ``new_evidence``. This is
    the reviewer's correction of 2026-09-18: proving the stage from source
    text is the ONLY way up from unresolved.

    **Within the procurement chain, forward movement is refused** unless
    ``new_evidence`` says a new observation actually arrived — a bid becoming
    a win is the defect being fixed. Moving BACKWARD is always allowed: a
    correction that under-claims is never a fabrication. Likewise moving INTO
    an unresolved state is always allowed, because retracting an unsupported
    claim is the honest direction.

    Args:
        old: The event currently recorded (``None`` = none established).
        new: The event being asserted.
        new_evidence: True only when a NEW piece of evidence was observed
            supporting ``new``.

    Returns:
        True when the transition is permitted.
    """
    if is_unresolved_event(old) and not is_unresolved_event(new):
        return bool(new_evidence)
    old_rank = chain_rank(old)
    new_rank = chain_rank(new)
    if old_rank is None or new_rank is None:
        return True  # not a procurement stage — not this guard's business
    if new_rank <= old_rank:
        return True  # same stage or a correction downward
    return bool(new_evidence)


def promotion_violation(
    old: EventType | str | None,
    new: EventType | str | None,
    *,
    new_evidence: bool = False,
) -> str:
    """The honest reason a promotion is refused, or ``""`` when it is allowed.

    Callers log/record this string instead of a bare False (§6, §12): "we
    refused to upgrade bid_submitted to contract_awarded without new
    evidence" is a finding; ``False`` is not.
    """
    if may_promote(old, new, new_evidence=new_evidence):
        return ""
    if is_unresolved_event(old):
        return (
            f"refused to assert {normalize_event_type(new)} from an "
            f"unresolved stage without new evidence — the source text must "
            f"establish the stage before it is claimed (check.txt §10)"
        )
    return (
        f"refused to promote {normalize_event_type(old)} -> "
        f"{normalize_event_type(new)} without new evidence "
        f"(check.txt §10: weaker procurement stages never silently become "
        f"stronger ones)"
    )

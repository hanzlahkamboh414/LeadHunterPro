"""Adapters: the three existing evidence models → the canonical record.

WHY ADAPTERS AND NOT A REWRITE (founder decision, 2026-09-18): the
canonical model in :mod:`app.research.models` supersedes three overlapping
records, but the repository will not be rewritten in one day. The agreed
path is a projection:

    existing plugin evidence → adapter → canonical → evidence store → event engine

So this module is the ONLY place that knows how an old record maps onto a
new one. Every mapper is additive and side-effect free, which is what makes
wiring the dormant USAspending / Google News / company-crawler plugins safe:
they keep returning the shape they always returned, and one call at the
boundary converts it.

THE ``bid_award`` MAPPING (founder-frozen, ``check.txt`` §10; corrected
2026-09-18 after review)
------------------------------------------------------------------------
``IntentEvidenceType.bid_award`` meant "won a bid / contract" — a single
value covering *bid submitted*, *apparent low bidder* AND *contract
awarded*. When such a row is mapped forward it becomes
:data:`~app.research.taxonomy.EventType.NEEDS_RESOLUTION`: the evidence IS
procurement-related, and the STAGE IS NOT ESTABLISHED.

Why not the weakest stage
-------------------------
The first implementation mapped it to ``bid_submitted``, reasoning that
ambiguity should resolve DOWN and that under-claiming is survivable. The
direction was right; the floor was wrong. ``bid_submitted`` is not a weaker
version of "bid/award-related evidence" — it is a DIFFERENT assertion, and a
page can be procurement-related without the company having submitted
anything:

* "Bid documents available; Acme picked up a set." — no bid submitted.
* "Council rejected all bids." — nothing submitted by anyone.
* "Acme named as a subcontractor on the award." — did not bid at all.

Mapping those to ``bid_submitted`` manufactures a fact. **Resolving
ambiguity downward is only safe when the weaker claim is still TRUE**, and
here it is not.

So the honest target is ``needs_resolution``, and the row can only rise to a
real stage when source text proves it — through
:func:`~app.research.taxonomy.may_promote` with ``new_evidence=True``. That
proof is the event engine's job (Phase 2), reading the page; it is not this
adapter's to assume.

The original string survives in ``CanonicalEvidence.legacy_type``, so the
migration stays auditable and the promotion is a later, evidence-backed step
rather than a guess made at the boundary.

``company_match`` IS CLASSIFIED HERE (fixed 2026-09-21)
------------------------------------------------------
Every mapper sets ``CompanyMatch`` through
:func:`classify_company_match`, from the record's own words plus the
company's domain. Before this, NO production path set it, so every stored
row read ``unknown`` and the pain gate — which requires ``CONFIRMED`` on
every supporting record — blocked all 22 hypotheses structurally. The
classifier's docstring carries the rule and, importantly, why ``INFERRED``
is not a substitute for ``CONFIRMED``.
"""

from __future__ import annotations

from typing import Any

from app.discovery.website.url_normalizer import extract_host
from app.engines.verification.identity_verifier import name_on_page, name_tokens
from app.research.models import CanonicalEvidence, new_evidence_id
from app.research.taxonomy import (
    CompanyMatch,
    EventType,
    EvidenceType,
    is_unresolved_event,
    normalize_event_type,
    promotion_violation,
)

#: ``IntentEvidenceType`` value -> the precise event it licenses.
#:
#: ``bid_award`` is deliberately the ONLY entry that maps to an unresolved
#: state. The others name a stage or an activity in their own value, so
#: reading them literally asserts nothing the source does not.
#:
#: ``news`` is deliberately ABSENT: a newspaper mentioning a company is a
#: SOURCE KIND, not an event. Its event must be read off the page by the
#: event engine, never assumed from the fact of publication (``check.txt``
#: §5 — news is Tier 3 evidence; the tier is not the event).
_LEGACY_INTENT_EVENTS: dict[str, EventType] = {
    # AMBIGUOUS — asserts no stage. See the module docstring.
    "bid_award": EventType.NEEDS_RESOLUTION,
    "hiring": EventType.HIRING,
    "project": EventType.PROJECT_ANNOUNCED,
    "expansion": EventType.MARKET_EXPANSION,
    "permit": EventType.PERMIT_ISSUED,
}

#: Which evidence type a producing pipeline's output is. Unknown producers
#: fall back to :data:`EvidenceType.COMPANY_PAGE` — an honest "we do not
#: know this source's record kind", never a flattering guess.
_SOURCE_EVIDENCE_TYPES: dict[str, EvidenceType] = {
    "usaspending": EvidenceType.GOVERNMENT_AWARD,
    "google_news": EvidenceType.NEWS_ARTICLE,
    "company_site": EvidenceType.COMPANY_PAGE,
    "directory": EvidenceType.DIRECTORY_LISTING,
    "plugin": EvidenceType.DIRECTORY_LISTING,
}


def event_type_for_legacy_intent(legacy: Any) -> EventType | None:
    """Map a legacy ``IntentEvidenceType`` to a precise event type.

    Returns ``None`` for values that license no event — most importantly
    ``news``, which is an evidence kind rather than an occurrence.

    Args:
        legacy: An ``IntentEvidenceType`` member, or its raw string.

    Returns:
        The precise :class:`EventType`, or ``None``.
    """
    key = getattr(legacy, "value", legacy)
    if not isinstance(key, str):
        return None
    return _LEGACY_INTENT_EVENTS.get(key.strip().lower())


def evidence_type_for_source(source: str) -> EvidenceType:
    """The record kind a producing pipeline yields."""
    return _SOURCE_EVIDENCE_TYPES.get(
        (source or "").strip().lower(), EvidenceType.COMPANY_PAGE
    )


def check_legacy_mapping(legacy: Any, mapped: EventType | str | None) -> str:
    """Honest reason a legacy→precise mapping would be a silent promotion.

    Called by :func:`from_intent_evidence` and asserted in the tests. The
    legacy ``bid_award`` value carries NO stage information, so the only
    legal mapping is an unresolved state: any procurement stage — including
    ``bid_submitted`` — asserts something the legacy value does not contain.

    Returns:
        The violation string, or ``""`` when the mapping is legal.
    """
    if mapped is None:
        return ""
    key = getattr(legacy, "value", legacy)
    if not isinstance(key, str) or key.strip().lower() != "bid_award":
        return ""
    if is_unresolved_event(mapped):
        return ""
    return (
        f"refusing to map legacy 'bid_award' to "
        f"'{normalize_event_type(mapped)}' — the legacy value establishes no "
        f"stage, and 'bid_submitted' is a claim in its own right, not a safe "
        f"floor (check.txt §10); it resolves to "
        f"'{EventType.NEEDS_RESOLUTION.value}'"
    )


def _own_domain(source_url: str, company_domain: str) -> bool:
    """True when *source_url* is served from the company's own domain.

    Exact host or a SUBDOMAIN of it, because ``careers.acme.com`` is the
    same company as ``acme.com`` while ``acme.co.uk`` is not. That strictness
    is the point: this decides whether a record is tied to the company, and a
    looser name-similarity test would call a namesake in another TLD the same
    company — the failure mode the taxonomy's separate ``MISMATCH`` state
    exists to prevent.

    A host that is not shaped like a domain (no dot, or containing spaces) is
    never a match: ``extract_host`` passes unparseable input straight through,
    so without this guard a garbage URL could equal a garbage key.
    """
    host = (extract_host(source_url) or "").strip().lower()
    key = (extract_host(company_domain) or "").strip().lower()
    if not host or not key or "." not in host or " " in host or "." not in key:
        return False
    return host == key or host.endswith(f".{key}")


def classify_company_match(
    *,
    company_name: str = "",
    domain: str = "",
    source_url: str = "",
    text: str = "",
) -> CompanyMatch:
    """How firmly a record is tied to the company — the canonical taxonomy.

    THE fix for the defect that made every pain hypothesis unprovable: no
    production code path ever set ``company_match``, so all 224 stored
    evidence rows read ``unknown``, and :mod:`app.research.pain.engine`
    requires ``CONFIRMED`` for every supporting record. 22 of 22 hypotheses
    blocked was structural, not a statement about the companies.

    The two states are the taxonomy's own definitions, applied literally —
    ``CONFIRMED`` is "the company is NAMED in the source itself", ``INFERRED``
    is "matched by domain/address, not by name":

    1. the company is named in the record's own text -> ``CONFIRMED``;
    2. else the record's host is the company's own domain -> ``INFERRED``;
    3. else -> ``UNKNOWN``.

    Order matters: a news article on ``constructiondive.com`` that names the
    company is CONFIRMED, while a page on the company's own site whose
    snippet never says the name is INFERRED. That is the honest reading of
    the two definitions, and it is deliberate that INFERRED does NOT satisfy
    the pain gate — a domain is an inference, not a naming.

    ``MISMATCH`` is never returned. Establishing that a record is about a
    DIFFERENT company requires knowing that other company's name, which the
    record does not give us; ``UNKNOWN`` says "could not be established",
    which is the true statement. Guessing here would either destroy real
    evidence or launder another company's evidence into ours.

    Args:
        company_name: The company the record is being attributed to.
        domain: Its domain (bare or a URL). Empty disables the domain test.
        source_url: Where the record was read.
        text: The record's own words — snippet, claim or headline.

    Returns:
        The :class:`CompanyMatch` state. Never ``MISMATCH``.
    """
    name = (company_name or "").strip()
    if name and name_on_page(name, name_tokens(name), text or ""):
        return CompanyMatch.CONFIRMED
    if _own_domain(source_url, domain):
        return CompanyMatch.INFERRED
    return CompanyMatch.UNKNOWN


def from_ai_evidence(
    evidence: Any,
    *,
    company_id: str,
    retrieved_at: str,
    source_type: str = "",
    project_key: str = "",
    company_name: str = "",
    domain: str = "",
) -> CanonicalEvidence:
    """Project a ``lead_research.models.AIEvidence`` onto the canonical record.

    The old record carries a claim and a URL but no quotation, so ``title``
    holds the claim and ``excerpt`` stays empty — honest, because the
    pipeline genuinely never stored the supporting sentence. Its
    ``confidence`` is a tier label, so it lands in ``verification`` and the
    numeric ``confidence`` is left at 0.0 rather than invented.

    The claim IS the record's text, so it is what ``company_match`` is
    classified from: an AI-written claim normally names its subject.

    Args:
        evidence: Any object with ``claim``/``source_url``/``confidence``/
            ``source_type``/``source_note``.
        company_id: The company this belongs to.
        retrieved_at: When we read it.
        source_type: The producing pipeline, when the caller knows it.
        project_key: Normalized project identity, when known.
        company_name: The company, for the ``company_match`` classification.
        domain: Its domain, for the same. Unset means "cannot establish".

    Returns:
        A validated :class:`CanonicalEvidence`.
    """
    claim = str(getattr(evidence, "claim", "") or "")
    url = str(getattr(evidence, "source_url", "") or "")
    tier = str(getattr(evidence, "confidence", "") or "")
    note = str(getattr(evidence, "source_note", "") or "")
    producer = source_type or str(getattr(evidence, "source_type", "") or "")
    extra: dict[str, Any] = {}
    if note:
        extra["source_note"] = note
    return CanonicalEvidence(
        evidence_id=new_evidence_id(f"ai|{company_id}|{url}|{claim}"),
        company_id=company_id,
        source_url=url,
        source_type=producer,
        title=claim,
        excerpt="",
        retrieved_at=retrieved_at,
        evidence_type=EvidenceType.COMPANY_PAGE,
        project_key=project_key,
        confidence=0.0,
        verification=tier,
        extra=extra,
        company_match=classify_company_match(
            company_name=company_name, domain=domain, source_url=url, text=claim
        ),
    )


def from_field_evidence(
    evidence: Any,
    *,
    company_id: str,
    retrieved_at: str = "",
    source_type: str = "",
    company_name: str = "",
    domain: str = "",
) -> CanonicalEvidence:
    """Project a ``discovery.website.evidence.FieldEvidence`` onto canonical.

    ``FieldEvidence`` is already the closest of the three: it has a page
    URL, a raw snippet, a numeric confidence and an extraction timestamp. It
    maps almost verbatim, and its ``field``/``selector``/``method`` — the
    per-field provenance that makes a wrong phone number auditable — ride
    along in ``extra`` rather than being dropped.

    Args:
        evidence: Any object with ``field``/``page_url``/``snippet``/
            ``confidence``/``extracted_at``/``selector``/``method``.
        company_id: The company this belongs to.
        retrieved_at: Overrides the record's own ``extracted_at`` when given.
        source_type: The producing pipeline.
        company_name: The company, for the ``company_match`` classification.
        domain: Its domain, for the same. This is the mapper where the domain
            half matters most: a field read off the company's own contact page
            names it by DOMAIN and often not in the snippet, so the honest
            state is ``INFERRED``.

    Returns:
        A validated :class:`CanonicalEvidence`.
    """
    url = str(getattr(evidence, "page_url", "") or "")
    snippet = str(getattr(evidence, "snippet", "") or "")
    confidence = getattr(evidence, "confidence", None)
    # ``FieldEvidence.confidence`` is a scored object (``.score``), but a
    # caller may hand over a bare number. Both are accepted; anything else
    # yields 0.0 rather than crashing on an attribute access.
    if isinstance(confidence, (int, float)):
        score = float(confidence)
    else:
        score = float(getattr(confidence, "score", 0.0) or 0.0)
    extracted_at = str(getattr(evidence, "extracted_at", "") or "")
    field_name = str(getattr(evidence, "field", "") or "")
    extra: dict[str, Any] = {}
    for key in ("field", "selector", "method"):
        value = str(getattr(evidence, key, "") or "")
        if value:
            extra[key] = value
    return CanonicalEvidence(
        evidence_id=new_evidence_id(f"field|{company_id}|{url}|{field_name}|{snippet}"),
        company_id=company_id,
        source_url=url,
        source_type=source_type,
        title=field_name,
        excerpt=snippet,
        retrieved_at=retrieved_at or extracted_at,
        evidence_type=EvidenceType.COMPANY_PAGE,
        confidence=min(max(score, 0.0), 1.0),
        extra=extra,
        company_match=classify_company_match(
            company_name=company_name, domain=domain, source_url=url, text=snippet
        ),
    )


def from_intent_evidence(
    evidence: Any,
    *,
    company_id: str,
    retrieved_at: str = "",
    project_key: str = "",
    company_name: str = "",
    domain: str = "",
) -> CanonicalEvidence:
    """Project an ``engines.lead.lead_models.IntentEvidence`` onto canonical.

    This is the mapper that carries the ``bid_award`` split: the legacy type
    is preserved in ``legacy_type`` and its event resolves through
    :func:`event_type_for_legacy_intent`, which for the ambiguous value
    yields :data:`EventType.NEEDS_RESOLUTION` — an explicit "the stage is
    not established", never an assumed stage.

    It is also the ONLY mapper on the live path (verified by grep,
    2026-09-21: ``app.research.intake`` calls this one and nothing else), so
    it is where the ``company_match`` defect actually bit — all 224 stored
    rows read ``unknown`` because nothing here ever asked.

    Args:
        evidence: Any object with ``type``/``source_url``/``snippet``/
            ``date``/``source``/``fetched_at``.
        company_id: The company this belongs to.
        retrieved_at: Overrides the record's own ``fetched_at`` when given.
        project_key: Normalized project identity, when known.
        company_name: The company, for the ``company_match`` classification.
        domain: Its domain, for the same.

    Returns:
        A validated :class:`CanonicalEvidence`.

    Raises:
        ValueError: If the legacy type would map to a silent promotion.
    """
    legacy = getattr(evidence, "type", None)
    legacy_key = str(getattr(legacy, "value", legacy) or "")
    mapped = event_type_for_legacy_intent(legacy)
    violation = check_legacy_mapping(legacy, mapped)
    if violation:
        raise ValueError(violation)

    producer = str(getattr(evidence, "source", "") or "")
    url = str(getattr(evidence, "source_url", "") or "")
    snippet = str(getattr(evidence, "snippet", "") or "")
    return CanonicalEvidence(
        evidence_id=new_evidence_id(f"intent|{company_id}|{url}|{legacy_key}|{snippet}"),
        company_id=company_id,
        source_url=url,
        source_type=producer,
        excerpt=snippet,
        published_at=str(getattr(evidence, "date", "") or ""),
        retrieved_at=retrieved_at or str(getattr(evidence, "fetched_at", "") or ""),
        evidence_type=evidence_type_for_source(producer),
        project_key=project_key,
        event_candidate=mapped,
        confidence=0.0,
        legacy_type=legacy_key,
        company_match=classify_company_match(
            company_name=company_name, domain=domain, source_url=url, text=snippet
        ),
    )


def plan_legacy_migration(legacy: Any) -> tuple[EventType | None, str]:
    """What a legacy value becomes, and the honest note explaining it.

    The dry-run half of the migration: callers (and the CLI report) can show
    what the split will do BEFORE anything is written, and the note is the
    audit trail the founder asked for rather than a silent rewrite.

    Returns:
        ``(event_type, note)``.
    """
    key = str(getattr(legacy, "value", legacy) or "")
    mapped = event_type_for_legacy_intent(legacy)
    if key.strip().lower() == "bid_award":
        return mapped, (
            "legacy 'bid_award' establishes no procurement stage (it covered "
            "bid / apparent low / awarded), so it resolves to "
            "'needs_resolution' — NOT to 'bid_submitted'. bid_submitted is a "
            "claim in its own right, and a procurement page does not prove "
            "the company submitted anything (check.txt §10, reviewed "
            "2026-09-18). Promote to a stage only from source text, with new "
            "evidence."
        )
    if mapped is None:
        return None, (
            f"legacy '{key}' is an evidence kind, not an event — no event "
            f"candidate is set; the event engine must read it off the page"
        )
    return mapped, f"legacy '{key}' maps directly to '{mapped.value}'"


def promotion_guard(old: Any, new: Any, *, new_evidence: bool = False) -> str:
    """Re-exported promotion check for callers that only import adapters."""
    return promotion_violation(old, new, new_evidence=new_evidence)

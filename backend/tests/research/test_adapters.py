"""The three legacy evidence models → canonical, and the ``bid_award`` split.

This module is where the existing defect is actually removed. The old
``IntentEvidenceType.bid_award`` covered *bid submitted*, *apparent low
bidder* AND *contract awarded* in one value; every row already stored under
it is genuinely ambiguous.

**Corrected 2026-09-18 after founder review.** The first cut mapped it to
``bid_submitted`` — "ambiguity resolves DOWN". The direction was right, the
floor was wrong: ``bid_submitted`` is not a weaker version of
"bid/award-related evidence", it is a different assertion, and a
procurement page does not prove the company submitted anything. So the
mapping target is ``needs_resolution``, and leaving that state requires
source text — ``may_promote(..., new_evidence=True)``.

The tests therefore assert three things:

* ``bid_award`` maps to ``needs_resolution`` and to NO stage at all,
* no path through this module can produce a stage for it, even if someone
  later "improves" the lookup table by hand, and
* the module still refuses to invent facts in the other direction — a
  legacy value that names its own stage keeps it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pytest

from app.research.adapters import (
    check_legacy_mapping,
    event_type_for_legacy_intent,
    evidence_type_for_source,
    from_ai_evidence,
    from_field_evidence,
    from_intent_evidence,
    plan_legacy_migration,
    promotion_guard,
)
from app.research.taxonomy import (
    BID_AWARD_CHAIN,
    EventType,
    EvidenceType,
    chain_rank,
    is_contract_award,
    is_unresolved_event,
)


# --- stand-ins for the three real models ---------------------------------
# Deliberately plain dataclasses, not the real classes: the adapters are
# duck-typed by design (they read attributes through ``getattr``), and using
# stubs here proves that rather than assuming it.


@dataclass
class _LegacyIntent:
    """Shape of ``engines.lead.lead_models.IntentEvidence``."""

    type: object
    source_url: str = "https://example.gov/award/1"
    snippet: str = "Awarded to Acme."
    date: str = "2026-09-11"
    source: str = "usaspending"
    fetched_at: str = "2026-09-18T10:00:00"


@dataclass
class _Scored:
    """Shape of ``FieldEvidence.confidence`` — an object with ``.score``."""

    score: float


@dataclass
class _FieldEvidence:
    """Shape of ``discovery.website.evidence.FieldEvidence``."""

    field: str = "phone"
    page_url: str = "https://acme.com/contact"
    snippet: str = "Call (512) 555-0100"
    confidence: object = None
    extracted_at: str = "2026-09-18T10:00:00"
    selector: str = "div.contact > a.tel"
    method: str = "css"


@dataclass
class _AIEvidence:
    """Shape of ``lead_research.models.AIEvidence``."""

    claim: str = "Acme opened a new office in Dallas"
    source_url: str = "https://news.example.com/acme-dallas"
    confidence: str = "verified"
    source_type: str = "google_news"
    source_note: str = "trade press"


# --- the bid_award split --------------------------------------------------


def test_bid_award_resolves_to_an_unresolved_state():
    """The core of the fix: the ambiguous value asserts NO stage."""
    assert event_type_for_legacy_intent("bid_award") is EventType.NEEDS_RESOLUTION


def test_bid_award_never_resolves_to_any_stage():
    """Not the weakest stage either — that was the corrected mistake.

    ``bid_submitted`` is a claim in its own right. A page that mentions a
    company in a bid context (documents picked up, all bids rejected, named
    as a subcontractor) does not establish that it submitted anything.
    """
    mapped = event_type_for_legacy_intent("bid_award")
    assert chain_rank(mapped) is None, (
        "bid_award must carry no procurement rank at all — mapping it to "
        "bid_submitted manufactured a fact (check.txt §10, reviewed "
        "2026-09-18)"
    )
    for stage in BID_AWARD_CHAIN:
        assert mapped is not stage
        assert check_legacy_mapping("bid_award", stage), (
            f"mapping bid_award -> {stage} must be refused"
        )


def test_bid_award_is_not_a_contract_award():
    assert is_contract_award(event_type_for_legacy_intent("bid_award")) is False


def test_bid_award_is_unresolved():
    assert is_unresolved_event(event_type_for_legacy_intent("bid_award")) is True


@pytest.mark.parametrize("stage", BID_AWARD_CHAIN)
def test_check_legacy_mapping_refuses_every_stage(stage):
    """The guard that catches a hand-edited lookup table."""
    reason = check_legacy_mapping("bid_award", stage)
    assert reason, f"mapping bid_award -> {stage} must be refused"
    assert "bid_award" in reason
    assert EventType.NEEDS_RESOLUTION.value in reason


def test_check_legacy_mapping_allows_the_unresolved_target():
    assert check_legacy_mapping("bid_award", EventType.NEEDS_RESOLUTION) == ""


def test_check_legacy_mapping_allows_unrelated_legacy_values():
    """Only ``bid_award`` is ambiguous; the others map where they say."""
    assert check_legacy_mapping("hiring", EventType.HIRING) == ""
    assert check_legacy_mapping("permit", EventType.PERMIT_ISSUED) == ""
    assert check_legacy_mapping("permit", EventType.CONTRACT_AWARDED) == ""


def test_intent_evidence_keeps_the_original_value_for_audit():
    record = from_intent_evidence(
        _LegacyIntent(type="bid_award"), company_id="cmp_1"
    )
    assert record.legacy_type == "bid_award"
    assert record.event_candidate == EventType.NEEDS_RESOLUTION
    assert record.evidence_type is EvidenceType.GOVERNMENT_AWARD


def test_an_unresolved_row_can_be_promoted_from_source_text():
    """The founder's condition: prove the stage, then promote.

    ``needs_resolution`` is not a dead end — it is a state you leave only
    with new evidence, which is Phase 2's event engine reading the page.
    """
    assert (
        promotion_guard(
            EventType.NEEDS_RESOLUTION, EventType.CONTRACT_AWARDED
        )
        != ""
    )
    assert (
        promotion_guard(
            EventType.NEEDS_RESOLUTION,
            EventType.CONTRACT_AWARDED,
            new_evidence=True,
        )
        == ""
    )


def test_an_unresolved_row_may_be_left_for_any_stage_with_evidence():
    """Every stage is reachable from unresolved — but only with evidence."""
    for stage in BID_AWARD_CHAIN:
        assert promotion_guard(EventType.NEEDS_RESOLUTION, stage) != ""
        assert (
            promotion_guard(
                EventType.NEEDS_RESOLUTION, stage, new_evidence=True
            )
            == ""
        )


def test_promoting_into_an_unresolved_state_is_always_allowed():
    """Retracting an unsupported stage is the honest direction."""
    assert promotion_guard(EventType.CONTRACT_AWARDED, EventType.NEEDS_RESOLUTION) == ""
    assert promotion_guard(EventType.BID_SUBMITTED, EventType.NEEDS_RESOLUTION) == ""


def test_intent_evidence_refuses_a_hand_edited_lookup_table(monkeypatch):
    """The guard runs on the CALL PATH, not only in the helper.

    This is the regression test that matters: if someone later "fixes" the
    mapping table by hand so ``bid_award`` becomes a win — the most tempting
    shortcut in this whole module — ``from_intent_evidence`` must raise
    rather than quietly store a fabricated award.
    """
    from app.research import adapters as adapters_mod

    monkeypatch.setitem(
        adapters_mod._LEGACY_INTENT_EVENTS,
        "bid_award",
        EventType.CONTRACT_AWARDED,
    )
    with pytest.raises(ValueError) as exc:
        from_intent_evidence(_LegacyIntent(type="bid_award"), company_id="cmp_1")
    message = str(exc.value)
    assert "bid_award" in message
    assert EventType.NEEDS_RESOLUTION.value in message


def test_intent_evidence_refuses_a_hand_edited_floor_too(monkeypatch):
    """Even the "safe-looking" floor is refused.

    Setting ``bid_award`` to ``bid_submitted`` was the ORIGINAL
    implementation and it looked harmless. This test pins that it is not:
    any named stage asserts something the legacy value does not contain.
    """
    from app.research import adapters as adapters_mod

    monkeypatch.setitem(
        adapters_mod._LEGACY_INTENT_EVENTS,
        "bid_award",
        EventType.BID_SUBMITTED,
    )
    with pytest.raises(ValueError) as exc:
        from_intent_evidence(_LegacyIntent(type="bid_award"), company_id="cmp_1")
    assert "bid_submitted" in str(exc.value)


# --- the other legacy values map where they say ---------------------------


@pytest.mark.parametrize(
    "legacy,expected",
    [
        ("hiring", EventType.HIRING),
        ("project", EventType.PROJECT_ANNOUNCED),
        ("expansion", EventType.MARKET_EXPANSION),
        ("permit", EventType.PERMIT_ISSUED),
    ],
)
def test_unambiguous_legacy_values_map_directly(legacy, expected):
    assert event_type_for_legacy_intent(legacy) is expected


def test_permit_legacy_value_is_never_an_award():
    """§21: permit → permit_issued, full stop."""
    mapped = event_type_for_legacy_intent("permit")
    assert mapped is EventType.PERMIT_ISSUED
    assert mapped not in (EventType.CONTRACT_AWARDED, EventType.CONTRACT_SIGNED)


def test_news_is_an_evidence_kind_not_an_event():
    """A newspaper mentioning a company is not, by itself, an occurrence."""
    assert event_type_for_legacy_intent("news") is None
    record = from_intent_evidence(
        _LegacyIntent(type="news", source="google_news"), company_id="cmp_1"
    )
    assert record.event_candidate is None
    assert record.evidence_type is EvidenceType.NEWS_ARTICLE


def test_unknown_legacy_value_licenses_no_event():
    """An unrecognised value yields None, never a flattering default."""
    assert event_type_for_legacy_intent("something_new") is None
    assert event_type_for_legacy_intent("") is None
    assert event_type_for_legacy_intent(None) is None


def test_enum_members_and_strings_map_identically():
    """The real ``IntentEvidenceType`` is a ``str`` enum — its member works."""

    class _Fake(str, Enum):
        BID_AWARD = "bid_award"

    assert event_type_for_legacy_intent(_Fake.BID_AWARD) is (
        EventType.NEEDS_RESOLUTION
    )
    assert event_type_for_legacy_intent("bid_award") is EventType.NEEDS_RESOLUTION


# --- evidence-type lookup -------------------------------------------------


@pytest.mark.parametrize(
    "source,expected",
    [
        ("usaspending", EvidenceType.GOVERNMENT_AWARD),
        ("google_news", EvidenceType.NEWS_ARTICLE),
        ("company_site", EvidenceType.COMPANY_PAGE),
        ("directory", EvidenceType.DIRECTORY_LISTING),
        ("USASPENDING", EvidenceType.GOVERNMENT_AWARD),
    ],
)
def test_source_maps_to_its_record_kind(source, expected):
    assert evidence_type_for_source(source) is expected


def test_unknown_source_is_company_page_not_a_flattering_guess():
    assert evidence_type_for_source("mystery_source") is EvidenceType.COMPANY_PAGE
    assert evidence_type_for_source("") is EvidenceType.COMPANY_PAGE


# --- the three mappers ----------------------------------------------------


def test_intent_evidence_maps_verbatim_fields():
    record = from_intent_evidence(_LegacyIntent(type="hiring"), company_id="cmp_1")
    assert record.company_id == "cmp_1"
    assert record.source_url == "https://example.gov/award/1"
    assert record.excerpt == "Awarded to Acme."
    assert record.published_at == "2026-09-11"
    assert record.retrieved_at == "2026-09-18T10:00:00"


def test_intent_evidence_accepts_an_explicit_project_key():
    record = from_intent_evidence(
        _LegacyIntent(type="project"),
        company_id="cmp_1",
        project_key="city-of-austin-riverside-bridge",
    )
    assert record.project_key == "city-of-austin-riverside-bridge"


def test_ai_evidence_keeps_its_tier_out_of_the_numeric_confidence():
    """A label is not a probability — no fabricated precision."""
    record = from_ai_evidence(
        _AIEvidence(), company_id="cmp_1", retrieved_at="2026-09-18T10:00:00"
    )
    assert record.verification == "verified"
    assert record.confidence == 0.0
    assert record.title == "Acme opened a new office in Dallas"
    assert record.excerpt == "", "AIEvidence stores no quotation — do not invent one"


def test_ai_evidence_keeps_the_source_note_in_extra():
    record = from_ai_evidence(
        _AIEvidence(), company_id="cmp_1", retrieved_at="2026-09-18T10:00:00"
    )
    assert record.extra["source_note"] == "trade press"


def test_field_evidence_maps_verbatim_and_keeps_its_provenance():
    """The per-field provenance that makes a wrong phone auditable."""
    record = from_field_evidence(
        _FieldEvidence(confidence=_Scored(score=0.83)), company_id="cmp_1"
    )
    assert record.source_url == "https://acme.com/contact"
    assert record.excerpt == "Call (512) 555-0100"
    assert record.confidence == 0.83
    assert record.retrieved_at == "2026-09-18T10:00:00"
    assert record.extra["field"] == "phone"
    assert record.extra["selector"] == "div.contact > a.tel"
    assert record.extra["method"] == "css"


def test_field_evidence_accepts_a_bare_numeric_confidence():
    record = from_field_evidence(
        _FieldEvidence(confidence=0.5), company_id="cmp_1"
    )
    assert record.confidence == 0.5


def test_field_evidence_with_no_confidence_is_zero_not_a_guess():
    record = from_field_evidence(_FieldEvidence(), company_id="cmp_1")
    assert record.confidence == 0.0


def test_all_three_mappers_produce_a_storable_record():
    """Every mapper's output passes the canonical record's own validation."""
    records = [
        from_intent_evidence(_LegacyIntent(type="hiring"), company_id="cmp_1"),
        from_ai_evidence(
            _AIEvidence(), company_id="cmp_1", retrieved_at="2026-09-18T10:00:00"
        ),
        from_field_evidence(_FieldEvidence(confidence=_Scored(0.5)), company_id="cmp_1"),
    ]
    for record in records:
        row = record.to_row()
        assert row["company_id"] == "cmp_1"
        assert row["source_url"]
        assert row["content_hash"]


def test_mappers_reject_evidence_with_no_source_url():
    """§7 applies to migrated rows exactly as it does to native ones."""
    with pytest.raises(ValueError):
        from_ai_evidence(
            _AIEvidence(source_url=""),
            company_id="cmp_1",
            retrieved_at="2026-09-18T10:00:00",
        )
    with pytest.raises(ValueError):
        from_intent_evidence(
            _LegacyIntent(type="hiring", source_url=""), company_id="cmp_1"
        )


# --- dry-run migration report --------------------------------------------


def test_plan_reports_the_unresolved_mapping_with_its_reason():
    """The dry-run report must state the honest target AND why not a stage."""
    mapped, note = plan_legacy_migration("bid_award")
    assert mapped is EventType.NEEDS_RESOLUTION
    assert "needs_resolution" in note
    assert "bid_submitted" in note, (
        "the note must name the tempting wrong answer explicitly, so the "
        "next reader does not 'fix' it back"
    )


def test_plan_explains_why_news_gets_no_event():
    mapped, note = plan_legacy_migration("news")
    assert mapped is None
    assert "evidence kind" in note


def test_plan_reports_a_direct_mapping():
    mapped, note = plan_legacy_migration("hiring")
    assert mapped is EventType.HIRING
    assert "hiring" in note


# --- the promotion guard surfaced through this module --------------------


def test_promotion_guard_re_exports_the_taxonomy_rule():
    assert promotion_guard(EventType.BID_SUBMITTED, EventType.CONTRACT_AWARDED) != ""
    assert promotion_guard(EventType.BID_SUBMITTED, EventType.BID_SUBMITTED) == ""
    assert (
        promotion_guard(
            EventType.BID_SUBMITTED,
            EventType.CONTRACT_AWARDED,
            new_evidence=True,
        )
        == ""
    )


def test_no_legacy_value_is_ever_mapped_above_its_own_rank():
    """A blanket invariant over the whole table.

    ``bid_award`` is the strictest case: it carries no rank at all. Every
    other legacy value names its own stage or activity in its own string, so
    reading it literally asserts nothing extra.
    """
    for legacy in ("bid_award", "hiring", "project", "expansion", "permit"):
        mapped = event_type_for_legacy_intent(legacy)
        if mapped is None:
            continue
        assert check_legacy_mapping(legacy, mapped) == ""
        if legacy == "bid_award":
            assert is_unresolved_event(mapped)
            assert chain_rank(mapped) is None
        elif chain_rank(mapped) is not None:
            assert 0 <= chain_rank(mapped) < len(BID_AWARD_CHAIN)

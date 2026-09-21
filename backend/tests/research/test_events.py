"""Phase 2: evidence -> events, with deterministic validation."""

from __future__ import annotations

import json

import pytest

from app.research.events import (
    EventExtractionError,
    EventRecord,
    extract_company_events,
)
from app.research.models import CanonicalEvidence
from app.research.store import ResearchEvidenceStore
from app.research.taxonomy import CompanyMatch, EventType, EvidenceType


def _evidence(
    evidence_id: str,
    *,
    company_id: str = "cmp_acme",
    excerpt: str = "City council awarded the contract to Acme Construction.",
    evidence_type: EvidenceType = EvidenceType.GOVERNMENT_AWARD,
    project_key: str = "city-of-austin-library-renovation",
) -> CanonicalEvidence:
    return CanonicalEvidence(
        evidence_id=evidence_id,
        company_id=company_id,
        source_url=f"https://austintexas.gov/awards/{evidence_id}",
        source_type="city_procurement",
        publisher="City of Austin",
        title="Library renovation award",
        excerpt=excerpt,
        published_at="2026-07-14",
        retrieved_at="2026-09-20T10:00:00",
        company_match=CompanyMatch.CONFIRMED,
        evidence_type=evidence_type,
        project_key=project_key,
        confidence=0.96,
    )


def _answer(*events: dict) -> str:
    return json.dumps({"events": list(events)})


def _proposal(**overrides: object) -> dict:
    out: dict[str, object] = {
        "event_type": "contract_awarded",
        "project_key": "city-of-austin-library-renovation",
        "occurred_at": "2026-07-14",
        "confidence": 0.93,
        "evidence_ids": ["ev_1"],
    }
    out.update(overrides)
    return out


def test_one_ai_call_receives_only_canonical_evidence_and_returns_event() -> None:
    calls: list[str] = []

    def ask(prompt: str) -> str:
        calls.append(prompt)
        return _answer(_proposal())

    result = extract_company_events([_evidence("ev_1")], ai_ask=ask)

    assert len(calls) == 1
    assert "https://austintexas.gov/awards/ev_1" in calls[0]
    assert "City council awarded the contract" in calls[0]
    assert "search" not in calls[0].lower()
    assert result.rejections == ()
    assert result.events == (
        EventRecord(
            event_id=result.events[0].event_id,
            company_id="cmp_acme",
            event_type=EventType.CONTRACT_AWARDED,
            project_key="city-of-austin-library-renovation",
            occurred_at="2026-07-14",
            confidence=0.93,
            evidence_ids=("ev_1",),
        ),
    )


def test_unknown_event_type_is_rejected_by_closed_enum() -> None:
    result = extract_company_events(
        [_evidence("ev_1")],
        ai_ask=lambda _: _answer(_proposal(event_type="probably_overloaded")),
    )

    assert result.events == ()
    assert "unknown event_type" in result.rejections[0]


def test_invented_evidence_id_is_rejected() -> None:
    result = extract_company_events(
        [_evidence("ev_1")],
        ai_ask=lambda _: _answer(_proposal(evidence_ids=["ev_hallucinated"])),
    )

    assert result.events == ()
    assert "unknown evidence_id" in result.rejections[0]


def test_evidence_from_two_companies_is_refused_before_ai_call() -> None:
    called = False

    def ask(_: str) -> str:
        nonlocal called
        called = True
        return _answer()

    with pytest.raises(ValueError, match="one company"):
        extract_company_events(
            [_evidence("ev_1"), _evidence("ev_2", company_id="cmp_other")],
            ai_ask=ask,
        )

    assert called is False


def test_unconfirmed_evidence_for_another_named_entity_never_reaches_ai() -> None:
    called = False

    def ask(_: str) -> str:
        nonlocal called
        called = True
        return _answer()

    evidence = CanonicalEvidence(
        evidence_id="ev_wrong",
        company_id="cmp_turner",
        source_url="https://www.usaspending.gov/award/123",
        retrieved_at="2026-09-20",
        excerpt="WHITING-TURNER CONTRACTING COMPANY — $173,722,190",
        evidence_type=EvidenceType.GOVERNMENT_AWARD,
        company_match=CompanyMatch.UNKNOWN,
    )
    result = extract_company_events(
        [evidence], company_name="Turner Construction Company", ai_ask=ask
    )

    assert called is False
    assert result.events == ()
    assert "company identity" in result.rejections[0]


def test_exact_company_name_can_confirm_an_unknown_legacy_row() -> None:
    calls: list[str] = []
    evidence = CanonicalEvidence(
        evidence_id="ev_turner",
        company_id="cmp_turner",
        source_url="https://www.usaspending.gov/award/456",
        source_type="usaspending",
        retrieved_at="2026-09-20",
        excerpt="TURNER CONSTRUCTION COMPANY — $393,700,142 — Defense",
        evidence_type=EvidenceType.GOVERNMENT_AWARD,
        company_match=CompanyMatch.UNKNOWN,
    )

    result = extract_company_events(
        [evidence],
        company_name="Turner Construction Company",
        ai_ask=lambda prompt: (
            calls.append(prompt)
            or _answer(_proposal(
                evidence_ids=["ev_turner"], project_key="", occurred_at="2026-07-14"
            ))
        ),
    )

    assert len(calls) == 1
    assert len(result.events) == 1


def test_permit_record_cannot_become_contract_award() -> None:
    result = extract_company_events(
        [
            _evidence(
                "ev_1",
                excerpt="Permit issued for the library renovation.",
                evidence_type=EvidenceType.PERMIT_RECORD,
            )
        ],
        ai_ask=lambda _: _answer(_proposal()),
    )

    assert result.events == ()
    assert "permit" in result.rejections[0]
    assert "contract_awarded" in result.rejections[0]


def test_procurement_stage_requires_exact_supporting_words() -> None:
    result = extract_company_events(
        [_evidence("ev_1", excerpt="Acme picked up the bid documents.")],
        ai_ask=lambda _: _answer(_proposal()),
    )

    assert result.events == ()
    assert "source text does not establish" in result.rejections[0]


def test_event_date_must_be_grounded_in_supporting_evidence() -> None:
    result = extract_company_events(
        [_evidence("ev_1")],
        ai_ask=lambda _: _answer(_proposal(occurred_at="2026-08-31")),
    )

    assert result.events == ()
    assert "occurred_at" in result.rejections[0]
    assert "supporting evidence" in result.rejections[0]


def test_same_project_event_merges_independent_evidence() -> None:
    evidence = [_evidence("ev_1"), _evidence("ev_2")]
    result = extract_company_events(
        evidence,
        ai_ask=lambda _: _answer(
            _proposal(evidence_ids=["ev_1"]),
            _proposal(evidence_ids=["ev_2"], confidence=0.88),
        ),
    )

    assert len(result.events) == 1
    assert result.events[0].evidence_ids == ("ev_1", "ev_2")
    assert result.events[0].confidence == 0.93


def test_malformed_ai_json_fails_loudly() -> None:
    with pytest.raises(EventExtractionError, match="valid JSON"):
        extract_company_events([_evidence("ev_1")], ai_ask=lambda _: "not json")


def test_event_store_round_trip_and_idempotence(tmp_path) -> None:
    store = ResearchEvidenceStore(str(tmp_path / "research.db"))
    company = store.resolve_company("acme.example", name="Acme")
    evidence = _evidence("ev_1", company_id=company.company_id)
    store.add_evidence(evidence)
    event = extract_company_events(
        [evidence], ai_ask=lambda _: _answer(_proposal())
    ).events[0]

    assert store.add_event(event) is True
    assert store.add_event(event) is False
    assert store.events_for_company(company.company_id) == [event]

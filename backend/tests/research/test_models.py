"""The canonical evidence record's contract (``check.txt`` §7, §8).

Two of these tests guard the root cause directly: ``published_at`` and
``project_key`` are the fields whose ABSENCE made a 2019 award and a
last-week award indistinguishable, and made one project on three websites
look like three projects. If they stop being stored, the event engine has
nothing to date or group on and the old defect returns silently.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.research.models import (
    MAX_EXCERPT_CHARS,
    CanonicalEvidence,
    CompanyIdentity,
    content_hash,
    new_evidence_id,
    project_key,
)
from app.research.taxonomy import CompanyMatch, EventType, EvidenceType


def _evidence(**overrides) -> CanonicalEvidence:
    """A minimal valid record; tests override only the field under test."""
    fields = dict(
        evidence_id="ev_test",
        company_id="cmp_test",
        source_url="https://example.gov/award/123",
        retrieved_at="2026-09-18T10:00:00",
    )
    fields.update(overrides)
    return CanonicalEvidence(**fields)


# --- the field whose absence caused the incident --------------------------


def test_published_at_is_stored_separately_from_retrieved_at():
    """When it HAPPENED is not when we READ it. Conflating them is the bug."""
    old = _evidence(published_at="2019-03-04", retrieved_at="2026-09-18T10:00:00")
    new = _evidence(published_at="2026-09-11", retrieved_at="2026-09-18T10:00:00")
    assert old.published_at != new.published_at
    assert old.retrieved_at == new.retrieved_at
    assert old.to_row()["published_at"] == "2019-03-04"


def test_published_at_may_be_empty_but_is_never_invented():
    """Unknown publication date is stored as unknown, not defaulted to now."""
    record = _evidence()
    assert record.published_at == ""
    assert record.to_row()["published_at"] == ""


def test_project_key_groups_the_same_project_across_sources():
    """One project on a city site, a paper and the contractor's page."""
    assert project_key("Riverside Bridge Replacement", owner="City of Austin") == (
        project_key("riverside bridge replacement", owner="CITY OF AUSTIN")
    )


def test_project_key_is_empty_when_no_project_is_named():
    """Honest blank beats an invented key that merges a city's projects."""
    assert project_key("") == ""
    assert project_key("   ") == ""
    assert project_key("", owner="City of Austin", jurisdiction="TX") == ""


def test_project_key_distinguishes_different_owners():
    """Same project name, different owner, different key — no false merge."""
    a = project_key("Main Street Repair", owner="City of Austin")
    b = project_key("Main Street Repair", owner="City of Dallas")
    assert a != b


# --- source_url is mandatory ---------------------------------------------


def test_source_url_is_required():
    """§7: a claim that cannot name its page is not evidence."""
    with pytest.raises(ValueError) as exc:
        _evidence(source_url="")
    assert "source_url" in str(exc.value)


def test_whitespace_only_source_url_is_rejected():
    with pytest.raises(ValueError):
        _evidence(source_url="   ")


# --- evidence about another company is never stored ----------------------


def test_mismatch_is_refused_at_construction():
    """Reject at the boundary, not in every downstream consumer."""
    with pytest.raises(ValueError) as exc:
        _evidence(company_match=CompanyMatch.MISMATCH)
    assert "MISMATCH" in str(exc.value) or "different company" in str(exc.value)


def test_mismatch_string_is_also_refused():
    with pytest.raises(ValueError):
        _evidence(company_match="mismatch")


@pytest.mark.parametrize(
    "match", [CompanyMatch.CONFIRMED, CompanyMatch.INFERRED, CompanyMatch.UNKNOWN]
)
def test_non_mismatch_states_are_storable(match):
    assert _evidence(company_match=match).company_match is match


def test_match_string_is_coerced_to_the_enum():
    assert _evidence(company_match="confirmed").company_match is CompanyMatch.CONFIRMED


# --- confidence is bounded, never fabricated -----------------------------


@pytest.mark.parametrize("value", [-0.01, 1.01, 2.0])
def test_confidence_outside_zero_to_one_is_rejected(value):
    with pytest.raises(ValueError):
        _evidence(confidence=value)


def test_confidence_rejects_non_numbers():
    """A tier LABEL is not a probability — it must not land here."""
    with pytest.raises(TypeError):
        _evidence(confidence="verified")


def test_default_confidence_is_zero_not_a_flattering_guess():
    assert _evidence().confidence == 0.0


@pytest.mark.parametrize("value", [0.0, 0.5, 1.0, 0])
def test_confidence_bounds_are_inclusive(value):
    assert _evidence(confidence=value).confidence == float(value)


def test_verification_carries_the_producers_own_tier_verbatim():
    """The old label coexists with confidence; neither overrides the other."""
    record = _evidence(verification="verified", confidence=0.0)
    assert record.verification == "verified"
    assert record.confidence == 0.0


# --- excerpt is a quotation, not a copy of the page ----------------------


def test_excerpt_is_truncated_to_the_bound():
    record = _evidence(excerpt="x" * (MAX_EXCERPT_CHARS + 500))
    assert len(record.excerpt) == MAX_EXCERPT_CHARS


def test_excerpt_under_the_bound_is_untouched():
    record = _evidence(excerpt="Awarded $4.2M for the Riverside Bridge.")
    assert record.excerpt == "Awarded $4.2M for the Riverside Bridge."


# --- dedup ----------------------------------------------------------------


def test_dedup_key_ignores_the_title():
    """A publisher may rewrite a headline without changing the fact."""
    a = _evidence(title="Austin awards bridge contract")
    b = _evidence(title="Bridge contract awarded in Austin")
    assert a.dedup_key == b.dedup_key


def test_dedup_key_changes_with_the_quoted_text():
    a = _evidence(excerpt="Awarded $4.2M.")
    b = _evidence(excerpt="Awarded $9.8M.")
    assert a.dedup_key != b.dedup_key


def test_dedup_key_changes_with_the_page():
    a = _evidence(source_url="https://example.gov/a")
    b = _evidence(source_url="https://example.gov/b")
    assert a.dedup_key != b.dedup_key


def test_content_hash_is_deterministic():
    """Same page + same sentence == same hash, so re-research updates."""
    assert content_hash("https://x.gov/1", "text") == content_hash(
        "https://x.gov/1", "text"
    )
    assert content_hash("https://x.gov/1", " text ") == content_hash(
        "https://x.gov/1", "text"
    )


def test_evidence_id_is_deterministic():
    assert new_evidence_id("a|b|c") == new_evidence_id("a|b|c")
    assert new_evidence_id("a|b|c") != new_evidence_id("a|b|d")
    assert new_evidence_id("a").startswith("ev_")


# --- event candidate ------------------------------------------------------


def test_event_candidate_defaults_to_none_until_the_engine_reads_it():
    """An unread observation honestly has no candidate."""
    assert _evidence().event_candidate is None
    assert _evidence().to_row()["event_candidate"] == ""


def test_event_candidate_serializes_to_its_bare_value():
    record = _evidence(event_candidate=EventType.CONTRACT_AWARDED)
    assert record.to_row()["event_candidate"] == "contract_awarded"


def test_event_candidate_string_is_normalized():
    assert _evidence(event_candidate="CONTRACT_AWARDED").event_candidate == (
        "contract_awarded"
    )


# --- round trip -----------------------------------------------------------


def test_row_round_trip_preserves_every_stored_field():
    original = _evidence(
        source_type="usaspending",
        publisher="City of Austin",
        title="Award notice",
        excerpt="Awarded $4.2M.",
        published_at="2026-09-11",
        company_match=CompanyMatch.CONFIRMED,
        evidence_type=EvidenceType.GOVERNMENT_AWARD,
        project_key="city-of-austin-riverside-bridge",
        event_candidate=EventType.CONTRACT_AWARDED,
        confidence=0.9,
        legacy_type="bid_award",
        verification="verified",
    )
    rebuilt = CanonicalEvidence.from_row(original.to_row())
    assert rebuilt.to_row() == original.to_row()


def test_from_row_tolerates_a_row_missing_optional_columns():
    """Forward compatibility: an older row lacks newer columns."""
    rebuilt = CanonicalEvidence.from_row(
        {
            "evidence_id": "ev_x",
            "company_id": "cmp_x",
            "source_url": "https://x.gov/1",
            "retrieved_at": "2026-09-18T10:00:00",
        }
    )
    assert rebuilt.company_match is CompanyMatch.UNKNOWN
    assert rebuilt.confidence == 0.0
    assert rebuilt.legacy_type == ""


# --- company identity -----------------------------------------------------


def test_company_identity_serializes_for_api_output():
    identity = CompanyIdentity(
        company_id="cmp_1", company_key="acme.com", name="Acme", domains=("acme.com",)
    )
    assert identity.to_dict() == {
        "company_id": "cmp_1",
        "company_key": "acme.com",
        "name": "Acme",
        "domains": ["acme.com"],
    }


def test_company_identity_is_immutable():
    identity = CompanyIdentity(company_id="cmp_1")
    with pytest.raises(FrozenInstanceError):
        identity.company_id = "cmp_2"  # type: ignore[misc]

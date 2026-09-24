"""Phase 2 Step 3 — deterministic city / state verification.

Fully offline — no network, no AI. Pins the accuracy-first behaviour:

- the search query is search intent, NEVER company location evidence;
- city is verified ONLY from explicit evidence (address, headquarters /
  "located in", licensing or directory listing);
- "serving Dallas", "Dallas project", "DFW area", "Dallas-Fort Worth
  metroplex", a company name, and state-only statements NEVER verify a city;
- state requires explicit evidence — never defaulted to TX / Texas / USA;
- conflicting city evidence is recorded, never silently resolved;
- every verified claim carries FieldEvidence; the query never becomes
  evidence; no evidence => no invented FieldEvidence.
"""

from __future__ import annotations

import pytest

from app.engines.verification.location_verifier import LocationVerifier, state_from_text
from app.engines.verification.models import VerificationStatus


def _verify(
    *,
    city="",
    state="",
    evidence_texts=None,
    query="",
    source="",
    source_url="",
):
    return LocationVerifier().verify(
        city=city,
        state=state,
        evidence_texts=evidence_texts,
        query=query,
        source=source,
        source_url=source_url,
    )


# ---------------------------------------------------------------------------
# The query is search intent, never evidence
# ---------------------------------------------------------------------------


class TestQueryIsNotEvidence:
    def test_query_alone_verifies_nothing(self):
        result = _verify(query="Dallas Texas")
        assert result.city is None
        assert result.state is None
        assert result.location_status == VerificationStatus.unknown
        assert result.confidence == 0.0
        assert result.evidence == []

    def test_query_dallas_with_empty_evidence(self):
        result = _verify(city="Dallas", state="TX", query="Dallas Texas")
        assert result.city is None
        assert result.state is None
        assert result.location_status == VerificationStatus.unknown

    def test_query_never_becomes_field_evidence(self):
        result = _verify(
            query="Dallas Texas",
            evidence_texts=["123 Main St, Houston, TX"],
        )
        values = [e.value for e in result.evidence]
        assert "Dallas Texas" not in values
        assert "Dallas" not in values  # the query city never leaks in
        assert result.city == "Houston"


# ---------------------------------------------------------------------------
# State-only evidence: state yes, city NO
# ---------------------------------------------------------------------------


class TestStateOnly:
    def test_state_only_evidence_has_no_city(self):
        result = _verify(
            evidence_texts=["Texas roofing contractor"], query="Dallas Texas"
        )
        assert result.state == "TX"
        assert result.city is None  # never Dallas
        assert result.location_status == VerificationStatus.partially_verified

    def test_state_only_plus_dallas_query_city_still_none(self):
        result = _verify(
            evidence_texts=["Texas contractor"], query="Dallas Texas"
        )
        assert result.city is None
        assert result.state == "TX"


# ---------------------------------------------------------------------------
# Explicit address -> verified
# ---------------------------------------------------------------------------


class TestExplicitAddress:
    def test_explicit_address_verified(self):
        result = _verify(evidence_texts=["123 Main St, Dallas, TX"])
        assert result.city == "Dallas"
        assert result.state == "TX"
        assert result.location_status == VerificationStatus.verified
        assert result.confidence > 0.5

    def test_headquarters_and_located_in_verified(self):
        result = _verify(
            evidence_texts=[
                "Headquarters: Dallas, Texas",
                "Located in Dallas, TX",
            ]
        )
        assert result.city == "Dallas"
        assert result.state == "TX"
        assert result.location_status == VerificationStatus.verified

    def test_address_evidence_carries_provenance(self):
        result = _verify(
            evidence_texts=["123 Main St, Dallas, TX"],
            source="licensing_board",
            source_url="https://tlic.example.gov/license/12345",
        )
        assert len(result.evidence) == 2
        fields = {e.field for e in result.evidence}
        assert fields == {"city", "state"}
        city_ev = next(e for e in result.evidence if e.field == "city")
        assert city_ev.value == "Dallas"
        assert city_ev.source == "licensing_board"
        assert city_ev.source_url == "https://tlic.example.gov/license/12345"
        assert city_ev.is_reliable is True
        assert isinstance(city_ev.confidence, float)
        assert city_ev.fetched_at  # auto-stamped


# ---------------------------------------------------------------------------
# Service-area / non-location language NEVER verifies a city
# ---------------------------------------------------------------------------


class TestServiceAreaRejected:
    @pytest.mark.parametrize(
        "evidence",
        [
            "Serving Dallas, TX",
            "serves the Dallas, TX area",
            "Dallas project",
            "projects in Dallas",
            "DFW area",
            "Dallas-Fort Worth metroplex",
        ],
    )
    def test_service_area_never_verifies_city(self, evidence):
        result = _verify(evidence_texts=[evidence], query="Dallas Texas")
        assert result.city is None
        assert result.state is None
        assert result.location_status == VerificationStatus.unknown

    def test_serves_dallas_rejected_even_with_query(self):
        result = _verify(evidence_texts=["Serving Dallas, TX"], query="Dallas Texas")
        assert result.city is None
        assert result.state is None
        assert any("service-area" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# A company name is not location evidence
# ---------------------------------------------------------------------------


class TestCompanyNameNotEvidence:
    def test_company_name_alone_does_not_prove_city(self):
        result = _verify(
            city="Dallas",
            state="TX",
            evidence_texts=["Dallas Roofing LLC"],
            query="Roofing Dallas Texas",
        )
        assert result.city is None
        assert result.state is None
        assert result.location_status == VerificationStatus.unknown
        assert any("unverified claim" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Explicit evidence beats a conflicting query / candidate
# ---------------------------------------------------------------------------


class TestEvidenceBeatsQuery:
    def test_houston_evidence_not_overridden_by_dallas_query(self):
        result = _verify(
            city="Dallas",
            state="TX",
            evidence_texts=["123 Main St, Houston, TX"],
            query="Dallas Texas",
        )
        assert result.city == "Houston"
        assert result.state == "TX"
        assert result.location_status == VerificationStatus.verified
        assert result.location_match is False  # verified city != query target

    def test_matching_query_location_match_true(self):
        result = _verify(
            evidence_texts=["123 Main St, Dallas, TX"], query="Dallas Texas"
        )
        assert result.location_status == VerificationStatus.verified
        assert result.location_match is True


# ---------------------------------------------------------------------------
# Conflicting evidence is recorded, never silently resolved
# ---------------------------------------------------------------------------


class TestConflict:
    def test_conflicting_cities_not_silently_resolved(self):
        result = _verify(
            evidence_texts=[
                "123 Main St, Dallas, TX",
                "123 Main St, Houston, TX",
            ]
        )
        assert result.city is None  # never silently Dallas or Houston
        assert result.state == "TX"  # both sources agree on the state
        assert result.location_status in (
            VerificationStatus.unknown,
            VerificationStatus.partially_verified,
        )
        assert any("conflicting" in r for r in result.reasons)
        conflicting_cities = {
            e.value for e in result.evidence if e.field == "city"
        }
        assert conflicting_cities == {"Dallas", "Houston"}
        assert all(
            e.is_reliable is False for e in result.evidence if e.field == "city"
        )


# ---------------------------------------------------------------------------
# No evidence => unknown, no invented FieldEvidence
# ---------------------------------------------------------------------------


class TestNoEvidence:
    def test_no_evidence_no_field_evidence(self):
        result = _verify()
        assert result.city is None
        assert result.state is None
        assert result.location_status == VerificationStatus.unknown
        assert result.confidence == 0.0
        assert result.evidence == []


# ---------------------------------------------------------------------------
# Deterministic only + output shape
# ---------------------------------------------------------------------------


class TestDeterminismAndShape:
    def test_verify_has_no_ai_input(self):
        """No AI / LLM / network parameter — verification is deterministic."""
        import inspect

        params = inspect.signature(LocationVerifier.verify).parameters
        for forbidden in ("ai", "llm", "model", "fetcher", "http"):
            assert forbidden not in params

    def test_output_shape(self):
        result = _verify(evidence_texts=["123 Main St, Dallas, TX"])
        assert isinstance(result.city, str) or result.city is None
        assert isinstance(result.state, str) or result.state is None
        assert result.location_status in VerificationStatus
        assert isinstance(result.location_match, bool)
        assert isinstance(result.confidence, float)
        assert 0.0 <= result.confidence <= 1.0
        assert isinstance(result.reasons, list) and result.reasons
        assert isinstance(result.evidence, list)

        data = result.to_dict()
        for key in (
            "city",
            "state",
            "location_status",
            "location_match",
            "confidence",
            "reasons",
            "evidence",
        ):
            assert key in data
        ev = data["evidence"][0]
        for key in (
            "field",
            "value",
            "source",
            "source_url",
            "confidence",
            "fetched_at",
            "is_reliable",
        ):
            assert key in ev


# ---------------------------------------------------------------------------
# The canonical free-text -> state fold (added 2026-09-21)
# ---------------------------------------------------------------------------


class TestStateFromText:
    """``state_from_text`` is the ONE answer to "which state is this?".

    It exists because two other answers had grown: the scorer's
    ``"texas" in loc or "tx" in loc`` substring test and ``harvester.store``'s
    own tail-reading fold. The scorer's version credited any string merely
    CONTAINING "tx" and read a bare state name anywhere in a compound string
    as proof — measured live, that handed the Texas service-area signal to 14
    dossiers whose real home is elsewhere.

    The rule pinned here is: exactly one state, or "". A string naming two is
    ambiguous and this module's standing policy for conflicting location
    evidence is to claim neither.
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # the forms the live store actually carries
            ("Houston, TX", "TX"),
            ("Dallas, TX", "TX"),
            ("Houston, Texas", "TX"),
            ("Texas", "TX"),
            ("New York, NY", "NY"),
            ("Miami, FL", "FL"),
            # forms the OLD tail-reading fold already handled — pinned so the
            # delegation in harvester.store is a no-op for them
            ("Dallas TX", "TX"),
            ("Vancouver, WA", "WA"),
            ("austin tx", "TX"),
            ("washington", "WA"),
            ("Harris County, Texas", "TX"),
            # forms it MISSED — these are why the fold moved to this module
            ("Plano TX 75024", "TX"),
            ("Houston, TX 77002", "TX"),
            ("Plano TX 75024-1234", "TX"),
            ("Texas, USA", "TX"),
            ("Texas, US", "TX"),
            ("Texas, United States", "TX"),
        ],
    )
    def test_a_single_state_is_folded(self, text, expected):
        assert state_from_text(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "London",
            "Smith Jones",
            "unverified",
            "Netherlands",
            # a US code we do not know is still not a US state
            "Niagara Falls, ON",
            "Toronto, Canada",
        ],
    )
    def test_a_string_naming_no_us_state_is_an_honest_miss(self, text):
        assert state_from_text(text) == ""

    @pytest.mark.parametrize(
        "text",
        [
            # THE rule: two US states is ambiguous, so neither is claimed.
            # All four were measured on the live store, and every one is a
            # company whose real headquarters is elsewhere.
            "Dallas, TX (corporate HQ: Tustin, CA)",
            "Seattle, WA (headquarters); offices in Houston, TX and other US cities",
            "Allentown, PA (HQ); Houston, TX (operational presence)",
            # a state name inside a city name is still a mention of that state
            "Kansas City, MO",
        ],
    )
    def test_a_string_naming_two_states_is_not_resolved(self, text):
        assert state_from_text(text) == ""

    def test_a_compound_string_about_one_state_still_folds(self):
        """The counter-case to the rule above: verbosity is not ambiguity.

        Every place named here is in Texas, so the string is not ambiguous and
        the fold must still resolve it — a rule that rejected any compound
        string would be a different, worse rule.
        """
        assert state_from_text(
            "Lubbock, TX (with additional offices in Dallas, TX and San Angelo, TX)"
        ) == "TX"
        assert state_from_text(
            "Texas (offices in Grand Prairie, Houston, and Buda)"
        ) == "TX"

    def test_a_non_us_home_with_a_us_office_names_that_one_state(self):
        """An honest LIMIT, pinned so nobody reads it as a guarantee.

        "Amsterdam, Netherlands (with Dallas, TX office)" names exactly ONE
        US state, so the fold returns TX — by its own contract it reports the
        US state a string names, not where the company is headquartered. It
        cannot detect "home elsewhere, US office here" when the other place is
        not a US state, because a non-US place is not a state it knows.

        That is a limitation of a US-state fold, not a defect in it, and the
        live store holds exactly one such dossier. Detecting it would need a
        non-US place gazetteer, which is not what this module is for.
        """
        text = "Amsterdam, Netherlands (with Dallas, TX office)"
        assert state_from_text(text) == "TX"

    def test_service_area_language_is_not_location_evidence(self):
        """The module's standing rule, applied to the fold as well.

        A company that SERVES Texas is not a company IN Texas.
        """
        assert state_from_text("TX area") == ""
        assert state_from_text("Dallas-Fort Worth metroplex") == ""

    def test_a_word_ending_in_a_state_code_is_not_that_state(self):
        """``\b`` on both sides is load-bearing.

        Without it "Berlin" reads as IN (Indiana) and every location ending in
        a two-letter US code becomes a coin flip.
        """
        assert state_from_text("Berlin") == ""
        assert state_from_text("Dublin") == ""
        assert state_from_text("Turin") == ""

    def test_a_real_trailing_code_still_folds(self):
        """The counter-case: the boundary must not abolish the form."""
        assert state_from_text("Portland OR") == "OR"
        assert state_from_text("Portland, OR") == "OR"

    def test_the_harvester_fold_delegates_to_this_one(self):
        """One implementation, two names — the whole point of the change.

        If these ever disagree, the scorer and the serve-time filter are
        answering the same question differently again.
        """
        from app.harvester.store import state_from_location

        for text in (
            "Houston, TX", "Plano TX 75024", "Texas, USA", "Dallas TX",
            "Seattle, WA (headquarters); offices in Houston, TX",
            "Niagara Falls, ON", "", "London", "unverified",
        ):
            assert state_from_location(text) == state_from_text(text), text

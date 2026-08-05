"""Infrastructure tests for the Direct Website Discovery evidence model.

Phase 2.3A scope: the model only. Nothing here extracts a field from a
page, because no extractor exists yet. These tests pin the shape of a
provenance record and the invariants it guarantees — not what any
particular website contains.
"""

from __future__ import annotations

import pytest

from app.discovery.website.confidence import Confidence
from app.discovery.website.evidence import (
    EvidenceSet,
    ExtractedField,
    FieldEvidence,
    normalize_field,
)

PAGE = "https://acme.com/contact"


def make_evidence(field="phone", page_url=PAGE, **kwargs) -> FieldEvidence:
    """Build a valid record so each test states only what it is about."""
    return FieldEvidence(field=field, page_url=page_url, **kwargs)


class TestExtractedField:
    def test_values_are_canonical_strings(self):
        assert ExtractedField.COMPANY_NAME.value == "company_name"
        assert ExtractedField.PHONE.value == "phone"

    def test_str_renders_the_bare_value(self):
        assert str(ExtractedField.EMAIL) == "email"

    def test_is_a_str_subclass(self):
        # This is what makes the enum open: any string is a usable field.
        assert isinstance(ExtractedField.PHONE, str)

    def test_covers_the_declared_field_list(self):
        assert {f.value for f in ExtractedField} == {
            "company_name",
            "phone",
            "email",
            "address",
            "leadership",
            "social",
            "services",
        }


class TestNormalizeField:
    def test_enum_member_returns_its_value(self):
        assert normalize_field(ExtractedField.ADDRESS) == "address"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("PHONE", "phone"),
            ("  phone  ", "phone"),
            ("Company_Name", "company_name"),
            ("EMAIL", "email"),
        ],
    )
    def test_case_and_whitespace_are_normalized(self, raw, expected):
        assert normalize_field(raw) == expected

    def test_unknown_field_is_accepted(self):
        # Open by design: a future extractor may record a field the
        # framework has never heard of, without this module changing.
        assert normalize_field("Certifications") == "certifications"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_field_raises(self, value):
        with pytest.raises(ValueError):
            normalize_field(value)

    @pytest.mark.parametrize("value", [None, 42, [], object()])
    def test_non_string_raises_type_error(self, value):
        with pytest.raises(TypeError):
            normalize_field(value)


class TestFieldEvidenceConstruction:
    def test_required_fields_are_stored(self):
        evidence = make_evidence()
        assert evidence.field == "phone"
        assert evidence.page_url == PAGE

    def test_optional_fields_default_to_empty(self):
        evidence = make_evidence()
        assert evidence.selector == ""
        assert evidence.method == ""
        assert evidence.snippet == ""
        assert evidence.extracted_at == ""

    def test_confidence_defaults_to_unknown(self):
        # Nothing has been established about a bare record, and that is
        # a stronger default than quietly assuming certainty.
        assert make_evidence().confidence == Confidence.UNKNOWN

    def test_every_field_is_stored(self):
        evidence = FieldEvidence(
            field=ExtractedField.PHONE,
            page_url=PAGE,
            selector="a.tel",
            method="tel_link",
            confidence=Confidence(0.95),
            snippet="Call (555) 123-4567",
            extracted_at="2026-08-05T12:00:00Z",
        )
        assert evidence.selector == "a.tel"
        assert evidence.method == "tel_link"
        assert evidence.snippet == "Call (555) 123-4567"
        assert evidence.extracted_at == "2026-08-05T12:00:00Z"


class TestFieldCanonicalization:
    def test_enum_is_stored_as_its_value(self):
        assert make_evidence(field=ExtractedField.COMPANY_NAME).field == "company_name"

    def test_raw_string_is_normalized(self):
        assert make_evidence(field="  PHONE  ").field == "phone"

    def test_unknown_field_is_preserved(self):
        assert make_evidence(field="certifications").field == "certifications"

    def test_empty_field_raises(self):
        with pytest.raises(ValueError):
            make_evidence(field="")


class TestPageURLCanonicalization:
    def test_bare_domain_gains_a_scheme(self):
        assert make_evidence(page_url="acme.com/contact").page_url == PAGE

    @pytest.mark.parametrize(
        "variant",
        [
            "https://www.acme.com/contact",
            "HTTPS://ACME.COM/contact",
            "https://acme.com/contact/",
            "https://acme.com:443/contact",
        ],
    )
    def test_variants_agree_on_their_origin(self, variant):
        # Two observations from the same page must not look like two
        # pages, or provenance stops being comparable.
        assert make_evidence(page_url=variant).page_url == PAGE

    def test_fragment_is_dropped(self):
        assert make_evidence(page_url="acme.com/contact#form").page_url == PAGE

    def test_blank_page_url_raises(self):
        # Evidence that cannot say which page it came from is not evidence.
        with pytest.raises(ValueError):
            make_evidence(page_url="")

    def test_whitespace_only_page_url_raises(self):
        with pytest.raises(ValueError):
            make_evidence(page_url="   ")

    @pytest.mark.parametrize(
        "value",
        ["mailto:sales@acme.com", "tel:+15551234567", "javascript:void(0)", "ftp://x.com"],
    )
    def test_non_web_page_url_raises(self, value):
        with pytest.raises(ValueError):
            make_evidence(page_url=value)

    @pytest.mark.parametrize("value", [None, 42])
    def test_non_string_page_url_raises_type_error(self, value):
        with pytest.raises(TypeError):
            make_evidence(page_url=value)


class TestConfidenceCoercion:
    def test_float_becomes_a_confidence(self):
        evidence = make_evidence(confidence=0.75)
        assert evidence.confidence == Confidence(0.75)
        assert isinstance(evidence.confidence, Confidence)

    def test_existing_confidence_is_kept(self):
        confidence = Confidence(0.5)
        assert make_evidence(confidence=confidence).confidence is confidence

    def test_out_of_range_confidence_raises(self):
        with pytest.raises(ValueError):
            make_evidence(confidence=1.7)

    def test_non_numeric_confidence_raises(self):
        with pytest.raises(TypeError):
            make_evidence(confidence="high")


class TestImmutability:
    def test_fields_cannot_be_reassigned(self):
        # Evidence records what was observed; a correction is a new
        # observation, not an edit to history.
        evidence = make_evidence()
        with pytest.raises(Exception):
            evidence.page_url = "https://elsewhere.com/"

    def test_equal_records_compare_equal(self):
        assert make_evidence(selector="a.tel") == make_evidence(selector="a.tel")

    def test_is_hashable(self):
        assert len({make_evidence(), make_evidence(), make_evidence(field="email")}) == 2


class TestFieldEvidenceSerialization:
    def test_to_dict_has_every_field(self):
        assert set(make_evidence().to_dict()) == {
            "field",
            "page_url",
            "selector",
            "method",
            "confidence",
            "snippet",
            "extracted_at",
        }

    def test_confidence_serializes_as_a_plain_float(self):
        value = make_evidence(confidence=0.95).to_dict()["confidence"]
        assert value == 0.95
        assert isinstance(value, float)

    def test_round_trip_preserves_the_record(self):
        original = make_evidence(
            selector="a.tel",
            method="tel_link",
            confidence=0.95,
            snippet="(555) 123-4567",
            extracted_at="2026-08-05T12:00:00Z",
        )
        assert FieldEvidence.from_dict(original.to_dict()) == original

    def test_from_dict_tolerates_missing_optionals(self):
        evidence = FieldEvidence.from_dict({"field": "email", "page_url": PAGE})
        assert evidence.selector == ""
        assert evidence.confidence == Confidence.UNKNOWN

    @pytest.mark.parametrize(
        "data", [{"page_url": PAGE}, {"field": "phone"}]
    )
    def test_from_dict_requires_field_and_page_url(self, data):
        with pytest.raises(KeyError):
            FieldEvidence.from_dict(data)

    def test_str_names_the_field_page_and_locator(self):
        text = str(make_evidence(selector="a.tel", confidence=0.95))
        assert "phone" in text
        assert PAGE in text
        assert "a.tel" in text


class TestEvidenceSetAdd:
    def test_add_records_an_observation(self):
        evidence = EvidenceSet()
        evidence.add(make_evidence())
        assert len(evidence) == 1

    def test_extend_records_several(self):
        evidence = EvidenceSet()
        evidence.extend([make_evidence(), make_evidence(field="email")])
        assert len(evidence) == 2

    def test_constructor_seeds_from_an_iterable(self):
        assert len(EvidenceSet([make_evidence(), make_evidence(field="email")])) == 2

    def test_a_fresh_set_is_empty(self):
        evidence = EvidenceSet()
        assert len(evidence) == 0
        assert evidence.fields == ()

    def test_add_rejects_a_non_evidence(self):
        with pytest.raises(TypeError):
            EvidenceSet().add({"field": "phone"})

    def test_repeated_observations_of_one_field_are_all_kept(self):
        # A phone in the header, the footer and the contact page is three
        # pieces of support, not one — collapsing them would destroy the
        # very thing evidence exists to record.
        evidence = EvidenceSet()
        evidence.add(make_evidence(selector="header a.tel"))
        evidence.add(make_evidence(selector="footer a.tel"))
        assert len(evidence.for_field("phone")) == 2


class TestEvidenceSetQuery:
    def test_for_field_returns_insertion_order(self):
        evidence = EvidenceSet(
            [make_evidence(selector="first"), make_evidence(selector="second")]
        )
        assert [e.selector for e in evidence.for_field("phone")] == ["first", "second"]

    def test_for_field_accepts_the_enum(self):
        evidence = EvidenceSet([make_evidence()])
        assert len(evidence.for_field(ExtractedField.PHONE)) == 1

    def test_for_field_is_empty_for_an_unseen_field(self):
        assert EvidenceSet().for_field("email") == ()

    def test_for_field_returns_a_tuple(self):
        assert isinstance(EvidenceSet([make_evidence()]).for_field("phone"), tuple)

    def test_has_reports_presence(self):
        evidence = EvidenceSet([make_evidence()])
        assert evidence.has("phone") is True
        assert evidence.has("email") is False

    def test_fields_lists_first_seen_order(self):
        evidence = EvidenceSet(
            [make_evidence(field="email"), make_evidence(field="phone")]
        )
        assert evidence.fields == ("email", "phone")


class TestEvidenceSetContainer:
    def test_len_counts_every_observation_not_every_field(self):
        evidence = EvidenceSet(
            [make_evidence(), make_evidence(), make_evidence(field="email")]
        )
        assert len(evidence) == 3

    def test_iteration_yields_every_observation(self):
        evidence = EvidenceSet([make_evidence(), make_evidence(field="email")])
        assert {e.field for e in evidence} == {"phone", "email"}

    def test_contains_accepts_a_string_and_the_enum(self):
        evidence = EvidenceSet([make_evidence()])
        assert "phone" in evidence
        assert ExtractedField.PHONE in evidence
        assert "email" not in evidence

    def test_contains_is_safe_for_unusable_keys(self):
        # Membership answers rather than raising, so an odd key cannot
        # break a caller's `if x in evidence` guard.
        evidence = EvidenceSet([make_evidence()])
        assert 42 not in evidence
        assert None not in evidence
        assert "" not in evidence

    def test_repr_reports_counts_per_field(self):
        evidence = EvidenceSet([make_evidence(), make_evidence()])
        assert "phone=2" in repr(evidence)

    def test_repr_of_an_empty_set_says_so(self):
        assert "empty" in repr(EvidenceSet())


class TestEvidenceSetSerialization:
    def test_to_dict_groups_by_field(self):
        evidence = EvidenceSet(
            [make_evidence(), make_evidence(), make_evidence(field="email")]
        )
        data = evidence.to_dict()
        assert set(data) == {"phone", "email"}
        assert len(data["phone"]) == 2

    def test_round_trip_preserves_every_observation(self):
        evidence = EvidenceSet(
            [
                make_evidence(selector="a.tel", confidence=0.9),
                make_evidence(field="email", method="mailto_link"),
            ]
        )
        restored = EvidenceSet.from_dict(evidence.to_dict())
        assert restored.to_dict() == evidence.to_dict()
        assert len(restored) == len(evidence)

    def test_empty_set_serializes_to_an_empty_mapping(self):
        assert EvidenceSet().to_dict() == {}


class TestNoExtractionLogic:
    """Phase 2.3A ships the model only — extraction arrives later."""

    @pytest.mark.parametrize(
        "name", ["extract", "parse", "fetch", "crawl", "from_html", "from_page"]
    )
    def test_the_model_does_not_extract(self, name):
        assert not hasattr(FieldEvidence, name)
        assert not hasattr(EvidenceSet, name)

    @pytest.mark.parametrize(
        "name", ["best_for_field", "best", "winner", "resolve", "rank"]
    )
    def test_the_model_does_not_choose_between_observations(self, name):
        # Deciding which of three observed phone numbers is correct is
        # extraction policy, and it arrives with the extractors.
        assert not hasattr(EvidenceSet, name)

    @pytest.mark.parametrize(
        "name", ["requests", "aiohttp", "httpx", "BeautifulSoup", "bs4", "selectolax"]
    )
    def test_the_module_pulls_in_no_http_or_parsing_library(self, name):
        from app.discovery.website import evidence as module

        assert name not in vars(module)

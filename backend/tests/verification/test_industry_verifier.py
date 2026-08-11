"""Phase 2 Step 2 — deterministic industry / business-type verification.

Fully offline — the verifier performs no network and uses no AI. Pins the
accuracy-first behaviour:

- manufacturers / suppliers / distributors / wholesalers / associations and
  material/product-only companies must never be accepted as contractors;
- negative evidence has priority over weak contractor keywords;
- a company is contractor ONLY when a construction trade matches AND strong
  service evidence (install / repair / replacement / contractor) is present;
- query / location context can never influence the result (no such input);
- business_type is always one of the five approved values.
"""

from __future__ import annotations

import pytest

from app.engines.verification.industry_verifier import (
    BUSINESS_TYPES,
    IndustryVerifier,
)
from app.engines.verification.models import VerificationStatus


def _verify(name, *, description="", title="", website="", source="", source_url=""):
    return IndustryVerifier().verify(
        company_name=name,
        title=title,
        description=description,
        website=website,
        source=source,
        source_url=source_url,
    )


# ---------------------------------------------------------------------------
# Strong contractor evidence -> contractor
# ---------------------------------------------------------------------------


class TestContractorAccepted:
    @pytest.mark.parametrize(
        "name,description",
        [
            ("Acme Roofing", "roofing contractor serving Dallas, TX"),
            ("Peak Roofing", "commercial roofing contractor"),
            ("Best Roofing", "roofing installation and replacement"),
            ("True Roof", "roof installation and replacement"),
            ("Apex Roofing", "roof repair contractor"),
            ("Crest Roofing", "roofing construction company"),
            ("CoolAir", "HVAC contractor"),
            ("Flow Plumbing", "plumbing contractor"),
            ("Bright Electric", "electrical contractor"),
            ("Solid Concrete", "concrete contractor"),
        ],
    )
    def test_strong_contractor_evidence_accepted(self, name, description):
        result = _verify(name, description=description)
        assert result.business_type == "contractor"
        assert result.industry_match is True
        assert result.verification_status == VerificationStatus.verified
        assert result.confidence > 0.5

    def test_trade_and_service_signals_recorded(self):
        result = _verify("Acme Roofing", description="licensed roofing contractor")
        assert result.trade_category == "roofing"
        assert any("roofing" in r and "service" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Manufacturer / supplier / distributor / wholesaler -> REJECT
# ---------------------------------------------------------------------------


class TestManufacturerRejected:
    @pytest.mark.parametrize(
        "description",
        [
            "roofing materials manufacturer",
            "shingle manufacturer",
            "roofing products manufacturer",
            "insulation manufacturer",
            "building materials manufacturer",
            "manufacturer of roofing products",
        ],
    )
    def test_manufacturer_rejected(self, description):
        result = _verify("Some Co", description=description)
        assert result.business_type == "manufacturer"
        assert result.industry_match is False
        assert result.verification_status == VerificationStatus.rejected
        assert result.confidence > 0.5

    def test_owens_corning_style_manufacturer_rejected(self):
        """The flagship accuracy case: a materials manufacturer is rejected
        even though its name is a well-known brand."""
        result = _verify(
            "Owens Corning",
            description=(
                "Owens Corning is a global building and construction "
                "materials manufacturer of roofing shingles and insulation."
            ),
        )
        assert result.business_type == "manufacturer"
        assert result.industry_match is False
        assert result.verification_status == VerificationStatus.rejected


class TestSupplierRejected:
    @pytest.mark.parametrize(
        "description",
        [
            "roofing materials supplier",
            "roofing supply company",
            "roofing distributor",
            "building materials supplier",
            "wholesale roofing products",
            "roofing products distributor",
        ],
    )
    def test_supplier_distributor_wholesaler_rejected(self, description):
        result = _verify("Some Co", description=description)
        assert result.business_type == "supplier"
        assert result.industry_match is False
        assert result.verification_status == VerificationStatus.rejected
        assert result.confidence > 0.5


# ---------------------------------------------------------------------------
# Association -> REJECT
# ---------------------------------------------------------------------------


class TestAssociationRejected:
    @pytest.mark.parametrize(
        "name,description",
        [
            ("National Roofing Association", ""),
            ("Builders Association", ""),
            ("Some Co", "trade association for local contractors"),
            ("Some Co", "roofing association serving its region"),
            ("Some Co", "industry association"),
            ("Some Co", "contractors association of Texas"),
        ],
    )
    def test_association_rejected(self, name, description):
        result = _verify(name, description=description)
        assert result.business_type == "association"
        assert result.industry_match is False
        assert result.verification_status == VerificationStatus.rejected


# ---------------------------------------------------------------------------
# Material/product-only language -> NEVER contractor (rule 5)
# ---------------------------------------------------------------------------


class TestMaterialOnlyNotContractor:
    @pytest.mark.parametrize(
        "description",
        [
            "roofing materials",
            "roofing products",
            "shingles and roofing products",
            "construction products",
            "building products",
            "roofing systems",
        ],
    )
    def test_material_only_language_never_contractor(self, description):
        result = _verify("Acme Materials", description=description)
        assert result.business_type == "unknown"
        assert result.industry_match is False
        assert result.verification_status == VerificationStatus.unknown

    def test_product_language_with_service_evidence_is_contractor(self):
        """Rule 5 escape: material language is OK when separate strong
        service evidence (installation/repair) is present."""
        result = _verify(
            "Acme Roofing",
            description="we install and repair shingle roofing systems",
        )
        assert result.business_type == "contractor"
        assert result.industry_match is True
        assert result.verification_status == VerificationStatus.verified


# ---------------------------------------------------------------------------
# Negative evidence priority + insufficient evidence
# ---------------------------------------------------------------------------


class TestNegativePriority:
    def test_manufacturer_overrides_weak_contractor_keyword(self):
        """Rule 6: strong negative evidence beats weak contractor language."""
        result = _verify(
            "Acme Roofing",
            description=(
                "roofing materials manufacturer with contractor services "
                "and construction support"
            ),
        )
        assert result.business_type == "manufacturer"
        assert result.industry_match is False
        assert result.verification_status == VerificationStatus.rejected

    def test_non_construction_entity_rejected(self):
        """Software / directory / media entities are never contractors."""
        result = _verify("Acme SaaS", description="cloud-based software for roofers")
        assert result.business_type == "unknown"
        assert result.industry_match is False
        assert result.verification_status == VerificationStatus.rejected


class TestInsufficientEvidence:
    def test_empty_evidence_is_unknown(self):
        result = _verify("Acme Roofing")
        assert result.business_type == "unknown"
        assert result.industry_match is False
        assert result.verification_status == VerificationStatus.unknown
        assert result.confidence == 0.0

    def test_generic_company_with_no_service_evidence_is_unknown(self):
        result = _verify("Roofing Company")
        assert result.business_type == "unknown"
        assert result.industry_match is False
        assert result.verification_status == VerificationStatus.unknown


# ---------------------------------------------------------------------------
# No query-derived evidence + evidence provenance
# ---------------------------------------------------------------------------


class TestNoQueryContext:
    def test_verify_has_no_query_input(self):
        """verify() exposes no query/location/industry-hint parameter, so
        query context can never be passed as evidence."""
        import inspect

        params = inspect.signature(IndustryVerifier.verify).parameters
        assert "query" not in params
        assert "location" not in params
        assert "industry_hint" not in params

    def test_query_style_words_cannot_flip_manufacturer(self):
        """'Roofing', 'Dallas', 'Texas' in the text do not override a
        manufacturer signal — query-like context is not evidence."""
        result = _verify(
            "Dallas Roofing Co",
            description="roofing materials manufacturer in Dallas Texas",
        )
        assert result.business_type == "manufacturer"
        assert result.industry_match is False

    def test_query_style_words_do_not_create_contractor(self):
        """A trade word + location with no service evidence is still unknown."""
        result = _verify(
            "Dallas Roofing Co", description="roofing products for Dallas Texas"
        )
        assert result.business_type == "unknown"
        assert result.industry_match is False


class TestEvidenceProvenance:
    def test_decision_carries_field_evidence(self):
        result = _verify(
            "Acme Roofing",
            description="roofing contractor",
            source="directory_crawl",
            source_url="https://acmeroofing.com/about",
        )
        assert len(result.evidence) == 1
        ev = result.evidence[0]
        assert ev.field == "business_type"
        assert ev.value == "contractor"
        assert ev.source == "directory_crawl"
        assert ev.source_url == "https://acmeroofing.com/about"
        assert ev.is_reliable is True

    def test_manufacturer_decision_carries_negative_evidence(self):
        result = _verify(
            "Some Co", description="building materials manufacturer", source="search"
        )
        assert len(result.evidence) == 1
        assert result.evidence[0].value == "manufacturer"
        assert result.evidence[0].is_reliable is True

    def test_unknown_creates_no_field_evidence(self):
        """No evidence supplied -> no invented FieldEvidence."""
        result = _verify("Acme Roofing")
        assert result.evidence == []

    def test_output_shape(self):
        """The result exposes all required fields with correct types."""
        result = _verify("Acme Roofing", description="roofing contractor")
        assert result.business_type in BUSINESS_TYPES
        assert isinstance(result.industry_match, bool)
        assert result.verification_status in VerificationStatus
        assert isinstance(result.confidence, float)
        assert 0.0 <= result.confidence <= 1.0
        assert isinstance(result.reasons, list) and result.reasons
        assert isinstance(result.evidence, list)

        data = result.to_dict()
        for key in (
            "business_type",
            "industry_match",
            "verification_status",
            "confidence",
            "reasons",
            "evidence",
        ):
            assert key in data


# ---------------------------------------------------------------------------
# Membership context does not turn a contractor into an association
# ---------------------------------------------------------------------------


class TestMembershipGuard:
    def test_contractor_that_mentions_association_membership_accepted(self):
        """'member of the NRCA' is membership context, not the company's type."""
        result = _verify(
            "Acme Roofing LLC",
            description=(
                "licensed roofing contractor and member of the National "
                "Roofing Contractors Association"
            ),
        )
        assert result.business_type == "contractor"
        assert result.industry_match is True
        assert result.verification_status == VerificationStatus.verified

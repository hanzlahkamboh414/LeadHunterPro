"""Deterministic industry / business-type verification (Phase 2, Step 2).

Prevents manufacturers, suppliers, distributors, wholesalers, associations,
and material/product-only companies from being treated as construction
contractors.

The existing ``ContractorClassifier`` remains the source of trade-scoring and
standard rejection signals (CLAUDE.md §14 — no replacement classifier is
created). This verifier converts those signals plus the SUPPLIED evidence
into a deterministic result:

- ``business_type`` is exactly one of ``contractor`` / ``manufacturer`` /
  ``supplier`` / ``association`` / ``unknown``;
- negative evidence (manufacturer / supplier / distributor / wholesaler /
  association, or any non-construction entity) has priority over weak
  contractor keywords (accuracy Rule 6);
- material/product-only language (``roofing materials``, ``shingles``,
  ``roofing systems``, ...) NEVER qualifies as a contractor unless separate
  strong service evidence — installation / repair / replacement / a
  ``contractor`` / ``construction company`` — is supplied (accuracy Rule 5);
- a company is only ``contractor`` when a construction trade matches AND
  explicit service evidence is present;
- no query / location / industry hint is accepted — ``verify()`` has no such
  parameter, and the classifier is invoked with ``industry_hint=""``, so
  query context can never become evidence (accuracy Rule 9).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.engines.verification.models import FieldEvidence, VerificationStatus
from app.search_providers.contractor_classifier import ContractorClassifier

#: The only business_type values the verifier may emit.
BUSINESS_TYPES = frozenset(
    {"contractor", "manufacturer", "supplier", "association", "unknown"}
)

#: Mapping of ContractorClassifier reject_reason strings to business_type.
#: Distributors, wholesalers and material companies all fall into the
#: "supplier" bucket — the spec names only five types.
_REJECT_REASON_TO_TYPE = {
    "manufacturer": "manufacturer",
    "supplier": "supplier",
    "distributor": "supplier",
    "wholesaler": "supplier",
    "building materials company": "supplier",
    "material company": "supplier",
}

#: Supplier-family phrasings the classifier's bare ``\bsupplier\b`` pattern
#: misses ("roofing supply company", "roofing supplies", "wholesale roofing
#: products", "building supplies"). Adjacency-aware so "we supply AND install"
#: (a contractor) is not misread as a supplier.
_SUPPLIER_EXTRA_PATTERN = re.compile(
    r"\b(wholesale\b|wholesaler\b|roofing\s+suppl(?:y|ies)\b|"
    r"roofing\s+supply\s+company\b|supply\s+company\b|"
    r"materials?\s+supplier\b|building\s+suppl(?:y|ies)\b)\b",
    re.IGNORECASE,
)

#: Strong service evidence — the only thing that turns a trade match into a
#: confirmed contractor. Deliberately excludes bare "construction", "build",
#: "services", "shingles", "materials" (product-only language, rule 5).
_SERVICE_PATTERN = re.compile(
    r"\b(contractor|contractors|contracting|installation|install|installed|"
    r"repair|repairing|replacement|replace|replacing|replaced|"
    r"reroof|re-roof|remodel|remodeling|renovate|renovation|"
    r"erection|construction\s+company)\b",
    re.IGNORECASE,
)

#: Material / product-only language that must never qualify as a contractor
#: on its own (rule 5). Used for reasons + confidence, not the verdict.
_PRODUCT_ONLY_PATTERN = re.compile(
    r"\b(roofing\s+materials?|roofing\s+products?|building\s+materials?|"
    r"building\s+products?|construction\s+materials?|construction\s+products?|"
    r"roofing\s+systems?|roofing\s+supplies?|roofing\s+accessories?|"
    r"shingles?|insulation\b|membrane\b|tpo\b|epdm\b|underlayment\b)\b",
    re.IGNORECASE,
)

#: Morphological trade-family fallback. Used ONLY when the classifier finds
#: no trade match AND no reject signal, so negative evidence is already
#: excluded. The classifier's exact-word patterns miss common forms —
#: e.g. its plumbing pattern is ``\b(plumb|...)\b``, which does NOT match
#: "plumbing" or "plumber". This rescues strong contractor evidence the
#: classifier cannot express, still gated on service evidence.
_TRADE_FAMILY_FALLBACK: dict[str, re.Pattern] = {
    "general_contractor": re.compile(
        r"\b(general\s+contractor|contracting|construction\s+company|"
        r"remodeler|renovator)\b",
        re.IGNORECASE,
    ),
    "roofing": re.compile(
        r"\b(roof(?:ing|er)?|shingle|tpo|epdm|metal\s+roof|gutter)\b",
        re.IGNORECASE,
    ),
    "plumbing": re.compile(
        r"\b(plumb(?:ing|er)?|pipe|drain|sewer|water\s+heater|mechanical)\b",
        re.IGNORECASE,
    ),
    "electrical": re.compile(
        r"\b(electric|electrical|wiring|breaker|panel|generator|"
        r"lighting|alarm)\b",
        re.IGNORECASE,
    ),
    "hvac": re.compile(
        r"\b(hvac|heating|cooling|air\s+condition|furnace|boiler|"
        r"ventilation)\b",
        re.IGNORECASE,
    ),
    "concrete": re.compile(
        r"\b(concrete|cement|foundation|slab|paving|driveway|flatwork)\b",
        re.IGNORECASE,
    ),
    "painting": re.compile(
        r"\b(paint|painting|coating|staining|drywall)\b",
        re.IGNORECASE,
    ),
    "flooring": re.compile(
        r"\b(floor|flooring|carpet|tile|hardwood|laminate|vinyl)\b",
        re.IGNORECASE,
    ),
    "steel": re.compile(
        r"\b(steel|structural|metal\s+fabrication|welding|iron\s+work|"
        r"erection)\b",
        re.IGNORECASE,
    ),
    "landscaping": re.compile(
        r"\b(landscape|landscaping|lawn|gardening|irrigation|sprinkler|"
        r"hardscape)\b",
        re.IGNORECASE,
    ),
}


def _fallback_trade(text: str) -> str | None:
    """First construction-trade family present in *text*, or None."""
    for trade, pattern in _TRADE_FAMILY_FALLBACK.items():
        if pattern.search(text):
            return trade
    return None


#: Association detection. An association named IN the company name is
#: decisive; in the description it must not be mere membership context
#: (e.g. "licensed contractor and member of the NRCA").
_MEMBERSHIP_GUARD = re.compile(
    r"\b(?:member of|members of|membership|members)\b", re.IGNORECASE
)
_NAME_ASSOCIATION = re.compile(r"\bassociation\b", re.IGNORECASE)
_DESC_ASSOCIATION = re.compile(
    r"\b(?:trade|builders?|contractors?|industry|roofing|home|state|national|"
    r"local|regional|manufacturers?)\s+association\b|association\b",
    re.IGNORECASE,
)


def _is_association_type(company_name: str, text: str) -> bool:
    """True when the supplied evidence marks the entity as an association."""
    if _NAME_ASSOCIATION.search(company_name):
        return True
    if _DESC_ASSOCIATION.search(text) and not _MEMBERSHIP_GUARD.search(text):
        return True
    return False


@dataclass
class IndustryVerificationResult:
    """Deterministic result of verifying a company's industry / business type."""

    business_type: str
    industry_match: bool
    verification_status: VerificationStatus
    trade_category: str
    confidence: float
    reasons: list[str]
    evidence: list[FieldEvidence]

    def __post_init__(self) -> None:
        if self.business_type not in BUSINESS_TYPES:
            raise ValueError(
                f"business_type must be one of {sorted(BUSINESS_TYPES)}, "
                f"got {self.business_type!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialisable view for downstream gates / metadata."""
        return {
            "business_type": self.business_type,
            "industry_match": self.industry_match,
            "verification_status": self.verification_status.value,
            "trade_category": self.trade_category,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "evidence": [
                {
                    "field": e.field,
                    "value": e.value,
                    "source": e.source,
                    "source_url": e.source_url,
                    "confidence": e.confidence,
                    "fetched_at": e.fetched_at,
                    "is_reliable": e.is_reliable,
                }
                for e in self.evidence
            ],
        }


class IndustryVerifier:
    """Deterministic business-type verification built on ContractorClassifier.

    Reuses the existing classifier for reject-signal and trade-scoring; this
    class converts those signals + the supplied evidence into the five-value
    business_type verdict. No network, no AI, no query context.
    """

    def __init__(self, *, classifier: ContractorClassifier | None = None) -> None:
        self._classifier = classifier or ContractorClassifier()

    def verify(
        self,
        *,
        company_name: str = "",
        title: str = "",
        description: str = "",
        website: str = "",
        source: str = "",
        source_url: str = "",
    ) -> IndustryVerificationResult:
        """Classify business type from SUPPLIED evidence only.

        Parameters:
            company_name: the company's own name.
            title: page title, if it is genuine page evidence.
            description: page / source blurb about the company.
            website: the company website (for blocked-domain signals).
            source / source_url: provenance of the record.

        Returns:
            An ``IndustryVerificationResult``; never raises.
        """
        name = (company_name or "").strip()
        ttl = (title or "").strip()
        desc = (description or "").strip()
        text = f"{name} {ttl} {desc}".lower()

        # --- Association: strong institutional negative evidence ---
        if _is_association_type(name, text):
            return self._build(
                business_type="association",
                industry_match=False,
                status=VerificationStatus.rejected,
                trade_category="",
                confidence=0.9,
                reasons=["association signal in supplied evidence"],
                evidence_value="association",
                source=source,
                source_url=source_url,
            )

        # --- Supplier phrasings the classifier's pattern misses ---
        supply_hit = _SUPPLIER_EXTRA_PATTERN.search(text)
        if supply_hit:
            return self._build(
                business_type="supplier",
                industry_match=False,
                status=VerificationStatus.rejected,
                trade_category="",
                confidence=0.85,
                reasons=[
                    f"matched supplier signal '{supply_hit.group(0)}' "
                    f"in supplied evidence"
                ],
                evidence_value="supplier",
                source=source,
                source_url=source_url,
            )

        # --- Reuse the classifier (no query: industry_hint is empty) ---
        clf = self._classifier.classify(
            name=name,
            title=ttl,
            description=desc,
            url=website,
            industry_hint="",
        )
        trade = clf.get("trade_category", "")

        if not clf.get("accepted"):
            reject_reason = clf.get("reject_reason")
            if reject_reason and reject_reason != "no construction trade match":
                # Negative evidence has priority over weak contractor words.
                named = _REJECT_REASON_TO_TYPE.get(reject_reason)
                if named:
                    business_type = named
                    confidence = (
                        0.9 if business_type == "manufacturer" else 0.85
                    )
                    evidence_value = business_type
                    reason = f"matched '{reject_reason}' in supplied evidence"
                else:
                    business_type = "unknown"
                    confidence = 0.8
                    evidence_value = "not_contractor"
                    reason = (
                        f"non-construction entity signal '{reject_reason}' "
                        f"in supplied evidence"
                    )
                return self._build(
                    business_type=business_type,
                    industry_match=False,
                    status=VerificationStatus.rejected,
                    trade_category="",
                    confidence=confidence,
                    reasons=[reason],
                    evidence_value=evidence_value,
                    source=source,
                    source_url=source_url,
                )
            # No reject signal and no trade match. Rescue strong service
            # evidence via the morphological trade-family fallback (e.g. the
            # classifier's \bplumb\b pattern misses "plumbing contractor").
            # Negative evidence has already been excluded, so this cannot
            # weaken a rejection; service evidence is still required.
            family = _fallback_trade(text)
            if family:
                service_hit = _SERVICE_PATTERN.search(text)
                if service_hit:
                    return self._build(
                        business_type="contractor",
                        industry_match=True,
                        status=VerificationStatus.verified,
                        trade_category=family,
                        confidence=0.85,
                        reasons=[
                            f"matched '{family}' trade-family and service "
                            f"evidence '{service_hit.group(0)}' (classifier "
                            f"trade pattern missed this form)"
                        ],
                        evidence_value="contractor",
                        source=source,
                        source_url=source_url,
                    )

            # Insufficient evidence.
            return self._build(
                business_type="unknown",
                industry_match=False,
                status=VerificationStatus.unknown,
                trade_category="",
                confidence=0.0,
                reasons=[
                    "no construction trade or reject evidence in supplied text"
                ],
                evidence_value=None,
                source=source,
                source_url=source_url,
            )

        # A trade matched. Only a contractor when service evidence confirms
        # the company actually performs the work (rule 5).
        service_hit = _SERVICE_PATTERN.search(text)
        if service_hit:
            return self._build(
                business_type="contractor",
                industry_match=True,
                status=VerificationStatus.verified,
                trade_category=trade,
                confidence=0.85,
                reasons=[
                    f"matched '{trade}' trade and service evidence "
                    f"'{service_hit.group(0)}'"
                ],
                evidence_value="contractor",
                source=source,
                source_url=source_url,
            )

        product_hit = _PRODUCT_ONLY_PATTERN.search(text)
        if product_hit:
            confidence = 0.2
            reason = (
                f"matched '{trade}' trade but only product/material language "
                f"('{product_hit.group(0)}') without installation/repair/"
                f"construction evidence"
            )
        else:
            confidence = 0.0
            reason = (
                f"matched '{trade}' trade but no service evidence confirming "
                f"contractor work"
            )
        return self._build(
            business_type="unknown",
            industry_match=False,
            status=VerificationStatus.unknown,
            trade_category=trade,
            confidence=confidence,
            reasons=[reason],
            evidence_value=None,
            source=source,
            source_url=source_url,
        )

    @staticmethod
    def _build(
        *,
        business_type: str,
        industry_match: bool,
        status: VerificationStatus,
        trade_category: str,
        confidence: float,
        reasons: list[str],
        evidence_value: str | None,
        source: str,
        source_url: str,
    ) -> IndustryVerificationResult:
        evidence: list[FieldEvidence] = []
        if evidence_value is not None:
            evidence.append(
                FieldEvidence(
                    field="business_type",
                    value=evidence_value,
                    source=source or "industry_verifier",
                    source_url=source_url,
                    confidence=confidence,
                    is_reliable=True,
                )
            )
        return IndustryVerificationResult(
            business_type=business_type,
            industry_match=industry_match,
            verification_status=status,
            trade_category=trade_category,
            confidence=confidence,
            reasons=reasons,
            evidence=evidence,
        )

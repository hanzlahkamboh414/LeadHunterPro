"""Phase 2 Step 4 — deterministic acceptance gate + evidence merge.

Combines the three Phase 2 verifiers (identity, industry, location) with
``SourceTier`` and discovery provenance into ONE deterministic acceptance
decision. No AI is involved — AI may score after the gate, but it may never
override a rejection (CLAUDE.md §3, §10; accuracy-first Rule 6).

Acceptance rules enforced here (accuracy-first):

- HARD rejections, in order: ``business_type`` is manufacturer / supplier /
  association; the supplied website is invalid, placeholder/reserved, or an
  aggregator/non-commercial host (a directory *member page* is a SOFT
  signal — it is not the official site, but it is not proof the company is
  fake, so it is treated as an evidence gap, not a rejection).
- Never rejected for missing email / phone / LinkedIn / decision maker /
  project signal / city / reachable-website — those are evidence gaps.
- ``verified`` requires ALL three dimensions confirmed from evidence
  (contractor industry + official site/identity + verified location);
  anything less is ``partially_verified`` or ``unknown`` — weak evidence is
  never upgraded to verified.
- Tier 4 (fixture/demo/test) can NEVER be a live verified lead
  (``source_tiers.is_live_tier``); the status is capped to ``unknown``.
- Location comes ONLY from ``LocationVerifier`` output — never from the
  query, a default TX, a DFW inference, or a company-name city token.

``merge_company_records`` is the evidence-merge half (Phase 2 Step 4, F):
the dedup flow keeps its ``(domain, name)`` key but merges duplicate
records instead of silently discarding the loser — evidence and provenance
are preserved, conflicting location is recorded explicitly (never resolved
to the first source, never promoted to verified).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.engines.verification.identity_verifier import IdentityVerificationResult
from app.engines.verification.industry_verifier import IndustryVerificationResult
from app.engines.verification.location_verifier import LocationVerificationResult
from app.engines.verification.models import FieldEvidence, VerificationStatus
from app.engines.verification.source_tiers import (
    SourceTier,
    is_live_tier,
    tier_for_source,
)

#: Business types that can never be a construction-contractor lead.
_HARD_REJECT_BUSINESS_TYPES = frozenset(
    {"manufacturer", "supplier", "association"}
)

#: A directory member page is a soft website signal: it is not the official
#: company site, but it is also not proof the company is fake. The identity
#: verifier emits exactly this rejection reason string.
_SOFT_WEBSITE_REASON_PREFIX = "directory member page"

#: Keys on a record dict that may carry evidence lists to merge.
_EVIDENCE_LIST_KEYS = (
    "identity_evidence",
    "industry_evidence",
    "location_evidence",
    "field_evidence",
    "evidence",
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class AcceptanceResult:
    """Deterministic acceptance decision for one discovered company."""

    accepted: bool
    verification_status: VerificationStatus
    verification_confidence: float
    reasons: list[str]
    source_tier: int
    field_evidence: list[FieldEvidence]
    identity: IdentityVerificationResult
    industry: IndustryVerificationResult
    location: LocationVerificationResult
    business_type: str
    #: Verified location — ONLY from LocationVerifier evidence (never query).
    city: str | None
    state: str | None
    location_match: bool

    @property
    def hard_rejected(self) -> bool:
        return self.verification_status == VerificationStatus.rejected

    def to_dict(self) -> dict[str, Any]:
        """Serialisable view for downstream gates / metadata."""
        return {
            "accepted": self.accepted,
            "verification_status": self.verification_status.value,
            "verification_confidence": self.verification_confidence,
            "reasons": list(self.reasons),
            "source_tier": self.source_tier,
            "business_type": self.business_type,
            "city": self.city or "",
            "state": self.state or "",
            "location_match": self.location_match,
            "field_evidence": [
                {
                    "field": e.field,
                    "value": e.value,
                    "source": e.source,
                    "source_url": e.source_url,
                    "confidence": e.confidence,
                    "fetched_at": e.fetched_at,
                    "is_reliable": e.is_reliable,
                }
                for e in self.field_evidence
            ],
            "identity": self.identity.to_dict(),
            "industry": self.industry.to_dict(),
            "location": self.location.to_dict(),
        }


# ---------------------------------------------------------------------------
# Gate internals (deterministic, no I/O)
# ---------------------------------------------------------------------------


def _is_hard_website_rejection(identity: IdentityVerificationResult) -> bool:
    """True when the website claim is provably fake/invalid.

    A directory *member page* is not a hard rejection — the URL is real but
    is a listing, not the official site; the company itself is not fake.
    """
    if identity.website_status != VerificationStatus.rejected:
        return False
    return not any(
        reason.startswith(_SOFT_WEBSITE_REASON_PREFIX)
        for reason in identity.reasons
    )


def _dedupe_evidence(entries: list[FieldEvidence]) -> list[FieldEvidence]:
    """Union evidence entries, dropping exact (field, value, source) dupes."""
    merged: list[FieldEvidence] = []
    seen: set[tuple[str, str, str]] = set()
    for ev in entries:
        key = (ev.field, str(ev.value), ev.source)
        if key not in seen:
            seen.add(key)
            merged.append(ev)
    return merged


def _resolve_tier(record: dict[str, Any], source_tier: int | None) -> int:
    """Resolve the record's source tier: explicit arg > record tag > mapping.

    Uses the existing ``source_tiers.tier_for_source`` (Phase 1 semantics) —
    no new tier system is introduced.
    """
    if source_tier is not None:
        return int(source_tier)
    explicit = record.get("source_tier")
    if explicit:
        return int(explicit)
    return int(
        tier_for_source(
            record.get("_discovery_source") or record.get("source"),
            record.get("source_type"),
        )
    )


def _is_verified_location(location: LocationVerificationResult) -> bool:
    if location.location_status != VerificationStatus.verified:
        return False
    return bool(location.city or location.state)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class AcceptanceGate:
    """Deterministic acceptance decision from the three verification results.

    The gate consumes verification RESULTS — it never runs the verifiers
    itself (identity verification may need a network fetch; that is the
    caller's concern). This keeps the gate pure, offline and total.
    """

    def evaluate(
        self,
        *,
        record: dict[str, Any] | None = None,
        identity: IdentityVerificationResult,
        industry: IndustryVerificationResult,
        location: LocationVerificationResult,
        source_tier: int | None = None,
    ) -> AcceptanceResult:
        """Evaluate one company against the acceptance rules.

        Parameters:
            record: the raw company dict (used for tier resolution only —
                never for location or other evidence).
            identity / industry / location: the three Phase 2 results.
            source_tier: optional explicit tier; defaults to
                ``tier_for_source`` on the record's source.

        Returns:
            An ``AcceptanceResult``; this method never raises.
        """
        record = record or {}
        tier = _resolve_tier(record, source_tier)
        business_type = (industry.business_type or "unknown") if industry else "unknown"

        field_evidence = _dedupe_evidence(
            list(
                (identity.evidence if identity else [])
                + (industry.evidence if industry else [])
                + (location.evidence if location else [])
            )
        )
        verified_city = location.city if location else None
        verified_state = location.state if location else None
        location_match = bool(location and location.location_match)

        # --- Hard rejections (deterministic; AI may never override) ---
        hard_reasons: list[str] = []
        if business_type in _HARD_REJECT_BUSINESS_TYPES:
            hard_reasons.append(
                f"business_type '{business_type}' is not a construction contractor"
            )
        if identity and _is_hard_website_rejection(identity):
            hard_reasons.append(
                "supplied website is invalid, placeholder, or an aggregator "
                "host — not an official company site"
            )

        if hard_reasons:
            return AcceptanceResult(
                accepted=False,
                verification_status=VerificationStatus.rejected,
                verification_confidence=0.0,
                reasons=hard_reasons,
                source_tier=tier,
                field_evidence=field_evidence,
                identity=identity,
                industry=industry,
                location=location,
                business_type=business_type,
                city=verified_city,
                state=verified_state,
                location_match=location_match,
            )

        # --- Evidence grading (never upgrade weak evidence to verified) ---
        industry_confirmed = bool(
            industry
            and industry.business_type == "contractor"
            and industry.verification_status == VerificationStatus.verified
        )
        identity_confirmed = bool(
            identity
            and (
                identity.official_site_confirmed
                or identity.website_status == VerificationStatus.verified
            )
        )
        location_confirmed = bool(location and _is_verified_location(location))
        confirmed = sum(
            [industry_confirmed, identity_confirmed, location_confirmed]
        )

        # --- Tier 4 cap: fixtures are never live-verified leads ---
        if not is_live_tier(tier):
            return AcceptanceResult(
                accepted=False,
                verification_status=VerificationStatus.unknown,
                verification_confidence=0.0,
                reasons=[
                    f"source_tier={tier} is not a live tier; the record "
                    "cannot be a live verified lead (live acceptance "
                    "requires live evidence)"
                ],
                source_tier=tier,
                field_evidence=field_evidence,
                identity=identity,
                industry=industry,
                location=location,
                business_type=business_type,
                city=verified_city,
                state=verified_state,
                location_match=location_match,
            )

        if confirmed == 3:
            status = VerificationStatus.verified
            accepted = True
            reasons = [
                "identity, industry, and location all verified from evidence"
            ]
        elif confirmed >= 1:
            status = VerificationStatus.partially_verified
            accepted = True
            reasons = [
                f"{confirmed} of 3 verification dimensions confirmed; "
                "remaining dimensions are evidence gaps, not rejections"
            ]
            if not industry_confirmed:
                reasons.append("industry/business type not confirmed as contractor")
            if not identity_confirmed:
                reasons.append("official website not confirmed from evidence")
            if not location_confirmed:
                reasons.append("location not verified from evidence")
        else:
            status = VerificationStatus.unknown
            accepted = False
            reasons = [
                "no verified evidence (identity, industry, location all "
                "unknown); company is unverified, not rejected"
            ]

        confidence = self._confidence(
            identity=identity,
            industry=industry,
            location=location,
            industry_confirmed=industry_confirmed,
            identity_confirmed=identity_confirmed,
            location_confirmed=location_confirmed,
            status=status,
        )

        return AcceptanceResult(
            accepted=accepted,
            verification_status=status,
            verification_confidence=confidence,
            reasons=reasons,
            source_tier=tier,
            field_evidence=field_evidence,
            identity=identity,
            industry=industry,
            location=location,
            business_type=business_type,
            city=verified_city,
            state=verified_state,
            location_match=location_match,
        )

    @staticmethod
    def _confidence(
        *,
        identity: IdentityVerificationResult,
        industry: IndustryVerificationResult,
        location: LocationVerificationResult,
        industry_confirmed: bool,
        identity_confirmed: bool,
        location_confirmed: bool,
        status: VerificationStatus,
    ) -> float:
        """Evidence-weighted confidence in [0, 1]; 0 for unknown/rejected."""
        if status not in (
            VerificationStatus.verified,
            VerificationStatus.partially_verified,
        ):
            return 0.0
        conf = 0.0
        if industry_confirmed and industry:
            conf += 0.4 * float(industry.confidence)
        if identity_confirmed and identity:
            conf += 0.4 * float(identity.confidence)
        if location_confirmed and location:
            conf += 0.2 * float(location.confidence)
        return round(min(conf, 0.95), 2)


# ---------------------------------------------------------------------------
# Evidence merge (Phase 2 Step 4, F) — used by the orchestrator dedup flow
# ---------------------------------------------------------------------------


def _str_field(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    return value.strip() if isinstance(value, str) else ""


def _source_of(record: dict[str, Any]) -> str:
    return record.get("source") or record.get("_discovery_source") or ""


def _source_url_of(record: dict[str, Any]) -> str:
    return record.get("source_url") or record.get("website") or ""


def merge_company_records(
    primary: dict[str, Any], duplicate: dict[str, Any]
) -> dict[str, Any]:
    """Merge *duplicate* into *primary* (same dedup key) without losing evidence.

    The ``(domain, name)`` dedup key is the caller's concern — this function
    merges everything else on the record:

    - evidence lists (``identity_evidence`` / ``industry_evidence`` /
      ``location_evidence`` / ``field_evidence`` / ``evidence``) are unioned
      so no source's evidence is silently discarded;
    - ``source_url`` / ``data_provenance`` provenance is filled in where the
      primary lacks it;
    - when the two records disagree on ``city`` / ``state``, BOTH conflicting
      values are preserved as evidence, the ambiguous ``city``/``state`` are
      cleared, and any prior ``verification_status`` is downgraded to
      ``unknown`` — a conflict is never resolved to the first source and
      never promoted to verified;
    - when the locations agree (or one is empty), the known value survives;
    - Phase 3 Step 3: ``ai`` / ``qualification`` intelligence metadata is
      preserved ADDITIVELY across the merge — keys present only on the
      duplicate survive, primary keys stay authoritative, and neither
      namespace can overwrite the deterministic ``verification_*`` keys,
      ``city`` / ``state``, or any evidence field.

    Returns a new dict; neither input is mutated.
    """
    merged: dict[str, Any] = dict(primary)

    # 1. Evidence union (strongest valid evidence preserved + conflicts kept).
    for key in _EVIDENCE_LIST_KEYS:
        left = primary.get(key) or []
        right = duplicate.get(key) or []
        if left or right:
            merged[key] = list(left) + list(right)

    # 2. Provenance: fill in whatever the primary lacks.
    for prov in ("source_url", "data_provenance", "_discovery_source", "source"):
        if not merged.get(prov):
            merged[prov] = duplicate.get(prov, "")

    # 3. Location conflict handling.
    city_a, city_b = _str_field(primary, "city"), _str_field(duplicate, "city")
    state_a, state_b = (
        _str_field(primary, "state").upper(),
        _str_field(duplicate, "state").upper(),
    )

    conflicts: list[str] = []
    if city_a and city_b and city_a.lower() != city_b.lower():
        conflicts.append(f"city conflict: {city_a!r} vs {city_b!r}")
    if state_a and state_b and state_a != state_b:
        conflicts.append(f"state conflict: {state_a!r} vs {state_b!r}")

    if conflicts:
        merged["location_conflict"] = list(conflicts)
        # The ambiguous fields are cleared — neither side wins silently.
        merged["city"] = ""
        merged["state"] = ""
        # Record BOTH values explicitly as unreliable evidence so the
        # conflict is preserved, not erased.
        conflict_evidence: list[dict[str, Any]] = []
        if city_a:
            conflict_evidence.append(
                {
                    "field": "city",
                    "value": city_a,
                    "source": _source_of(primary),
                    "source_url": _source_url_of(primary),
                    "confidence": 0.0,
                    "fetched_at": "",
                    "is_reliable": False,
                }
            )
        if city_b:
            conflict_evidence.append(
                {
                    "field": "city",
                    "value": city_b,
                    "source": _source_of(duplicate),
                    "source_url": _source_url_of(duplicate),
                    "confidence": 0.0,
                    "fetched_at": "",
                    "is_reliable": False,
                }
            )
        if state_a:
            conflict_evidence.append(
                {
                    "field": "state",
                    "value": state_a,
                    "source": _source_of(primary),
                    "source_url": _source_url_of(primary),
                    "confidence": 0.0,
                    "fetched_at": "",
                    "is_reliable": False,
                }
            )
        if state_b:
            conflict_evidence.append(
                {
                    "field": "state",
                    "value": state_b,
                    "source": _source_of(duplicate),
                    "source_url": _source_url_of(duplicate),
                    "confidence": 0.0,
                    "fetched_at": "",
                    "is_reliable": False,
                }
            )
        merged.setdefault("field_evidence", []).extend(conflict_evidence)

        # Never convert a conflict into verified status.
        merged["verification_status"] = "unknown"
        merged["verification_confidence"] = 0.0
        reasons = list(merged.get("verification_reasons") or [])
        reasons.extend(conflicts)
        merged["verification_reasons"] = reasons
    else:
        # Locations agree (or one is empty): keep the known value.
        if not merged.get("city"):
            merged["city"] = city_b
        if not merged.get("state"):
            merged["state"] = state_b

    # 4. AI intelligence namespaces survive additively. `ai` and `qualification`
    #    are separate dicts; both sides' entries are kept (duplicate-only keys
    #    preserved, primary wins conflicts). The merge never writes into the
    #    verification namespace, so no AI payload can overwrite deterministic
    #    verification, location, or evidence — AI stays downstream of the gate.
    for ns in ("ai", "qualification"):
        left = primary.get(ns)
        right = duplicate.get(ns)
        if isinstance(left, dict) or isinstance(right, dict):
            merged[ns] = {**(right or {}), **(left or {})}

    return merged

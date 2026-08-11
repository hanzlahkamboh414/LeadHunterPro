"""Deterministic city / state verification (Phase 2, Step 3).

Verifies a company's LOCATION from SUPPLIED evidence only — never from the
search query. The search query is search intent (a TARGET), not location
proof. This closes the accuracy hole where query text was silently written
into ``city``/``state`` (previously in
``texas_procurement._normalize_company`` and ``search_provider_source``).

Rules enforced here (accuracy-first):

- ``city`` is verified ONLY from explicit evidence: an address, an official
  contact / "headquarters" / "located in" statement, or an authoritative
  licensing / directory listing that names a city. The following NEVER verify
  a city: the query, service-area language ("serving Dallas", "Dallas
  project", "DFW area", "Dallas-Fort Worth metroplex"), a company name, or a
  state-only statement.
- ``state`` is verified ONLY from explicit state evidence (a state name or a
  ``city, TX`` address form). There is NO default to TX / Texas / USA.
- Conflicting city evidence (e.g. Dallas vs Houston) is recorded, never
  silently resolved to one of them.
- Every verified claim carries ``FieldEvidence`` (provenance); query text is
  never stored as evidence; when there is no actual evidence no FieldEvidence
  is manufactured.

Deterministic only — no AI, no network. ``_parse_location`` in
``app.connectors.texas_procurement`` is intentionally NOT reused here: it is a
Texas-connector QUERY parser that hard-defaults ``state`` to ``"TX"`` for any
location string (e.g. a bare city), which would violate "state requires
explicit evidence". This module keeps its own neutral evidence extractor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from app.engines.verification.models import FieldEvidence, VerificationStatus

# ---------------------------------------------------------------------------
# US state reference (code -> full name). Local copy: importing the Texas
# connector's ``_STATE_MAP`` would drag in that module (and its TX-default
# query parser) just for a lookup table (see module docstring).
# ---------------------------------------------------------------------------
_US_STATES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

_STATE_NAME_TO_CODE = {name.lower(): code for code, name in _US_STATES.items()}

#: Alternations — state NAMES longest-first (so "North Carolina" wins over
#: "Carolina") and state CODES (uppercase only, matched case-sensitively).
_STATE_NAMES_ALT = "|".join(sorted(_STATE_NAME_TO_CODE, key=len, reverse=True))
_STATE_CODES_ALT = "|".join(sorted(_US_STATES))


#: Service-area / non-location words. A location mention surrounded by these
#: is "the area we serve / cover", not where the company IS.
_SERVICE_AREA_WORDS = frozenset(
    {
        "serve", "serves", "serving", "service", "services",
        "project", "projects",
        "metroplex", "metro", "area", "region", "regions", "regional",
        "territory", "territories", "market", "markets",
        "covering", "covers", "cover",
        "throughout", "across", "surrounding",
        "community", "communities", "citywide", "countywide", "statewide",
        "wide",
    }
)
_SERVICE_AREA_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_SERVICE_AREA_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

#: Explicit ``CITY, State`` forms — the only place a city can be verified.
#: ``pre`` is the (city + immediate context) directly before the comma that
#: precedes the state; commas are excluded from ``pre`` so it never spans a
#: street boundary ("123 Main St, Dallas, TX" -> pre == "Dallas").
_CITY_STATE_NAME_RE = re.compile(
    r"(?P<pre>[A-Za-z0-9.:' -]{0,60}?),\s*"
    r"(?P<state_name>" + _STATE_NAMES_ALT + r")\b",
    re.IGNORECASE,
)
_CITY_STATE_CODE_RE = re.compile(
    r"(?P<pre>[A-Za-z0-9.:' -]{0,60}?),\s*"
    r"(?P<state_code>" + _STATE_CODES_ALT + r")\b"
)

#: Standalone state NAME mention (e.g. "Texas roofing contractor").
_STANDALONE_STATE_RE = re.compile(
    r"\b(" + _STATE_NAMES_ALT + r")\b", re.IGNORECASE
)

#: A capitalized (possibly hyphenated or two-word) city candidate.
_CITY_GROUP_RE = re.compile(r"[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*")


# ---------------------------------------------------------------------------
# Pure helpers (deterministic, no I/O)
# ---------------------------------------------------------------------------


def _after_token(text: str, end: int) -> str:
    """First alphabetic token after position *end*, lowercased (or "")."""
    tail = text[end:]
    m = re.match(r"[^A-Za-z]*([A-Za-z][A-Za-z-]*)", tail)
    return m.group(1).lower() if m else ""


def _extract_city(pre: str) -> str | None:
    """Last capitalized token-group in *pre*, or None.

    ``"Located in Dallas"`` -> ``"Dallas"``, ``"Fort Worth"`` -> ``"Fort
    Worth"``. A 1-2 character token (e.g. ``"St"`` in a city-less address) is
    not a usable city.
    """
    groups = _CITY_GROUP_RE.findall(pre)
    if not groups:
        return None
    city = groups[-1].strip()
    return city if len(city) >= 3 else None


def _extract_mentions(text: str) -> tuple[list[tuple[str | None, str | None]], bool]:
    """Explicit (city, state) mentions in *text* plus whether any candidate
    was rejected as service-area / non-location language."""
    mentions: list[tuple[str | None, str | None]] = []
    rejected = False

    def _service_area(match: re.Match, before: str) -> bool:
        window = f"{before} {_after_token(text, match.end())}"
        return bool(_SERVICE_AREA_RE.search(window))

    for m in _CITY_STATE_NAME_RE.finditer(text):
        if _service_area(m, m.group("pre")):
            rejected = True
            continue
        state = _STATE_NAME_TO_CODE[m.group("state_name").lower()]
        mentions.append((_extract_city(m.group("pre")), state))

    for m in _CITY_STATE_CODE_RE.finditer(text):
        if _service_area(m, m.group("pre")):
            rejected = True
            continue
        mentions.append((_extract_city(m.group("pre")), m.group("state_code")))

    for m in _STANDALONE_STATE_RE.finditer(text):
        if _service_area(m, text[max(0, m.start() - 30) : m.start()]):
            rejected = True
            continue
        state = _STATE_NAME_TO_CODE[m.group(1).lower()]
        mentions.append((None, state))

    return mentions, rejected


def _parse_target(query: str) -> tuple[str | None, str | None]:
    """Parse the QUERY into a (city, state) SEARCH TARGET.

    Used ONLY for ``location_match`` comparison — never as evidence. Neutral:
    unlike the Texas connector's ``_parse_location``, a bare city does NOT
    default to ``"TX"``.
    """
    if not query or not query.strip():
        return None, None
    q = query.strip()
    state: str | None = None
    state_start: int | None = None

    m = _STANDALONE_STATE_RE.search(q)
    if m:
        state = _STATE_NAME_TO_CODE[m.group(1).lower()]
        state_start = m.start()
    else:
        # Codes only when uppercase so "roofing OR siding" is not read as
        # the state Oregon.
        m2 = re.search(r"\b(" + _STATE_CODES_ALT + r")\b", q)
        if m2:
            state = m2.group(1)
            state_start = m2.start()

    city = _extract_city(q[:state_start]) if state_start is not None else None
    return city, state


def _compute_location_match(
    verified_city: str | None,
    verified_state: str | None,
    target_city: str | None,
    target_state: str | None,
) -> bool:
    """True when the verified location is consistent with the query TARGET.

    No verified location, or no target at all, is never a match. An unknown
    target half (e.g. a query with no state) is a non-constraint.
    """
    if verified_city is None and verified_state is None:
        return False
    if target_city is None and target_state is None:
        return False
    if target_city is not None and (
        verified_city is None or verified_city.lower() != target_city.lower()
    ):
        return False
    if target_state is not None and verified_state != target_state:
        return False
    return True


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class LocationVerificationResult:
    """Deterministic result of verifying a company's city / state."""

    city: str | None
    state: str | None
    location_status: VerificationStatus
    location_match: bool
    confidence: float
    reasons: list[str]
    evidence: list[FieldEvidence]

    def to_dict(self) -> dict[str, Any]:
        """Serialisable view for downstream gates / metadata."""
        return {
            "city": self.city,
            "state": self.state,
            "location_status": self.location_status.value,
            "location_match": self.location_match,
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


# ---------------------------------------------------------------------------
# The verifier
# ---------------------------------------------------------------------------


class LocationVerifier:
    """Deterministic city / state verification from supplied evidence only."""

    def verify(
        self,
        *,
        city: str = "",
        state: str = "",
        evidence_texts: Iterable[str] | None = None,
        query: str = "",
        source: str = "",
        source_url: str = "",
    ) -> LocationVerificationResult:
        """Verify city/state from SUPPLIED evidence only.

        Parameters:
            city: candidate city value (e.g. parsed from a raw record) — an
                UNVERIFIED claim, never evidence.
            state: candidate state value — an UNVERIFIED claim, never evidence.
            evidence_texts: explicit evidence strings (addresses, official
                headquarters / "located in" statements, licensing or directory
                listings) that can verify location.
            query: the search query — used ONLY as a search TARGET for
                ``location_match``, never as evidence.
            source / source_url: provenance of the record, carried into each
                FieldEvidence.

        Returns:
            A ``LocationVerificationResult``; this method never raises.
        """
        reasons: list[str] = []
        candidate_city = (city or "").strip() or None
        candidate_state = (state or "").strip().upper() or None
        texts = [
            t for t in (evidence_texts or []) if isinstance(t, str) and t.strip()
        ]
        ev_source = source or "location_verifier"

        mentions: list[tuple[str | None, str | None]] = []
        any_rejected = False
        for text in texts:
            ms, rejected = _extract_mentions(text)
            mentions.extend(ms)
            any_rejected = any_rejected or rejected

        cities = {c for c, _ in mentions if c}
        states = {s for _, s in mentions if s}

        verified_city: str | None = None
        verified_state: str | None = None
        status = VerificationStatus.unknown
        confidence = 0.0
        evidence: list[FieldEvidence] = []

        if not texts:
            reasons.append("no location evidence supplied")
        elif any_rejected and not mentions:
            reasons.append(
                "location evidence rejected as service-area / non-location language"
            )
        elif not mentions:
            reasons.append("no explicit city/state evidence in supplied text")

        if mentions:
            if len(cities) > 1:
                # Conflicting city evidence — record both, never pick one.
                status = VerificationStatus.unknown
                confidence = 0.2
                if len(states) == 1:
                    verified_state = next(iter(states))
                reasons.append(
                    "conflicting city evidence: " + ", ".join(sorted(cities))
                )
                for c in sorted(cities):
                    evidence.append(
                        FieldEvidence(
                            field="city",
                            value=c,
                            source=ev_source,
                            source_url=source_url,
                            confidence=0.0,
                            is_reliable=False,
                        )
                    )
                if verified_state:
                    evidence.append(
                        FieldEvidence(
                            field="state",
                            value=verified_state,
                            source=ev_source,
                            source_url=source_url,
                            confidence=0.7,
                            is_reliable=True,
                        )
                    )
            else:
                if cities:
                    verified_city = next(iter(cities))
                if len(states) == 1:
                    verified_state = next(iter(states))

                if verified_city:
                    status = VerificationStatus.verified
                    confidence = 0.9
                    suffix = f", {verified_state}" if verified_state else ""
                    reasons.append(
                        f"explicit city+state evidence: {verified_city}{suffix}"
                    )
                    evidence.append(
                        FieldEvidence(
                            field="city",
                            value=verified_city,
                            source=ev_source,
                            source_url=source_url,
                            confidence=0.9,
                            is_reliable=True,
                        )
                    )
                    if verified_state:
                        evidence.append(
                            FieldEvidence(
                                field="state",
                                value=verified_state,
                                source=ev_source,
                                source_url=source_url,
                                confidence=0.9,
                                is_reliable=True,
                            )
                        )
                elif verified_state:
                    status = VerificationStatus.partially_verified
                    confidence = 0.7
                    reasons.append(f"explicit state evidence only: {verified_state}")
                    evidence.append(
                        FieldEvidence(
                            field="state",
                            value=verified_state,
                            source=ev_source,
                            source_url=source_url,
                            confidence=0.7,
                            is_reliable=True,
                        )
                    )

        # Candidates are claims, not evidence — surface that they were ignored.
        if candidate_city and not verified_city:
            reasons.append(
                f"candidate city {candidate_city!r} is an unverified claim, "
                f"not evidence"
            )
        if candidate_state and not verified_state:
            reasons.append(
                f"candidate state {candidate_state!r} is an unverified claim, "
                f"not evidence"
            )

        target_city, target_state = _parse_target(query)
        location_match = _compute_location_match(
            verified_city, verified_state, target_city, target_state
        )

        return LocationVerificationResult(
            city=verified_city,
            state=verified_state,
            location_status=status,
            location_match=location_match,
            confidence=confidence,
            reasons=reasons,
            evidence=evidence,
        )

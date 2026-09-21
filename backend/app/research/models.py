"""Canonical models for the research evidence store (``check.txt`` §8).

WHY A CANONICAL MODEL EXISTS AT ALL (CLAUDE.md §14 — reuse, don't
duplicate): the repository already carried THREE overlapping evidence
records, each written for a different consumer and none of them storable:

    app/lead_research/models.py::AIEvidence        claim + source_url + confidence(str)
    app/discovery/website/evidence.py::FieldEvidence  field + page_url + snippet
    app/engines/lead/lead_models.py::IntentEvidence   type + source_url + date

Their docstrings already concede the split. That was survivable while each
one lived inside one dossier; it is not survivable once evidence is stored
per COMPANY and read back by an event engine, because no consumer can query
three shapes at once.

So this module defines ONE stored record, and
:mod:`app.research.adapters` projects the three existing classes onto it.
Nothing existing is rewritten in this phase — the adapters are additive, so
every current caller keeps working unchanged.

Three fields carry the root-cause fix (CLAUDE.md §7):

``published_at``
    When the event happened, as distinct from ``retrieved_at`` (when we read
    it). The old models had no published date at all, so a 2019 award and a
    last-week award were indistinguishable to the email writer.

``project_key``
    A normalized identity for the underlying project, so the SAME project
    found on a city site, a newspaper and the contractor's own page becomes
    ONE event with three supporting evidence rows (``check.txt`` §19)
    instead of three unrelated "projects".

``company_id``
    A stable internal company identity, so evidence accumulates per company
    rather than per email address, and survives the company changing domain.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from app.research.taxonomy import CompanyMatch, EventType, EvidenceType

#: Max characters kept in ``excerpt``. Evidence is a quotation, not a copy
#: of the page: enough to audit the claim, bounded so a 300-page PDF cannot
#: ride into the database through one row.
MAX_EXCERPT_CHARS = 1000


def project_key(
    name: str,
    *,
    owner: str = "",
    jurisdiction: str = "",
) -> str:
    """A normalized identity for one construction project (``check.txt`` §19).

    The same project surfaces on a city procurement page, a newspaper, the
    developer's site and the contractor's own site. Keying on a normalized
    (owner, name, jurisdiction) triple is what lets those four observations
    collapse into one project event with four evidence rows.

    Deliberately conservative: an empty ``name`` yields ``""`` — an honest
    "this evidence names no project" — rather than a key invented from the
    owner or jurisdiction alone, which would merge every unrelated project
    in a city into one bucket.

    Args:
        name: The project's name as the source states it.
        owner: Awarding body / developer, when known.
        jurisdiction: City/state, when known.

    Returns:
        A lowercase hyphen-joined key, or ``""`` when *name* is blank.
    """
    if not (name or "").strip():
        return ""
    parts = []
    for raw in (owner, name, jurisdiction):
        normalized = re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower())
        normalized = normalized.strip("-")
        if normalized:
            parts.append(normalized)
    return "-".join(parts)[:200]


def content_hash(source_url: str, excerpt: str) -> str:
    """Deterministic dedup key for one observation.

    Two runs that read the same sentence off the same page must produce the
    same hash, so re-research updates one row instead of accumulating
    duplicates. Keyed on the URL and the quoted text — NOT on title, which
    a publisher may rewrite between fetches without changing the fact.
    """
    payload = f"{(source_url or '').strip()}\n{(excerpt or '').strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class CompanyIdentity:
    """A company's stable identity (founder correction, 2026-09-18).

    The original design used the normalized domain as the primary identity.
    That orphans evidence the moment a company rebrands or runs two
    domains. So:

    ``company_id``
        The stable internal id. Never derived from the domain, so it is
        unaffected by a domain change.
    ``company_key``
        The normalized domain — a fast lookup handle, NOT the identity.
    ``domains``
        Every domain observed for this company, primary first. A company
        that changes domain keeps its ``company_id`` and gains an alias.
    """

    company_id: str
    company_key: str = ""
    name: str = ""
    domains: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize for API output and store round-trips."""
        return {
            "company_id": self.company_id,
            "company_key": self.company_key,
            "name": self.name,
            "domains": list(self.domains),
        }


@dataclass(frozen=True)
class CanonicalEvidence:
    """One stored observation, with the provenance to audit it.

    Immutable: evidence records what WAS observed. A correction is a new
    observation, not an edit of history.

    Attributes:
        evidence_id: Stable id for this observation.
        company_id: The company this is about (see :class:`CompanyIdentity`).
        source_url: Where it was read. Required — ``check.txt`` §7: a search
            snippet is not evidence, and neither is anything else that cannot
            name the page it came from.
        source_type: The pipeline that produced it (``usaspending``,
            ``google_news``, ``company_site``, ...). Provenance, not quality.
        publisher: Who published the page (``City of Austin``, ...), when
            the source states it. Empty when unknown — never guessed.
        title: The page/record title.
        excerpt: The exact supporting text, bounded to
            :data:`MAX_EXCERPT_CHARS`.
        published_at: When the fact happened (ISO date/datetime, or free
            text when the source is vague). May be empty.
        retrieved_at: When we read it. Always set.
        company_match: How firmly this is tied to the company. A
            :data:`~app.research.taxonomy.CompanyMatch.MISMATCH` is rejected
            at construction — evidence about a different company is never
            stored.
        evidence_type: What kind of record this is (see :class:`EvidenceType`).
        project_key: Normalized project identity, or ``""`` when the
            evidence names no project.
        event_candidate: The event this observation suggests, or ``None``.
            Populated by the event engine (Phase 2) — an observation that
            has not been read yet honestly has no candidate.
        confidence: Belief in this observation, 0.0–1.0.
        legacy_type: For rows migrated from the pre-split
            ``IntentEvidenceType``, the original value. Kept so the
            migration is auditable and reversible; ``""`` for native rows.
        verification: The producing model's OWN tier verbatim
            (``"verified"``/``"unverified"``), or ``""`` when it had none.
            Deliberately NOT derived from ``confidence`` and never converted
            into it: the existing models' tiers are labels, not
            probabilities, and inventing a number from a label would be
            fabricated precision. A source that states a real number puts it
            in ``confidence``; the two scales coexist and neither overrides
            the other.
    """

    evidence_id: str
    company_id: str
    source_url: str
    retrieved_at: str
    source_type: str = ""
    publisher: str = ""
    title: str = ""
    excerpt: str = ""
    published_at: str = ""
    company_match: CompanyMatch = CompanyMatch.UNKNOWN
    evidence_type: EvidenceType | str = EvidenceType.COMPANY_PAGE
    project_key: str = ""
    event_candidate: EventType | str | None = None
    confidence: float = 0.0
    legacy_type: str = ""
    verification: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate and canonicalize.

        Raises:
            TypeError: On a wrong typed field.
            ValueError: On a blank ``source_url``, an out-of-range
                ``confidence``, a stored ``MISMATCH``, or a promotion
                attempt smuggled in through ``event_candidate``.
        """
        if not isinstance(self.source_url, str) or not self.source_url.strip():
            raise ValueError(
                "CanonicalEvidence.source_url is required — a claim that "
                "cannot name its page is not evidence (check.txt §7)"
            )
        if not isinstance(self.confidence, (int, float)):
            raise TypeError(
                f"confidence must be a number, got {type(self.confidence).__name__}"
            )
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError(
                f"confidence must be within 0.0–1.0, got {self.confidence}"
            )

        match = (
            self.company_match
            if isinstance(self.company_match, CompanyMatch)
            else CompanyMatch(str(self.company_match).strip().lower())
        )
        if match is CompanyMatch.MISMATCH:
            raise ValueError(
                "evidence about a different company is never stored "
                "(company_match=MISMATCH)"
            )

        object.__setattr__(self, "company_match", match)
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "excerpt", (self.excerpt or "")[:MAX_EXCERPT_CHARS])
        object.__setattr__(self, "project_key", (self.project_key or "").strip())

        if self.event_candidate is not None:
            candidate = self.event_candidate
            if isinstance(candidate, str):
                object.__setattr__(
                    self, "event_candidate", candidate.strip().lower()
                )

    @property
    def dedup_key(self) -> str:
        """Deterministic identity for duplicate detection."""
        return content_hash(self.source_url, self.excerpt)

    def to_row(self) -> dict[str, Any]:
        """Serialize to a flat dict of SQLite-friendly scalars."""
        event = self.event_candidate
        if isinstance(event, EventType):
            event = event.value
        return {
            "evidence_id": self.evidence_id,
            "company_id": self.company_id,
            "source_url": self.source_url,
            "source_type": self.source_type,
            "publisher": self.publisher,
            "title": self.title,
            "excerpt": self.excerpt,
            "published_at": self.published_at,
            "retrieved_at": self.retrieved_at,
            "company_match": self.company_match.value,
            "evidence_type": str(self.evidence_type),
            "project_key": self.project_key,
            "event_candidate": event or "",
            "confidence": self.confidence,
            "legacy_type": self.legacy_type,
            "verification": self.verification,
            "content_hash": self.dedup_key,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> CanonicalEvidence:
        """Rebuild from a store row (``extra`` is not persisted)."""
        return cls(
            evidence_id=row["evidence_id"],
            company_id=row["company_id"],
            source_url=row["source_url"],
            source_type=row.get("source_type", ""),
            publisher=row.get("publisher", ""),
            title=row.get("title", ""),
            excerpt=row.get("excerpt", ""),
            published_at=row.get("published_at", ""),
            retrieved_at=row.get("retrieved_at", ""),
            company_match=row.get("company_match", CompanyMatch.UNKNOWN.value),
            evidence_type=row.get("evidence_type", ""),
            project_key=row.get("project_key", ""),
            event_candidate=row.get("event_candidate") or None,
            confidence=float(row.get("confidence", 0.0)),
            legacy_type=row.get("legacy_type", ""),
            verification=row.get("verification", ""),
        )


def new_evidence_id(seed: str) -> str:
    """A stable evidence id derived from the observation itself.

    Deterministic on purpose: re-running the same fetch produces the same
    id, so an upsert updates one row instead of creating a second one.
    """
    return "ev_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]

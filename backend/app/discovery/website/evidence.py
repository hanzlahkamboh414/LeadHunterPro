"""Field-level evidence model for Direct Website Discovery.

A company record assembled from a website is a set of claims: this is the
company's name, this is its phone number, this is its address. Every one
of those claims came from somewhere — a specific element, on a specific
page, found a specific way. Evidence is that provenance, recorded as data.

Without it, a wrong phone number is unfalsifiable: nobody can tell whether
it came from a ``tel:`` link in the header or a regex that matched a fax
number in a footer. With it, every field can be traced back to the exact
page and element it was read from, which is what makes extraction
auditable rather than merely plausible.

This module is the model only. Nothing here extracts, parses, fetches, or
decides what a field is worth — see :mod:`app.discovery.website.confidence`
for the matching discipline on scores.

Granularity is the reason this exists separately from
:class:`app.engines.source_connectors.evidence.Evidence`. That model
records provenance for a whole *company record* discovered by a connector:
one ``source_url`` and one ``confidence`` for everything. It is shipped
connector code and is **not** modified by this phase. Website extraction
needs provenance per *field* — a name and a phone lifted from the same
site routinely come from different pages, by different methods, and
deserve different confidence. One record-level object cannot express
that, and widening it would change a public model that shipped connectors
already depend on.

Field names follow the open-enum pattern established by
:class:`app.discovery.plugins.base_plugin.PluginCapability`: the members
of :class:`ExtractedField` are the known fields, but any string is
accepted, so a future extractor can record a field the framework has
never heard of without this module changing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.discovery.website.confidence import Confidence
from app.discovery.website.url_normalizer import normalize_url

logger = logging.getLogger(__name__)


class ExtractedField(str, Enum):
    """A field that can be extracted from a company website.

    Mirrors the extractor interfaces added in the next unit, so a piece
    of evidence and the extractor that produced it name the same thing.

    **Open by design.** Because this subclasses ``str``, evidence may
    name a field not listed here::

        FieldEvidence(field="certifications", ...)

    Every comparison in this module passes through
    :func:`normalize_field`, so an unknown field stores, filters and
    serializes exactly like a declared one.
    """

    COMPANY_NAME = "company_name"
    PHONE = "phone"
    EMAIL = "email"
    ADDRESS = "address"
    LEADERSHIP = "leadership"
    SOCIAL = "social"
    SERVICES = "services"

    def __str__(self) -> str:
        """Render as the bare value, not ``ExtractedField.X``."""
        return self.value


def normalize_field(field: ExtractedField | str) -> str:
    """Reduce a field name to its canonical string form.

    The single choke point every field comparison passes through, which
    is what allows unknown field names to behave like declared ones.

    Args:
        field: Enum member, or a raw field name.

    Returns:
        The lowercase canonical field name.

    Raises:
        TypeError: If *field* is neither an enum member nor a string.
        ValueError: If *field* is empty or whitespace.
    """
    if isinstance(field, ExtractedField):
        return field.value
    if isinstance(field, str):
        normalized = field.strip().lower()
        if not normalized:
            raise ValueError("Field name must not be empty")
        return normalized
    raise TypeError(
        f"Field must be an ExtractedField or str, got {type(field).__name__}"
    )


@dataclass(frozen=True)
class FieldEvidence:
    """Provenance for a single extracted field.

    Immutable: evidence records what *was* observed. Correcting it means
    recording a new observation, not editing history.

    Attributes:
        field: Which field this supports. Canonical lowercase string.
        page_url: Canonical URL of the page the value was read from.
            Required — evidence that cannot say which page it came from
            is not evidence.
        selector: Locator for the element the value was read from (CSS
            selector, XPath, or a JSON-LD path). Empty when the method
            has no meaningful locator, such as a whole-page regex.
        method: How the value was found — ``"tel_link"``, ``"jsonld"``,
            ``"meta_tag"``, ``"regex"``. Free-form on purpose: the set of
            methods arrives with the extractors, and pinning it down now
            would guess at implementations that do not exist.
        confidence: Belief in this observation.
        snippet: Raw text the value was read from, for audit.
        extracted_at: ISO-8601 timestamp, or ``""`` when not recorded.
            Never auto-filled — a model that stamps itself is not a model.

    Example:
        >>> ev = FieldEvidence(
        ...     field=ExtractedField.PHONE,
        ...     page_url="acme.com/contact",
        ...     selector="a.tel",
        ...     method="tel_link",
        ...     confidence=Confidence(0.95),
        ... )
        >>> ev.page_url
        'https://acme.com/contact'
    """

    field: str
    page_url: str
    selector: str = ""
    method: str = ""
    confidence: Confidence = Confidence.UNKNOWN
    snippet: str = ""
    extracted_at: str = ""

    def __post_init__(self) -> None:
        """Canonicalize the field name and page URL, then validate.

        The page URL is normalized through
        :func:`~app.discovery.website.url_normalizer.normalize_url` so
        that two pieces of evidence from ``acme.com/x`` and
        ``https://www.acme.com/x/`` agree about their origin.

        Raises:
            TypeError: If *field*, *page_url* or *confidence* has the
                wrong type.
            ValueError: If *field* is empty, or *page_url* is blank or is
                not a usable web URL.
        """
        object.__setattr__(self, "field", normalize_field(self.field))

        if not isinstance(self.page_url, str):
            raise TypeError(
                f"FieldEvidence.page_url must be a str, "
                f"got {type(self.page_url).__name__}"
            )
        if not self.page_url.strip():
            raise ValueError(
                f"FieldEvidence.page_url is required (field={self.field!r})"
            )
        normalized = normalize_url(self.page_url)
        if normalized is None:
            raise ValueError(
                f"FieldEvidence.page_url is not a usable web URL: "
                f"{self.page_url!r} (field={self.field!r})"
            )
        object.__setattr__(self, "page_url", normalized)
        object.__setattr__(self, "confidence", Confidence.from_value(self.confidence))

    def to_dict(self) -> dict[str, Any]:
        """Serialize for plugin metadata and API output."""
        return {
            "field": self.field,
            "page_url": self.page_url,
            "selector": self.selector,
            "method": self.method,
            "confidence": self.confidence.score,
            "snippet": self.snippet,
            "extracted_at": self.extracted_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FieldEvidence:
        """Rebuild evidence from its serialized form.

        Args:
            data: Mapping produced by :meth:`to_dict`.

        Returns:
            A validated :class:`FieldEvidence`.

        Raises:
            KeyError: If ``field`` or ``page_url`` is absent.
            ValueError: If a value fails validation.
        """
        return cls(
            field=data["field"],
            page_url=data["page_url"],
            selector=str(data.get("selector", "")),
            method=str(data.get("method", "")),
            confidence=Confidence.from_value(data.get("confidence", 0.0)),
            snippet=str(data.get("snippet", "")),
            extracted_at=str(data.get("extracted_at", "")),
        )

    def __str__(self) -> str:
        locator = self.selector or self.method or "?"
        return f"{self.field}@{self.page_url} via {locator} ({self.confidence})"


class EvidenceSet:
    """Every piece of evidence gathered for one company.

    Holds many observations per field, because a real website offers the
    same field more than once — a phone number in the header, again in
    the footer, again on the contact page — and each occurrence is
    independent support for the claim.

    Mutable by design: evidence accumulates as pages are visited. The
    individual :class:`FieldEvidence` records inside it stay immutable.

    Usage:
        evidence = EvidenceSet()
        evidence.add(FieldEvidence(field="phone", page_url=url, ...))
        for observation in evidence.for_field(ExtractedField.PHONE):
            ...

    Deliberately has **no** "best evidence" or "winning value" accessor.
    Choosing which of three observed phone numbers is correct is
    extraction policy, and it arrives with the extractors. Observations
    are returned in the order they were added; the caller decides what
    that ordering means.
    """

    def __init__(self, evidence: Iterable[FieldEvidence] | None = None) -> None:
        """Initialize, optionally seeded with existing evidence.

        Args:
            evidence: Records to add immediately.
        """
        self._by_field: dict[str, list[FieldEvidence]] = {}
        if evidence:
            self.extend(evidence)

    def add(self, evidence: FieldEvidence) -> None:
        """Record one observation.

        Args:
            evidence: The observation to store.

        Raises:
            TypeError: If *evidence* is not a :class:`FieldEvidence`.
        """
        if not isinstance(evidence, FieldEvidence):
            raise TypeError(
                f"EvidenceSet.add expects FieldEvidence, "
                f"got {type(evidence).__name__}"
            )
        self._by_field.setdefault(evidence.field, []).append(evidence)

    def extend(self, evidence: Iterable[FieldEvidence]) -> None:
        """Record several observations, in order.

        Args:
            evidence: The observations to store.
        """
        for item in evidence:
            self.add(item)

    def for_field(self, field: ExtractedField | str) -> tuple[FieldEvidence, ...]:
        """Every observation recorded for *field*, in insertion order.

        Args:
            field: Field name, declared or not.

        Returns:
            Observations for that field; empty when there are none.
        """
        return tuple(self._by_field.get(normalize_field(field), ()))

    def has(self, field: ExtractedField | str) -> bool:
        """Whether any observation supports *field*."""
        return bool(self._by_field.get(normalize_field(field)))

    @property
    def fields(self) -> tuple[str, ...]:
        """Fields with at least one observation, in first-seen order."""
        return tuple(self._by_field)

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        """Serialize every observation, grouped by field."""
        return {
            name: [item.to_dict() for item in items]
            for name, items in self._by_field.items()
        }

    @classmethod
    def from_dict(cls, data: dict[str, list[dict[str, Any]]]) -> EvidenceSet:
        """Rebuild an evidence set from its serialized form.

        Args:
            data: Mapping produced by :meth:`to_dict`.

        Returns:
            A populated :class:`EvidenceSet`.
        """
        return cls(
            FieldEvidence.from_dict(item)
            for items in data.values()
            for item in items
        )

    def __len__(self) -> int:
        """Total number of observations across every field."""
        return sum(len(items) for items in self._by_field.values())

    def __iter__(self) -> Iterator[FieldEvidence]:
        """Iterate every observation, grouped by field in first-seen order."""
        for items in self._by_field.values():
            yield from items

    def __contains__(self, field: object) -> bool:
        """Whether *field* has any observation.

        Membership tests answer rather than raise, so an unusable key —
        a non-string, or an empty one — reports ``False`` instead of
        breaking a caller's ``if x in evidence`` guard.
        """
        if not isinstance(field, str):
            return False
        try:
            return self.has(field)
        except ValueError:
            return False

    def __repr__(self) -> str:
        counts = ", ".join(
            f"{name}={len(items)}" for name, items in self._by_field.items()
        )
        return f"<EvidenceSet {counts or 'empty'}>"

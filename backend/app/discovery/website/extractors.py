"""Extractor interfaces for Direct Website Discovery.

Seven fields must eventually be lifted off a company website: its name,
phone, email, address, leadership, social profiles and services. This
module declares the contract each of those extractors satisfies. It
contains **no extraction logic** — every ``extract`` method is abstract,
and no class here can be instantiated.

Why interfaces before implementations:

- Unit 8's plugin can be written against a stable contract while the
  seven implementations land one at a time in a later phase.
- Each field can then be implemented, tested and replaced in isolation.
  A better address extractor is a new class, not a change to a shared one.
- The evidence obligation is expressed in the type system: an extractor
  returns :class:`ExtractionResult`, which *cannot* be constructed without
  a :class:`~app.discovery.website.evidence.FieldEvidence`. Extraction
  without provenance is unrepresentable rather than merely discouraged.

**Page input is structural, not nominal.** Extractors read a page through
:class:`PageContent`, a Protocol whose members mirror
``app.crawlers.html_parser.ParsedPage`` exactly. That existing class
therefore satisfies this contract with no adapter, no subclassing and no
modification — it is reused, not replaced.

The Protocol is used instead of importing ``ParsedPage`` directly because
``app/crawlers/__init__.py`` eagerly imports ``http_crawler`` and
``session_manager``, so *any* import from that package pulls in
``aiohttp``. Phase 2.3A must add no HTTP dependency, and structural typing
gives the same compatibility guarantee without one. A future page model
that is not ``ParsedPage`` also qualifies automatically, which keeps the
crawler replaceable per CLAUDE.md §4.

Extraction is synchronous. The page has already been fetched and parsed by
the time an extractor sees it; what remains is CPU-bound work over data in
memory, and ``HTMLParser.parse`` is itself synchronous. Making these
methods ``async`` would add coroutine overhead and buy nothing.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

from app.discovery.website.evidence import ExtractedField, FieldEvidence

logger = logging.getLogger(__name__)


@runtime_checkable
class PageContent(Protocol):
    """A parsed page, as an extractor sees it.

    Members mirror ``app.crawlers.html_parser.ParsedPage`` field for
    field, so that class satisfies this Protocol structurally. Any other
    page representation providing the same members qualifies too.

    Attributes:
        url: URL the page was fetched from.
        title: ``<title>`` text.
        description: ``<meta name="description">`` content.
        emails: Unique email addresses found on the page.
        phones: Unique phone numbers found on the page.
        social_links: Platform name to profile URL.
        text_content: Whitespace-normalized visible text.
        links: Absolute URLs found on the page.
        h1_texts: ``<h1>`` texts, in document order.
        meta_keywords: ``<meta name="keywords">`` content.
    """

    url: str
    title: str
    description: str
    emails: Sequence[str]
    phones: Sequence[str]
    social_links: Mapping[str, str]
    text_content: str
    links: Sequence[str]
    h1_texts: Sequence[str]
    meta_keywords: str


@dataclass(frozen=True)
class ExtractionResult:
    """One value an extractor found, with the evidence supporting it.

    Value and provenance travel together and cannot be separated: there
    is no way to produce a result without saying where it came from.

    Not hashable — ``attributes`` is a mapping. Results are compared and
    serialized, never used as dict keys.

    Attributes:
        value: The extracted value, stripped. Never empty.
        evidence: Where and how it was found.
        attributes: Detail that a bare string cannot carry, keyed by
            convention per field: ``"title"`` for a leadership result,
            ``"platform"`` for a social one. Free-form rather than seven
            bespoke result models — the same choice
            :class:`~app.discovery.plugins.plugin_config.PluginConfig`
            makes with its ``options`` dict.
    """

    value: str
    evidence: FieldEvidence
    attributes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate the pairing.

        Raises:
            TypeError: If *value* is not a string, or *evidence* is not a
                :class:`FieldEvidence`.
            ValueError: If *value* is empty or whitespace.
        """
        if not isinstance(self.value, str):
            raise TypeError(
                f"ExtractionResult.value must be a str, "
                f"got {type(self.value).__name__}"
            )
        stripped = self.value.strip()
        if not stripped:
            raise ValueError("ExtractionResult.value must not be empty")
        if not isinstance(self.evidence, FieldEvidence):
            raise TypeError(
                f"ExtractionResult.evidence must be a FieldEvidence, "
                f"got {type(self.evidence).__name__}"
            )
        object.__setattr__(self, "value", stripped)

    @property
    def field_name(self) -> str:
        """The field this result belongs to, taken from its evidence.

        Single-sourced so a result and its evidence can never disagree
        about which field was extracted.
        """
        return self.evidence.field

    def to_dict(self) -> dict[str, Any]:
        """Serialize for plugin metadata and API output."""
        return {
            "field": self.field_name,
            "value": self.value,
            "evidence": self.evidence.to_dict(),
            "attributes": dict(self.attributes),
        }


class FieldExtractor(ABC):
    """Contract for lifting one field off a parsed page.

    One extractor handles exactly one field, so implementations stay
    small and independently replaceable.

    Subclasses set :attr:`field` and implement :meth:`extract`. The seven
    subclasses below fix :attr:`field` and remain abstract — they name the
    seven contracts without implementing any of them.

    Attributes:
        field: The :class:`~app.discovery.website.evidence.ExtractedField`
            this extractor produces.
        name: Short identifier for logs and metadata. Defaults to the
            class name.
    """

    field: ClassVar[ExtractedField]
    name: ClassVar[str] = ""

    @abstractmethod
    def extract(self, page: PageContent) -> list[ExtractionResult]:
        """Find every occurrence of this extractor's field on *page*.

        Implementations must:

        - return ``[]`` when nothing is found, never ``None``, so callers
          need no null check;
        - return one result per occurrence rather than picking a winner —
          choosing between three observed phone numbers is a later
          decision, made with all the evidence in hand;
        - attach a
          :class:`~app.discovery.website.evidence.FieldEvidence` whose
          ``page_url`` is ``page.url``;
        - never fetch anything. An extractor sees only the page it is
          given. Following links is the crawler's job.

        Args:
            page: The parsed page to read.

        Returns:
            Results in the order found, possibly empty.
        """
        raise NotImplementedError

    @property
    def extractor_name(self) -> str:
        """Identifier for logs, defaulting to the class name."""
        return self.name or type(self).__name__

    def describe(self) -> dict[str, str]:
        """Summarize this extractor for diagnostics.

        Mirrors
        :meth:`app.discovery.plugins.base_plugin.BaseDiscoveryPlugin.describe`
        so extractors and plugins report themselves the same way, per the
        logging standard in CLAUDE.md §6.
        """
        return {
            "name": self.extractor_name,
            "field": str(self.field),
        }

    def __repr__(self) -> str:
        # !s forces __str__; bare interpolation of an enum goes through
        # __format__, whose mixed-in behaviour varies across versions.
        return f"<{type(self).__name__} field={self.field!s}>"


class CompanyNameExtractor(FieldExtractor, ABC):
    """Extracts the company's own name.

    Sources available on :class:`PageContent`: ``title``, ``h1_texts``,
    ``description``, ``text_content``. Implementations are expected to
    strip site-name boilerplate ("Acme Inc | Home") and may reuse the
    corporate-suffix handling already in
    ``app.engines.source_connectors.normalizer``.
    """

    field = ExtractedField.COMPANY_NAME


class PhoneExtractor(FieldExtractor, ABC):
    """Extracts contact phone numbers.

    Sources: ``phones`` (already regex-harvested by the existing parser)
    and ``text_content``. A ``tel:`` link is far stronger evidence than a
    loose text match, and implementations are expected to say so through
    ``confidence`` and ``method`` rather than by discarding the weaker one.
    """

    field = ExtractedField.PHONE


class EmailExtractor(FieldExtractor, ABC):
    """Extracts contact email addresses.

    Sources: ``emails`` and ``text_content``. Role addresses
    (``info@``, ``sales@``) and personal ones are both in scope; ranking
    them is not this interface's concern.
    """

    field = ExtractedField.EMAIL


class AddressExtractor(FieldExtractor, ABC):
    """Extracts postal addresses.

    Sources: ``text_content``, plus structured markup an implementation
    may parse itself. The hardest of the seven, because addresses have no
    reliable marker in plain text — which is exactly why it is isolated
    behind its own contract and can be replaced wholesale.
    """

    field = ExtractedField.ADDRESS


class LeadershipExtractor(FieldExtractor, ABC):
    """Extracts decision-makers: owners, principals, executives.

    Sources: ``text_content``, ``h1_texts``, ``links`` (an "About" or
    "Team" page is a strong signal, though following it is the crawler's
    job, not this extractor's).

    Convention: ``value`` is the person's name; the role goes in
    ``attributes["title"]``.
    """

    field = ExtractedField.LEADERSHIP


class SocialExtractor(FieldExtractor, ABC):
    """Extracts social media profile URLs.

    Source: ``social_links``, already keyed by platform by the existing
    parser.

    Convention: ``value`` is the profile URL; the platform goes in
    ``attributes["platform"]``.
    """

    field = ExtractedField.SOCIAL


class ServicesExtractor(FieldExtractor, ABC):
    """Extracts the services or trades a company offers.

    Sources: ``text_content``, ``meta_keywords``, ``h1_texts``,
    ``description``. One result per service, so a company offering three
    trades yields three results rather than one joined string.
    """

    field = ExtractedField.SERVICES


#: Every field contract declared here, in the order the phase specifies.
#: Unit 8's plugin iterates this rather than hard-coding seven names.
EXTRACTOR_INTERFACES: tuple[type[FieldExtractor], ...] = (
    CompanyNameExtractor,
    PhoneExtractor,
    EmailExtractor,
    AddressExtractor,
    LeadershipExtractor,
    SocialExtractor,
    ServicesExtractor,
)

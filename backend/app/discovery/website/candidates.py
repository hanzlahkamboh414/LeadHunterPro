"""Candidate Generator Interface for Direct Website Discovery.

Units 1-5 built the downstream half of this pipeline: canonicalize a URL,
reject a duplicate, score a belief, record provenance, lift a field off a
page. None of them answers the first question — where does the list of
URLs to visit come from?

This module is that seam. It names the abstraction "a thing that proposes
company websites for an industry and location", so a search-backed
generator, a state-registry generator, a directory generator and a static
seed list are all the same type: interchangeable at runtime and
individually deletable. The engine depends on the abstraction and never
on a provider, which is what keeps a search API optional infrastructure
rather than core architecture (CLAUDE.md §3, §8) and keeps every source
replaceable (§4).

It contains **no implementation**. ``generate`` is abstract, no concrete
generator ships here, and nothing in this module fetches, parses, ranks,
scores or filters anything.

Why URL validity is not defined here:
    :class:`Candidate` delegates entirely to
    :func:`~app.discovery.website.url_normalizer.normalize_url` and adds
    no host rules of its own. A second definition of "valid URL" inside
    this value object would disagree with
    :class:`~app.discovery.website.evidence.FieldEvidence` and
    :class:`~app.discovery.website.url_filter.DuplicateURLFilter`, which
    consult the same normalizer — so a URL blocked here would still reach
    the crawl queue through the filter, and the block would be a symptom
    fix rather than a root-cause one (CLAUDE.md §7). One definition, one
    place. Rejecting placeholder or unwanted hosts is discovery *policy*,
    configured later, not a property of the value object.

Why nothing here imports ``app.discovery.sources``:
    Reporting a :class:`~app.discovery.sources.status.SourceStatus` from a
    generator would duplicate a contract the plugin layer already owns,
    and importing that package executes its eager ``__init__``. Unit 5 met
    the same shape with ``app.crawlers`` pulling in ``aiohttp``. This
    module stays a leaf: it imports Units 1 and 3, and nothing else.

Why ``generate`` is synchronous:
    ``BaseSource.discover`` and ``BaseDiscoveryPlugin.discover`` are both
    synchronous. Matching them keeps one calling convention across the
    discovery layer; a generator that performs I/O does so inside its own
    implementation, in a later phase.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from app.discovery.website.confidence import Confidence
from app.discovery.website.url_normalizer import (
    canonical_key,
    extract_host,
    normalize_url,
)


@dataclass(frozen=True)
class Candidate:
    """One proposed company website, with its origin and prior belief.

    A candidate is an *input* to discovery — a URL judged worth visiting —
    not a result. Nothing here has been fetched.

    Immutable, and impossible to construct in an invalid state: the URL is
    canonicalized on the way in and an anonymous candidate is refused, so
    provenance survives aggregation across several generators.

    Not hashable — ``attributes`` is a mapping. Use :attr:`key` when a
    candidate needs an identity handle, the same way
    :class:`~app.discovery.website.extractors.ExtractionResult` is
    compared and serialized but never used as a dict key.

    Attributes:
        url: The candidate company website, stored canonicalized by
            :func:`~app.discovery.website.url_normalizer.normalize_url`.
        generator: Which generator proposed it, stripped and lowercased.
            Named ``generator`` rather than ``source`` because "source"
            already means ``BaseSource`` in this codebase, and
            ``candidate.source`` would read ambiguously against
            ``Evidence.source_url`` and ``BaseSource.source_name``.
        confidence: Prior belief that this URL is a real company site.
            Defaults to :attr:`Confidence.UNKNOWN` — nothing has been
            established before the page is visited.
        attributes: Detail a bare URL cannot carry. Free-form rather than
            a bespoke model per origin, the same choice ``PluginConfig``
            makes with its ``options`` dict. Keys by convention:

            - ``"title"`` — headline the origin gave for this URL
            - ``"snippet"`` — description text the origin gave
            - ``"query"`` — the query string that surfaced it

            The conventions are documented, not enforced; an origin with
            no such detail simply omits them.
    """

    url: str
    generator: str
    confidence: Confidence = Confidence.UNKNOWN
    attributes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Canonicalize the URL and generator name, coerce the belief.

        Raises:
            TypeError: If *url* or *generator* is not a string, or if
                *confidence* is neither a real number nor a
                :class:`~app.discovery.website.confidence.Confidence`.
            ValueError: If *url* is empty or is not a usable web URL, if
                *generator* is empty, or if *confidence* is out of range.
        """
        if not isinstance(self.url, str):
            raise TypeError(
                f"Candidate.url must be a str, got {type(self.url).__name__}"
            )
        if not self.url.strip():
            raise ValueError(
                f"Candidate.url is required (generator={self.generator!r})"
            )

        # Unit 1 is the single authority on what a usable web URL is; this
        # class adds no rules of its own. See the module docstring.
        normalized = normalize_url(self.url)
        if normalized is None:
            raise ValueError(
                f"Candidate.url is not a usable web URL: {self.url!r} "
                f"(generator={self.generator!r})"
            )
        object.__setattr__(self, "url", normalized)

        if not isinstance(self.generator, str):
            raise TypeError(
                f"Candidate.generator must be a str, "
                f"got {type(self.generator).__name__}"
            )
        if not self.generator.strip():
            raise ValueError(
                f"Candidate.generator is required (url={self.url!r})"
            )
        object.__setattr__(self, "generator", self.generator.strip().lower())

        object.__setattr__(
            self, "confidence", Confidence.from_value(self.confidence)
        )

    @property
    def key(self) -> str:
        """Scheme-insensitive identity handle, for deduplication.

        This is the value
        :class:`~app.discovery.website.url_filter.DuplicateURLFilter`
        compares. Never fetch it — it is not a URL.

        Never ``None``: :meth:`__post_init__` has already proved the URL
        canonicalizes, and ``canonical_key`` re-runs that same parse.
        """
        return canonical_key(self.url)

    @property
    def host(self) -> str:
        """Canonical host, for per-host budgets and grouping.

        Never ``None``, for the same reason as :attr:`key`.
        """
        return extract_host(self.url)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for plugin metadata and API output."""
        return {
            "url": self.url,
            "generator": self.generator,
            "confidence": self.confidence.score,
            "attributes": dict(self.attributes),
        }

    def __str__(self) -> str:
        return (
            f"Candidate(url={self.url!r}, generator={self.generator!r}, "
            f"confidence={self.confidence})"
        )


class CandidateGenerator(ABC):
    """Contract for proposing candidate company URLs.

    One generator is one origin of candidates. A search-backed generator,
    a state-registry generator, a directory generator and a static seed
    list all satisfy this contract, so the pipeline depends on the
    abstraction and never on a provider (CLAUDE.md §4). If a provider
    disappears, one class is deleted.

    Subclasses implement :meth:`generate`; everything else has a working
    default, mirroring
    :class:`~app.discovery.website.extractors.FieldExtractor`.

    Attributes:
        name: Short identifier for logs and metadata. Empty means "use the
            class name".
    """

    name: ClassVar[str] = ""

    @abstractmethod
    def generate(
        self,
        *,
        industry: str,
        location: str,
        limit: int,
    ) -> list[Candidate]:
        """Propose candidate company URLs for *industry* in *location*.

        The signature matches ``BaseSource.discover`` and
        ``BaseDiscoveryPlugin.discover`` keyword for keyword, so one
        calling convention holds across the whole discovery layer.

        Implementations must:

        1. return ``[]`` when nothing is found, never ``None``, so callers
           need no null check;
        2. **raise** when the origin could not be consulted at all. ``[]``
           means "ran, found nothing"; an exception means "could not run".
           Catching a network error and returning ``[]`` reports "no
           companies exist" when the truth is "I failed" — the hidden
           failure CLAUDE.md §12 forbids. Unit 8 maps an exception to
           ``SourceStatus.FAILED`` with the message as the fallback
           reason, and ``[]`` to ``SourceStatus.EMPTY``;
        3. return at most *limit* candidates;
        4. set :attr:`Candidate.generator` on every result to
           :attr:`generator_name`, so provenance survives aggregation
           across generators;
        5. never deduplicate across generators — that belongs to
           :class:`~app.discovery.website.url_filter.DuplicateURLFilter`,
           owned by the caller. A generator that hides its own duplicates
           makes the filter's statistics lie (CLAUDE.md §6);
        6. never fetch page content. A generator proposes URLs; visiting
           them is the crawler's job — the same boundary
           :class:`~app.discovery.website.extractors.FieldExtractor`
           draws for extraction;
        7. never fall back to fixture data. Bridge data is a separate,
           clearly named generator whose :attr:`Candidate.generator`
           value exposes it on every record, never a silent branch inside
           a live one (CLAUDE.md §1).

        Args:
            industry: Industry keyword (e.g. ``"Roofing"``).
            location: Geographic location (e.g. ``"Dallas Texas"``).
            limit: Maximum number of candidates to return.

        Returns:
            Candidates in the order proposed, possibly empty.

        Raises:
            NotImplementedError: Always. This declares a contract; Phase
                2.3A ships no generator that satisfies it.
        """
        raise NotImplementedError

    @property
    def generator_name(self) -> str:
        """Identifier for logs, defaulting to the class name."""
        return self.name or type(self).__name__

    def describe(self) -> dict[str, str]:
        """Summarize this generator for diagnostics.

        Mirrors
        :meth:`app.discovery.website.extractors.FieldExtractor.describe`
        so generators and extractors report themselves the same way, per
        the logging standard in CLAUDE.md §6.
        """
        return {"name": self.generator_name, "class": type(self).__name__}

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.generator_name!r}>"

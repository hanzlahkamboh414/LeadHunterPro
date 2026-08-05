"""Duplicate URL filtering for Direct Website Discovery.

A candidate generator will produce the same website many times over —
the same company reached from two directory pages, a homepage linked as
both ``http://`` and ``https://``, a listing that appends a trailing
slash. Crawling each variant wastes the crawl budget and reports one
company several times.

This filter is the choke point that prevents that. It is deliberately
separate from :class:`app.engines.source_connectors.deduplicator.Deduplicator`,
which deduplicates *already-discovered* :class:`CompanyResult` records by
domain and company name. That runs at the end of the pipeline and needs a
fetched record to work on; this runs at the start, on raw URL strings,
before anything has been fetched. Neither can do the other's job.

Two different canonical forms from :mod:`app.discovery.website.url_normalizer`
are used, and the split is the whole design:

- **Identity** is :func:`~app.discovery.website.url_normalizer.canonical_key`,
  which ignores scheme, ``www`` and trailing slash. This is what makes
  ``http://www.acme.com/`` and ``https://acme.com`` collapse to one entry.
- **The value handed back** is
  :func:`~app.discovery.website.url_normalizer.normalize_url`, a URL that
  is still safe to fetch, taken from whichever variant arrived *first*.

First-seen wins. The filter never rewrites a URL's scheme to make it
match an earlier one, because the earlier scheme is the one already known
to have been offered by a real source.

Rejections are counted by reason rather than lumped together: a URL that
was dropped because it is unparseable is a very different signal from one
dropped because it is a repeat, and CLAUDE.md §6 requires discovery
diagnostics to say which happened.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from app.discovery.website.url_normalizer import canonical_key, normalize_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilterStats:
    """Outcome counts for one filtering session.

    Attributes:
        total: URLs offered to the filter.
        accepted: URLs that were new and usable.
        duplicates: URLs rejected as repeats of an accepted URL.
        invalid: URLs rejected as unusable (empty, malformed, or a
            non-crawlable scheme such as ``mailto:``).
    """

    total: int = 0
    accepted: int = 0
    duplicates: int = 0
    invalid: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize for plugin metadata and health reporting."""
        return {
            "total": self.total,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "invalid": self.invalid,
        }


class DuplicateURLFilter:
    """Accepts each distinct website URL exactly once.

    The filter is stateful and order-sensitive by design: it is meant to
    be fed a stream of candidates and to remember everything it has
    already accepted. One instance corresponds to one discovery run;
    call :meth:`reset` to reuse it.

    Usage:
        url_filter = DuplicateURLFilter()
        for raw in candidates:
            url = url_filter.accept(raw)
            if url is not None:
                queue.append(url)
        logger.info("URL filter: %s", url_filter.stats.to_dict())

    Not thread-safe. Discovery runs are sequential today; a concurrent
    executor must give each worker its own filter or guard a shared one.
    """

    def __init__(self) -> None:
        """Initialize an empty filter."""
        self._seen: dict[str, str] = {}
        self._total = 0
        self._duplicates = 0
        self._invalid = 0

    def accept(self, url: str) -> str | None:
        """Offer *url* to the filter.

        Args:
            url: Raw candidate URL, in any shape the normalizer accepts.

        Returns:
            The canonical, fetchable URL when *url* is new and usable.
            ``None`` when it is a duplicate or not a usable web URL —
            check :attr:`stats` to tell those two cases apart.
        """
        self._total += 1

        key = canonical_key(url)
        if key is None:
            self._invalid += 1
            logger.debug("URL filter: rejected unusable candidate %r", url)
            return None

        existing = self._seen.get(key)
        if existing is not None:
            self._duplicates += 1
            logger.debug(
                "URL filter: %r duplicates already-accepted %r (key=%r)",
                url,
                existing,
                key,
            )
            return None

        # canonical_key already proved the URL parses, so this cannot be None.
        normalized = normalize_url(url)
        if normalized is None:  # pragma: no cover - defensive, unreachable
            self._invalid += 1
            return None

        self._seen[key] = normalized
        return normalized

    def filter(self, urls: Iterable[str]) -> list[str]:
        """Accept every new URL in *urls*, preserving first-seen order.

        Args:
            urls: Raw candidate URLs.

        Returns:
            Canonical URLs, one per distinct website, in the order they
            were first encountered.
        """
        return [accepted for url in urls if (accepted := self.accept(url)) is not None]

    def is_duplicate(self, url: str) -> bool:
        """Whether *url* has already been accepted, without recording it.

        An unusable URL is not a duplicate — it is invalid. This returns
        ``False`` for those, and the counters are left untouched.

        Args:
            url: Raw candidate URL.

        Returns:
            True if an equivalent URL was already accepted.
        """
        key = canonical_key(url)
        return key is not None and key in self._seen

    def reset(self) -> None:
        """Forget every accepted URL and zero the counters."""
        self._seen.clear()
        self._total = 0
        self._duplicates = 0
        self._invalid = 0

    @property
    def stats(self) -> FilterStats:
        """Outcome counts since construction or the last :meth:`reset`."""
        return FilterStats(
            total=self._total,
            accepted=len(self._seen),
            duplicates=self._duplicates,
            invalid=self._invalid,
        )

    @property
    def accepted(self) -> list[str]:
        """Every accepted URL, in first-seen order."""
        return list(self._seen.values())

    def __len__(self) -> int:
        """Number of distinct URLs accepted."""
        return len(self._seen)

    def __contains__(self, url: object) -> bool:
        """Whether *url* has already been accepted."""
        return isinstance(url, str) and self.is_duplicate(url)

    def __iter__(self) -> Iterator[str]:
        """Iterate accepted URLs in first-seen order."""
        return iter(self._seen.values())

    def __repr__(self) -> str:
        stats = self.stats
        return (
            f"<DuplicateURLFilter accepted={stats.accepted}"
            f" duplicates={stats.duplicates}"
            f" invalid={stats.invalid}>"
        )

"""URL canonicalization for Direct Website Discovery.

Candidate URLs reach the discovery pipeline from many shapes of input —
bare domains typed by a user, ``//`` protocol-relative links, links with
tracking fragments, mixed-case hosts, ``www`` prefixes, explicit default
ports, trailing slashes. Left alone, those variants all point at the same
page but compare as different strings, so the same website would be
crawled repeatedly and reported as several companies.

This module reduces those variants to one canonical form. It is pure
string work: no network access, no DNS resolution, no HTTP. A URL that
normalizes successfully is *syntactically* canonical, which says nothing
about whether it resolves.

Two forms are produced, and the distinction matters:

:func:`normalize_url`
    A canonical URL that is still safe to fetch. The scheme is preserved
    exactly as given, because an ``http``-only host does not necessarily
    answer on ``https`` — silently upgrading would fabricate a URL that
    may never resolve.

:func:`canonical_key`
    A scheme-insensitive identity key for deduplication only. Never fetch
    this; it is not a URL. ``http://x.com`` and ``https://www.x.com/``
    collapse to the same key, which is what makes duplicate filtering
    work across schemes.

Deliberate non-goals, so the behaviour is not mistaken for a bug:

- **Query strings are preserved and never reordered.** Parameter order is
  significant to some servers, and dropping a query would discard a real
  page identity (``/profile?id=12`` is not ``/profile``).
- **Fragments are always dropped.** A fragment addresses a position
  within a page, never a different page, and is not sent to the server.
- **Credentials in the netloc are dropped.** ``user:pass@host`` is not
  part of a page's identity and must not travel into logs or dedup keys.
- **Percent-encoding and duplicate slashes are left as-is.** Rewriting
  them can change which resource a server returns.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

#: Scheme assumed when a candidate arrives without one (``"acme.com"``).
DEFAULT_SCHEME = "https"

#: Schemes a website-discovery candidate may use. Anything else —
#: ``mailto``, ``tel``, ``javascript``, ``ftp``, ``data`` — is not a
#: crawlable page and is rejected rather than coerced.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Ports implied by their scheme. Present explicitly, they are redundant
#: and must be removed or ``x.com`` and ``x.com:443`` compare unequal.
_DEFAULT_PORTS = {"http": 80, "https": 443}

#: Matches a leading ``scheme:``. Needed to tell ``"acme.com/path"``
#: (no scheme, prepend one) from ``"mailto:a@b.com"`` (has a scheme,
#: reject it). Testing for ``"://"`` alone would misread ``mailto:``
#: as scheme-less and turn it into ``https://mailto:a@b.com``.
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _canonical_parts(url: str) -> tuple[str, str, str, str] | None:
    """Reduce *url* to canonical ``(scheme, netloc, path, query)``.

    The single parsing path shared by every public function in this
    module, so the three of them can never disagree about what a URL
    means.

    Args:
        url: Raw candidate URL. May omit the scheme.

    Returns:
        Tuple of canonical components, or ``None`` when *url* is empty,
        malformed, carries a non-web scheme, or has no host.
    """
    if not url or not url.strip():
        return None

    candidate = url.strip()
    if not _SCHEME_RE.match(candidate):
        # Protocol-relative ("//acme.com") keeps its slashes; a bare
        # domain ("acme.com/about") needs the full prefix.
        prefix = f"{DEFAULT_SCHEME}:" if candidate.startswith("//") else f"{DEFAULT_SCHEME}://"
        candidate = f"{prefix}{candidate}"

    try:
        parts = urlsplit(candidate)
        host = parts.hostname
        port = parts.port
    except ValueError:
        # Malformed authority — an out-of-range or non-numeric port.
        logger.debug("URL rejected, unparseable: %r", url)
        return None

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        logger.debug("URL rejected, scheme %r not crawlable: %r", scheme, url)
        return None
    if not host:
        logger.debug("URL rejected, no host: %r", url)
        return None

    host = host.lower().rstrip(".")
    # Strip `www.` only when a registrable domain survives it. Without the
    # dot count, the host "www.com" would be reduced to the bare TLD "com".
    if host.startswith("www.") and host.count(".") >= 2:
        host = host[4:]
    if not host:
        return None

    netloc = host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{host}:{port}"

    path = parts.path
    if path in ("", "/"):
        # Root is always "/" so "x.com" and "x.com/" agree.
        path = "/"
    else:
        # "/about/" and "/about" are the same page to every web server.
        path = path.rstrip("/") or "/"

    return scheme, netloc, path, parts.query


def normalize_url(url: str) -> str | None:
    """Return *url* in canonical, still-fetchable form.

    The scheme is preserved, not upgraded — see the module docstring for
    why. Use :func:`canonical_key` when comparing two URLs for identity.

    Args:
        url: Raw candidate URL. May omit the scheme, in which case
            :data:`DEFAULT_SCHEME` is assumed.

    Returns:
        The canonical URL, or ``None`` if *url* is not a usable web URL.

    Example:
        >>> normalize_url("HTTP://WWW.Acme.com:80/About/#team")
        'http://acme.com/About'
        >>> normalize_url("acme.com")
        'https://acme.com/'
        >>> normalize_url("mailto:sales@acme.com") is None
        True
    """
    parts = _canonical_parts(url)
    if parts is None:
        return None
    scheme, netloc, path, query = parts
    return urlunsplit((scheme, netloc, path, query, ""))


def canonical_key(url: str) -> str | None:
    """Return a scheme-insensitive identity key for *url*.

    Two URLs share a key exactly when they address the same page. This
    is the value a duplicate filter should compare. It is **not** a URL
    and must never be fetched.

    Args:
        url: Raw candidate URL.

    Returns:
        The dedup key, or ``None`` if *url* is not a usable web URL.

    Example:
        >>> canonical_key("http://www.acme.com/")
        'acme.com/'
        >>> canonical_key("https://acme.com") == canonical_key("http://www.acme.com/")
        True
    """
    parts = _canonical_parts(url)
    if parts is None:
        return None
    _scheme, netloc, path, query = parts
    return f"{netloc}{path}?{query}" if query else f"{netloc}{path}"


def extract_host(url: str) -> str | None:
    """Return the canonical host of *url*, lowercased and ``www``-stripped.

    Used where identity is the *site* rather than the page — grouping
    several pages under one company, or enforcing a per-host crawl budget.

    Args:
        url: Raw candidate URL.

    Returns:
        The canonical host (``"acme.com"``), or ``None`` if *url* is not
        a usable web URL.

    Example:
        >>> extract_host("https://WWW.Acme.com/contact")
        'acme.com'
    """
    parts = _canonical_parts(url)
    if parts is None:
        return None
    netloc = parts[1]
    # Drop any non-default port; the host alone is the site identity.
    return netloc.split(":", 1)[0]

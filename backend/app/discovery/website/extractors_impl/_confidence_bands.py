"""Confidence bands for extractor implementations.

:mod:`app.discovery.website.confidence` deliberately ships no
``HIGH``/``MEDIUM``/``LOW`` constants — its docstring states that choosing
where the cut-offs sit is *policy*, and that letting policy into the value
object would be the first piece of scoring logic to creep into
infrastructure.

This module is where that policy lives instead. It sits in the
implementation layer, alongside the extractors that actually make the
judgement, so :class:`~app.discovery.website.confidence.Confidence` stays a
frozen, policy-free value object.

The bands are shared rather than repeated per extractor so that seven
implementations cannot drift apart on what "high confidence" means. They
are private to the package (leading underscore): nothing outside
``extractors_impl`` should depend on these particular numbers, because they
are a tuning decision, not a contract.

Band meanings, as used by the seven extractors:

- :data:`HIGH` — the value came from a structured, purpose-built source:
  a ``tel:``/``mailto:`` link, a parser-validated phone or email, a
  platform-keyed social link, or the page ``<title>``.
- :data:`MEDIUM` — the value came from a source that usually carries it but
  is not dedicated to it: an ``<h1>``, or ``<meta name="keywords">``.
- :data:`LOW` — the value was inferred from free text, where a match is
  suggestive rather than authoritative: prose addresses, leadership names,
  services named in body copy, or a company name taken from the
  description.
"""

from __future__ import annotations

from app.discovery.website.confidence import Confidence

#: Structured, purpose-built source — tel:/mailto: links, parser-validated
#: contact details, platform-keyed social links, the page title.
HIGH = Confidence(0.90)

#: Source that usually carries the value but is not dedicated to it —
#: h1 headings, meta keywords.
MEDIUM = Confidence(0.60)

#: Inferred from free text, where a match is suggestive, not authoritative.
LOW = Confidence(0.30)

__all__ = ["HIGH", "LOW", "MEDIUM"]

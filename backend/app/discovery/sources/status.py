"""Discovery source status types.

Every source must return one of these statuses so the orchestrator can
make deterministic fallback decisions without any source being a
required dependency.
"""

from __future__ import annotations

from enum import Enum, auto


class SourceStatus(str, Enum):
    """Return status from a source's discover() call.

    Deliberately unchanged since the beginning, and deliberately NOT
    extended: every discovery source in the codebase returns these four, so
    adding near-synonyms (a separate "source error" beside ``ERROR``, a
    "no data" beside ``EMPTY``) would create two vocabularies meaning the
    same thing. Finer distinctions belong in ``metadata["reason"]`` — see
    :class:`SourceReason`.
    """

    #: Source executed successfully and returned companies.
    SUCCESS = "success"
    #: Source executed but found no matching companies.
    EMPTY = "empty"
    #: The source could not be REACHED at all — DNS failure, timeout,
    #: connection refused. It was never given the chance to answer, which is
    #: what separates this from ``ERROR``.
    UNAVAILABLE = "unavailable"
    #: The source answered, and the answer was a failure: it threw, rejected
    #: our request, or returned something unusable. ``metadata["reason"]``
    #: says which — a request WE malformed is a defect of ours, not an
    #: outage, and the two must not read alike.
    ERROR = "error"


class SourceHealth(str, Enum):
    """Health check result for the source health report."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class SourceReason(str, Enum):
    """WHY a source call ended as it did (``metadata["reason"]``).

    :class:`SourceStatus` answers "can the orchestrator use this result?",
    in four values every source already speaks. This answers the follow-up
    question a four-value vocabulary cannot: "is anything broken, and on
    which side?" It is machine-readable on purpose — analytics and
    alerting key off it — while ``detail`` and ``note`` carry the
    human-readable diagnostic context and stay free text.

    The pairs are fixed:

        HTTP 4xx        -> ERROR       + REQUEST_ERROR
        HTTP 5xx        -> ERROR       + SOURCE_ERROR
        timeout/network -> UNAVAILABLE + ACCESS_ERROR
        HTTP 200, empty -> EMPTY       + NO_DATA

    The distinction is not cosmetic. Collapsing it is how the USAspending
    intent plugin spent its entire life reporting a malformed request body
    as an unreachable source — a permanently dead source that every status
    line described as merely flaky.

    Deliberately additive: no existing source has to adopt it, and a source
    that sets no ``reason`` behaves exactly as before.
    """

    #: The source rejected the request we built (HTTP 4xx). OUR bug — a
    #: permanent defect until someone fixes it, so it must never be filed
    #: under the same label as a source being down.
    REQUEST_ERROR = "request_error"
    #: The source answered with a failure (HTTP 5xx, an unparsable body).
    #: Their side is broken.
    SOURCE_ERROR = "source_error"
    #: The source could not be reached at all (timeout, DNS, refused).
    ACCESS_ERROR = "access_error"
    #: The source answered correctly and there was nothing to return. Not a
    #: failure — it is the reason ``EMPTY`` is a real answer.
    NO_DATA = "no_data"

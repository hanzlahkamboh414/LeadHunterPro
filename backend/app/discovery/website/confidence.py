"""Confidence model for Direct Website Discovery.

Every field extracted from a website carries a degree of belief. A phone
number lifted from a ``tel:`` link is close to certain; the same number
guessed from free text in a page footer is not. Downstream ranking and
validation need that difference expressed as data, not as a comment.

This module is the model only. It holds a score and guarantees the score
is meaningful. It does **not** compute one — there is no scoring
algorithm here, and none belongs here. Deciding that a ``tel:`` link is
worth 0.95 while a footer regex match is worth 0.4 is extraction policy,
which arrives in a later phase along with the extractors themselves.

Why a value object rather than a bare ``float``:

- The ``0.0..1.0`` invariant is enforced once, at construction, instead
  of being re-checked (or forgotten) at every use site. A ``Confidence``
  that exists is always valid.
- ``NaN``, ``2.5``, ``-1`` and ``"high"`` fail loudly at the point the
  mistake was made, rather than silently poisoning a comparison later.
  A silent bad score would look exactly like a real one.
- Future scoring logic gets one obvious place to attach, without every
  caller changing type.

Deliberately absent, so the omissions read as decisions rather than gaps:

- **No confidence bands** (``HIGH``/``MEDIUM``/``LOW``). Choosing where
  the cut-offs sit is ranking policy and would be the first piece of
  scoring logic to creep in.
- **No combination or aggregation.** How two independent observations of
  the same field merge is a real question with several defensible
  answers; it is answered by the scoring phase, not pre-empted here.

Related but distinct: :class:`app.engines.source_connectors.evidence.Evidence`
carries a ``confidence`` float for a whole *company record* discovered by
a connector. That model is shipped and untouched. This one describes a
single *field*, which is a different granularity — see
:mod:`app.discovery.website.evidence`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

#: Lowest meaningful score — no belief at all.
MIN_SCORE: float = 0.0

#: Highest meaningful score — certainty.
MAX_SCORE: float = 1.0


@dataclass(frozen=True, order=True)
class Confidence:
    """A validated degree of belief in a single extracted field.

    Immutable, hashable, and orderable, so confidences can be compared,
    sorted, and used as dict keys or set members without surprises.

    Attributes:
        score: Belief in ``[0.0, 1.0]``. Always a ``float``, even when
            constructed from an ``int``.

    Example:
        >>> Confidence(0.9) > Confidence(0.4)
        True
        >>> Confidence(0.9).meets(0.8)
        True
        >>> Confidence.CERTAIN.score
        1.0
    """

    score: float

    #: Certainty. Reserved for facts a page states unambiguously.
    CERTAIN: ClassVar[Confidence]
    #: No belief. The correct value when nothing has been established yet.
    UNKNOWN: ClassVar[Confidence]

    def __post_init__(self) -> None:
        """Coerce to ``float`` and reject anything outside the range.

        Raises:
            TypeError: If *score* is not a real number. ``bool`` is
                rejected explicitly: it passes ``isinstance(x, int)``,
                so a flag passed where a score belongs would otherwise
                become ``1.0`` silently.
            ValueError: If *score* is ``NaN`` or outside ``[0.0, 1.0]``.
        """
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise TypeError(
                f"Confidence.score must be a real number, "
                f"got {type(self.score).__name__}"
            )

        value = float(self.score)
        if math.isnan(value):
            raise ValueError("Confidence.score must not be NaN")
        if not MIN_SCORE <= value <= MAX_SCORE:
            raise ValueError(
                f"Confidence.score must be in [{MIN_SCORE}, {MAX_SCORE}], "
                f"got {value!r}"
            )

        # Frozen dataclasses forbid normal assignment; this is the
        # documented way to normalize a field during __post_init__.
        object.__setattr__(self, "score", value)

    @property
    def is_unknown(self) -> bool:
        """Whether nothing has been established (score is exactly zero)."""
        return self.score == MIN_SCORE

    @property
    def is_certain(self) -> bool:
        """Whether the value is asserted without doubt (score is exactly one)."""
        return self.score == MAX_SCORE

    def meets(self, threshold: float) -> bool:
        """Whether this confidence reaches *threshold*.

        Mirrors :meth:`app.engines.source_connectors.evidence.Evidence.is_reliable`
        so the two provenance models read the same way at call sites.

        Args:
            threshold: Minimum acceptable score.

        Returns:
            True if ``score >= threshold``.
        """
        return self.score >= threshold

    def to_dict(self) -> dict[str, Any]:
        """Serialize for plugin metadata and API output."""
        return {"score": self.score}

    @classmethod
    def from_value(cls, value: Any) -> Confidence:
        """Build a :class:`Confidence` from a loose input.

        Accepts an existing :class:`Confidence` unchanged, or any real
        number. Invalid input raises rather than being clamped: a score
        of ``1.7`` means the producer has a bug, and clamping it to
        ``1.0`` would hide that bug behind a plausible value.

        Args:
            value: A :class:`Confidence`, ``int``, or ``float``.

        Returns:
            A validated :class:`Confidence`.

        Raises:
            TypeError: If *value* is not a number or a Confidence.
            ValueError: If *value* is out of range or ``NaN``.
        """
        if isinstance(value, cls):
            return value
        return cls(value)

    def __str__(self) -> str:
        """Render as the bare score, for log lines."""
        return f"{self.score:.2f}"

    def __repr__(self) -> str:
        return f"<Confidence {self.score:.2f}>"


Confidence.CERTAIN = Confidence(MAX_SCORE)
Confidence.UNKNOWN = Confidence(MIN_SCORE)

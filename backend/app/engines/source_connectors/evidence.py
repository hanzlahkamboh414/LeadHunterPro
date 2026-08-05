"""Evidence model for connector discovery results.

Tracks the provenance and confidence of each discovered company record.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Evidence:
    """Source evidence for a single discovered company.

    Attributes:
        source_url: The original URL where the company was found.
        source_name: Human-readable name of the data source (e.g. 'AGC Texas').
        confidence: Confidence score in [0.0, 1.0].
        raw_snippet: Original text snippet from the source.
        fetched_at: ISO-8601 timestamp of when the data was fetched.
    """

    source_url: str
    source_name: str
    confidence: float = 1.0
    raw_snippet: str = ""
    fetched_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Evidence":
        """Create an Evidence from a raw dictionary.

        Args:
            data: Dictionary containing evidence fields.

        Returns:
            A new Evidence instance.
        """
        return cls(
            source_url=str(data.get("source_url", "")),
            source_name=str(data.get("source_name", "")),
            confidence=float(data.get("confidence", 1.0)),
            raw_snippet=str(data.get("raw_snippet", "")),
            fetched_at=str(data.get("fetched_at", "")),
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain dictionary."""
        return {
            "source_url": self.source_url,
            "source_name": self.source_name,
            "confidence": self.confidence,
            "raw_snippet": self.raw_snippet,
            "fetched_at": self.fetched_at,
        }

    def is_reliable(self, threshold: float = 0.5) -> bool:
        """Return True if confidence meets the given threshold.

        Args:
            threshold: Minimum confidence score (0.0–1.0).

        Returns:
            True if confidence >= threshold.
        """
        return self.confidence >= threshold

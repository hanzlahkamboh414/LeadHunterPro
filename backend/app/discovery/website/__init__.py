"""Direct Website Discovery.

Infrastructure for discovering companies by visiting their websites
directly, rather than by querying a search API. This package is a
concrete discovery plugin and deliberately lives *outside*
:mod:`app.discovery.plugins`, which Phase 2.1 froze as framework-only.

Phase 2.3A provides infrastructure only: URL canonicalization,
duplicate filtering, evidence and confidence models, and the interfaces
a future crawler will satisfy. No HTTP, parsing, crawling, extraction
or scoring happens here.
"""

from __future__ import annotations

from app.discovery.website.candidates import Candidate, CandidateGenerator
from app.discovery.website.confidence import MAX_SCORE, MIN_SCORE, Confidence
from app.discovery.website.evidence import (
    EvidenceSet,
    ExtractedField,
    FieldEvidence,
    normalize_field,
)
from app.discovery.website.extractors import (
    EXTRACTOR_INTERFACES,
    AddressExtractor,
    CompanyNameExtractor,
    EmailExtractor,
    ExtractionResult,
    FieldExtractor,
    LeadershipExtractor,
    PageContent,
    PhoneExtractor,
    ServicesExtractor,
    SocialExtractor,
)
from app.discovery.website.plugin_config_view import PluginConfigView, make_config_view
from app.discovery.website.url_filter import DuplicateURLFilter, FilterStats
from app.discovery.website.url_normalizer import (
    DEFAULT_SCHEME,
    canonical_key,
    extract_host,
    normalize_url,
)

__all__ = [
    "DEFAULT_SCHEME",
    "EXTRACTOR_INTERFACES",
    "MAX_SCORE",
    "MIN_SCORE",
    "AddressExtractor",
    "Candidate",
    "CandidateGenerator",
    "CompanyNameExtractor",
    "Confidence",
    "DuplicateURLFilter",
    "EmailExtractor",
    "EvidenceSet",
    "ExtractedField",
    "ExtractionResult",
    "FieldEvidence",
    "FieldExtractor",
    "FilterStats",
    "LeadershipExtractor",
    "PageContent",
    "PhoneExtractor",
    "PluginConfigView",
    "ServicesExtractor",
    "SocialExtractor",
    "canonical_key",
    "extract_host",
    "make_config_view",
    "normalize_field",
    "normalize_url",
]

"""Concrete field extractor implementations.

Each extractor implements one of the FieldExtractor interfaces from Unit 5.
Extractors analyze parsed HTML (PageContent) and return structured evidence.
"""

from __future__ import annotations

from app.discovery.website.extractors_impl.address import HtmlAddressExtractor
from app.discovery.website.extractors_impl.company_name import (
    HtmlCompanyNameExtractor,
)
from app.discovery.website.extractors_impl.email import HtmlEmailExtractor
from app.discovery.website.extractors_impl.leadership import HtmlLeadershipExtractor
from app.discovery.website.extractors_impl.phone import HtmlPhoneExtractor
from app.discovery.website.extractors_impl.services import HtmlServicesExtractor
from app.discovery.website.extractors_impl.social import HtmlSocialExtractor

__all__ = [
    "HtmlAddressExtractor",
    "HtmlCompanyNameExtractor",
    "HtmlEmailExtractor",
    "HtmlLeadershipExtractor",
    "HtmlPhoneExtractor",
    "HtmlServicesExtractor",
    "HtmlSocialExtractor",
]

"""Infrastructure tests for the Direct Website Discovery extractor interfaces.

Phase 2.3A ships interfaces only. These tests verify that the contracts
are declared, abstract, and satisfiable by the parser that already exists
— never that any field is correctly extracted, because no extractor is
implemented yet.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field as dataclass_field

import pytest

from app.discovery.website.confidence import Confidence
from app.discovery.website.evidence import ExtractedField, FieldEvidence
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

PAGE_URL = "https://acme.com/contact"


@dataclass
class StubPage:
    """Minimal PageContent, mirroring ParsedPage's members.

    Used so these tests need no HTML, no parser and no network.
    """

    url: str = PAGE_URL
    title: str = ""
    description: str = ""
    emails: list[str] = dataclass_field(default_factory=list)
    phones: list[str] = dataclass_field(default_factory=list)
    social_links: dict[str, str] = dataclass_field(default_factory=dict)
    text_content: str = ""
    links: list[str] = dataclass_field(default_factory=list)
    h1_texts: list[str] = dataclass_field(default_factory=list)
    meta_keywords: str = ""


def make_evidence(field=ExtractedField.PHONE, **kwargs) -> FieldEvidence:
    return FieldEvidence(field=field, page_url=PAGE_URL, **kwargs)


class TestPageContentProtocol:
    def test_stub_page_satisfies_the_protocol(self):
        assert isinstance(StubPage(), PageContent)

    def test_an_object_missing_members_does_not_satisfy_it(self):
        class NotAPage:
            url = PAGE_URL

        assert not isinstance(NotAPage(), PageContent)

    @pytest.mark.parametrize(
        "member",
        [
            "url",
            "title",
            "description",
            "emails",
            "phones",
            "social_links",
            "text_content",
            "links",
            "h1_texts",
            "meta_keywords",
        ],
    )
    def test_declares_every_member_extractors_read(self, member):
        assert member in PageContent.__annotations__

    def test_declares_no_extra_members(self):
        # The Protocol must mirror ParsedPage exactly; an extra member
        # would silently make the existing parser non-conforming.
        assert len(PageContent.__annotations__) == 10


class TestParsedPageCompatibility:
    """The reuse proof: the existing parser satisfies this contract as-is.

    Skipped when the crawler package's optional dependencies are absent —
    Phase 2.3A adds none of them.
    """

    def test_existing_parsed_page_satisfies_page_content(self):
        module = pytest.importorskip("app.crawlers.html_parser")
        page = module.ParsedPage(url=PAGE_URL)
        assert isinstance(page, PageContent)

    def test_no_adapter_or_subclassing_is_required(self):
        module = pytest.importorskip("app.crawlers.html_parser")
        assert not issubclass(module.ParsedPage, FieldExtractor)
        assert PageContent not in module.ParsedPage.__mro__


class TestExtractionResult:
    def test_value_and_evidence_are_stored(self):
        evidence = make_evidence()
        result = ExtractionResult(value="(555) 123-4567", evidence=evidence)
        assert result.value == "(555) 123-4567"
        assert result.evidence is evidence

    def test_value_is_stripped(self):
        assert ExtractionResult(value="  Acme  ", evidence=make_evidence()).value == "Acme"

    def test_attributes_default_to_empty(self):
        assert ExtractionResult(value="Acme", evidence=make_evidence()).attributes == {}

    def test_attributes_carry_detail_a_string_cannot(self):
        result = ExtractionResult(
            value="Jane Doe",
            evidence=make_evidence(field=ExtractedField.LEADERSHIP),
            attributes={"title": "President"},
        )
        assert result.attributes["title"] == "President"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_value_raises(self, value):
        with pytest.raises(ValueError):
            ExtractionResult(value=value, evidence=make_evidence())

    @pytest.mark.parametrize("value", [None, 42, ["Acme"]])
    def test_non_string_value_raises_type_error(self, value):
        with pytest.raises(TypeError):
            ExtractionResult(value=value, evidence=make_evidence())

    @pytest.mark.parametrize("evidence", [None, "acme.com", {"field": "phone"}])
    def test_evidence_must_be_a_field_evidence(self, evidence):
        # A value without provenance must be unrepresentable, not merely
        # discouraged.
        with pytest.raises(TypeError):
            ExtractionResult(value="Acme", evidence=evidence)

    def test_evidence_is_required(self):
        with pytest.raises(TypeError):
            ExtractionResult(value="Acme")

    def test_field_name_comes_from_the_evidence(self):
        result = ExtractionResult(
            value="Acme", evidence=make_evidence(field=ExtractedField.COMPANY_NAME)
        )
        assert result.field_name == "company_name"

    def test_is_immutable(self):
        result = ExtractionResult(value="Acme", evidence=make_evidence())
        with pytest.raises(Exception):
            result.value = "Other"

    def test_to_dict_has_every_key(self):
        result = ExtractionResult(value="Acme", evidence=make_evidence())
        assert set(result.to_dict()) == {"field", "value", "evidence", "attributes"}

    def test_to_dict_nests_the_evidence(self):
        result = ExtractionResult(
            value="Acme", evidence=make_evidence(confidence=Confidence(0.9))
        )
        data = result.to_dict()
        assert data["evidence"]["page_url"] == PAGE_URL
        assert data["evidence"]["confidence"] == 0.9


class TestFieldExtractorContract:
    def test_the_base_class_is_abstract(self):
        with pytest.raises(TypeError):
            FieldExtractor()

    def test_extract_is_abstract(self):
        assert "extract" in FieldExtractor.__abstractmethods__

    def test_a_subclass_implementing_extract_is_concrete(self):
        class Impl(CompanyNameExtractor):
            def extract(self, page):
                return []

        assert Impl().extract(StubPage()) == []

    def test_extractor_name_defaults_to_the_class_name(self):
        class MyNameExtractor(CompanyNameExtractor):
            def extract(self, page):
                return []

        assert MyNameExtractor().extractor_name == "MyNameExtractor"

    def test_extractor_name_honours_an_explicit_name(self):
        class Impl(CompanyNameExtractor):
            name = "title_name"

            def extract(self, page):
                return []

        assert Impl().extractor_name == "title_name"

    def test_describe_reports_name_and_field(self):
        class Impl(PhoneExtractor):
            name = "tel_link"

            def extract(self, page):
                return []

        assert Impl().describe() == {"name": "tel_link", "field": "phone"}

    def test_repr_names_the_field(self):
        class Impl(EmailExtractor):
            def extract(self, page):
                return []

        assert "email" in repr(Impl())

    def test_a_subclass_can_return_results(self):
        class Impl(PhoneExtractor):
            def extract(self, page):
                return [
                    ExtractionResult(
                        value="(555) 123-4567",
                        evidence=FieldEvidence(
                            field=self.field, page_url=page.url, method="tel_link"
                        ),
                    )
                ]

        results = Impl().extract(StubPage())
        assert len(results) == 1
        assert results[0].field_name == "phone"
        assert results[0].evidence.page_url == PAGE_URL


class TestSevenFieldInterfaces:
    @pytest.mark.parametrize(
        ("interface", "expected"),
        [
            (CompanyNameExtractor, ExtractedField.COMPANY_NAME),
            (PhoneExtractor, ExtractedField.PHONE),
            (EmailExtractor, ExtractedField.EMAIL),
            (AddressExtractor, ExtractedField.ADDRESS),
            (LeadershipExtractor, ExtractedField.LEADERSHIP),
            (SocialExtractor, ExtractedField.SOCIAL),
            (ServicesExtractor, ExtractedField.SERVICES),
        ],
    )
    def test_each_interface_fixes_its_field(self, interface, expected):
        assert interface.field is expected

    @pytest.mark.parametrize("interface", EXTRACTOR_INTERFACES)
    def test_each_interface_is_abstract(self, interface):
        # Interfaces only: Phase 2.3A implements no extraction.
        with pytest.raises(TypeError):
            interface()

    @pytest.mark.parametrize("interface", EXTRACTOR_INTERFACES)
    def test_each_interface_derives_from_field_extractor(self, interface):
        assert issubclass(interface, FieldExtractor)
        assert issubclass(interface, ABC)

    @pytest.mark.parametrize("interface", EXTRACTOR_INTERFACES)
    def test_no_interface_implements_extract(self, interface):
        assert "extract" in interface.__abstractmethods__
        assert "extract" not in vars(interface)

    def test_the_registry_lists_seven_interfaces(self):
        assert len(EXTRACTOR_INTERFACES) == 7

    def test_the_registry_covers_every_extracted_field(self):
        assert {i.field for i in EXTRACTOR_INTERFACES} == set(ExtractedField)

    def test_the_registry_has_no_duplicate_fields(self):
        fields = [i.field for i in EXTRACTOR_INTERFACES]
        assert len(fields) == len(set(fields))

    def test_the_registry_is_a_tuple(self):
        assert isinstance(EXTRACTOR_INTERFACES, tuple)


class TestNoImplementation:
    """Phase 2.3A adds no fetching, parsing or scoring."""

    @pytest.mark.parametrize(
        "name",
        ["requests", "aiohttp", "httpx", "BeautifulSoup", "bs4", "selectolax", "re"],
    )
    def test_the_module_pulls_in_no_http_or_parsing_library(self, name):
        from app.discovery.website import extractors as module

        assert name not in vars(module)

    @pytest.mark.parametrize("name", ["fetch", "crawl", "get", "download", "parse"])
    def test_the_contract_declares_no_fetching_method(self, name):
        assert not hasattr(FieldExtractor, name)

    @pytest.mark.parametrize("name", ["score", "rank", "best", "combine", "resolve"])
    def test_the_contract_declares_no_scoring_method(self, name):
        # Choosing between observations is a later decision, made with all
        # the evidence in hand.
        assert not hasattr(FieldExtractor, name)
        assert not hasattr(ExtractionResult, name)

    def test_extract_is_synchronous(self):
        import inspect

        assert not inspect.iscoroutinefunction(FieldExtractor.extract)

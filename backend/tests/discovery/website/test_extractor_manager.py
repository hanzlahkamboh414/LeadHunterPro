"""Tests for ExtractorManager.

The manager is an orchestrator, not a parser. These tests verify what it
is responsible for — loading all seven extractors, running them over a
page, aggregating their results, and isolating failures — and deliberately
do **not** re-test how any individual extractor parses HTML. That belongs
to the seven test modules in this package, and duplicating it here would
make every extractor tuning change break the manager's tests too.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field

from app.discovery.website.evidence import ExtractedField
from app.discovery.website.extractor_manager import ExtractorManager
from app.discovery.website.extractors import (
    EXTRACTOR_INTERFACES,
    ExtractionResult,
    FieldExtractor,
)

PAGE_URL = "https://acme-roofing.com/contact"


@dataclass
class _StubPage:
    """A PageContent whose ten members mirror ParsedPage.

    Defaults are empty so a test can populate only the fields it cares
    about; the "rich" page below fills every one.
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


def _rich_page() -> _StubPage:
    """A page carrying a signal for every one of the seven fields."""
    return _StubPage(
        title="Acme Roofing | Home",
        description="Acme Roofing provides commercial roofing.",
        emails=["info@acme-roofing.com"],
        phones=["(555) 123-4567"],
        social_links={"linkedin": "https://linkedin.com/company/acme"},
        text_content=(
            "Our founder Jane Doe, President, started the company. "
            "Office: 123 Main St, Dallas, TX 75201. "
            "We provide concrete work."
        ),
        h1_texts=["Acme Roofing Company"],
        meta_keywords="roofing, excavation",
    )


class _BoomExtractor(FieldExtractor):
    """An extractor that always raises, to prove failures are isolated."""

    field = ExtractedField.COMPANY_NAME
    name = "boom"

    def extract(self, page):
        raise RuntimeError("extractor exploded")


class TestConstruction:
    """The manager loads the full extractor set."""

    def test_loads_seven_extractors(self):
        manager = ExtractorManager()
        assert len(manager.get_extractors()) == 7

    def test_all_are_field_extractors(self):
        manager = ExtractorManager()
        for extractor in manager.get_extractors():
            assert isinstance(extractor, FieldExtractor)

    def test_covers_every_declared_field_exactly_once(self):
        # One implementation per interface: the manager must not silently
        # ship six extractors, or two that fight over the same field.
        manager = ExtractorManager()
        fields = [e.field for e in manager.get_extractors()]
        assert len(fields) == len(set(fields))
        assert set(fields) == {i.field for i in EXTRACTOR_INTERFACES}

    def test_get_extractors_returns_a_copy(self):
        manager = ExtractorManager()
        borrowed = manager.get_extractors()
        borrowed.clear()
        assert len(manager.get_extractors()) == 7


class TestDescribe:
    """describe() reports the loaded set for diagnostics."""

    def test_count_matches_loaded_extractors(self):
        manager = ExtractorManager()
        described = manager.describe()
        assert described["count"] == len(manager.get_extractors())

    def test_lists_every_extractor(self):
        manager = ExtractorManager()
        described = manager.describe()
        assert len(described["extractors"]) == 7

    def test_each_entry_carries_name_and_field(self):
        manager = ExtractorManager()
        for entry in manager.describe()["extractors"]:
            assert entry["name"]
            assert entry["field"]


class TestExtractAll:
    """Running every extractor over one page."""

    def test_empty_page_yields_no_results(self):
        manager = ExtractorManager()
        assert manager.extract_all(_StubPage()) == []

    def test_returns_list_never_none(self):
        manager = ExtractorManager()
        results = manager.extract_all(_StubPage())
        assert results is not None
        assert isinstance(results, list)

    def test_rich_page_yields_results(self):
        manager = ExtractorManager()
        results = manager.extract_all(_rich_page())
        assert results

    def test_every_result_is_an_extraction_result(self):
        manager = ExtractorManager()
        for result in manager.extract_all(_rich_page()):
            assert isinstance(result, ExtractionResult)

    def test_all_seven_fields_represented(self):
        manager = ExtractorManager()
        results = manager.extract_all(_rich_page())
        found = {r.field_name for r in results}
        assert found == {str(i.field) for i in EXTRACTOR_INTERFACES}

    def test_evidence_points_at_the_page(self):
        manager = ExtractorManager()
        for result in manager.extract_all(_rich_page()):
            assert result.evidence.page_url == PAGE_URL

    def test_aggregate_equals_sum_of_parts(self):
        # The manager must neither drop nor duplicate results: running the
        # extractors itself must produce exactly what it produces via them.
        manager = ExtractorManager()
        page = _rich_page()
        expected = sum(len(e.extract(page)) for e in manager.get_extractors())
        assert len(manager.extract_all(page)) == expected


class TestFailureIsolation:
    """One broken extractor must not silence the others."""

    def test_other_extractors_still_run(self):
        manager = ExtractorManager()
        surviving = [
            e for e in manager.get_extractors() if e.field == ExtractedField.PHONE
        ]
        manager._extractors = [_BoomExtractor(), *surviving]

        results = manager.extract_all(_rich_page())

        assert [r.field_name for r in results] == [str(ExtractedField.PHONE)]

    def test_failure_is_logged_not_swallowed(self, caplog):
        manager = ExtractorManager()
        manager._extractors = [_BoomExtractor()]

        with caplog.at_level("ERROR"):
            results = manager.extract_all(_rich_page())

        assert results == []
        assert "boom" in caplog.text
        assert "extractor exploded" in caplog.text

    def test_failure_does_not_propagate(self):
        manager = ExtractorManager()
        manager._extractors = [_BoomExtractor()]
        # No raise: the manager's contract is to absorb extractor failures.
        assert manager.extract_all(_rich_page()) == []

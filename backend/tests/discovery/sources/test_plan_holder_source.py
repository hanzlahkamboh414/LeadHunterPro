"""Tests for :mod:`app.discovery.sources.plan_holder_source`.

Fully offline: the search and fetch seams are injected, and the only PDF
parsed is the committed plan-holder fixture — the same one
``test_pdf_plan_holder_parser`` runs end-to-end. These tests pin the SOURCE
behaviour (find PDFs -> parse -> emit records -> honest status), not the
parser (already covered by its own 40 tests).
"""

from __future__ import annotations

import pathlib

import pytest

from app.discovery.sources.plan_holder_source import (
    MAX_PDFS,
    PlanHolderSource,
    _is_pdf,
)
from app.discovery.sources.status import SourceStatus

FIXTURE_PDF = (
    pathlib.Path(__file__).resolve().parents[2]
    / "fixtures"
    / "pdf"
    / "hrgreen_plan_holder_list.pdf"
)

pytestmark_fixture = pytest.mark.skipif(
    not FIXTURE_PDF.exists(),
    reason=f"planholder fixture PDF not present at {FIXTURE_PDF}",
)

FIXTURE_URL = "https://www.hrgreen.com/wp-content/uploads/2024/12/Plan-Holder-List_20250121.pdf"


class _FakeResult:
    """Minimal stand-in for a SearchResult (only ``url`` is read)."""

    def __init__(self, url: str) -> None:
        self.url = url


def _fixture_bytes() -> bytes:
    return FIXTURE_PDF.read_bytes()


def _make_source(
    *,
    search: object | None = None,
    fetch: object | None = None,
) -> PlanHolderSource:
    """A source with the given (or fake) seams; fetch reads the fixture."""
    if fetch is None:
        def fetch(url: str) -> bytes:  # noqa: ANN001, ANN202
            return _fixture_bytes()
    return PlanHolderSource(search=search, fetch=fetch)


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


@pytestmark_fixture
def test_success_emits_all_rows() -> None:
    source = _make_source(
        search=lambda dorks: [_FakeResult(FIXTURE_URL)],
    )
    status, records, meta = source.discover(
        industry="Roofing", location="Cedar Rapids IA", limit=50
    )
    assert status == SourceStatus.SUCCESS
    assert meta["data_source"] == "live"
    # 28 distinct companies in the fixture (dupes across pages collapse here).
    assert len(records) == 28
    # Every record is unverified and carries the plan-holder detail block.
    for record in records:
        assert record["gate_accepted"] is False
        assert "plan_holder" in record
        assert record["source_url"] == FIXTURE_URL
        assert record["data_provenance"].startswith("plan_holder_pdf")


@pytestmark_fixture
def test_success_carries_person_and_person_bound_email() -> None:
    source = _make_source(search=lambda dorks: [_FakeResult(FIXTURE_URL)])
    _, records, _ = source.discover(
        industry="Roofing", location="Cedar Rapids IA", limit=50
    )
    pirc = next(r for r in records if r["company_name"] == "Pirc-Tobin")
    holder = pirc["plan_holder"]
    assert holder["person"]["name"] == "Charlie Arnold"
    assert holder["person"]["role"] == ""  # never backfilled (hard rule #2)
    assert holder["person"]["role_relevance"] is False
    assert holder["emails"][0]["tier"] == "person_bound"
    assert holder["emails"][0]["email"] == "cjarnold@pirctobin.com"
    # Website is derived from the real email domain — not a placeholder.
    assert pirc["website"] == "https://pirctobin.com"


@pytestmark_fixture
def test_free_mail_row_has_no_derived_website() -> None:
    """``aol.com``/``gmail.com`` are not a company domain -> website stays empty."""
    source = _make_source(search=lambda dorks: [_FakeResult(FIXTURE_URL)])
    _, records, _ = source.discover(
        industry="Roofing", location="Cedar Rapids IA", limit=50
    )
    citywide = next(r for r in records if r["company_name"] == "City Wide Construction Corp.")
    assert citywide["website"] == ""
    assert citywide["plan_holder"]["free_mail_only"] is True
    assert citywide["plan_holder"]["domain"] == ""


@pytestmark_fixture
def test_intra_source_dedup_by_domain() -> None:
    """Two PDFs listing the same firm collapse to one record (domain key)."""
    source = _make_source(
        search=lambda dorks: [_FakeResult(FIXTURE_URL), _FakeResult(FIXTURE_URL + "?copy")],
    )
    _, records, meta = source.discover(
        industry="Roofing", location="Cedar Rapids IA", limit=50
    )
    assert len(records) == 28
    assert meta["rows_raw"] == 56  # both PDFs parsed before dedup
    assert meta["rows_emitted"] == 28


@pytestmark_fixture
def test_limit_respects_bound() -> None:
    source = _make_source(search=lambda dorks: [_FakeResult(FIXTURE_URL)])
    _, records, _ = source.discover(
        industry="Roofing", location="Cedar Rapids IA", limit=5
    )
    assert len(records) == 5


# ---------------------------------------------------------------------------
# Empty / unavailable / error statuses
# ---------------------------------------------------------------------------


def test_limit_zero_is_empty() -> None:
    source = _make_source()
    status, records, meta = source.discover(industry="R", location="TX", limit=0)
    assert status == SourceStatus.EMPTY
    assert records == []
    assert meta["reason"] == "limit_zero"


def test_no_pdf_results_is_empty() -> None:
    source = _make_source(search=lambda dorks: [_FakeResult("https://example.com/dir")])
    status, records, meta = source.discover(industry="R", location="TX", limit=10)
    assert status == SourceStatus.EMPTY
    assert records == []
    assert meta["reason"] == "no_pdf_results"


@pytestmark_fixture
def test_skip_pdfs_filters_seen_urls() -> None:
    """A pass that already parsed PDFs only parses the unseen ones — the
    mechanism that lets multi-pass discovery ADVANCE instead of repeating."""
    source = _make_source(
        search=lambda dorks: [
            _FakeResult("https://a.com/old.pdf"),
            _FakeResult(FIXTURE_URL),
        ],
    )
    status, records, meta = source.discover(
        industry="Roofing", location="Cedar Rapids IA", limit=50,
        skip_pdfs={"https://a.com/old.pdf"},
    )
    assert status == SourceStatus.SUCCESS
    assert len(records) == 28  # only the fixture was parsed
    assert meta["pdf_urls"] == [FIXTURE_URL]  # old.pdf filtered out
    assert meta["pdfs_skipped"] == 1


def test_all_pdfs_already_parsed_is_honest_empty() -> None:
    """When every surfaced PDF was parsed in an earlier pass, the source says
    so explicitly (no_unseen_pdfs) so the caller can stop discovery — it must
    not silently re-parse the identical documents."""
    source = _make_source(search=lambda dorks: [_FakeResult("https://a.com/seen.pdf")])
    status, records, meta = source.discover(
        industry="R", location="TX", limit=10,
        skip_pdfs={"https://a.com/seen.pdf"},
    )
    assert status == SourceStatus.EMPTY
    assert records == []
    assert meta["reason"] == "no_unseen_pdfs"
    assert meta["pdfs_already_parsed"] == 1


def test_only_non_pdf_urls_are_filtered_out() -> None:
    source = _make_source(
        search=lambda dorks: [_FakeResult("https://a.com/list.html"), _FakeResult("https://a.com/list.pdf")]
    )
    status, records, _ = source.discover(industry="R", location="TX", limit=50)
    assert status == SourceStatus.SUCCESS  # the .pdf one parsed the fixture
    assert len(records) == 28


def test_all_fetches_fail_is_unavailable() -> None:
    def failing_fetch(url: str) -> bytes | None:  # noqa: ANN001
        return None

    source = _make_source(
        search=lambda dorks: [_FakeResult("https://a.com/list.pdf")],
        fetch=failing_fetch,
    )
    status, records, meta = source.discover(industry="R", location="TX", limit=10)
    assert status == SourceStatus.UNAVAILABLE
    assert records == []
    assert meta["reason"] == "all_fetches_failed"


@pytestmark_fixture
def test_reachable_pdf_with_no_rows_is_empty() -> None:
    def garbage_fetch(url: str) -> bytes:  # noqa: ANN001
        return b"this is not a pdf at all"

    source = _make_source(
        search=lambda dorks: [_FakeResult("https://a.com/list.pdf")],
        fetch=garbage_fetch,
    )
    status, records, meta = source.discover(industry="R", location="TX", limit=10)
    assert status == SourceStatus.EMPTY
    assert records == []
    assert meta["reason"] == "no_rows_in_pdfs"


def test_unexpected_search_error_is_error() -> None:
    def boom(dorks: list[str]) -> list:  # noqa: ANN001
        raise RuntimeError("search exploded")

    source = _make_source(search=boom)
    status, records, meta = source.discover(industry="R", location="TX", limit=10)
    assert status == SourceStatus.ERROR
    assert records == []
    assert "error" in meta


def test_pdf_cap_limits_fetches() -> None:
    """No more than MAX_PDFS PDFs are fetched in one call (bounded fan-out)."""
    urls = [_FakeResult(f"https://a.com/list{i}.pdf") for i in range(20)]

    def counting_fetch(url: str) -> bytes | None:  # noqa: ANN001
        return None  # every fetch fails -> UNAVAILABLE, but only MAX_PDFS tried

    source = _make_source(search=lambda dorks: urls, fetch=counting_fetch)
    status, records, meta = source.discover(industry="R", location="TX", limit=10)
    assert status == SourceStatus.UNAVAILABLE
    assert meta["pdfs_targeted"] == MAX_PDFS
    assert meta["fetch_failures"] == MAX_PDFS


# ---------------------------------------------------------------------------
# Lazy default search (credit control)
# ---------------------------------------------------------------------------


class _FakeSearchResult:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeSearchResponse:
    def __init__(self, results: list[_FakeSearchResult]) -> None:
        self.results = results


def _patch_manager(monkeypatch: pytest.MonkeyPatch, fake: object) -> None:
    monkeypatch.setattr(
        "app.search_providers.manager.SearchProviderManager", lambda: fake
    )


def test_default_search_is_lazy_stops_at_pdf_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live path runs dorks one at a time and stops once MAX_PDFS PDF URLs are
    collected, so a dork that already surfaces enough PDFs does not spend
    search credits on the remaining dorks (credit control)."""
    called: list[str] = []

    class _FakeManager:
        async def search(self, query: object) -> _FakeSearchResponse:  # noqa: ANN001
            called.append(query.keywords)
            # Each dork surfaces MAX_PDFS PDFs -> the loop must stop after the first.
            return _FakeSearchResponse(
                [_FakeSearchResult(f"https://a.com/list{i}.pdf") for i in range(MAX_PDFS)]
            )

    _patch_manager(monkeypatch, _FakeManager())
    source = PlanHolderSource()
    urls = source._default_search_pdfs(["dork1", "dork2", "dork3"])  # noqa: SLF001
    assert len(urls) == MAX_PDFS
    assert called == ["dork1"]  # stopped after the first dork


def test_default_search_continues_until_cap_met(monkeypatch: pytest.MonkeyPatch) -> None:
    """When each dork surfaces one PDF, the loop keeps searching until it has
    MAX_PDFS URLs or the dorks run out — it must not stop before the cap."""
    called: list[str] = []

    class _FakeManager:
        async def search(self, query: object) -> _FakeSearchResponse:  # noqa: ANN001
            called.append(query.keywords)
            return _FakeSearchResponse(
                [_FakeSearchResult(f"https://a.com/{query.keywords}.pdf")]
            )

    _patch_manager(monkeypatch, _FakeManager())
    source = PlanHolderSource()
    urls = source._default_search_pdfs(  # noqa: SLF001
        ["d1", "d2", "d3", "d4", "d5", "d6", "d7", "d8", "d9", "d10", "d11", "d12"]
    )
    assert len(urls) == MAX_PDFS
    assert len(called) == MAX_PDFS  # one dork per PDF needed


def test_default_search_skips_already_parsed_pdfs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass that already parsed PDFs must not re-collect them — the loop keeps
    querying dorks until it has MAX_PDFS *unseen* URLs (advance, not repeat)."""
    called: list[str] = []

    class _FakeManager:
        async def search(self, query: object) -> _FakeSearchResponse:  # noqa: ANN001
            called.append(query.keywords)
            # Every dork surfaces the same two PDFs already parsed.
            return _FakeSearchResponse(
                [_FakeSearchResult("https://a.com/list1.pdf"),
                 _FakeSearchResult("https://a.com/list2.pdf")]
            )

    _patch_manager(monkeypatch, _FakeManager())
    source = PlanHolderSource()
    urls = source._default_search_pdfs(  # noqa: SLF001
        ["d1", "d2", "d3", "d4", "d5", "d6"],
        skip={"https://a.com/list1.pdf", "https://a.com/list2.pdf"},
    )
    assert urls == []  # everything surfaced is already parsed
    assert len(called) == len(["d1", "d2", "d3", "d4", "d5", "d6"])  # exhausted all dorks


# ---------------------------------------------------------------------------
# Dork construction + pdf filter
# ---------------------------------------------------------------------------


def test_build_dorks_injects_industry_and_location() -> None:
    source = PlanHolderSource()
    dorks = source._build_dorks("Roofing", "Dallas TX")  # noqa: SLF001
    assert dorks
    for dork in dorks:
        assert "Roofing" in dork
        assert "Dallas TX" in dork
        assert "filetype:pdf" in dork
    assert len(dorks) == len(set(dorks))  # no duplicates


def test_is_pdf_heuristic() -> None:
    assert _is_pdf("https://a.com/list.pdf")
    assert _is_pdf("https://a.com/Plan-Holder-List_20250121.PDF?x=1")
    assert not _is_pdf("https://a.com/list.html")
    assert not _is_pdf("https://a.com/pdfroofing")

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
    _content_out_of_region,
    _is_pdf,
    _order_by_location,
)
from app.discovery.sources.status import SourceStatus
from app.discovery.template_candidates import TemplateCandidateStore
from app.discovery.yield_learning import (
    MIN_TRIALS,
    DiscoveryYieldStore,
    segment_key,
)

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
    # The committed fixture is an IOWA list, so the honest target region that
    # keeps it is Cedar Rapids IA (a TX target would now be content-dropped).
    status, records, _ = source.discover(industry="R", location="Cedar Rapids IA", limit=50)
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
    urls, _ = source._default_search_pdfs(  # noqa: SLF001
        [("t1", "dork1"), ("t2", "dork2"), ("t3", "dork3")]
    )
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
    urls, _ = source._default_search_pdfs(  # noqa: SLF001
        [(f"t{i}", f"d{i}") for i in range(1, 13)]
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
    urls, _ = source._default_search_pdfs(  # noqa: SLF001
        [(f"t{i}", f"d{i}") for i in range(1, 7)],
        skip={"https://a.com/list1.pdf", "https://a.com/list2.pdf"},
    )
    assert urls == []  # everything surfaced is already parsed
    assert len(called) == 6  # exhausted all dorks


class _FakeErrorSearchResponse:
    """A search response reporting a provider failure (status='error')."""

    def __init__(self, error: str = "tavily: HTTP 432 usage limit") -> None:
        self.results = []
        self.status = "error"
        self.error = error


def test_default_search_surfaces_provider_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dork search that ERRORS must surface WHY, not silently return [].

    Root-cause fix (§5): when every provider fails (Tavily 432 quota +
    SearXNG down), the failure is recorded so ``discover()`` can report
    UNAVAILABLE instead of a misleading ``no_pdf_results`` that reads as
    'nothing exists on the web'."""

    class _FakeManager:
        async def search(self, query: object) -> _FakeErrorSearchResponse:  # noqa: ANN001
            return _FakeErrorSearchResponse("tavily: HTTP 432 usage limit")

    _patch_manager(monkeypatch, _FakeManager())
    source = PlanHolderSource()
    urls, _ = source._default_search_pdfs([("t1", "dork1")])  # noqa: SLF001
    assert urls == []
    assert any("HTTP 432" in e for e in source._last_search_errors)


def test_search_failure_is_unavailable_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass where every dork search errored reports UNAVAILABLE + search_failed,
    so the pass_log records the real failure — never a quiet 'no_pdf_results'
    that would look like an empty-but-healthy web (§5/§12)."""

    class _FakeManager:
        async def search(self, query: object) -> _FakeErrorSearchResponse:  # noqa: ANN001
            return _FakeErrorSearchResponse("tavily: HTTP 432 usage limit")

    _patch_manager(monkeypatch, _FakeManager())
    source = PlanHolderSource()
    status, records, meta = source.discover(industry="R", location="TX", limit=10)
    assert status == SourceStatus.UNAVAILABLE
    assert records == []
    assert meta["reason"] == "search_failed"
    assert meta["search_errors"]


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


# ---------------------------------------------------------------------------
# Phase G — Layer-1 dork-yield gate + attribution
# ---------------------------------------------------------------------------


def test_learned_dead_dork_is_not_dispatched(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A dork template proven dead (MIN_TRIALS dispatches, zero working) is NOT
    dispatched again — its search is skipped so no provider credit is spent."""
    called: list[str] = []

    class _FakeManager:
        async def search(self, query: object) -> _FakeSearchResponse:  # noqa: ANN001
            called.append(query.keywords)
            return _FakeSearchResponse(
                [_FakeSearchResult(f"https://a.com/{query.keywords}.pdf")]
            )

    _patch_manager(monkeypatch, _FakeManager())
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    for _ in range(MIN_TRIALS):
        store.record_dispatch("t1")  # t1 proven dead (zero working)
    source = PlanHolderSource(yield_store=store)
    urls, _ = source._default_search_pdfs(  # noqa: SLF001
        [("t1", "dead dork"), ("t2", "live dork")]
    )
    assert called == ["live dork"]  # t1 never dispatched
    assert urls == ["https://a.com/live dork.pdf"]
    # t1 trials unchanged; t2 recorded one dispatch.
    assert store.get("t1") == {"trials": MIN_TRIALS, "working": 0}
    assert store.get("t2") == {"trials": 1, "working": 0}


@pytestmark_fixture
def test_discover_tags_records_with_producing_dork(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """Every record carries the dork TEMPLATE that surfaced its PDF, and the
    producing dork's dispatch is recorded — the attribution that lets research
    credit a WORKING lead back to Layer 1. Phase I: the trial lands on the
    (template, segment) row the discover() call derives from its own
    industry/location."""
    class _FakeManager:
        async def search(self, query: object) -> _FakeSearchResponse:  # noqa: ANN001
            return _FakeSearchResponse([_FakeSearchResult(FIXTURE_URL)])

    _patch_manager(monkeypatch, _FakeManager())
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    source = PlanHolderSource(
        fetch=lambda url: _fixture_bytes(), yield_store=store,
    )
    status, records, _ = source.discover(
        industry="Roofing", location="Cedar Rapids IA", limit=50,
    )
    assert status == SourceStatus.SUCCESS
    assert len(records) == 28
    for record in records:
        assert record["_discovery_dork"]  # the producing template, non-empty
    dork = records[0]["_discovery_dork"]
    seg = segment_key("Roofing", "Cedar Rapids IA")
    assert store.get(dork, seg)["trials"] >= 1


# ---------------------------------------------------------------------------
# Phase H — LLM-generated candidate consumption
# ---------------------------------------------------------------------------

_CAND = '"plan holder roster" {industry} {location} filetype:pdf'


def test_generated_candidate_joins_dork_pairs_only_when_provided(tmp_path) -> None:
    """A candidate store's dispatch-eligible generated dork joins the static
    set (deduped, AFTER the statics) — and only when ``candidate_store`` is
    actually injected. Without one, behavior is exactly pre-Phase-H."""
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    cand = TemplateCandidateStore(str(tmp_path / "cand.db"))
    assert cand.propose(_CAND) is True

    # No candidate_store -> only the static dorks dispatch.
    plain = PlanHolderSource(yield_store=store)
    pairs = plain._build_dork_pairs("Roofing", "Cedar Rapids IA")  # noqa: SLF001
    assert not any(tpl == _CAND for tpl, _ in pairs)

    # With candidate_store -> the generated pattern appears after the statics.
    source = PlanHolderSource(yield_store=store, candidate_store=cand)
    pairs = source._build_dork_pairs("Roofing", "Cedar Rapids IA")  # noqa: SLF001
    templates = [tpl for tpl, _ in pairs]
    assert _CAND in templates
    assert templates[-1] == _CAND  # generated dorks dispatch after statics
    # Filled dork carries both placeholders + filetype (a real dork shape).
    filled = dict(pairs)[_CAND]
    assert "Roofing" in filled
    assert "Cedar Rapids IA" in filled
    assert "filetype:pdf" in filled


def test_generated_candidate_dropped_by_yield_does_not_dispatch(tmp_path) -> None:
    """A candidate the Phase G loop proved dead (MIN_TRIALS, zero working) is
    NOT dispatched — it enters the same drop rule as any static dork."""
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    cand = TemplateCandidateStore(str(tmp_path / "cand.db"))
    cand.propose(_CAND)
    for _ in range(MIN_TRIALS):
        store.record_dispatch(_CAND)  # proven dead, zero working

    source = PlanHolderSource(yield_store=store, candidate_store=cand)
    pairs = source._build_dork_pairs("Roofing", "Cedar Rapids IA")  # noqa: SLF001
    assert not any(tpl == _CAND for tpl, _ in pairs)


# ---------------------------------------------------------------------------
# Phase I — segment-aware dispatch gate
# ---------------------------------------------------------------------------

def test_segment_dead_dork_still_dispatches_for_other_segment(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """A dork proven dead FOR ONE (trade, location) segment is skipped there but
    still dispatched for another — the whole point of Phase I coverage. Its
    dispatch trial lands on the (template, segment) row."""
    called: list[str] = []

    class _FakeManager:
        async def search(self, query: object) -> _FakeSearchResponse:  # noqa: ANN001
            called.append(query.keywords)
            return _FakeSearchResponse(
                [_FakeSearchResult(f"https://a.com/{query.keywords}.pdf")]
            )

    _patch_manager(monkeypatch, _FakeManager())
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    seg_dead = segment_key("roofing", "dallas tx")
    seg_live = segment_key("roofing", "houston tx")
    for _ in range(MIN_TRIALS):
        store.record_dispatch("t1", seg_dead)  # t1 dead for dallas, nothing for houston

    source = PlanHolderSource(yield_store=store)
    urls_dead, _ = source._default_search_pdfs(  # noqa: SLF001
        [("t1", "dork")], segment=seg_dead,
    )
    assert called == []          # dead segment -> nothing dispatched
    assert urls_dead == []

    called.clear()
    urls_live, _ = source._default_search_pdfs(  # noqa: SLF001
        [("t1", "dork")], segment=seg_live,
    )
    assert called == ["dork"]     # live segment -> still dispatched
    assert urls_live == ["https://a.com/dork.pdf"]
    # The trial was credited to the LIVE segment's row, not the global one.
    assert store.get("t1", seg_dead) == {"trials": MIN_TRIALS, "working": 0}
    assert store.get("t1", seg_live) == {"trials": 1, "working": 0}


def test_segment_dispatch_recorded_per_segment(monkeypatch, tmp_path) -> None:
    """NO segment argument (the Phase G path) records on the GLOBAL row; a
    segment argument records on that SEGMENT's row — old callers stay on the
    global row byte-for-byte."""
    class _FakeManager:
        async def search(self, query: object) -> _FakeSearchResponse:  # noqa: ANN001
            return _FakeSearchResponse([_FakeSearchResult(FIXTURE_URL)])

    _patch_manager(monkeypatch, _FakeManager())
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    source = PlanHolderSource(yield_store=store)
    seg = segment_key("roofing", "dallas tx")
    source._default_search_pdfs([("t1", "dork")], segment=seg)  # noqa: SLF001
    assert store.get("t1") is None              # no global row
    assert store.get("t1", seg) == {"trials": 1, "working": 0}


# ---------------------------------------------------------------------------
# Location Fix (Phase D): out-of-region gate before fetch/parse
# ---------------------------------------------------------------------------


class _SnippetResult(_FakeResult):
    """A search result that also carries a snippet (drives the location gate).

    ``url`` only names the PDF; ``snippet`` is the search engine's text the
    gate reasons about. Defaults keep it a drop-in for URL-only callers.
    """

    def __init__(self, url: str, snippet: str = "") -> None:
        super().__init__(url)
        self.title = ""
        self.snippet = snippet


def test_location_gate_drops_different_state_and_orders_target_first() -> None:
    """Location Fix Phase D: an out-of-region PDF (snippet names a DIFFERENT
    state) is dropped for a Houston run; on-target PDFs dispatch before neutral
    ones. A snippet naming BOTH the target and another state stays (the target's
    bare code is detected, so the doc is NOT over-dropped)."""
    text = {
        "nyc.pdf": "New York City DOT plan holder list",
        "hou.pdf": "Houston TX project bidders",
        "nolang.pdf": "Bidder list publication",
        "hx_and_sc.pdf": "Harris County TX and South Carolina outreach",
    }
    urls = ["nyc.pdf", "hou.pdf", "nolang.pdf", "hx_and_sc.pdf"]
    kept, dropped = _order_by_location(urls, text, "TX")
    assert dropped == 1
    assert "nyc.pdf" not in kept
    # on-target (rank 2) first, neutral (rank 1) last; the TX+SC doc stays.
    assert kept == ["hou.pdf", "hx_and_sc.pdf", "nolang.pdf"]


def test_location_gate_keeps_all_when_target_state_unknown() -> None:
    """A bare-city / unparseable query has no target state -> nothing rejected
    (conservative): never lose leads on an ambiguous location."""
    text = {"nyc.pdf": "New York City DOT plan holders", "a.pdf": "x"}
    kept, dropped = _order_by_location(list(text), text, None)
    assert dropped == 0
    assert kept == list(text)


def test_location_gate_keeps_all_when_no_snippet_signal() -> None:
    """URL-only results (no title/snippet) carry no state signal -> all kept, so
    no-signal runs and URL-only fakes are never gated (rank 1 = keep)."""
    urls = ["a.pdf", "b.pdf"]
    kept, dropped = _order_by_location(urls, {}, "TX")
    assert dropped == 0
    assert kept == urls


def test_gate_all_dropped_reports_empty_out_of_region() -> None:
    """When EVERY surfaced PDF is out-of-region, discover() reports an honest
    EMPTY (all_pdfs_out_of_region) with the drop count — never a fetch-failure
    UNAVAILABLE and never a silent success (§6)."""
    source = _make_source(
        search=lambda dorks: [
            _SnippetResult("https://dot.ny.gov/plans.pdf", "New York State DOT bidder list"),
            _SnippetResult("https://az.gov/plans.pdf", "Arizona DOT prospective bidders"),
        ],
    )
    status, records, meta = source.discover(
        industry="Roofing", location="Houston TX", limit=50
    )
    assert status == SourceStatus.EMPTY
    assert meta["reason"] == "all_pdfs_out_of_region"
    assert meta["pdfs_found"] == 2
    assert meta["pdfs_location_dropped"] == 2
    assert records == []


@pytestmark_fixture
def test_content_gate_drops_parsed_doc_that_snippet_missed() -> None:
    """Content gate (Phase D, content pass): a snippet that names NO state slips
    past the snippet gate, but the PARSED document's own states expose it as
    out-of-region. hrgreen is an IOWA list; a Houston TX query must not emit its
    rows. ``pdfs_content_dropped`` surfaces the evidence, never silent (§6).

    This replaces the old success-metadata test whose premise was wrong after
    the content gate: an Iowa list was being treated as an in-region TX lead —
    exactly the dishonesty this phase fixes."""
    source = _make_source(
        search=lambda dorks: [_SnippetResult(FIXTURE_URL, "hrgreen plan holder list")],
    )
    status, records, meta = source.discover(
        industry="Roofing", location="Houston TX", limit=50
    )
    assert status == SourceStatus.EMPTY
    assert meta["reason"] == "no_rows_in_pdfs"
    # snippet-neutral (no state in the snippet) -> the snippet gate kept it.
    assert meta["pdfs_location_dropped"] == 0
    # …but the parsed text says IA, not TX -> the content gate dropped its rows.
    assert meta["pdfs_content_dropped"] == 1
    assert meta["target_state"] == "TX"
    assert records == []


@pytestmark_fixture
def test_content_gate_keeps_in_region_doc() -> None:
    """A document whose parsed text names the TARGET state survives the content
    gate: hrgreen is an Iowa list, so a Cedar Rapids IA query still emits all 28
    records with zero content-drops (no over-gating on the correct region)."""
    source = _make_source(
        search=lambda dorks: [_SnippetResult(FIXTURE_URL, "hrgreen plan holder list")],
    )
    status, records, meta = source.discover(
        industry="Roofing", location="Cedar Rapids IA", limit=50
    )
    assert status == SourceStatus.SUCCESS
    assert meta["pdfs_content_dropped"] == 0
    assert len(records) == 28


def test_content_out_of_region_rules() -> None:
    """Pure rule check for the parsed-content locality gate: drop ONLY when the
    document names at least one state and none of them is the target. A silent
    document, a document naming the target, or an unknown target all keep."""
    assert _content_out_of_region(["WA"], "TX") is True  # names a non-target state
    assert _content_out_of_region(["TX"], "TX") is False  # names the target
    assert _content_out_of_region(["TX", "WA"], "TX") is False  # target among others
    assert _content_out_of_region(["WA"], None) is False  # no target state
    assert _content_out_of_region([], "TX") is False  # location-silent doc -> keep


@pytestmark_fixture
def test_parser_report_exposes_document_state_codes() -> None:
    """The parser's additive report surface (the contract the content gate reads):
    parsing the committed Iowa fixture yields exactly ['IA'] in ``state_codes``."""
    from app.discovery.pdf_plan_holder_parser import PdfPlanHolderParser

    result = PdfPlanHolderParser().parse(_fixture_bytes(), source_url="x")
    assert result.report.state_codes == ["IA"]

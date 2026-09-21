"""Phase 1 intake: the plugins' evidence reaches the canonical store.

These tests are the proof that the wiring works end to end without a single
network call. Every plugin is a stub, so what is actually being verified is
the part that is new: identity resolution, adaptation, storage, dedup across
runs, and — the part that matters most — the honest run state.

The state assertions are the point of the module. ``NOT_FOUND`` ("every
provider answered, none had anything") and ``NOT_ACCESSIBLE`` ("we could not
look") are different facts about a company; a test that let them collapse
would let a coverage hole be reported as a confident negative, which is the
defect the four-state rule exists to prevent.
"""

from __future__ import annotations

import pytest

from app.discovery.sources.status import SourceFailureReason, SourceStatus
from app.engines.lead.lead_models import IntentEvidence, IntentEvidenceType
from app.research.intake import collect_company_evidence
from app.research.store import ResearchEvidenceStore
from app.research.taxonomy import EventType, ResearchState


# --- stand-in plugins -----------------------------------------------------
# Deliberately NOT the real plugins: they make network calls, and the intake's
# contract is with the SHAPE they return, not with their internals.


class _Plugin:
    """A stub intent plugin returning a fixed result."""

    def __init__(self, name, status=SourceStatus.SUCCESS, items=(), error=None,
                 reason=None):
        self.name = name
        self._status = status
        self._items = list(items)
        self._error = error
        self._reason = reason
        self.calls: list[dict[str, str]] = []

    def collect_evidence(self, *, company_name, website="", location=""):
        self.calls.append({
            "company_name": company_name, "website": website, "location": location,
        })
        meta = {"source": self.name}
        if self._error:
            meta["error"] = self._error
        if self._reason:
            meta["reason"] = self._reason
        return self._status, list(self._items), meta


class _Exploding:
    """A plugin that raises — a third-party endpoint having a bad day."""

    name = "exploding"

    def collect_evidence(self, *, company_name, website="", location=""):
        raise RuntimeError("endpoint refused the connection")


def _evidence(url="https://example.gov/award/1", kind=IntentEvidenceType.project):
    return IntentEvidence(
        type=kind,
        source_url=url,
        snippet="Acme broke ground on the Riverside project.",
        date="2026-09-11",
        source="usaspending",
    )


@pytest.fixture()
def store(tmp_path):
    return ResearchEvidenceStore(str(tmp_path / "research_evidence.db"))


def _run(store, plugins, *, domain="acme.com", name="Acme Construction"):
    return collect_company_evidence(
        company_name=name, domain=domain, store=store, plugins=plugins
    )


# --- the happy path -------------------------------------------------------


def test_evidence_reaches_the_store_against_a_stable_company_id(store):
    result = _run(store, [_Plugin("usaspending", items=[_evidence()])])

    assert result.state == ResearchState.VERIFIED.value
    assert result.evidence_collected == 1
    assert result.evidence_stored == 1
    assert result.company_id
    assert store.count_evidence(result.company_id) == 1


def test_the_company_is_resolved_by_domain_and_reused(store):
    first = _run(store, [_Plugin("usaspending", items=[_evidence()])])
    second = _run(store, [_Plugin("google_news", items=[_evidence()])])

    assert first.company_id == second.company_id
    assert second.evidence_stored == 0, "the same observation is one row"
    assert second.evidence_known == 1
    assert second.state == ResearchState.VERIFIED.value, (
        "already-known evidence is still evidence — not an empty result"
    )


def test_two_companies_keep_their_evidence_apart(store):
    a = _run(store, [_Plugin("usaspending", items=[_evidence()])], domain="acme.com")
    b = _run(store, [_Plugin("usaspending", items=[_evidence()])], domain="bravo.com")

    assert a.company_id != b.company_id
    assert store.count_evidence(a.company_id) == 1
    assert store.count_evidence(b.company_id) == 1


def test_bid_award_evidence_is_stored_as_unresolved(store):
    """The corrected mapping survives all the way into the database.

    This is the Phase 0 correction meeting Phase 1: a legacy ``bid_award``
    row must land as ``needs_resolution`` — never as a procurement stage.
    """
    row = _evidence(kind=IntentEvidenceType.bid_award)
    result = _run(store, [_Plugin("usaspending", items=[row])])

    stored = store.evidence_for_company(result.company_id)
    assert len(stored) == 1
    assert stored[0].legacy_type == "bid_award"
    assert stored[0].event_candidate == EventType.NEEDS_RESOLUTION
    assert stored[0].event_candidate is not EventType.BID_SUBMITTED


def test_the_evidence_keeps_its_traceable_url(store):
    """A signal with no URL is not evidence (hard rule #5)."""
    result = _run(store, [_Plugin("usaspending", items=[_evidence()])])
    stored = store.evidence_for_company(result.company_id)

    assert stored[0].source_url == "https://example.gov/award/1"
    assert stored[0].excerpt


# --- the honest states never collapse -------------------------------------


def test_all_providers_answered_and_found_nothing_is_not_found(store):
    """Every provider ran and had nothing — a supported negative."""
    result = _run(store, [
        _Plugin("usaspending", status=SourceStatus.EMPTY),
        _Plugin("google_news", status=SourceStatus.EMPTY),
    ])

    assert result.state == ResearchState.NOT_FOUND.value
    assert "no evidence" in result.honest_reason


def test_no_provider_answering_is_not_accessible_not_not_found(store):
    """'We could not look' must never be filed as 'there is nothing'."""
    result = _run(store, [_Exploding()])

    assert result.state == ResearchState.NOT_ACCESSIBLE.value
    assert result.state != ResearchState.NOT_FOUND.value
    assert "exploding" in result.honest_reason
    assert result.honest_reason, "a negative with no reason is not diagnostic"


def test_a_partial_look_is_not_reported_as_not_found(store):
    """One answered with nothing, one was unreachable — the negative is unsupported.

    The strict reading: 'not found' claims the search covered the ground it
    was meant to cover. With a provider missing, it did not, so the honest
    state is the one that says so.
    """
    result = _run(store, [
        _Plugin("usaspending", status=SourceStatus.EMPTY),
        _Plugin("google_news", status=SourceStatus.UNAVAILABLE),
    ])

    assert result.state == ResearchState.NOT_ACCESSIBLE.value
    assert "partial" in result.honest_reason
    assert "google_news" in result.honest_reason


def test_a_verified_run_that_was_only_partial_says_so(store):
    """The other half of the collapse rule, one level up.

    Evidence arrived, so the state is VERIFIED — but one provider was dead,
    and its silence might have covered something. Naming only the answering
    providers would let a reader take the run as full coverage. Verified is
    a statement about what WAS found, never a claim about what was ruled out.
    """
    result = _run(store, [
        _Plugin("company_site", items=[_evidence()]),
        _Plugin("usaspending", status=SourceStatus.UNAVAILABLE),
    ])

    assert result.state == ResearchState.VERIFIED.value
    assert "company_site" in result.honest_reason
    assert "usaspending" in result.honest_reason, (
        "a provider that did not answer must be named even when the run "
        "succeeded overall"
    )
    assert "partial" in result.honest_reason


def test_a_complete_verified_run_does_not_claim_to_be_partial(store):
    """The counter-case: an optional note that always fires is noise."""
    result = _run(store, [_Plugin("company_site", items=[_evidence()])])

    assert result.state == ResearchState.VERIFIED.value
    assert "partial" not in result.honest_reason


def test_a_rejected_request_is_never_described_as_unreachable(store):
    """The D2 collapse, one layer up — fixed live 2026-09-18.

    A provider returning ERROR/request_error reached the source and was
    refused. "Could not be reached" is a claim about the network, and for a
    4xx it is simply false. That wording is what let a permanently
    malformed USAspending request read as a flaky endpoint for its entire
    life, so the summary must not default to it.
    """
    result = _run(store, [
        _Plugin(
            "usaspending",
            status=SourceStatus.ERROR,
            reason=SourceFailureReason.REQUEST_ERROR.value,
        ),
    ])

    assert result.state == ResearchState.NOT_ACCESSIBLE.value
    assert "rejected our request" in result.honest_reason
    assert "could not be reached" not in result.honest_reason


def test_a_genuine_access_failure_is_still_described_as_unreachable(store):
    """The counter-case: the honest phrase must not be abolished."""
    result = _run(store, [
        _Plugin(
            "usaspending",
            status=SourceStatus.UNAVAILABLE,
            reason=SourceFailureReason.ACCESS_ERROR.value,
        ),
    ])

    assert "could not be reached" in result.honest_reason


def test_a_provider_that_gives_no_reason_is_still_named(store):
    """Never a bare "something failed" — the name survives the wording."""
    result = _run(store, [_Plugin("google_news", status=SourceStatus.ERROR)])

    assert "google_news" in result.honest_reason
    assert "did not answer" in result.honest_reason


def test_unreachable_providers_are_named_not_counted_as_found_nothing(store):
    result = _run(store, [_Plugin("usaspending", status=SourceStatus.ERROR)])

    assert result.state == ResearchState.NOT_ACCESSIBLE.value
    entry = result.providers[0]
    assert entry["provider"] == "usaspending"
    assert entry["status"] == SourceStatus.ERROR.value


def test_a_negative_run_is_still_closed_in_the_ledger_with_its_reason(store):
    """§6: the run ledger must explain itself, so a thin result is diagnosable."""
    result = _run(store, [_Plugin("usaspending", status=SourceStatus.EMPTY)])
    run = store.get_run(result.run_id)

    assert run is not None
    assert run["state"] == ResearchState.NOT_FOUND.value
    assert run["honest_reason"]


# --- failure isolation ----------------------------------------------------


def test_one_broken_plugin_does_not_stop_the_others(store):
    result = _run(store, [
        _Exploding(),
        _Plugin("usaspending", items=[_evidence()]),
    ])

    assert result.state == ResearchState.VERIFIED.value
    assert result.evidence_stored == 1
    by_name = {p["provider"]: p for p in result.providers}
    assert by_name["exploding"]["status"] == SourceStatus.ERROR.value
    assert "RuntimeError" in by_name["exploding"]["error"]


def test_evidence_with_no_url_is_rejected_not_stored(store):
    """An untraceable signal is dropped, and the drop is counted."""
    result = _run(store, [_Plugin("usaspending", items=[_evidence(url="  ")])])

    assert result.evidence_collected == 0
    assert result.providers[0]["blank_url"] == 1
    assert result.state == ResearchState.NOT_FOUND.value


def test_a_duplicate_within_one_run_is_stored_once(store):
    """Two providers reporting the same page is one observation."""
    same = _evidence()
    result = _run(store, [
        _Plugin("usaspending", items=[same]),
        _Plugin("google_news", items=[_evidence()]),
    ])

    assert result.evidence_collected == 1
    assert result.evidence_stored == 1


def test_a_domainless_company_is_refused_rather_than_guessed(store):
    """No domain -> no identity -> refuse. Evidence must have an owner."""
    result = collect_company_evidence(
        company_name="Acme", domain="", store=store, plugins=[_Plugin("x")]
    )

    assert result.state == ResearchState.NOT_ACCESSIBLE.value
    assert result.company_id == ""
    assert "no company domain" in result.honest_reason


# --- what the plugins were actually asked ---------------------------------


def test_the_plugin_receives_the_company_context(store):
    """The research path's own knowledge is what the plugins search on."""
    plugin = _Plugin("usaspending", items=[_evidence()])
    collect_company_evidence(
        company_name="Acme Construction",
        domain="acme.com",
        website="https://acme.com",
        location="Austin, TX",
        store=store,
        plugins=[plugin],
    )

    assert plugin.calls == [{
        "company_name": "Acme Construction",
        "website": "https://acme.com",
        "location": "Austin, TX",
    }]


def test_a_missing_website_falls_back_to_the_companys_own_domain(store):
    """``https://<domain>`` — the company's own domain, never an invented one."""
    plugin = _Plugin("company_site", items=[_evidence()])
    collect_company_evidence(
        company_name="Acme", domain="acme.com", website="", store=store,
        plugins=[plugin],
    )

    assert plugin.calls[0]["website"] == "https://acme.com"


def test_the_result_names_where_the_plugins_came_from(store):
    """A fallback must be visible, never silent (CLAUDE.md §6)."""
    result = _run(store, [_Plugin("usaspending", items=[_evidence()])])

    assert result.plugins_available == ["usaspending"]
    assert [p["provider"] for p in result.providers] == ["usaspending"]

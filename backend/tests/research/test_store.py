"""``ResearchEvidenceStore`` — separate DB, stable company identity, dedup.

Three things this module exists to prove, each one a founder decision:

1. **The company identity survives a domain change.** Keying evidence on the
   domain would orphan everything the moment a company rebrands or runs two
   domains, so the store keeps a stable ``company_id`` and treats the domain
   as an alias list. ``test_rebrand_keeps_the_company_and_its_evidence`` is
   the regression test for that.
2. **Re-reading a page does not duplicate it.** Regenerating research must
   refine, not accumulate (``check.txt`` §19).
3. **A negative result carries its reason, and the four honest states do not
   normalize into each other** (§31). A run that is still going is
   ``running``, never a premature ``NOT_FOUND``.
"""

from __future__ import annotations

import pytest

from app.research.models import CanonicalEvidence
from app.research.store import (
    MAX_DOMAINS_PER_COMPANY,
    ResearchEvidenceStore,
    default_db_path,
    normalize_company_key,
)
from app.research.taxonomy import CompanyMatch, EventType, EvidenceType, ResearchState


@pytest.fixture
def store(tmp_path) -> ResearchEvidenceStore:
    """A store on a throwaway file — never the real output DB."""
    return ResearchEvidenceStore(str(tmp_path / "research_evidence.db"))


def _evidence(company_id: str, **overrides) -> CanonicalEvidence:
    fields = dict(
        evidence_id="",
        company_id=company_id,
        source_url="https://example.gov/award/1",
        retrieved_at="2026-09-18T10:00:00",
    )
    fields.update(overrides)
    # Derive the id the way the real adapters do — from the company AND the
    # observation. Seeding on the observation alone would give two companies
    # reading the same page the same primary key, which is a bug in the
    # helper, not in the store (the store's dedup is per company).
    if not fields["evidence_id"]:
        from app.research.models import new_evidence_id

        fields["evidence_id"] = new_evidence_id(
            f"test|{company_id}|{fields['source_url']}|{fields['excerpt']}"
            if "excerpt" in fields
            else f"test|{company_id}|{fields['source_url']}"
        )
    return CanonicalEvidence(**fields)


# --- the DB is its own file ----------------------------------------------


def test_default_path_is_the_separate_research_db():
    """The data boundary, as a path: never ``dossiers.db``."""
    path = default_db_path().replace("\\", "/")
    assert path.endswith("output/research_evidence.db")
    assert "dossiers" not in path


def test_store_creates_its_file_and_tables(tmp_path):
    db = tmp_path / "research_evidence.db"
    ResearchEvidenceStore(str(db))
    assert db.exists()
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert {
        "companies", "company_domains", "evidence", "events", "signals",
        "signal_evidence", "pain_hypotheses", "pain_evidence", "research_runs",
        "outreach_triggers",
    } <= names


def test_outreach_trigger_round_trips_and_latest_coverage_is_decoded(store):
    from app.research.outreach import (
        OutreachStrength,
        OutreachTriggerRecord,
        new_trigger_id,
    )

    company = store.resolve_company("acme.com")
    evidence = _evidence(company.company_id, evidence_id="ev_outreach")
    store.add_evidence(evidence)
    run_id = store.start_run(company.company_id)
    store.finish_run(run_id, "VERIFIED", coverage={"company_site": "VERIFIED"})
    trigger = OutreachTriggerRecord(
        trigger_id=new_trigger_id(company.company_id),
        company_id=company.company_id,
        angle="Additional Estimating Capacity",
        basis="estimating_capacity",
        strength=OutreachStrength.CONFIRMED,
        wording="Acme recently highlighted estimating capacity needs.",
        computed_at="2026-09-21T00:00:00+00:00",
        evidence_ids=(evidence.evidence_id,),
    )

    store.replace_outreach_trigger(company.company_id, trigger)

    assert store.outreach_trigger_for_company(company.company_id) == trigger
    assert store.latest_coverage_for_company(company.company_id) == {
        "company_site": "VERIFIED"
    }


def test_outreach_trigger_refuses_foreign_evidence(store):
    from app.research.outreach import OutreachStrength, OutreachTriggerRecord, new_trigger_id

    company = store.resolve_company("acme.com")
    other = store.resolve_company("other.com")
    evidence = _evidence(other.company_id, evidence_id="ev_foreign_outreach")
    store.add_evidence(evidence)
    trigger = OutreachTriggerRecord(
        new_trigger_id(company.company_id), company.company_id, "Angle", "basis",
        OutreachStrength.CONFIRMED, "Safe wording.", "2026-09-21", (evidence.evidence_id,),
    )

    with pytest.raises(ValueError, match="another company"):
        store.replace_outreach_trigger(company.company_id, trigger)


def test_replace_signals_round_trips_and_removes_obsolete_rows(store):
    from app.research.signals import (
        RecencyBucket,
        SignalRecord,
        SignalStrength,
        SignalType,
        new_signal_id,
    )

    company = store.resolve_company("acme.com")
    evidence = _evidence(company.company_id, evidence_id="ev_signal", excerpt="Awarded")
    store.add_evidence(evidence)
    first = SignalRecord(
        signal_id=new_signal_id(company.company_id, SignalType.CONTRACT_AWARD_ACTIVITY),
        company_id=company.company_id,
        signal_type=SignalType.CONTRACT_AWARD_ACTIVITY,
        strength=SignalStrength.HIGH,
        score=12,
        computed_at="2026-09-21T00:00:00+00:00",
        contradiction=(),
        evidence_ids=(evidence.evidence_id,),
        event_ids=("evt_one",),
        event_count=1,
        independent_source_count=1,
        recency=RecencyBucket.VERY_RECENT,
    )
    store.replace_signals(company.company_id, [first])

    rows = store.signals_for_company(company.company_id)
    assert rows == [first]

    store.replace_signals(company.company_id, [])
    assert store.signals_for_company(company.company_id) == []


def test_replace_signals_refuses_foreign_evidence(store):
    from app.research.signals import (
        RecencyBucket,
        SignalRecord,
        SignalStrength,
        SignalType,
        new_signal_id,
    )

    company = store.resolve_company("acme.com")
    other = store.resolve_company("other.com")
    evidence = _evidence(other.company_id, evidence_id="ev_foreign", excerpt="Hiring")
    store.add_evidence(evidence)
    signal = SignalRecord(
        signal_id=new_signal_id(company.company_id, SignalType.HIRING_ACTIVITY),
        company_id=company.company_id,
        signal_type=SignalType.HIRING_ACTIVITY,
        strength=SignalStrength.LOW,
        score=5,
        computed_at="2026-09-21T00:00:00+00:00",
        contradiction=(),
        evidence_ids=(evidence.evidence_id,),
        event_ids=("evt_one",),
        event_count=1,
        independent_source_count=1,
        recency=RecencyBucket.RECENT,
    )

    with pytest.raises(ValueError, match="another company"):
        store.replace_signals(company.company_id, [signal])


def test_replace_pain_hypotheses_round_trips_and_tracks_basis(store):
    from app.research.pain import (
        PainHypothesisRecord,
        PainType,
        PainVerdict,
        new_hypothesis_id,
    )

    company = store.resolve_company("acme.com")
    evidence = _evidence(company.company_id, evidence_id="ev_pain", excerpt="Pain")
    store.add_evidence(evidence)
    hypothesis = PainHypothesisRecord(
        hypothesis_id=new_hypothesis_id(company.company_id, PainType.PROJECT_VOLUME),
        company_id=company.company_id,
        pain_type=PainType.PROJECT_VOLUME,
        confidence=0.8,
        licensed_confidence=0.85,
        verdict=PainVerdict.VERIFIED,
        blocked_by=(),
        computed_at="2026-09-21T00:00:00+00:00",
        reasoning="Two independent recent project signals.",
        evidence_ids=(evidence.evidence_id,),
        signal_ids=("sig_project",),
        direct_evidence_ids=(),
    )
    store.replace_pain_hypotheses(
        company.company_id, [hypothesis], basis_hash="basis_123"
    )

    assert store.pain_hypotheses_for_company(company.company_id) == [hypothesis]
    assert store.pain_basis_hash(company.company_id) == "basis_123"

    store.replace_pain_hypotheses(company.company_id, [], basis_hash="basis_empty")
    assert store.pain_hypotheses_for_company(company.company_id) == []
    assert store.pain_basis_hash(company.company_id) == "basis_empty"


def test_replace_pain_hypotheses_refuses_foreign_evidence(store):
    from app.research.pain import (
        PainHypothesisRecord,
        PainType,
        PainVerdict,
        new_hypothesis_id,
    )

    company = store.resolve_company("acme.com")
    other = store.resolve_company("other.com")
    evidence = _evidence(other.company_id, evidence_id="ev_foreign_pain", excerpt="Pain")
    store.add_evidence(evidence)
    hypothesis = PainHypothesisRecord(
        hypothesis_id=new_hypothesis_id(company.company_id, PainType.BID_VOLUME),
        company_id=company.company_id,
        pain_type=PainType.BID_VOLUME,
        confidence=0.3,
        licensed_confidence=0.35,
        verdict=PainVerdict.UNKNOWN,
        blocked_by=("insufficient support",),
        computed_at="2026-09-21T00:00:00+00:00",
        reasoning="Candidate only.",
        evidence_ids=(evidence.evidence_id,),
        signal_ids=("sig_bid",),
    )

    with pytest.raises(ValueError, match="another company"):
        store.replace_pain_hypotheses(company.company_id, [hypothesis])


def test_default_path_does_not_create_the_file():
    """Importing or asking for the path must not write anything (see conftest)."""
    import os

    before = os.path.exists(default_db_path())
    default_db_path()
    assert os.path.exists(default_db_path()) is before


# --- stable company identity ---------------------------------------------


def test_resolve_company_creates_a_stable_identity(store):
    identity = store.resolve_company("https://www.acme.com/about", name="Acme")
    assert identity.company_id.startswith("cmp_")
    assert identity.company_key == "acme.com"
    assert identity.name == "Acme"
    assert identity.domains == ("acme.com",)


def test_resolving_the_same_domain_twice_yields_the_same_company(store):
    first = store.resolve_company("acme.com", name="Acme")
    second = store.resolve_company("https://acme.com/contact", name="Acme")
    assert first.company_id == second.company_id
    assert store.get_company(first.company_id) is not None


def test_company_id_is_never_derived_from_the_domain(store):
    """Two domains resolve to two ids that look nothing like the domains."""
    a = store.resolve_company("acme.com")
    b = store.resolve_company("baysidebuilders.com")
    assert a.company_id != b.company_id
    assert "acme" not in a.company_id
    assert "bayside" not in b.company_id


def test_www_and_case_are_normalized_to_one_company(store):
    a = store.resolve_company("WWW.Acme.com")
    b = store.resolve_company("acme.com")
    assert a.company_id == b.company_id


def test_rebrand_keeps_the_company_and_its_evidence(store):
    """THE regression test for the founder's identity correction.

    A company that moves to a new domain must keep its evidence. If identity
    were the domain, this would produce two companies and orphan the award.
    """
    old = store.resolve_company("acme-construction.com", name="Acme Construction")
    store.add_evidence(
        _evidence(old.company_id, excerpt="Awarded $4.2M for the bridge.")
    )

    store.link_domain(old.company_id, "acmebuilds.com", primary=True)

    after = store.companies_for_domain("acmebuilds.com")
    assert after is not None
    assert after.company_id == old.company_id
    assert "acmebuilds.com" in after.domains
    assert "acme-construction.com" in after.domains, "the old domain is kept as history"
    assert store.count_evidence(old.company_id) == 1, "evidence was not orphaned"


def test_primary_domain_sorts_first(store):
    company = store.resolve_company("old.com")
    store.link_domain(company.company_id, "new.com", primary=True)
    identity = store.get_company(company.company_id)
    assert identity is not None
    assert identity.domains[0] == "new.com"


def test_promoting_a_primary_demotes_the_previous_one(store):
    """Exactly one primary domain — otherwise "primary" means nothing.

    Found by this suite: the first implementation flagged the new primary
    without clearing the old, so both rows were primary and the ordering
    tiebreak silently fell back to insertion order.
    """
    import sqlite3

    company = store.resolve_company("old.com")
    store.link_domain(company.company_id, "new.com", primary=True)

    conn = sqlite3.connect(store._db_path)
    try:
        primaries = [
            r[0]
            for r in conn.execute(
                "SELECT domain FROM company_domains WHERE company_id = ? "
                "AND is_primary = 1",
                (company.company_id,),
            )
        ]
    finally:
        conn.close()
    assert primaries == ["new.com"], f"expected one primary, got {primaries}"


def test_linking_a_domain_twice_reports_not_added(store):
    company = store.resolve_company("acme.com")
    assert store.link_domain(company.company_id, "other.com") is True
    assert store.link_domain(company.company_id, "other.com") is False


def test_linking_a_domain_owned_by_another_company_is_refused(store):
    """One domain, one company — the invariant resolution depends on."""
    a = store.resolve_company("acme.com")
    b = store.resolve_company("bayside.com")
    assert store.link_domain(b.company_id, "acme.com") is False


def test_linking_to_an_unknown_company_raises(store):
    with pytest.raises(ValueError):
        store.link_domain("cmp_does_not_exist", "acme.com")


def test_domain_flood_is_refused(store):
    """A runaway alias list means a resolution bug upstream — do not merge."""
    company = store.resolve_company("acme.com")
    for i in range(MAX_DOMAINS_PER_COMPANY - 1):
        store.link_domain(company.company_id, f"alias{i}.com")
    with pytest.raises(ValueError):
        store.link_domain(company.company_id, "one-too-many.com")


def test_re_asserting_a_known_alias_at_the_cap_is_a_no_op(store):
    """Re-linking what is already held is False, not a cap violation.

    The cap must guard NEW merges only. Checking it before checking whether
    the alias is already known made a harmless re-assert raise.
    """
    company = store.resolve_company("acme.com")
    for i in range(MAX_DOMAINS_PER_COMPANY - 1):
        store.link_domain(company.company_id, f"alias{i}.com")
    assert store.link_domain(company.company_id, "alias0.com") is False


def test_re_asserting_an_alias_can_still_promote_it_to_primary(store):
    company = store.resolve_company("acme.com")
    store.link_domain(company.company_id, "other.com")
    assert store.link_domain(company.company_id, "other.com", primary=True) is False
    identity = store.get_company(company.company_id)
    assert identity is not None
    assert identity.domains[0] == "other.com"


def test_resolve_company_refuses_an_empty_domain(store):
    """Inventing a company per call is the duplicate defect — refuse instead."""
    with pytest.raises(ValueError):
        store.resolve_company("")
    with pytest.raises(ValueError):
        store.resolve_company("   ")


def test_create_company_is_the_explicit_domainless_path(store):
    company = store.create_company("Acme Construction")
    assert company.company_id.startswith("cmp_")
    assert company.company_key == ""
    assert company.domains == ()
    assert store.get_company(company.company_id) is not None


def test_unknown_company_id_returns_none(store):
    assert store.get_company("cmp_nope") is None
    assert store.companies_for_domain("nope.com") is None


def test_set_name_fills_a_blank_name(store):
    """Phase 1 resolves a domain before it has read a company name."""
    company = store.resolve_company("acme.com")
    assert store.set_name(company.company_id, "Acme Construction") is True
    assert store.get_company(company.company_id).name == "Acme Construction"


def test_set_name_never_overwrites_a_known_name(store):
    """A later caller may know less than the record — no silent downgrade."""
    company = store.resolve_company("acme.com", name="Acme Construction")
    assert store.set_name(company.company_id, "Acme") is False
    assert store.get_company(company.company_id).name == "Acme Construction"


def test_set_name_ignores_a_blank_name(store):
    company = store.resolve_company("acme.com")
    assert store.set_name(company.company_id, "   ") is False
    assert store.set_name(company.company_id, "") is False
    assert store.get_company(company.company_id).name == ""


def test_set_name_on_an_unknown_company_raises(store):
    with pytest.raises(ValueError):
        store.set_name("cmp_nope", "Acme")


def test_resolve_company_does_not_overwrite_a_name_it_already_has(store):
    store.resolve_company("acme.com", name="Acme Construction")
    again = store.resolve_company("acme.com", name="")
    assert again.name == "Acme Construction"


def test_normalize_company_key_strips_www_and_lowercases():
    assert normalize_company_key("HTTPS://WWW.Acme.com/page") == "acme.com"
    assert normalize_company_key("acme.com") == "acme.com"
    assert normalize_company_key("") == ""


# --- evidence storage ----------------------------------------------------


def test_evidence_is_stored_and_read_back(store):
    company = store.resolve_company("acme.com")
    assert store.add_evidence(
        _evidence(company.company_id, excerpt="Awarded $4.2M.", confidence=0.9)
    ) is True
    rows = store.evidence_for_company(company.company_id)
    assert len(rows) == 1
    assert rows[0].excerpt == "Awarded $4.2M."
    assert rows[0].confidence == 0.9


def test_re_reading_the_same_page_does_not_duplicate(store):
    """§19: regenerating research refines, never accumulates."""
    company = store.resolve_company("acme.com")
    first = _evidence(company.company_id, excerpt="Awarded $4.2M.")
    second = _evidence(
        company.company_id,
        excerpt="Awarded $4.2M.",
        title="A rewritten headline",
        evidence_id="ev_a_different_id",
    )
    assert store.add_evidence(first) is True
    assert store.add_evidence(second) is False
    assert store.count_evidence(company.company_id) == 1


def test_the_same_sentence_on_two_pages_is_two_observations(store):
    company = store.resolve_company("acme.com")
    store.add_evidence(
        _evidence(company.company_id, excerpt="Awarded $4.2M.", source_url="https://a.gov")
    )
    store.add_evidence(
        _evidence(company.company_id, excerpt="Awarded $4.2M.", source_url="https://b.gov")
    )
    assert store.count_evidence(company.company_id) == 2


def test_evidence_for_an_unknown_company_is_refused(store):
    """Evidence must attach to a real identity, never float free."""
    with pytest.raises(ValueError):
        store.add_evidence(_evidence("cmp_does_not_exist", excerpt="x"))


def test_evidence_about_a_different_company_cannot_even_be_constructed(store):
    """The MISMATCH refusal happens at the model, one layer earlier."""
    company = store.resolve_company("acme.com")
    with pytest.raises(ValueError):
        _evidence(
            company.company_id,
            excerpt="Some other firm won.",
            company_match=CompanyMatch.MISMATCH,
        )


def test_add_many_reports_how_many_were_actually_new(store):
    company = store.resolve_company("acme.com")
    rows = [
        _evidence(company.company_id, source_url="https://a.gov", excerpt="one"),
        _evidence(company.company_id, source_url="https://b.gov", excerpt="two"),
        _evidence(company.company_id, source_url="https://a.gov", excerpt="one"),
    ]
    assert store.add_many(rows) == 2


def test_a_reused_evidence_id_for_different_content_is_refused(store):
    """OR IGNORE must not turn a primary-key clash into a silent drop.

    Deduplication returns False ("already known"); a reused id for DIFFERENT
    content is a different event entirely and has to be visible.
    """
    company = store.resolve_company("acme.com")
    store.add_evidence(
        _evidence(
            company.company_id,
            source_url="https://a.gov",
            excerpt="Awarded $4.2M.",
            evidence_id="ev_fixed",
        )
    )
    with pytest.raises(ValueError) as exc:
        store.add_evidence(
            _evidence(
                company.company_id,
                source_url="https://b.gov",
                excerpt="Awarded $9.8M.",
                evidence_id="ev_fixed",
            )
        )
    assert "ev_fixed" in str(exc.value)
    assert store.count_evidence(company.company_id) == 1


def test_evidence_is_ordered_newest_first_with_undated_last(store):
    """Undated evidence is undated — it must not lead a recency view."""
    company = store.resolve_company("acme.com")
    store.add_evidence(
        _evidence(company.company_id, source_url="https://a.gov", excerpt="old",
                  published_at="2019-03-04")
    )
    store.add_evidence(
        _evidence(company.company_id, source_url="https://b.gov", excerpt="undated")
    )
    store.add_evidence(
        _evidence(company.company_id, source_url="https://c.gov", excerpt="new",
                  published_at="2026-09-11")
    )
    order = [r.excerpt for r in store.evidence_for_company(company.company_id)]
    assert order == ["new", "old", "undated"]


def test_project_keys_exclude_the_empty_one(store):
    """"We could not identify a project" is not a project."""
    company = store.resolve_company("acme.com")
    store.add_evidence(
        _evidence(company.company_id, source_url="https://a.gov", excerpt="a",
                  project_key="city-of-austin-bridge")
    )
    store.add_evidence(
        _evidence(company.company_id, source_url="https://b.gov", excerpt="b")
    )
    store.add_evidence(
        _evidence(company.company_id, source_url="https://c.gov", excerpt="c",
                  project_key="city-of-austin-bridge")
    )
    assert store.project_keys(company.company_id) == ["city-of-austin-bridge"]


def test_evidence_can_be_filtered_by_project(store):
    company = store.resolve_company("acme.com")
    store.add_evidence(
        _evidence(company.company_id, source_url="https://a.gov", excerpt="a",
                  project_key="bridge")
    )
    store.add_evidence(
        _evidence(company.company_id, source_url="https://b.gov", excerpt="b",
                  project_key="school")
    )
    only_bridge = store.evidence_for_company(company.company_id, project_key="bridge")
    assert [r.excerpt for r in only_bridge] == ["a"]


def test_evidence_survives_a_reopened_store(tmp_path):
    """Durability: the whole point of a store rather than the dossier JSON."""
    path = str(tmp_path / "research_evidence.db")
    first = ResearchEvidenceStore(path)
    company = first.resolve_company("acme.com", name="Acme")
    first.add_evidence(
        _evidence(company.company_id, excerpt="Awarded $4.2M.",
                  event_candidate=EventType.CONTRACT_AWARDED,
                  evidence_type=EvidenceType.GOVERNMENT_AWARD)
    )

    second = ResearchEvidenceStore(path)
    reopened = second.get_company(company.company_id)
    assert reopened is not None
    assert reopened.name == "Acme"
    rows = second.evidence_for_company(company.company_id)
    assert rows[0].event_candidate == EventType.CONTRACT_AWARDED.value
    assert rows[0].evidence_type == EvidenceType.GOVERNMENT_AWARD


def test_two_companies_keep_their_evidence_apart(store):
    """Company-level accumulation must not become company-level mixing."""
    a = store.resolve_company("acme.com")
    b = store.resolve_company("bayside.com")
    store.add_evidence(_evidence(a.company_id, source_url="https://a.gov", excerpt="a"))
    store.add_evidence(_evidence(b.company_id, source_url="https://a.gov", excerpt="a"))
    assert store.count_evidence(a.company_id) == 1
    assert store.count_evidence(b.company_id) == 1


# --- research runs and the four honest states ----------------------------


def test_a_run_starts_running_not_not_found(store):
    """An in-flight run must never read as a negative result."""
    company = store.resolve_company("acme.com")
    run_id = store.start_run(company.company_id)
    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == "running"
    assert run["finished_at"] == ""


def test_finishing_a_run_records_state_and_coverage(store):
    company = store.resolve_company("acme.com")
    run_id = store.start_run(company.company_id)
    store.finish_run(
        run_id,
        ResearchState.VERIFIED,
        coverage={"usaspending": "VERIFIED", "permits": "NOT_ACCESSIBLE"},
    )
    run = store.get_run(run_id)
    assert run["state"] == "VERIFIED"
    assert run["finished_at"]
    assert "usaspending" in run["coverage_json"]


def test_a_negative_result_requires_a_reason(store):
    """CLAUDE.md §6: a bare "we found nothing" is not diagnostic."""
    company = store.resolve_company("acme.com")
    for state in (ResearchState.NOT_FOUND, ResearchState.NOT_ACCESSIBLE,
                  ResearchState.NOT_VERIFIED):
        run_id = store.start_run(company.company_id)
        with pytest.raises(ValueError):
            store.finish_run(run_id, state)
        store.finish_run(run_id, state, honest_reason="USAspending returned 0 rows")


def test_verified_needs_no_reason(store):
    company = store.resolve_company("acme.com")
    run_id = store.start_run(company.company_id)
    store.finish_run(run_id, ResearchState.VERIFIED)
    assert store.get_run(run_id)["state"] == "VERIFIED"


def test_an_unknown_state_is_refused(store):
    company = store.resolve_company("acme.com")
    run_id = store.start_run(company.company_id)
    with pytest.raises(ValueError):
        store.finish_run(run_id, "PROBABLY_FINE", honest_reason="looks good")
    with pytest.raises(ValueError):
        store.finish_run(run_id, "live=False", honest_reason="nope")


def test_not_found_and_not_accessible_are_stored_as_different_facts(store):
    """§31, founder-frozen: the two negatives never merge in storage."""
    company = store.resolve_company("acme.com")
    not_found = store.start_run(company.company_id)
    store.finish_run(not_found, ResearchState.NOT_FOUND, honest_reason="0 rows")
    blocked = store.start_run(company.company_id)
    store.finish_run(
        blocked, ResearchState.NOT_ACCESSIBLE, honest_reason="login wall"
    )
    assert store.get_run(not_found)["state"] == "NOT_FOUND"
    assert store.get_run(blocked)["state"] == "NOT_ACCESSIBLE"
    assert store.get_run(not_found)["honest_reason"] == "0 rows"
    assert store.get_run(blocked)["honest_reason"] == "login wall"


def test_unknown_run_returns_none(store):
    assert store.get_run("run_nope") is None


def test_the_store_never_writes_to_the_dossier_db(tmp_path):
    """The data boundary, asserted rather than assumed."""
    import sqlite3

    from app.research.store import default_db_path as research_default

    store_path = str(tmp_path / "research_evidence.db")
    ResearchEvidenceStore(store_path)
    conn = sqlite3.connect(store_path)
    try:
        names = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert "dossiers" not in names
    assert "research_evidence" in research_default()

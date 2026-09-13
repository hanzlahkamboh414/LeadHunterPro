"""Phase E — identity-rejection learning.

Three contracts, all rooted in the 2026-09-08 purge (companies the user proved
are not clients must STAY purged instead of resurfacing to burn credit again):

1. **delete-feedback** — deleting a dossier with a rejection-class reason
   ("irrelevant" / "not our client") feeds its company+domain into fit-learning
   as a USER rejection on the SAME db file. Junk / manual deletes are hygiene,
   NOT verdicts — they must never feed. A nonexistent delete feeds nothing.

2. **serial intake gate** — ``discover_until_target`` drops a freshly-discovered
   lead whose company/domain is already user-rejected BEFORE it reaches research.

3. **streaming intake gate** — ``run_full``'s producer drops the same rejected
   lead from the buffer, so it is never researched nor shown.

Root-cause policy (§7): the pipeline re-researching a company the user already
deleted is the defect; these gates teach the identity-level skip so the re-run
behaves like the deletion never happened.
"""

from __future__ import annotations

from app.lead_research.fit_learning import KIND_COMPANY, KIND_DOMAIN, FitLearningStore
from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings
from app.lead_research.service import LeadResearchStore, _is_rejection_reason


def _make_dossier(email: str = "test@example.com") -> LeadDossier:
    """A dossier whose company name + domain are what _feed_user_rejection reads."""
    return LeadDossier(
        email=email,
        domain="example.com",
        company=CompanyProfile(name="Test Co", industry="construction", location="TX"),
        person=PersonFindings(name="Test", role="Owner", bound=True, role_relevance=True),
        recommendation="contact_now",
        potential_score=7.0,
    )


# ---------------------------------------------------------------------------
# _is_rejection_reason — which delete reasons are a VERDICT (vs hygiene)
# ---------------------------------------------------------------------------

def test_is_rejection_reason_matches_and_folds_separators():
    assert _is_rejection_reason("irrelevant") is True
    assert _is_rejection_reason("Not Our Client") is True
    assert _is_rejection_reason("non_client") is True
    assert _is_rejection_reason("non-client-2026-09-08") is True
    assert _is_rejection_reason("nonclient") is True


def test_is_rejection_reason_excludes_junk_and_manual():
    """The Junk sweep and a plain manual delete are hygiene — NOT a company
    verdict — so they must never feed identity-rejection learning."""
    assert _is_rejection_reason("junk") is False
    assert _is_rejection_reason("manual") is False
    assert _is_rejection_reason("") is False


# ---------------------------------------------------------------------------
# delete-feedback — rejection feeds company+domain on the SAME db
# ---------------------------------------------------------------------------

def test_rejection_delete_feeds_company_and_domain(tmp_path):
    """Deleting as 'irrelevant' teaches the learning table (same db file). With
    the 2026-09-12 gaming guard, a LONE uncorroborated delete is recorded for
    audit but is not yet decisive — the gate opens only on corroboration."""
    db = str(tmp_path / "lead_research.db")
    store = LeadResearchStore(db_path=db)
    store.save(_make_dossier(email="alice@acme.com"))

    assert store.delete("alice@acme.com", reason="irrelevant",
                        user_id="u1") is True

    learning = FitLearningStore(db)
    assert learning.get(KIND_COMPANY, "test co")["user_rejects"] == 1
    assert learning.get(KIND_DOMAIN, "example.com")["user_rejects"] == 1
    assert learning.should_skip_company("Test Co") is False  # guard: 1 lone user
    assert learning.should_skip_domain("example.com") is False


def test_rejection_delete_corroborated_by_research_is_decisive(tmp_path):
    """When the dossier's own research said 'not our client', the user's delete
    is corroborated — one verdict then purges company+domain decisively (the
    2026-09-08 purge contract, now evidence-gated)."""
    db = str(tmp_path / "lead_research.db")
    store = LeadResearchStore(db_path=db)
    dossier = _make_dossier(email="alice@acme.com")
    dossier.fit = "Not our client — self-estimates in-house"
    store.save(dossier)

    assert store.delete("alice@acme.com", reason="irrelevant",
                        user_id="u1") is True

    learning = FitLearningStore(db)
    assert learning.should_skip_company("Test Co") is True
    assert learning.should_skip_domain("example.com") is True


def test_second_distinct_user_delete_is_decisive(tmp_path):
    """Two INDEPENDENT users deleting the same identity is decisive even with
    no research agreement — the anti-gaming corroboration path."""
    db = str(tmp_path / "lead_research.db")
    store = LeadResearchStore(db_path=db)
    store.save(_make_dossier(email="alice@acme.com"))
    assert store.delete("alice@acme.com", reason="irrelevant",
                        user_id="u1") is True
    store.save(_make_dossier(email="bob@acme.com"))
    assert store.delete("bob@acme.com", reason="irrelevant",
                        user_id="u2") is True

    learning = FitLearningStore(db)
    assert learning.get(KIND_COMPANY, "test co") == {
        "trials": 0, "kept": 0, "user_rejects": 2,
        "rejector_ids": "u1,u2", "corroborated": 0,
    }
    assert learning.should_skip_company("Test Co") is True
    assert learning.should_skip_domain("example.com") is True


def test_non_rejection_delete_does_not_feed(tmp_path):
    """'junk' and 'manual' deletes delete the dossier but teach NOTHING — the
    company can legitimately resurface (default-keep, never over-prune)."""
    for reason in ("junk", "manual"):
        db = str(tmp_path / f"{reason}.db")
        store = LeadResearchStore(db_path=db)
        store.save(_make_dossier(email="bob@example.com"))
        assert store.delete("bob@example.com", reason=reason) is True

        learning = FitLearningStore(db)
        assert learning.should_skip_company("Test Co") is False
        assert learning.should_skip_domain("example.com") is False


def test_nonexistent_delete_feeds_nothing(tmp_path):
    db = str(tmp_path / "lead_research.db")
    store = LeadResearchStore(db_path=db)
    assert store.delete("nobody@example.com", reason="irrelevant") is False

    learning = FitLearningStore(db)
    assert learning.should_skip_company("Test Co") is False
    assert learning.should_skip_domain("example.com") is False


# ---------------------------------------------------------------------------
# serial intake gate — discover_until_target drops a user-rejected lead
# ---------------------------------------------------------------------------

def test_serial_intake_drops_user_rejected_company(monkeypatch, tmp_path):
    from app.discovery.sources.status import SourceStatus
    from app.lead_research.service import PendingLeadsStore
    from app.leads.pipeline import ResearchQuery, discover_until_target

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    dossiers = LeadResearchStore(db_path=db)
    # The user previously deleted "Rejected Co" as not-a-client (corroborated
    # — the research agreed).
    FitLearningStore(db).reject_company("Rejected Co", corroborated=True)

    def _discover(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        records = [
            {"company_name": "Rejected Co", "source_url": "https://x.example",
             "trade_category": "general_contractor",  # P2: gc-query needs gc evidence
             "plan_holder": {"domain": "rejected.com",
                             "emails": [{"email": "no@rejected.com"}],
                             "person": {"name": ""}}},
            {"company_name": "Kept Co", "source_url": "https://x.example",
             "trade_category": "general_contractor",  # P2: gc-query needs gc evidence
             "plan_holder": {"domain": "kept.com",
                             "emails": [{"email": "yes@kept.com"}],
                             "person": {"name": ""}}},
        ]
        return SourceStatus.SUCCESS, records, {"pdf_urls": ["https://x.example"]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)
    query = ResearchQuery(trade="gc", location="Texas", target_emails=1)
    leads, _ = discover_until_target(
        query, pending_store=pending, dossier_store=dossiers, max_passes=1,
    )

    # The rejected company was dropped at intake; only the kept one is served.
    assert {l["email"] for l in leads} == {"yes@kept.com"}


def test_serial_intake_drops_user_rejected_domain(monkeypatch, tmp_path):
    from app.discovery.sources.status import SourceStatus
    from app.lead_research.service import PendingLeadsStore
    from app.leads.pipeline import ResearchQuery, discover_until_target

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    dossiers = LeadResearchStore(db_path=db)
    # The user deleted an email on this domain as not-a-client (corroborated).
    FitLearningStore(db).reject_domain("rejected.com", corroborated=True)

    def _discover(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        records = [
            {"company_name": "X Co", "source_url": "https://x.example",
             "trade_category": "general_contractor",  # P2: gc-query needs gc evidence
             "plan_holder": {"domain": "rejected.com",
                             "emails": [{"email": "no@rejected.com"}],
                             "person": {"name": ""}}},
            {"company_name": "Y Co", "source_url": "https://x.example",
             "trade_category": "general_contractor",  # P2: gc-query needs gc evidence
             "plan_holder": {"domain": "kept.com",
                             "emails": [{"email": "yes@kept.com"}],
                             "person": {"name": ""}}},
        ]
        return SourceStatus.SUCCESS, records, {"pdf_urls": ["https://x.example"]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)
    query = ResearchQuery(trade="gc", location="Texas", target_emails=1)
    leads, _ = discover_until_target(
        query, pending_store=pending, dossier_store=dossiers, max_passes=1,
    )

    assert {l["email"] for l in leads} == {"yes@kept.com"}


# ---------------------------------------------------------------------------
# streaming intake gate — run_full's producer never buffers a rejected lead
# ---------------------------------------------------------------------------

def test_streaming_intake_never_researches_rejected_lead(monkeypatch, tmp_path):
    from app.discovery.sources.status import SourceStatus
    from app.lead_research.service import PendingLeadsStore
    from app.leads.pipeline import ResearchQuery, run_full

    db = str(tmp_path / "leads.db")
    store = LeadResearchStore(db_path=db)
    pending = PendingLeadsStore(db_path=db)
    FitLearningStore(db).reject_domain("rejected.com", corroborated=True)

    calls: list[tuple] = []

    class _Agent:
        def research(self, email, domain, *a, **k):
            calls.append((email, domain))
            return LeadDossier(
                email=email, domain=domain,
                company=CompanyProfile(name="Acme", industry="general contractor",
                                       location="Texas"),
                person=PersonFindings(name="Jane", role="Owner", bound=True,
                                      role_relevance=True),
                potential_score=8.5, recommendation="contact_now",
            )

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _Agent)

    def _discover(trade, location, limit, skip_pdfs=None, yield_store=None, candidate_store=None):
        records = [
            {"company_name": "Rejected Co", "source_url": "https://x.example",
             "trade_category": "general_contractor",  # P2: gc-query needs gc evidence
             "plan_holder": {"domain": "rejected.com",
                             "emails": [{"email": "no@rejected.com"}],
                             "person": {"name": ""}}},
            {"company_name": "Kept Co", "source_url": "https://x.example",
             "trade_category": "general_contractor",  # P2: gc-query needs gc evidence
             "plan_holder": {"domain": "kept.com",
                             "emails": [{"email": "yes@kept.com"}],
                             "person": {"name": ""}}},
        ]
        return SourceStatus.SUCCESS, records, {"pdf_urls": ["https://x.example"]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)
    query = ResearchQuery(trade="gc", location="Texas", target_emails=1)
    outcome = run_full(query, store=store, pending_store=pending)

    # The rejected email was never researched NOR shown; the kept one was.
    assert {e["email"] for e in outcome["results"]} == {"yes@kept.com"}
    assert ("no@rejected.com", "rejected.com") not in calls
    assert ("yes@kept.com", "kept.com") in calls
    assert store.get("no@rejected.com") is None

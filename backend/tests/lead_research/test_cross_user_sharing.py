"""Cross-user lead sharing (Phase 2) + lead exclusivity (2026-09-12).

Two layers, deliberately kept side by side:

* STORE-level sharing mechanics — dossier_owners junction rows make a lead
  visible to a user's dashboard. These still exist because the ADMIN assign
  path (push folder to user X) and the LEGACY shared leads (researched while
  sharing was the rule) must keep working.

* PIPELINE-level exclusivity (new) — "ak lead ya email sirf ak user ko show
  honi chahye": a NEW lead owned by another user is SKIPPED at every intake
  gate of this run (pending-cache serve, fresh discovery, research time, and
  the live race guard). The pipeline never stamps a new owner on another
  user's lead; only the admin (assign) or a legacy junction row shares.
"""

from __future__ import annotations

from app.lead_research.models import LeadDossier
from app.lead_research.service import LeadResearchStore, _email_hash


def _mk(email: str) -> LeadDossier:
    return LeadDossier(email=email, domain=email.split("@", 1)[1])


def test_save_records_researcher_as_owner(tmp_path):
    store = LeadResearchStore(str(tmp_path / "t.db"))
    store.save(_mk("a@x.com"), user_id="alice")
    # Visible to the researcher...
    assert [d.email for d in store.list_all(user_id="alice")] == ["a@x.com"]
    # ...and invisible to a fresh user (a new user's dashboard stays fresh).
    assert store.list_all(user_id="bob") == []


def test_add_owner_shares_cached_lead(tmp_path):
    """The cache-hit path: bob's run reused alice's dossier — bob's
    dashboard must now show the lead (sharing, not mixing: it is exactly
    what bob searched for)."""
    store = LeadResearchStore(str(tmp_path / "t.db"))
    store.save(_mk("a@x.com"), user_id="alice")
    assert store.add_owner("a@x.com", "bob") is True
    # All the USER-facing views honour the junction, not just list_all.
    assert [d.email for d in store.list_all(user_id="bob")] == ["a@x.com"]
    assert _email_hash("a@x.com") in store.all_meta(user_id="bob")
    assert store.query_leads(user_id="bob", recommendation="*")[1] == 1
    assert store.distinct_dates(user_id="bob")  # date view sees the shared lead


def test_add_owner_is_idempotent_and_honest(tmp_path):
    store = LeadResearchStore(str(tmp_path / "t.db"))
    store.save(_mk("a@x.com"), user_id="alice")
    assert store.add_owner("a@x.com", "alice") is False  # already owner
    assert store.add_owner("a@x.com", "bob") is True
    assert store.add_owner("a@x.com", "bob") is False  # OR IGNORE, no dup row
    assert store.add_owner("ghost@x.com", "bob") is False  # unknown email
    assert store.add_owner("a@x.com", "") is False  # no user = no-op


def test_race_two_saves_both_users_visible(tmp_path):
    """Two jobs researching the same email concurrently: the first save
    owns the user_id column, but the second researcher must not LOSE the
    lead from their dashboard (the old single-column behavior did)."""
    store = LeadResearchStore(str(tmp_path / "t.db"))
    store.save(_mk("a@x.com"), user_id="alice")
    store.save(_mk("a@x.com"), user_id="bob")  # upsert: column stays alice
    assert [d.email for d in store.list_all(user_id="alice")] == ["a@x.com"]
    assert [d.email for d in store.list_all(user_id="bob")] == ["a@x.com"]


def test_sharing_never_leaks_to_third_users(tmp_path):
    """The isolation guarantee holds: a shared lead is visible to its
    owners only — carol, who never searched it, sees nothing."""
    store = LeadResearchStore(str(tmp_path / "t.db"))
    store.save(_mk("a@x.com"), user_id="alice")
    store.add_owner("a@x.com", "bob")
    assert store.list_all(user_id="carol") == []
    assert store.query_leads(user_id="carol", recommendation="*")[1] == 0
    assert store.all_meta(user_id="carol") == {}


def test_legacy_rows_still_admin_only_for_users(tmp_path):
    """Pre-auth rows (user_id='') remain invisible to regular users even
    with the junction — legacy semantics unchanged."""
    store = LeadResearchStore(str(tmp_path / "t.db"))
    store.save(_mk("legacy@x.com"))  # no user_id
    assert store.list_all(user_id="bob") == []
    # Admin's own-dashboard scope (include_legacy) still sees them.
    assert store.list_all(user_id="boss", is_admin=False,
                          include_legacy=True)


def test_set_user_bulk_assign_makes_durable_owner(tmp_path):
    """Admin 'push folder to user X' — X becomes an owner row too, so the
    assignment survives even if the user_id column moves again later."""
    store = LeadResearchStore(str(tmp_path / "t.db"))
    store.save(_mk("a@x.com"), user_id="admin1")
    store.set_user_bulk(["a@x.com"], "bob")
    # Column moved to bob (existing admin semantics) AND owner row exists.
    assert store.list_all(user_id="bob")
    store.set_user_bulk(["a@x.com"], "carol")  # column moves on...
    assert store.list_all(user_id="bob")  # ...but bob's owner row persists


def test_set_user_bulk_unassign_removes_all_owners(tmp_path):
    """Admin unassign ('' = back to admin-only) must clear EVERY owner —
    or the leads would silently stay on user dashboards."""
    store = LeadResearchStore(str(tmp_path / "t.db"))
    store.save(_mk("a@x.com"), user_id="alice")
    store.add_owner("a@x.com", "bob")
    assert store.set_user_bulk(["a@x.com"], "") == 1
    assert store.list_all(user_id="alice") == []
    assert store.list_all(user_id="bob") == []
    # Admin still sees everything (unfiltered view).
    assert len(store.list_all()) == 1


def test_hidden_still_applies_to_shared_leads(tmp_path):
    """The admin's Hide control keeps working on shared leads — hiding is
    a dossier-level flag, so it applies to every owner's view."""
    store = LeadResearchStore(str(tmp_path / "t.db"))
    store.save(_mk("a@x.com"), user_id="alice")
    store.add_owner("a@x.com", "bob")
    store.set_hidden("a@x.com", True)
    assert store.query_leads(user_id="bob", recommendation="*")[1] == 0
    assert store.query_leads(user_id="alice", recommendation="*")[1] == 0


def test_pipeline_skips_another_users_lead(tmp_path, monkeypatch):
    """run_research exclusivity: a lead owned by alice is NOT bob's lead —
    bob's run skips it entirely (no re-research, no owner stamp, nothing on
    bob's dashboard). The old behavior stamped bob as owner on the cache hit."""
    from app.leads.pipeline import run_research

    store = LeadResearchStore(str(tmp_path / "t.db"))
    store.save(_mk("a@x.com"), user_id="alice")

    class _BoomAgent:
        """Any research attempt is a failure — exclusivity must SKIP, not
        re-research."""
        def __init__(self, *a, **k):
            pass

        def research(self, *a, **k):
            raise AssertionError("another user's lead must be skipped, not re-researched")

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _BoomAgent)
    results = run_research(
        [{"email": "a@x.com", "domain": "x.com"}],
        store=store, trade="gc", location="Dallas TX", user_id="bob",
    )
    # Skipped: no result entry, no owner stamp, bob's dashboard stays clean.
    assert results == []
    assert store.list_all(user_id="bob") == []
    # Alice's ownership is untouched.
    assert [d.email for d in store.list_all(user_id="alice")] == ["a@x.com"]


def test_pipeline_race_guard_caches_stale_snapshot(tmp_path, monkeypatch):
    """The live race guard: bob's run started BEFORE alice saved (so the
    taken-pool snapshot is stale/empty) — the cache-hit moment re-checks
    ownership and still skips the email."""
    from app.leads.pipeline import run_research

    store = LeadResearchStore(str(tmp_path / "t.db"))
    store.save(_mk("a@x.com"), user_id="alice")
    # Simulate the stale snapshot: the pool was computed before alice saved.
    monkeypatch.setattr(store, "taken_by_others", lambda user_id: set())

    class _BoomAgent:
        def __init__(self, *a, **k):
            pass

        def research(self, *a, **k):
            raise AssertionError("race guard must skip, not re-research")

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _BoomAgent)
    results = run_research(
        [{"email": "a@x.com", "domain": "x.com"}],
        store=store, trade="gc", location="Dallas TX", user_id="bob",
    )
    assert results == []
    assert store.list_all(user_id="bob") == []


def test_pipeline_legacy_shared_lead_still_cache_hits(tmp_path, monkeypatch):
    """Legacy shared leads keep working: bob already owns a junction row
    (shared while sharing was the rule / admin-assigned) — bob's run reuses
    the research (cache hit, no re-research) and still sees the lead."""
    from app.leads.pipeline import run_research

    store = LeadResearchStore(str(tmp_path / "t.db"))
    store.save(_mk("a@x.com"), user_id="alice")
    store.add_owner("a@x.com", "bob")  # the legacy sharing row

    class _BoomAgent:
        def __init__(self, *a, **k):
            pass

        def research(self, *a, **k):
            raise AssertionError("an owned lead must be a cache hit, not re-researched")

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _BoomAgent)
    results = run_research(
        [{"email": "a@x.com", "domain": "x.com"}],
        store=store, trade="gc", location="Dallas TX", user_id="bob",
    )
    assert results and results[0].get("cached") is True
    assert [d.email for d in store.list_all(user_id="bob")] == ["a@x.com"]


def test_pipeline_suppresses_user_deleted_lead(tmp_path, monkeypatch):
    """Delete-suppression at research time: a lead deleted (any reason) is
    never re-entered by ANY user's run — bob gets nothing, no research."""
    from app.leads.pipeline import run_research

    store = LeadResearchStore(str(tmp_path / "t.db"))
    store.save(_mk("a@x.com"), user_id="bob")
    store.delete("a@x.com", reason="bad_data", user_id="bob", username="bob")

    class _BoomAgent:
        def __init__(self, *a, **k):
            pass

        def research(self, *a, **k):
            raise AssertionError("a user-deleted lead must never be re-researched")

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _BoomAgent)
    results = run_research(
        [{"email": "a@x.com", "domain": "x.com"}],
        store=store, trade="gc", location="Dallas TX", user_id="bob",
    )
    assert results == []
    assert store.list_all(user_id="bob") == []


# ---------------------------------------------------------------------------
# discover_until_target intake gates — the cache-serve and fresh lanes
# ---------------------------------------------------------------------------

def _record(name: str, email: str, domain: str) -> dict:
    return {
        "company_name": name,
        "source_url": f"https://{domain}",
        # P2 trade gate: the gc-query tests below need records that fold to
        # 'gc' — unlabeled records no longer serve to a trade-filtered run.
        "trade_category": "general_contractor",
        "plan_holder": {"domain": domain,
                        "emails": [{"email": email}],
                        "person": {"name": ""}},
    }


def test_discovery_never_serves_deleted_or_taken_from_cache(
        tmp_path, monkeypatch):
    """Pending-cache serve: a deleted email and another user's email are
    dropped (purged) from the served window — only genuinely-fresh rows
    reach the run."""
    from app.discovery.sources.status import SourceStatus
    from app.lead_research.service import PendingLeadsStore
    from app.leads.pipeline import ResearchQuery, discover_until_target

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    store = LeadResearchStore(db_path=db)
    # deleted@x.com: a real dossier the user then deleted.
    store.save(_mk("deleted@x.com"), user_id="bob")
    store.delete("deleted@x.com", reason="duplicate", user_id="bob")
    # theirs@y.com: alice's lead (exclusivity blocks it for bob).
    store.save(_mk("theirs@y.com"), user_id="alice")
    pending.add([
        {"email": "deleted@x.com", "domain": "x.com", "location": "Texas",
         "trade": "gc"},
        {"email": "theirs@y.com", "domain": "y.com", "location": "Texas",
         "trade": "gc"},
        {"email": "fresh@z.com", "domain": "z.com", "location": "Texas",
         "trade": "gc"},
    ])

    def _should_not_run(trade, location, limit, skip_pdfs=None,
                        yield_store=None, candidate_store=None):
        raise AssertionError("cache has a servable lead — no live discovery needed")

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _should_not_run)
    query = ResearchQuery(trade="gc", location="Texas", target_emails=1)
    leads, _ = discover_until_target(
        query, pending_store=pending, dossier_store=store, max_passes=1,
        user_id="bob",
    )
    assert [l["email"] for l in leads] == ["fresh@z.com"]
    # The suppressed rows were PURGED — they stop shadowing future windows.
    assert pending.count() == 1


def test_discovery_fresh_gate_drops_deleted_and_taken(
        tmp_path, monkeypatch):
    """Fresh-discovery lane: live sources re-surface a deleted email and
    another user's email — both are dropped before they are ever buffered."""
    from app.discovery.sources.status import SourceStatus
    from app.lead_research.service import PendingLeadsStore
    from app.leads.pipeline import ResearchQuery, discover_until_target

    db = str(tmp_path / "leads.db")
    pending = PendingLeadsStore(db_path=db)
    store = LeadResearchStore(db_path=db)
    store.save(_mk("deleted@x.com"), user_id="bob")
    store.delete("deleted@x.com", reason="low_quality", user_id="bob")
    store.save(_mk("theirs@y.com"), user_id="alice")

    def _discover(trade, location, limit, skip_pdfs=None,
                  yield_store=None, candidate_store=None):
        records = [
            _record("D", "deleted@x.com", "x.com"),
            _record("T", "theirs@y.com", "y.com"),
            _record("F", "fresh@z.com", "z.com"),
        ]
        return SourceStatus.SUCCESS, records, {"pdf_urls": []}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)
    query = ResearchQuery(trade="gc", location="Texas", target_emails=5)
    leads, _ = discover_until_target(
        query, pending_store=pending, dossier_store=store, max_passes=1,
        user_id="bob",
    )
    assert {l["email"] for l in leads} == {"fresh@z.com"}

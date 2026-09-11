"""Cross-user lead sharing (Phase 2 demand fix) — store-level tests.

10 simultaneous users will run overlapping searches (Monday load test).
The credit-side sharing already existed (store.get is email-keyed, so a
cache hit never re-researches); this phase adds the VISIBILITY side: a
user whose own search surfaced an already-researched lead must still see
it on their dashboard. The dossiers.user_id column keeps the FIRST
researcher; the dossier_owners junction records every user whose search
surfured the lead.
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


def test_pipeline_cache_hit_stamps_owner(tmp_path, monkeypatch):
    """run_research cache-hit path: a lead researched under user A, then
    surfaced by user B's run, gets B stamped as owner — B reuses the
    research (no agent call) AND sees the lead."""
    from app.leads.pipeline import run_research

    store = LeadResearchStore(str(tmp_path / "t.db"))
    store.save(_mk("a@x.com"), user_id="alice")

    class _BoomAgent:
        """Any research attempt is a failure — the cache hit must be the
        ONLY path (proves no re-research)."""
        def __init__(self, *a, **k):
            pass

        def research(self, *a, **k):
            raise AssertionError("cache hit must not re-research")

    monkeypatch.setattr("app.lead_research.agent.AILeadResearchAgent", _BoomAgent)
    results = run_research(
        [{"email": "a@x.com", "domain": "x.com"}],
        store=store, trade="gc", location="Dallas TX", user_id="bob",
    )
    assert results and results[0].get("cached") is True
    assert [d.email for d in store.list_all(user_id="bob")] == ["a@x.com"]

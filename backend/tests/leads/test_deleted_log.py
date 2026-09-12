"""Tests for the deleted_leads audit trail in LeadResearchStore.

Every dossier deletion (manual or junk sweep) is logged into the
``deleted_leads`` table in the SAME transaction, so the admin audit
trail can never show a deletion that the dossier survived (or vice
versa). These tests verify that contract.
"""

from __future__ import annotations

from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings
from app.lead_research.service import LeadResearchStore


def _make_dossier(email: str = "test@example.com", rec: str = "contact_now") -> LeadDossier:
    """Minimal valid dossier for store tests."""
    return LeadDossier(
        email=email,
        domain="example.com",
        company=CompanyProfile(name="Test Co", industry="construction", location="TX"),
        person=PersonFindings(name="Test", role="Owner", bound=True, role_relevance=True),
        recommendation=rec,
        potential_score=7.0,
    )


# --- delete + audit trail ---

def test_delete_logs_to_deleted_leads(tmp_path):
    """Deleting a stored dossier writes to deleted_leads with reason."""
    store = LeadResearchStore(db_path=str(tmp_path / "lead_research.db"))
    d = _make_dossier(email="alice@acme.com")
    store.save(d)

    assert store.delete("alice@acme.com", reason="manual") is True

    log = store.deleted_log()
    assert log["total"] == 1
    entry = log["deleted"][0]
    assert entry["email"] == "alice@acme.com"
    assert entry["reason"] == "manual"
    assert entry["deleted_at"]  # non-empty timestamp
    assert entry["admin_decision"] == ""  # admin has not answered yet


def test_clear_junk_logs_reason_junk(tmp_path):
    """clear_junk should record reason='junk' for each deleted dossier."""
    store = LeadResearchStore(db_path=str(tmp_path / "lead_research.db"))
    d = _make_dossier(email="bob@dead.com", rec="skip")
    store.save(d)

    # Simulate what the API clear_junk endpoint does: delete with reason="junk"
    # for dossiers whose recommendation is "skip" (after regate).
    removed = 0
    for dossier in store.list_all():
        if dossier.recommendation == "skip":
            if store.delete(dossier.email, reason="junk"):
                removed += 1
    assert removed == 1

    log = store.deleted_log()
    assert log["total"] == 1
    entry = log["deleted"][0]
    assert entry["email"] == "bob@dead.com"
    assert entry["reason"] == "junk"


def test_deleted_log_respects_limit(tmp_path):
    """limit parameter caps how many entries are returned (newest first)."""
    store = LeadResearchStore(db_path=str(tmp_path / "lead_research.db"))
    for i in range(5):
        d = _make_dossier(email=f"user{i}@acme.com")
        store.save(d)
        store.delete(f"user{i}@acme.com", reason="manual")

    # Limit 3 returns only the 3 most recent
    log = store.deleted_log(limit=3)
    assert log["total"] == 5  # total is honest even when limited
    assert len(log["deleted"]) == 3
    # All 5 exist in the table — verify with unlimited query
    all_log = store.deleted_log(limit=100)
    assert len(all_log["deleted"]) == 5


def test_delete_nonexistent_email_returns_false(tmp_path):
    """Deleting an email that was never stored returns False and adds no log entry."""
    store = LeadResearchStore(db_path=str(tmp_path / "lead_research.db"))

    assert store.delete("nobody@test.com", reason="manual") is False
    assert store.deleted_log() == {"total": 0, "deleted": []}


def test_delete_returns_false_and_no_double_log(tmp_path):
    """Second delete on same email returns False (dossier already gone),
    so no duplicate entry appears in the audit trail."""
    store = LeadResearchStore(db_path=str(tmp_path / "lead_research.db"))
    d = _make_dossier(email="twice@acme.com")
    store.save(d)

    assert store.delete("twice@acme.com", reason="manual") is True
    assert store.delete("twice@acme.com", reason="junk") is False  # already gone

    log = store.deleted_log()
    assert log["total"] == 1
    entry = log["deleted"][0]
    assert entry["email"] == "twice@acme.com"
    assert entry["reason"] == "manual"  # original reason preserved

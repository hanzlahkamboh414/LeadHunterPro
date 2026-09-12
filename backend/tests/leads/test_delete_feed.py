"""The admin delete-feed loop + lead exclusivity + delete-suppression (2026-09-12).

Contract under test — the user's requirement:
* every USER delete (any reason) records WHO deleted + WHY + a full dossier
  snapshot, so the admin feed can show "fulane user ne ye lead fulani reason
  se delete ki";
* the admin answers each row: CONFIRM (backs the "not our client" verdict —
  the rejection becomes corroborated, i.e. decisive) or RESTORE (the lead
  comes back, assigned to the user who deleted it, and the identity learning
  the delete fed is CLEARED);
* deleted emails form a suppression pool — they must never resurface to ANY
  user until restored;
* an email is exclusive: once one user owns it (their research/save), other
  users' runs must skip it.
"""

from __future__ import annotations

from app.lead_research.fit_learning import FitLearningStore
from app.lead_research.models import CompanyProfile, LeadDossier, PersonFindings
from app.lead_research.service import LeadResearchStore


def _make_dossier(email: str = "test@example.com", rec: str = "contact_now",
                  domain: str = "example.com") -> LeadDossier:
    """Minimal valid dossier for store tests."""
    return LeadDossier(
        email=email,
        domain=domain,
        company=CompanyProfile(name="Test Co", industry="construction", location="TX"),
        person=PersonFindings(name="Test", role="Owner", bound=True, role_relevance=True),
        recommendation=rec,
        potential_score=7.0,
    )


# --- delete records WHO + WHY + the restore snapshot ---

def test_delete_records_user_and_stashes_dossier(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "lead_research.db"))
    store.save(_make_dossier(email="alice@acme.com"), user_id="u1")

    assert store.delete("alice@acme.com", reason="not_our_client",
                        user_id="u1", username="shakir") is True

    entry = store.deleted_log()["deleted"][0]
    assert entry["email"] == "alice@acme.com"
    assert entry["reason"] == "not_our_client"
    assert entry["user_id"] == "u1"
    assert entry["username"] == "shakir"
    assert entry["admin_decision"] == ""  # pending the admin's answer

    # The stash is the deleted dossier itself — Restore can bring it back.
    import json
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "lead_research.db"))
    stashed = conn.execute(
        "SELECT dossier_json FROM deleted_leads WHERE email = ?",
        ("alice@acme.com",),
    ).fetchone()[0]
    conn.close()
    assert LeadDossier.from_dict(json.loads(stashed)).email == "alice@acme.com"


def test_delete_username_falls_back_to_user_id(tmp_path):
    """deleted_log shows user_id as the name when username is absent."""
    store = LeadResearchStore(db_path=str(tmp_path / "lead_research.db"))
    store.save(_make_dossier(email="bob@acme.com"), user_id="u9")

    store.delete("bob@acme.com", reason="bad_data", user_id="u9")

    entry = store.deleted_log()["deleted"][0]
    assert entry["username"] == "u9"


def test_delete_purges_owners_rows(tmp_path):
    """A deleted lead has no owners — the email must not block anyone."""
    store = LeadResearchStore(db_path=str(tmp_path / "lead_research.db"))
    store.save(_make_dossier(email="carol@acme.com"), user_id="u1")
    store.add_owner("carol@acme.com", "u2")

    assert "carol@acme.com" in store.taken_by_others("u3")

    store.delete("carol@acme.com", reason="duplicate", user_id="u1")

    assert "carol@acme.com" not in store.taken_by_others("u3")


# --- the delete-suppression pool ---

def test_deleted_emails_suppresses_until_restored(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "lead_research.db"))
    store.save(_make_dossier(email="x@acme.com"), user_id="u1")
    store.save(_make_dossier(email="y@acme.com"), user_id="u1")

    store.delete("x@acme.com", reason="bad_data", user_id="u1")
    store.delete("y@acme.com", reason="low_quality", user_id="u1")
    assert store.deleted_emails() == {"x@acme.com", "y@acme.com"}

    # Admin restores one — only that email leaves the pool.
    store.restore_deleted("x@acme.com")
    assert store.deleted_emails() == {"y@acme.com"}


# --- the admin's CONFIRM answer ---

def test_confirm_deleted_backs_rejection_decisively(tmp_path):
    """Confirm on a 'not our client' delete corroborates the identity purge."""
    db = str(tmp_path / "lead_research.db")
    store = LeadResearchStore(db_path=db)
    store.save(_make_dossier(email="d@acme.com", domain="acme.com"), user_id="u1")

    # User delete with the strong reason — UNCORROBORATED (single user, the
    # research did not agree) → recorded but not yet decisive.
    store.delete("d@acme.com", reason="not_our_client", user_id="u1")
    learning = FitLearningStore(db)
    assert learning.should_skip_domain("acme.com") is False

    result = store.confirm_deleted("d@acme.com")
    assert result == {"email": "d@acme.com", "admin_decision": "confirmed"}

    # The admin backed the verdict → decisive for everyone.
    assert learning.should_skip_domain("acme.com") is True
    assert learning.should_skip_company("Test Co") is True

    # The feed shows the answer.
    assert store.deleted_log()["deleted"][0]["admin_decision"] == "confirmed"


def test_confirm_deleted_non_rejection_adds_no_learning(tmp_path):
    """Confirm on a hygiene reason (bad_data) only marks the row."""
    db = str(tmp_path / "lead_research.db")
    store = LeadResearchStore(db_path=db)
    store.save(_make_dossier(email="e@acme.com", domain="acme.com"), user_id="u1")
    store.delete("e@acme.com", reason="bad_data", user_id="u1")

    result = store.confirm_deleted("e@acme.com")
    assert result is not None

    learning = FitLearningStore(db)
    assert learning.should_skip_domain("acme.com") is False
    assert learning.should_skip_company("Test Co") is False


def test_confirm_deleted_unknown_email_returns_none(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "lead_research.db"))
    assert store.confirm_deleted("nobody@acme.com") is None


# --- the admin's RESTORE answer ---

def test_restore_deleted_resaves_dossier_and_clears_learning(tmp_path):
    db = str(tmp_path / "lead_research.db")
    store = LeadResearchStore(db_path=db)
    store.save(_make_dossier(email="f@acme.com", domain="acme.com"), user_id="u1")

    # A decisive rejection: two users clicked "not our client" independently.
    store.delete("f@acme.com", reason="not_our_client", user_id="u1")
    store.save(_make_dossier(email="f@acme.com", domain="acme.com"), user_id="u2")
    store.delete("f@acme.com", reason="not_our_client", user_id="u2")
    learning = FitLearningStore(db)
    assert learning.should_skip_domain("acme.com") is True  # 2 distinct users
    assert store.get("f@acme.com") is None

    # The admin says it was wrong.
    result = store.restore_deleted("f@acme.com")
    assert result == {"email": "f@acme.com", "admin_decision": "restored",
                      "restored_dossier": "yes"}

    # The dossier is back, assigned to the user who deleted it (row above:
    # u2 — the conflict-update keeps the LATEST delete).
    restored = store.get("f@acme.com")
    assert restored is not None
    assert restored.email == "f@acme.com"
    assert store.owned_by_another("f@acme.com", "u2") is False

    # The identity learning is cleared — the company reopens for everyone.
    assert learning.should_skip_domain("acme.com") is False
    assert learning.should_skip_company("Test Co") is False

    # And the suppression is lifted.
    assert "f@acme.com" not in store.deleted_emails()


def test_restore_deleted_without_snapshot_lifts_suppression_only(tmp_path):
    """Pre-snapshot delete rows (deleted before the stash column existed —
    production rows carry an EMPTY dossier_json) restore as suppression-lift
    only: the lead is servable again, but there is nothing to re-save."""
    import sqlite3

    db = str(tmp_path / "lead_research.db")
    store = LeadResearchStore(db_path=db)
    # A pre-migration deleted_leads row: reason + who, but no dossier stash.
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO deleted_leads (email, reason, user_id, username, "
        "dossier_json, admin_decision) "
        "VALUES ('legacy@old.com', 'manual', 'u1', '', '', '')"
    )
    conn.commit()
    conn.close()
    assert "legacy@old.com" in store.deleted_emails()

    result = store.restore_deleted("legacy@old.com")
    assert result == {"email": "legacy@old.com", "admin_decision": "restored",
                      "restored_dossier": "no"}
    assert store.get("legacy@old.com") is None
    assert "legacy@old.com" not in store.deleted_emails()


def test_restore_deleted_unknown_email_returns_none(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "lead_research.db"))
    assert store.restore_deleted("nobody@acme.com") is None


def test_restore_after_confirm_lifts_suppression(tmp_path):
    """A restored row leaves the pool even when it was confirmed earlier."""
    db = str(tmp_path / "lead_research.db")
    store = LeadResearchStore(db_path=db)
    store.save(_make_dossier(email="g@acme.com"), user_id="u1")
    store.delete("g@acme.com", reason="not_our_client", user_id="u1")
    store.confirm_deleted("g@acme.com")
    assert "g@acme.com" in store.deleted_emails()  # confirm keeps suppression

    store.restore_deleted("g@acme.com")
    assert "g@acme.com" not in store.deleted_emails()


# --- lead exclusivity: taken_by_others / owned_by_another ---

def test_taken_by_others_blocks_other_users_only(tmp_path):
    store = LeadResearchStore(db_path=str(tmp_path / "lead_research.db"))
    store.save(_make_dossier(email="mine@acme.com"), user_id="u1")
    store.save(_make_dossier(email="theirs@acme.com"), user_id="u2")
    store.save(_make_dossier(email="legacy@acme.com"))  # pre-auth, un-owned
    store.add_owner("mine@acme.com", "u2")  # u2's search also surfaced u1's lead

    taken = store.taken_by_others("u1")
    assert "theirs@acme.com" in taken  # u2 owns it
    assert "mine@acme.com" not in taken  # u1 owns it (even shared with u2)
    assert "legacy@acme.com" not in taken  # legacy never blocks

    # u3 sees BOTH owned emails, but not the legacy row.
    taken3 = store.taken_by_others("u3")
    assert taken3 == {"mine@acme.com", "theirs@acme.com"}


def test_taken_by_others_empty_user_sees_nothing(tmp_path):
    """Anonymous/system context (user_id='') never blocks — no exclusivity."""
    store = LeadResearchStore(db_path=str(tmp_path / "lead_research.db"))
    store.save(_make_dossier(email="a@acme.com"), user_id="u1")
    assert store.taken_by_others("") == set()


def test_owned_by_another_live_check(tmp_path):
    """The research-time race guard — per-email ownership, read live."""
    store = LeadResearchStore(db_path=str(tmp_path / "lead_research.db"))
    store.save(_make_dossier(email="live@acme.com"), user_id="u1")
    store.save(_make_dossier(email="legacy@acme.com"))  # un-owned

    assert store.owned_by_another("live@acme.com", "u1") is False  # own
    assert store.owned_by_another("live@acme.com", "u2") is True   # other's
    assert store.owned_by_another("legacy@acme.com", "u2") is False  # un-owned
    assert store.owned_by_another("missing@acme.com", "u2") is False  # not saved

    # u2's search surfaces u1's lead (junction row) → no longer "another's".
    store.add_owner("live@acme.com", "u2")
    assert store.owned_by_another("live@acme.com", "u2") is False

    # Anonymous context never claims exclusivity.
    assert store.owned_by_another("live@acme.com", "") is False

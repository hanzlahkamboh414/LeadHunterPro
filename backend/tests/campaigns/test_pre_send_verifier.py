"""Recipients cannot reach the send path before campaign preflight."""

from datetime import timedelta

from app.campaigns.pre_send_verifier import CampaignEmailVerifier
from app.campaigns.scheduler import _iso
from app.campaigns.store import CampaignStore
from app.email.email_cleaner import is_acceptable_email
from app.email_accounts import google
from app.lead_research.service import LeadResearchStore
from tests.campaigns.test_campaigns import NOW, _dossier
from tests.campaigns.test_followups import _setup, _make_campaign


def test_encoded_space_and_welded_phone_are_rejected():
    assert not is_acceptable_email("%20info@acme.com")
    assert not is_acceptable_email("277-3108mcg@acme.com")
    assert is_acceptable_email("24hrservice@acme.com")
    assert is_acceptable_email("123sales@acme.com")


def test_unexplained_percent_prefix_is_held_not_deleted(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "campaigns.db"))
    leads = LeadResearchStore(db_path=str(tmp_path / "leads.db"))
    email = "%sales@acme.com"
    leads.save(_dossier(email))
    store.create("u1", account_id=1, name="n", subject="s", body="b",
                 emails=[email], start_at=_iso(NOW))
    worker = CampaignEmailVerifier(
        store, leads, domain_check=lambda domain: ("ready", "MX record found"))
    assert worker.run_once()["hold"] == 1
    assert leads.get(email) is not None
    assert worker.status(email) == "hold"


def test_worker_removes_only_definite_invalid_recipients(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "campaigns.db"))
    leads = LeadResearchStore(db_path=str(tmp_path / "leads.db"))
    bad = "%20info@acme.com"
    valid = "123sales@acme.com"
    for email in (bad, valid):
        leads.save(_dossier(email))
    campaign = store.create(
        "u1", account_id=1, name="n", subject="s", body="b",
        emails=[bad, valid], start_at=_iso(NOW - timedelta(minutes=1)))
    worker = CampaignEmailVerifier(
        store, leads, domain_check=lambda domain: ("ready", "MX record found"))
    result = worker.run_once()
    assert result["invalid"] == 1 and result["ready"] == 1
    assert leads.get(bad) is None
    assert leads.get(valid) is not None
    assert [s["email"] for s in store.sends(campaign["id"], "u1")] == [valid]
    assert store.email_check_status(bad) == "invalid"
    # MX proves a route for the domain, not that this mailbox exists.
    assert worker.status(valid) == "hold"


def test_send_waits_until_worker_verifies(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch)
    campaign = _make_campaign(ctx, emails=("123sales@acme.com",))
    worker = CampaignEmailVerifier(
        ctx["store"], ctx["leads"],
        domain_check=lambda domain: ("ready", "MX record found"))
    ctx["sched"]._verifier = worker
    sent = []
    monkeypatch.setattr(google, "send_gmail", lambda *args, **kwargs: sent.append(kwargs))
    assert ctx["sched"].run_once()["sent"] == 0
    assert sent == []
    assert worker.run_once()["ready"] == 1
    assert ctx["sched"].run_once()["sent"] == 0
    ctx["store"].save_email_check("123sales@acme.com", "ready", "first-party:reply-confirmed")
    assert ctx["sched"].run_once()["sent"] == 1
    assert len(sent) == 1


def test_uncertain_dns_holds_without_deleting(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "campaigns.db"))
    leads = LeadResearchStore(db_path=str(tmp_path / "leads.db"))
    email = "jane@acme.com"
    leads.save(_dossier(email))
    campaign = store.create(
        "u1", account_id=1, name="n", subject="s", body="b",
        emails=[email], start_at=_iso(NOW - timedelta(minutes=1)))
    worker = CampaignEmailVerifier(
        store, leads, domain_check=lambda domain: ("hold", "DNS lookup unavailable"))
    assert worker.run_once()["hold"] == 1
    assert leads.get(email) is not None
    assert len(store.sends(campaign["id"], "u1")) == 1
    assert worker.status(email) == "hold"


def test_held_address_does_not_block_verified_address(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "campaigns.db"))
    campaign = store.create(
        "u1", account_id=1, name="n", subject="s", body="b",
        emails=["hold@slow.test", "ready@acme.com"],
        start_at=_iso(NOW - timedelta(minutes=1)))
    store.save_email_check("hold@slow.test", "hold", "DNS unavailable")
    store.save_email_check("ready@acme.com", "ready", "first-party:reply-confirmed")
    assert store.next_pending(campaign["id"], verified_only=True)["email"] == "ready@acme.com"

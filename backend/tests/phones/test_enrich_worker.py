"""Enrichment worker contracts — the background lane (stores + enricher
injected, hermetic: no network, no threads — run_once driven directly)."""

from __future__ import annotations

import pytest

from app.lead_research.service import PendingLeadsStore
from app.phones.enrich_worker import PhoneEnrichmentWorker
from app.phones.store import PhoneLeadsStore


@pytest.fixture()
def stores(tmp_path):
    return (
        PhoneLeadsStore(db_path=str(tmp_path / "phones.db")),
        PendingLeadsStore(db_path=str(tmp_path / "research.db")),
    )


def _rec(phone, business="Acme GC", **kw):
    return {
        "phone": phone, "person_name": "SMITH, JANE",
        "business_name": business, "trade_category": "GENERAL",
        "city": "VANCOUVER", "state": "WA", "source": "wa_license",
        "license_status": "ACTIVE", "source_url": "https://data.wa.gov/x",
        **kw,
    }


def test_run_once_enriches_claimed_leads_and_feeds_emails_vertical(stores):
    phone_store, pending_store = stores
    phone_store.add([_rec("5031110001"), _rec("5031110002"), _rec("5031110003")])
    # alice claims two of the three; the third belongs to nobody yet.
    alice = phone_store.serve("gc", "", "", 2, "alice")
    assert len(alice) == 2

    results = {alice[0]["id"]: {"email": "info@acme.com",
                                "email_source": "website",
                                "website": "https://acme.com"},
               alice[1]["id"]: {"email": "", "email_source": "",
                                "website": "https://other.com"}}

    def fake_enrich(lead):
        return results[lead["id"]]

    worker = PhoneEnrichmentWorker(
        phone_store, pending_store, enrich=fake_enrich, batch_size=10,
    )
    stats = worker.run_once()

    # Only the CLAIMED leads were worked on.
    assert stats["considered"] == 2
    assert stats["found"] == 1 and stats["no_email"] == 1
    assert stats["errors"] == 0

    # The found email fed the emails vertical's pending cache (one row —
    # the miss fed nothing).
    assert stats["fed_to_emails"] == 1
    cached = pending_store.get("info@acme.com")
    assert cached is not None
    assert cached["company"] == "Acme GC"
    assert cached["dork"] == "phone_enrichment"
    assert cached["trade"] == "gc"
    assert cached["location"] == "VANCOUVER, WA"

    # The lead rows carry the outcome.
    mine = phone_store.list_owned("alice")
    by_email = {l["email"]: l for l in mine}
    assert by_email["info@acme.com"]["email_source"] == "website"
    assert by_email["info@acme.com"]["website"] == "https://acme.com"
    assert by_email[""]["enriched_at"] != ""  # honest miss, stamped done


def test_unclaimed_leads_wait_for_an_owner(stores):
    phone_store, pending_store = stores
    phone_store.add([_rec("5031110001")])  # served to nobody

    worker = PhoneEnrichmentWorker(
        phone_store, pending_store,
        enrich=lambda l: pytest.fail("must not enrich an unowned lead"),
    )
    stats = worker.run_once()
    assert stats["considered"] == 0
    lead = phone_store.serve("gc", "", "", 10, "alice")
    assert lead[0]["email"] == ""  # untouched


def test_enriched_leads_are_never_retried(stores):
    phone_store, pending_store = stores
    phone_store.add([_rec("5031110001")])
    phone_store.serve("gc", "", "", 10, "alice")
    phone_store.set_enrichment(
        phone_store.list_owned("alice")[0]["id"],
        email="", email_source="", website="",
    )

    worker = PhoneEnrichmentWorker(
        phone_store, pending_store,
        enrich=lambda l: pytest.fail("an enriched lead is done"),
    )
    assert worker.run_once()["considered"] == 0


def test_one_bad_lead_does_not_kill_the_pass(stores):
    phone_store, pending_store = stores
    phone_store.add([_rec("5031110001"), _rec("5031110002")])
    leads = phone_store.serve("gc", "", "", 10, "alice")

    def flaky_enrich(lead):
        if lead["id"] == leads[0]["id"]:
            raise RuntimeError("network exploded")
        return {"email": "info@second.com", "email_source": "website",
                "website": "https://second.com"}

    worker = PhoneEnrichmentWorker(
        phone_store, pending_store, enrich=flaky_enrich,
    )
    stats = worker.run_once()
    assert stats["errors"] == 1
    assert stats["found"] == 1
    assert stats["fed_to_emails"] == 1


def test_poison_lead_is_set_aside_after_repeated_failures(stores):
    """A lead that hard-fails every pass must not starve the batch head —
    after _MAX_FAILURES consecutive errors it is skipped until restart."""
    phone_store, pending_store = stores
    phone_store.add([_rec("5031110001")])
    phone_store.serve("gc", "", "", 10, "alice")

    worker = PhoneEnrichmentWorker(
        phone_store, pending_store,
        enrich=lambda l: (_ for _ in ()).throw(RuntimeError("always broken")),
    )
    for _ in range(3):
        assert worker.run_once()["errors"] == 1
    # 4th pass: set aside, no more error churn.
    stats = worker.run_once()
    assert stats["errors"] == 0
    assert stats["skipped_poison"] == 1
    assert stats["considered"] == 0


def test_batch_size_bounds_one_pass(stores):
    phone_store, pending_store = stores
    phone_store.add([_rec(f"503111{n:04d}") for n in range(1, 9)])
    phone_store.serve("gc", "", "", 10, "alice")  # all 8 claimed

    worker = PhoneEnrichmentWorker(
        phone_store, pending_store,
        enrich=lambda l: {"email": "", "email_source": "", "website": ""},
        batch_size=3,
    )
    stats = worker.run_once()
    assert stats["considered"] == 3

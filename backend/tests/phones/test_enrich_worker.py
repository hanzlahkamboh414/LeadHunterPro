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


def test_pattern_inference_lane_feeds_the_emails_vertical(stores):
    """A P5-verified email (dork=pattern_inference) feeds the pending cache
    under its OWN lane tag — the feed stays honest about where it came from."""
    phone_store, pending_store = stores
    phone_store.add([_rec("5031110001")])
    phone_store.serve("gc", "", "", 10, "alice")

    worker = PhoneEnrichmentWorker(
        phone_store, pending_store,
        enrich=lambda l: {
            "email": "jane.smith@acmegc.com",
            "email_source": "pattern_inference",
            "website": "https://acmegc.com",
            "dork": "pattern_inference",
        },
    )
    stats = worker.run_once()
    assert stats["found"] == 1 and stats["fed_to_emails"] == 1
    cached = pending_store.get("jane.smith@acmegc.com")
    assert cached is not None
    assert cached["dork"] == "pattern_inference"

    # And the phone lead's owner sees it on their own row, source-tagged.
    mine = phone_store.list_owned("alice")
    assert mine[0]["email"] == "jane.smith@acmegc.com"
    assert mine[0]["email_source"] == "pattern_inference"


def test_overture_lane_feeds_the_emails_vertical(stores):
    """A P8 Overture-joined email (dork=overture) feeds the pending cache
    under its OWN lane tag, exactly like the other lanes."""
    phone_store, pending_store = stores
    phone_store.add([_rec("5031110001")])
    phone_store.serve("gc", "", "", 10, "alice")

    worker = PhoneEnrichmentWorker(
        phone_store, pending_store,
        enrich=lambda l: {
            "email": "info@acme.com",
            "email_source": "overture",
            "website": "https://acme.com",
            "dork": "overture",
        },
    )
    stats = worker.run_once()
    assert stats["found"] == 1 and stats["fed_to_emails"] == 1
    cached = pending_store.get("info@acme.com")
    assert cached is not None
    assert cached["dork"] == "overture"

    mine = phone_store.list_owned("alice")
    assert mine[0]["email_source"] == "overture"


def test_unclaimed_trade_queue_verifies_without_inventing_person(stores):
    phone_store, pending_store = stores
    raw = _rec("6125550100", business="North Star Roofing", person_name="",
               trade_category="", state="MN", source="mn_dli_registration")
    raw["state_only"] = True
    phone_store.add([raw])
    assert phone_store.serve("", "MN", "", 1, "alice") == []

    worker = PhoneEnrichmentWorker(
        phone_store, pending_store,
        enrich=lambda l: pytest.fail("email lane must not claim raw lead"),
        resolve_trade_fn=lambda lead: {
            "trade": "roofing",
            "evidence_url": "https://northstarroofing.com/services",
            "evidence_kind": "company_website",
        },
    )
    stats = worker.run_trade_once()
    assert stats["verified"] == 1
    served = phone_store.serve("roofing", "MN", "", 1, "alice")
    assert len(served) == 1
    assert served[0]["person_name"] == ""
    assert worker.run_trade_once()["considered"] == 0


def test_trade_miss_is_deferred_and_one_error_does_not_stop_batch(stores):
    phone_store, pending_store = stores
    for n in range(3):
        raw = _rec(f"612555010{n}", business=f"Unknown {n}",
                   trade_category="", state="MN")
        raw["state_only"] = True
        phone_store.add([raw])

    def resolve(lead):
        if lead["business_name"] == "Unknown 0":
            raise RuntimeError("temporary network error")
        if lead["business_name"] == "Unknown 1":
            return {"trade": "", "evidence_url": "", "evidence_kind": ""}
        return {"trade": "roofing", "evidence_url": "https://unknown2.com/roofing",
                "evidence_kind": "company_website"}

    worker = PhoneEnrichmentWorker(
        phone_store, pending_store, resolve_trade_fn=resolve,
        trade_batch_size=3,
    )
    stats = worker.run_trade_once()
    assert stats == {"considered": 3, "verified": 1, "unverified": 1,
                     "errors": 1, "skipped_poison": 0}
    assert phone_store.unclaimed_count("roofing", "MN") == 1
    pending = phone_store.pending_trade_enrichment(10)
    assert [r["business_name"] for r in pending] == ["Unknown 0"]


def test_slow_trade_lookup_does_not_delay_claimed_email_lane(stores):
    import threading

    phone_store, pending_store = stores
    raw = _rec("6125550100", business="North Star Roofing",
               trade_category="", state="MN")
    raw["state_only"] = True
    phone_store.add([raw, _rec("5031110001")])
    phone_store.serve("gc", "WA", "", 1, "alice")
    trade_started = threading.Event()
    release_trade = threading.Event()
    email_done = threading.Event()

    def slow_trade(lead):
        trade_started.set()
        release_trade.wait(2)
        return {"trade": "", "evidence_url": "", "evidence_kind": ""}

    def fast_email(lead):
        email_done.set()
        return {"email": "", "email_source": "", "website": ""}

    worker = PhoneEnrichmentWorker(
        phone_store, pending_store, enrich=fast_email,
        resolve_trade_fn=slow_trade, interval_s=0.01,
        trade_interval_s=0.01,
        batch_size=1, trade_batch_size=1,
    )
    try:
        worker.start()
        assert trade_started.wait(1)
        assert email_done.wait(0.5)
    finally:
        release_trade.set()
        worker.stop()

"""P4 — LinkedInLeadsStore contracts.

The LinkedIn vertical's own linkedin_leads.db: honest person-URL gating at
stock (a company page is never a person lead), trade fold at ingest (P1),
strict trade gate at serve (P2), and the dossier_owners-style exclusivity —
a lead claimed by one user never serves to another.
"""

from __future__ import annotations

from app.linkedin.store import (
    LinkedInLeadsStore,
    clean_linkedin_url,
    is_person_linkedin_url,
    split_location,
)


# ---------------------------------------------------------------------------
# honest URL gating
# ---------------------------------------------------------------------------

def test_is_person_linkedin_url():
    assert is_person_linkedin_url("https://www.linkedin.com/in/jane-smith")
    assert is_person_linkedin_url(
        "http://linkedin.com/in/carlos.guerrero/"
    )
    assert is_person_linkedin_url("https://linkedin.com/pub/jane-smith/1/2/3")
    # Tracking junk is stripped before the check — the same profile, with a
    # trk= parameter, is still the same honest lead.
    assert is_person_linkedin_url(
        "https://www.linkedin.com/in/jane-smith?trk=public_profile_feed"
    )
    # NOT person leads: company/school pages, bare roots, lookalikes.
    assert not is_person_linkedin_url("https://www.linkedin.com/company/acme")
    assert not is_person_linkedin_url("https://www.linkedin.com/")
    assert not is_person_linkedin_url("https://notlinkedin.com/in/jane")
    assert not is_person_linkedin_url("")
    assert not is_person_linkedin_url("linkedin.com/in/jane")  # no scheme


def test_clean_linkedin_url_dedup_form():
    assert clean_linkedin_url(
        "https://www.linkedin.com/in/jane-smith/?trk=x#footer"
    ) == "https://www.linkedin.com/in/jane-smith"
    assert clean_linkedin_url(
        "https://www.linkedin.com/in/jane-smith"
    ) == clean_linkedin_url("https://www.linkedin.com/in/jane-smith/")


def test_split_location():
    assert split_location("Vancouver, WA") == ("Vancouver", "WA")
    assert split_location("Austin, TX") == ("Austin", "TX")
    # Unparsable = honest empty, never a guessed state.
    assert split_location("Texas") == ("", "")
    assert split_location("Vancouver, Washington") == ("", "")
    assert split_location("") == ("", "")


# ---------------------------------------------------------------------------
# add + serve
# ---------------------------------------------------------------------------

def _rec(url="https://www.linkedin.com/in/jane-smith", person="Jane Smith",
         role="Owner", company="Acme GC", trade="General Contractor",
         location="Vancouver, WA", email="info@acme.com") -> dict:
    return {
        "person_name": person, "role": role, "linkedin_url": url,
        "company_name": company, "domain": "acme.com", "trade": trade,
        "location": location, "source": "email_research",
        "source_email": email,
    }


def test_add_folds_trade_dedups_and_drops(tmp_path):
    store = LinkedInLeadsStore(db_path=str(tmp_path / "li.db"))
    counts = store.add([
        _rec(),                                         # inserted (gc fold)
        _rec(url="https://www.linkedin.com/in/jane-smith?trk=1"),  # dup (clean form)
        _rec(url="https://www.linkedin.com/company/acme-co"),      # dropped
    ])
    assert counts == {"inserted": 1, "duplicate": 1, "dropped": 1}

    served = store.serve("", "", "", 10, "u1")
    assert len(served) == 1
    assert served[0]["trade"] == "gc"          # P1 fold at ingest
    assert served[0]["city"] == "Vancouver" and served[0]["state"] == "WA"
    assert served[0]["source_email"] == "info@acme.com"


def test_serve_trade_and_location_gates(tmp_path):
    store = LinkedInLeadsStore(db_path=str(tmp_path / "li.db"))
    store.add([
        _rec(person="A", url="https://www.linkedin.com/in/a",
             trade="General Contractor", location="Vancouver, WA"),
        _rec(person="B", url="https://www.linkedin.com/in/b",
             trade="Painting/Wallcovering", location="Austin, TX"),
        _rec(person="C", url="https://www.linkedin.com/in/c",
             trade="", location="Vancouver, WA"),
    ])
    assert [l["person_name"] for l in store.serve("gc", "", "", 10, "u1")] == ["A"]
    assert [l["person_name"] for l in store.serve("painting", "", "", 10, "u2")] == ["B"]
    assert [l["person_name"] for l in store.serve("", "WA", "", 10, "u1")] == ["A", "C"]
    # Unknown-trade rows never serve to a trade-filtered run (P2 rule).
    assert store.serve("gc", "", "", 10, "u3") == []


def test_serve_is_exclusive_across_users(tmp_path):
    store = LinkedInLeadsStore(db_path=str(tmp_path / "li.db"))
    store.add([
        _rec(person="A", url="https://www.linkedin.com/in/a"),
        _rec(person="B", url="https://www.linkedin.com/in/b"),
    ])
    alice = store.serve("", "", "", 1, "alice")
    assert len(alice) == 1
    bob = store.serve("", "", "", 10, "bob")
    assert [l["id"] for l in bob] != [alice[0]["id"]] and len(bob) == 1
    # Alice's re-search still serves her own lead back; carol gets nothing.
    assert [l["id"] for l in store.serve("", "", "", 10, "alice")] == [alice[0]["id"]]
    assert store.serve("", "", "", 10, "carol") == []
    assert store.unclaimed_count("") == 0


def test_list_owned_and_pool_stats(tmp_path):
    store = LinkedInLeadsStore(db_path=str(tmp_path / "li.db"))
    store.add([
        _rec(person="A", url="https://www.linkedin.com/in/a"),
        _rec(person="B", url="https://www.linkedin.com/in/b",
             trade="Painting"),
    ])
    store.serve("gc", "", "", 10, "alice")
    mine = store.list_owned("alice")
    assert len(mine) == 1 and mine[0]["trade"] == "gc"
    assert store.list_owned("bob") == []
    stats = store.pool_stats()
    assert stats["total"] == 2 and stats["claimed"] == 1
    assert stats["unclaimed"] == 1 and stats["by_trade"]["gc"] == 1

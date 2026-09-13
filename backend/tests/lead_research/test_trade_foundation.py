"""Phase P1 wiring tests — the trade label survives EVERY seam it must
cross to reach the serve path:

    discovery record (trade_category)
        -> pipeline lead dict (``trade`` key, folded via tradefold)   [leak #2 fix]
        -> PendingLeadsStore.add / take / get (``trade`` column)
    researched dossier (company.industry)
        -> dossiers.trade (research evidence, refreshed on re-research)

P1 only CARRIES the data; P2's take(trade=...) gate is the next phase.
"""

from __future__ import annotations

from app.lead_research.models import LeadDossier
from app.lead_research.service import LeadResearchStore, PendingLeadsStore
from app.leads.pipeline import company_records_to_leads, extract_email_leads


def _plan_record(company: str, email: str, domain: str, trade: str) -> dict:
    return {
        "company_name": company,
        "source_url": "https://x.example",
        "trade_category": trade,
        "plan_holder": {
            "domain": domain,
            "emails": [{"email": email}],
            "person": {"name": ""},
        },
    }


def _website_record(company: str, website: str, trade: str) -> dict:
    return {
        "company_name": company,
        "website": website,
        "source_url": website,
        "trade_category": trade,
        "city": "Dallas",
        "state": "TX",
    }


# ---------------------------------------------------------------------------
# Pipeline threading (the leak #2 fix: trade_category used to be DROPPED)
# ---------------------------------------------------------------------------

def test_extract_email_leads_carries_folded_trade():
    leads = extract_email_leads([
        _plan_record("A Drywall Co", "a@x.com", "x.com", "Drywall Contractor"),
        _plan_record("B Masonry", "b@y.com", "y.com", "Masonry"),
    ])
    by_email = {l["email"]: l["trade"] for l in leads}
    assert by_email["a@x.com"] == "drywall"
    # Unsold/unknown source label folds honestly to '' — never a guess.
    assert by_email["b@y.com"] == ""


def test_website_lane_lead_carries_folded_trade():
    def fake_discover(url):
        return {"emails": ["info@" + url.split("//", 1)[1]]}

    leads, stats = company_records_to_leads(
        [_website_record("Sparky Electric", "https://sparky.example", "C-10")],
        email_discover=fake_discover,
    )
    assert stats["with_email"] == 1
    assert leads[0]["trade"] == "electrical"


def test_missing_trade_category_is_honest_empty():
    leads = extract_email_leads([_plan_record("A", "a@x.com", "x.com", "")])
    assert leads[0]["trade"] == ""


# ---------------------------------------------------------------------------
# PendingLeadsStore — add / take / get thread the trade column
# ---------------------------------------------------------------------------

def test_pending_add_take_get_roundtrip_trade(tmp_path):
    pending = PendingLeadsStore(db_path=str(tmp_path / "p.db"))
    pending.add([
        {"email": "dry@x.com", "domain": "x.com", "trade": "drywall"},
        {"email": "unk@y.com", "domain": "y.com", "trade": ""},
    ])
    assert pending.get("dry@x.com")["trade"] == "drywall"
    assert pending.get("unk@y.com")["trade"] == ""
    taken = pending.take(10)
    by_email = {l["email"]: l["trade"] for l in taken}
    assert by_email["dry@x.com"] == "drywall"
    assert by_email["unk@y.com"] == ""


def test_pending_backfill_folds_company_name(tmp_path):
    """A pre-P1 row (no trade in the lead dict) is lazily backfilled from
    its company NAME at the next store boot — weak evidence, clear matches
    only."""
    db = str(tmp_path / "p.db")
    pending = PendingLeadsStore(db_path=db)
    pending.add([
        {"email": "a@x.com", "domain": "x.com", "company": "AAA Drywall Inc"},
        {"email": "b@y.com", "domain": "y.com", "company": "Vague Holdings"},
    ])
    # Simulate pre-P1 rows: wipe the trade the add() just wrote (the column
    # did not exist for them).
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("UPDATE pending_leads SET trade = ''")
    conn.commit()
    conn.close()

    reloaded = PendingLeadsStore(db_path=db)  # boot = lazy backfill
    assert reloaded.get("a@x.com")["trade"] == "drywall"
    # Vague name folds honestly to '' — no guess.
    assert reloaded.get("b@y.com")["trade"] == ""


def test_pending_take_location_filter_still_works_with_trade(tmp_path):
    pending = PendingLeadsStore(db_path=str(tmp_path / "p.db"))
    pending.add([
        {"email": "tx@x.com", "domain": "x.com", "location": "Texas",
         "trade": "roofing"},
        {"email": "ca@y.com", "domain": "y.com", "location": "California",
         "trade": "electrical"},
    ])
    taken = pending.take(10, location="Texas")
    assert [l["email"] for l in taken] == ["tx@x.com"]
    assert taken[0]["trade"] == "roofing"


# ---------------------------------------------------------------------------
# Dossiers — researched industry string folds into the trade column
# ---------------------------------------------------------------------------

def _dossier(email: str, industry: str) -> LeadDossier:
    d = LeadDossier(email=email, domain=email.split("@", 1)[1])
    d.company.industry = industry
    return d


def test_dossier_save_folds_industry_to_trade(tmp_path):
    store = LeadResearchStore(str(tmp_path / "t.db"))
    store.save(_dossier("a@x.com", "Drywall Contractor"), user_id="alice")
    import sqlite3
    row = sqlite3.connect(str(tmp_path / "t.db")).execute(
        "SELECT trade FROM dossiers WHERE email = 'a@x.com'"
    ).fetchone()
    assert row[0] == "drywall"


def test_dossier_resave_refreshes_trade_from_new_evidence(tmp_path):
    """Re-research with a DIFFERENT industry string updates the trade —
    research evidence wins, stale discovery labels do not survive."""
    db = str(tmp_path / "t.db")
    store = LeadResearchStore(db)
    store.save(_dossier("a@x.com", "General Contractor"), user_id="alice")
    store.save(_dossier("a@x.com", "Roofing Contractor"), user_id="alice")
    import sqlite3
    row = sqlite3.connect(db).execute(
        "SELECT trade FROM dossiers WHERE email = 'a@x.com'"
    ).fetchone()
    assert row[0] == "roofing"


def test_dossier_unknown_industry_stays_empty(tmp_path):
    store = LeadResearchStore(str(tmp_path / "t.db"))
    store.save(_dossier("a@x.com", "Bakery"), user_id="alice")
    import sqlite3
    row = sqlite3.connect(str(tmp_path / "t.db")).execute(
        "SELECT trade FROM dossiers WHERE email = 'a@x.com'"
    ).fetchone()
    assert row[0] == ""


def test_dossier_backfill_folds_industry(tmp_path):
    """A pre-P1 dossier row is lazily backfilled at boot from the industry
    string it already carries in dossier_json."""
    db = str(tmp_path / "t.db")
    store = LeadResearchStore(db)
    store.save(_dossier("a@x.com", "Electrical Contractor"), user_id="alice")
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("UPDATE dossiers SET trade = ''")
    conn.commit()
    conn.close()

    LeadResearchStore(db)  # boot = lazy backfill
    row = sqlite3.connect(db).execute(
        "SELECT trade FROM dossiers WHERE email = 'a@x.com'"
    ).fetchone()
    assert row[0] == "electrical"

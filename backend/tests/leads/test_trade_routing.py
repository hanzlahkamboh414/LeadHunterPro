"""Phase P2 — trade routing tests.

The user's complaint this fixes: "agr user drywall ka data mang rha ha to
sirf drywall k related hi data show ho" — a Drywall search used to show GC
companies. P2 adds the SERVE-side gate on top of P1's data plumbing:

* ``PendingLeadsStore.take(trade=...)`` — only that trade's rows are served;
  other-trade rows stay banked in their own pools for their own consumers;
  '' (honest unknown) rows never serve to a trade-filtered run;
* the pipeline folds the SEARCHED trade once (``normalize_trade``) and gates
  BOTH serve paths (cache take + fresh discovery serve) with it — while ALL
  fresh discoveries are still stocked (other-trade leads are pool currency,
  never a loss);
* fail-open: a search whose trade folds to '' (custom free text outside the
  14) keeps the old unfiltered behavior — a labeling gap never starves a run.
"""

from __future__ import annotations

from app.discovery.sources.status import SourceStatus
from app.lead_research.service import LeadResearchStore, PendingLeadsStore
from app.leads.pipeline import (
    ResearchQuery,
    _fold_record_trade,
    discover_until_target,
)


def _rec(company: str, email: str, domain: str, trade_category: str) -> dict:
    """A plan-holder discovery record with an explicit trade label."""
    return {
        "company_name": company,
        "source_url": "https://x.example",
        "trade_category": trade_category,
        "plan_holder": {
            "domain": domain,
            "emails": [{"email": email}],
            "person": {"name": ""},
        },
    }


# ---------------------------------------------------------------------------
# Record fold — company-name fallback (same evidence as the P1 backfill)
# ---------------------------------------------------------------------------

def test_fold_record_trade_prefers_the_label():
    assert _fold_record_trade(_rec("AAA", "a@x.com", "x.com", "C-9")) == "drywall"


def test_fold_record_trade_falls_back_to_company_name():
    """A plan-holder record carries no classifier label — its NAME is the
    only trade evidence, the same weak evidence the P1 boot backfill uses
    (a row's trade must be identical fresh or backfilled)."""
    rec = _rec("AAA Drywall Inc", "a@x.com", "x.com", "")
    assert _fold_record_trade(rec) == "drywall"


def test_fold_record_trade_honest_unknown():
    assert _fold_record_trade(_rec("Vague Holdings", "a@x.com", "x.com", "")) == ""


# ---------------------------------------------------------------------------
# PendingLeadsStore.take(trade=...) — the serve gate
# ---------------------------------------------------------------------------

def test_take_trade_gate_serves_only_that_trade(tmp_path):
    pending = PendingLeadsStore(db_path=str(tmp_path / "p.db"))
    pending.add([
        {"email": "dw@x.com", "domain": "x.com", "trade": "drywall"},
        {"email": "gc@y.com", "domain": "y.com", "trade": "gc"},
        {"email": "unk@z.com", "domain": "z.com", "trade": ""},
    ])
    taken = pending.take(10, trade="drywall")
    assert [l["email"] for l in taken] == ["dw@x.com"]


def test_take_trade_gate_excludes_unknown_rows(tmp_path):
    """'' is an honest unknown, not a match for anything — serving it to a
    trade-filtered run would be the exact cross-trade leak."""
    pending = PendingLeadsStore(db_path=str(tmp_path / "p.db"))
    pending.add([{"email": "unk@z.com", "domain": "z.com", "trade": ""}])
    assert pending.take(10, trade="drywall") == []
    # ...but the no-gate call (searched trade didn't fold) still serves it.
    assert [l["email"] for l in pending.take(10)] == ["unk@z.com"]


def test_take_trade_gate_composes_with_location(tmp_path):
    pending = PendingLeadsStore(db_path=str(tmp_path / "p.db"))
    pending.add([
        {"email": "tx@x.com", "domain": "x.com", "location": "Texas",
         "trade": "roofing"},
        {"email": "ca@y.com", "domain": "y.com", "location": "California",
         "trade": "roofing"},
        {"email": "tx2@z.com", "domain": "z.com", "location": "Texas",
         "trade": "electrical"},
    ])
    taken = pending.take(10, location="Texas", trade="roofing")
    assert [l["email"] for l in taken] == ["tx@x.com"]


def test_take_trade_gate_default_is_no_gate(tmp_path):
    """Backward-compatible default: no trade argument = unfiltered serve."""
    pending = PendingLeadsStore(db_path=str(tmp_path / "p.db"))
    pending.add([
        {"email": "dw@x.com", "domain": "x.com", "trade": "drywall"},
        {"email": "gc@y.com", "domain": "y.com", "trade": "gc"},
    ])
    assert len(pending.take(10)) == 2


# ---------------------------------------------------------------------------
# discover_until_target — cache serve + fresh discovery gate
# ---------------------------------------------------------------------------

def _stores(tmp_path):
    db = str(tmp_path / "leads.db")
    return db, LeadResearchStore(db_path=db), PendingLeadsStore(db_path=db)


def test_cache_serves_only_searched_trade(tmp_path, monkeypatch):
    """The leak's cache half: a DryWall query must not serve the GC rows
    sitting in the same pending pool."""
    db, store, pending = _stores(tmp_path)

    def _no_live_search(trade, location, limit, skip_pdfs=None,
                        yield_store=None, candidate_store=None):
        raise AssertionError("cache has a servable lead — no live search needed")

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _no_live_search)
    pending.add([
        {"email": "dw@x.com", "domain": "x.com", "location": "Texas",
         "trade": "drywall"},
        {"email": "gc@y.com", "domain": "y.com", "location": "Texas",
         "trade": "gc"},
        {"email": "unk@z.com", "domain": "z.com", "location": "Texas",
         "trade": ""},
    ])
    query = ResearchQuery(trade="DryWall", location="Texas", target_emails=1)
    leads, _ = discover_until_target(
        query, pending_store=pending, dossier_store=store, max_passes=1,
    )
    assert [l["email"] for l in leads] == ["dw@x.com"]


def test_fresh_discovery_serves_searched_trade_banks_others(
        tmp_path, monkeypatch):
    """The leak's discovery half: a DryWall search that surfaces GC and
    unlabeled companies shows ONLY the drywall lead — but the others are
    BANKED into their pools, so a later GC search serves them instantly."""
    db, store, pending = _stores(tmp_path)

    records = [
        _rec("AAA Drywall", "dw@x.com", "x.com", "Drywall Contractor"),
        _rec("Big GC", "gc@y.com", "y.com", "general_contractor"),
        _rec("Vague", "unk@z.com", "z.com", ""),
    ]

    def _discover(trade, location, limit, skip_pdfs=None, yield_store=None,
                  candidate_store=None):
        return SourceStatus.SUCCESS, records, {"pdf_urls": ["https://p/1.pdf"]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)
    query = ResearchQuery(trade="DryWall", location="Texas", target_emails=5)
    leads, pass_log = discover_until_target(
        query, pending_store=pending, dossier_store=store, max_passes=1,
    )

    # ONLY the drywall lead served (purity — the user's complaint).
    assert [l["email"] for l in leads] == ["dw@x.com"]
    # The pass log says honestly where the rest went.
    assert pass_log[0]["new_leads"] == 1
    assert pass_log[0]["trade_stocked_other"] == 2
    # ...and the pools: the GC lead is banked with its trade, ready for the
    # next GC consumer; the unknown lead is banked as ''.
    assert pending.get("gc@y.com")["trade"] == "gc"
    assert pending.get("unk@z.com")["trade"] == ""

    # A GC search now serves the banked GC lead from the pool — no leak, no
    # waste (the drywall search's "wrong trade" data became GC inventory).
    def _no_live_search(trade, location, limit, skip_pdfs=None,
                        yield_store=None, candidate_store=None):
        raise AssertionError("pool has a servable GC lead — no search needed")

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _no_live_search)
    gc_query = ResearchQuery(trade="GC", location="Texas", target_emails=1)
    gc_leads, _ = discover_until_target(
        gc_query, pending_store=pending, dossier_store=store, max_passes=1,
    )
    assert [l["email"] for l in gc_leads] == ["gc@y.com"]


def test_unfoldable_search_trade_fails_open(tmp_path, monkeypatch):
    """A searched trade that folds to '' (custom free text, e.g. "Paving")
    keeps the OLD unfiltered behavior — the gate must never starve a run on
    a labeling gap (fail-open, like every gate here)."""
    db, store, pending = _stores(tmp_path)
    records = [
        _rec("AAA Drywall", "dw@x.com", "x.com", "Drywall Contractor"),
        _rec("Big GC", "gc@y.com", "y.com", "general_contractor"),
        _rec("Vague", "unk@z.com", "z.com", ""),
    ]

    def _discover(trade, location, limit, skip_pdfs=None, yield_store=None,
                  candidate_store=None):
        return SourceStatus.SUCCESS, records, {"pdf_urls": ["https://p/1.pdf"]}

    monkeypatch.setattr("app.leads.pipeline.run_discovery", _discover)
    query = ResearchQuery(trade="Paving", location="Texas", target_emails=5)
    leads, _ = discover_until_target(
        query, pending_store=pending, dossier_store=store, max_passes=1,
    )
    assert {l["email"] for l in leads} == {"dw@x.com", "gc@y.com", "unk@z.com"}


def test_searched_trade_label_folds_from_founder_spellings():
    """The Execute screen sends the founder's exact TRADES spellings — every
    one of the 14 must fold to its canonical slug so the gate engages."""
    from app.discovery.tradefold import normalize_trade

    for label, slug in [
        ("GC", "gc"), ("Electrical", "electrical"), ("Mechanical", "mechanical"),
        ("Plumbing", "plumbing"), ("Lumber", "lumber"), ("Demolition", "demolition"),
        ("MEP", "mep"), ("DryWall", "drywall"), ("Roofing", "roofing"),
        ("Concrete", "concrete"), ("LandScaping", "landscaping"),
        ("Flooring", "flooring"), ("Painting", "painting"),
        ("Finishes", "finishes"),
    ]:
        assert normalize_trade(label) == slug, label

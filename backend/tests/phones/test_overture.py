"""Overture connector contracts (P8) — hermetic: REAL DuckDB + a local
test Parquet file standing in for the S3 places theme, so the sync SQL
(unnest, cleaning, the phone GROUP BY, arg_min provenance pairing) is
exercised for real without network. The S3 seam is `source_glob`/`s3_con`;
stage 0 is `overture_fn` — both injectable, both used here.
"""

from __future__ import annotations

import duckdb
import pytest

from app.phones.enrich import enrich_lead
from app.phones.overture import OvertureStore, _latest_release


@pytest.fixture()
def places_parquet(tmp_path):
    """A miniature places theme: the shapes that matter (shared phone,
    formatted phone, no email, junk email, no-@ email, multi-email)."""
    path = str(tmp_path / "places.parquet")
    con = duckdb.connect()
    con.execute(f"""
        COPY (
            SELECT * FROM (VALUES
                -- two places share a phone: min(email) wins, and the
                -- website must pair with THAT email (arg_min), never mix
                (['+15125550001']::VARCHAR[], ['info@acme.com']::VARCHAR[],
                 ['https://acme.com']::VARCHAR[]),
                (['+15125550001']::VARCHAR[], ['sales@zzz.com']::VARCHAR[],
                 ['https://zzz.com']::VARCHAR[]),
                -- a formatted number is cleaned to bare digits
                (['(214) 555-0002']::VARCHAR[], ['Contact@Builder.COM']::VARCHAR[],
                 ['https://builder.com']::VARCHAR[]),
                -- no email: never a row in place_contacts
                (['+15125550003']::VARCHAR[], NULL, NULL),
                -- junk email: synced, but dropped at LOOKUP (§14 reuse)
                (['+15125550004']::VARCHAR[], ['noreply@example.com']::VARCHAR[],
                 NULL),
                -- no @: not an email, dropped at sync
                (['+15125550005']::VARCHAR[], ['not-an-email']::VARCHAR[], NULL),
                -- a place with several emails contributes its first
                (['+15125550006']::VARCHAR[], ['a@x.com', 'b@x.com']::VARCHAR[],
                 ['https://x.com']::VARCHAR[])
            ) AS t(phones, emails, websites)
        ) TO '{path}' (FORMAT PARQUET)
    """)
    con.close()
    return path


@pytest.fixture()
def synced_store(tmp_path, places_parquet):
    store = OvertureStore(db_path=str(tmp_path / "overture.duckdb"))
    store.sync(source_glob=places_parquet)
    return store


def test_sync_materializes_the_phone_email_join(synced_store):
    """Only email-bearing places land in the table; the release is recorded."""
    assert synced_store.is_synced()
    assert synced_store.release() == "(test source)"
    hits = synced_store.lookup_emails([
        "+15125550001", "2145550002", "+15125550003",
        "+15125550005", "+15125550006",
    ])
    # 00001 (shared, min email), 0002 (cleaned), 00006 — the no-email,
    # no-@ rows are absent.
    assert set(hits) == {"+15125550001", "2145550002", "+15125550006"}


def test_lookup_pairs_email_with_its_own_website(synced_store):
    """The shared phone's picked email carries ITS website, not the other
    place's — provenance pairing, never a mix."""
    hit = synced_store.lookup_emails(["+15125550001"])["+15125550001"]
    assert hit["email"] == "info@acme.com"
    assert hit["website"] == "https://acme.com"


def test_lookup_normalizes_and_lowercases(synced_store):
    hit = synced_store.lookup_emails(["2145550002"])["2145550002"]
    assert hit["email"] == "contact@builder.com"
    assert hit["website"] == "https://builder.com"


def test_lookup_drops_junk_emails(synced_store):
    """Overture's example.com placeholder is as worthless as a scraped one
    — the crawl's own furniture rules apply (§14)."""
    assert synced_store.lookup_emails(["+15125550004"]) == {}


def test_lookup_before_sync_is_honest_empty(tmp_path):
    store = OvertureStore(db_path=str(tmp_path / "never.duckdb"))
    assert not store.is_synced()
    assert store.release() == ""
    assert store.lookup_emails(["+15125550001"]) == {}


def test_lookup_ignores_blanks_and_dedups(synced_store):
    hits = synced_store.lookup_emails(
        ["+15125550001", "+15125550001", "", "  ", None])  # type: ignore[list-item]
    assert set(hits) == {"+15125550001"}


class _FakeCon:
    """The _latest_release seam: just execute().fetchall(). The regex and
    the max() live in the SQL, so the fake answers what that SQL returns —
    one row with the already-maxed release name."""

    def __init__(self, answer):
        self._answer = answer

    def execute(self, sql):  # noqa: ARG002 — the SQL is fixed in the module
        return self

    def fetchall(self):
        return self._answer


def test_latest_release_reads_the_glob_answer():
    assert _latest_release(_FakeCon([("2026-08-19.0",)])) == "2026-08-19.0"
    assert _latest_release(_FakeCon([("",)])) == ""  # glob matched nothing
    assert _latest_release(_FakeCon([])) == ""  # no rows at all


# -- stage 0: the enrich worker's Overture shortcut --------------------------


def _lead(**kw):
    return {
        "business_name": "Acme GC", "person_name": "Jane Smith",
        "city": "VANCOUVER", "state": "WA", "trade": "gc",
        "phone": "+15125550001", **kw,
    }


def test_stage0_hit_answers_without_spending_a_search():
    """An Overture hit short-circuits the whole pipeline — no search, no
    crawl, no credits (the free dataset answers first)."""
    out = enrich_lead(
        _lead(),
        overture_fn=lambda p: {
            "email": "info@acme.com", "website": "https://acme.com"},
        search_fn=lambda q, n: pytest.fail("stage 0 hit must not search"),
    )
    assert out == {"email": "info@acme.com", "email_source": "overture",
                   "website": "https://acme.com", "dork": "overture"}


def test_stage0_works_even_without_a_business_name():
    """The join is phone-keyed — a lead whose business name is missing can
    still get its email, where the crawl stage could not even start."""
    out = enrich_lead(
        _lead(business_name=""),
        overture_fn=lambda p: {"email": "info@acme.com", "website": ""},
    )
    assert out["email"] == "info@acme.com"
    assert out["email_source"] == "overture"


def test_stage0_miss_falls_through_to_the_normal_pipeline():
    """No Overture row (or no local sync) changes nothing: the web stages
    run exactly as before."""
    out = enrich_lead(
        _lead(),
        overture_fn=lambda p: {},
        search_fn=lambda q, n: ["https://acmegc.com"],
        fetch_fn=lambda url, **kw: type("P", (), {
            "ok": True, "text": "<p>info@acmegc.com</p>"})(),
    )
    assert out["email"] == "info@acmegc.com"
    assert out["email_source"] == "website"
    assert out["dork"] == "phone_enrichment"


def test_stage0_junk_email_is_not_a_hit():
    """A dataset email that fails the furniture rules is a MISS — stage 0
    falls through rather than answering with a placeholder."""
    out = enrich_lead(
        _lead(),
        overture_fn=lambda p: {"email": "noreply@example.com", "website": ""},
        search_fn=lambda q, n: [],
    )
    assert out["email"] == ""


def test_default_overture_fn_is_safe_without_duckdb_or_sync(monkeypatch):
    """The production default must never raise, whatever the environment
    does — an unavailable Overture is an honest empty dict."""
    import app.phones.overture as overture_module

    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("duckdb exploded")

    monkeypatch.setattr(overture_module, "OvertureStore", _Boom)
    from app.phones.enrich import _default_overture
    assert _default_overture("+15125550001") == {}

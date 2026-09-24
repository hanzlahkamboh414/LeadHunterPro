"""The TDLR branch of ``purge_crawl_artifacts.py`` must not delete valid rows.

Regression guard for the 2026-09-22 incident: the sweep deleted ``phone_leads``
TDLR rows matching ``state != 'TX'``. After the 2026-09-15 parser fix that
predicate selected the CORRECTLY parsed rows — TDLR licenses contractors whose
mailing address is in another state, and 31 such rows (LA, TN, OK, AR, NM, MT,
KY, CO, VA, NC, ID, DE) were deleted and had to be restored from snapshot.

A parse FRAGMENT is a different thing from an out-of-state code. The old
parser split multi-word cities wrongly, storing two-letter CITY fragments as
the state ("AN" out of SAN ANTONIO, "CH" out of CORPUS CHRISTI, "EL" out of
EL PASO). The gate must be membership in the USPS set the parser itself
validates against — never an equality test against ``TX``, which cannot tell
those two cases apart.

The predicate is exercised against a real SQLite table because the SQL is
where the defect lived; asserting on the constant alone would not have caught
it.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from app.phones import soda

#: Loaded by path: ``scripts/`` has no ``__init__.py``, so it is not an
#: importable package, and this must not depend on the caller's cwd.
_SWEEP_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "purge_crawl_artifacts.py"
)


def _load_sweep():
    spec = importlib.util.spec_from_file_location("_purge_sweep", _SWEEP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {_SWEEP_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sweep = _load_sweep()

#: The real ``phone_leads`` DDL (``app/phones/store.py:132``).
_DDL = """
CREATE TABLE phone_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    person_name TEXT NOT NULL DEFAULT '',
    business_name TEXT NOT NULL DEFAULT '',
    trade TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    license_status TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    email_source TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    enriched_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (phone, business_name)
)
"""

TDLR_URL = "https://data.texas.gov/resource/7358-krk7.json"
WA_URL = "https://data.wa.gov/resource/m8qx-ubtq.json"

#: (phone, business_name, city, state, source_url, expected_to_be_selected)
_ROWS = [
    # Correctly parsed Texas rows — never touched.
    ("5125637173", "INFINITE POWER LLC", "BUDA", "TX", TDLR_URL, False),
    # The incident: a valid out-of-state code paired with a real city. TDLR
    # licenses these companies; they hold a Texas licence and can work in
    # Texas. ``state != 'TX'`` deleted every one of them.
    ("3182221111", "ARKLA ELECTRIC", "SHREVEPORT", "LA", TDLR_URL, False),
    ("9185551234", "TULSA MECHANICAL", "TULSA", "OK", TDLR_URL, False),
    ("6155559876", "MUSIC CITY AIR", "NASHVILLE", "TN", TDLR_URL, False),
    ("2145554321", "BORDER PLUMBING", "TEXARKANA", "AR", TDLR_URL, False),
    # Genuine parse fragments from the OLD parser — the only rows this branch
    # is for. A fragment is never a USPS code, so it can never come back from
    # ``_parse_tdlr_row``.
    ("5129998888", "SAN ANTONIO CO", "SAN", "AN", TDLR_URL, True),
    ("5127776666", "CORPUS CHRISTI CO", "CORPUS CHRISTI", "CH", TDLR_URL, True),
    ("5124443333", "EL PASO CO", "EL", "EL", TDLR_URL, True),
    # Blank state is "not known yet", not corruption — the ``_is_junk``
    # discipline for an empty email. Deleting on missing data is exactly the
    # over-reach that caused the incident.
    ("5121112222", "AUSTIN CO", "AUSTIN", "", TDLR_URL, False),
    # A different source with a valid out-of-state code: outside this branch
    # entirely, and a reminder the criterion is scoped by source_url.
    ("5039573452", "PORTLAND ELEC", "PORTLAND", "OR", WA_URL, False),
    # A non-TDLR row with a fragment-looking state — also outside the branch.
    ("2065551234", "SEATTLE CO", "SEATTLE", "XX", WA_URL, False),
]


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_DDL)
    conn.executemany(
        "INSERT INTO phone_leads "
        "(phone, business_name, city, state, source_url, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, '', '')",
        [(p, b, c, s, u) for p, b, c, s, u, _ in _ROWS],
    )
    return conn


def _empty_db():
    """The real schema with NO rows.

    The noop test must isolate what the CURRENT parser produces. Seeding it
    from ``_ROWS`` would re-introduce the old-parser fragments the other
    tests deliberately pin, and the assertion would then fail on fixture
    data instead of on parser output — a test that cannot tell the two
    apart is not measuring the fix.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_DDL)
    return conn


def _select_fragments(conn):
    """The sweep's own SELECT, parameterised the way the sweep does it."""
    return conn.execute(
        "SELECT business_name, city, state FROM phone_leads "
        f"WHERE {sweep._TDLR_FRAGMENT_WHERE}",
        (sweep.TDLR_RESOURCE, *sweep._USPS_STATES),
    ).fetchall()


def test_state_table_is_the_parsers_own():
    """The gate must read the table ``_parse_tdlr_row`` validates against.

    A second copy of the USPS list would drift from the parser and start
    mis-classifying rows as fragments again — the defect's own mechanism.
    """
    from app.engines.verification.location_verifier import _US_STATES

    assert frozenset(sweep._USPS_STATES) == frozenset(_US_STATES)
    assert "TX" in sweep._USPS_STATES


def test_selects_only_real_parse_fragments():
    conn = _make_db()
    try:
        selected = {row["state"] for row in _select_fragments(conn)}
    finally:
        conn.close()
    assert selected == {"AN", "CH", "EL"}


def test_valid_out_of_state_rows_are_never_selected():
    """The incident, pinned: LA/OK/TN/AR are not fragments and must survive."""
    conn = _make_db()
    try:
        names = {row["business_name"] for row in _select_fragments(conn)}
    finally:
        conn.close()
    for survivor in (
        "ARKLA ELECTRIC", "TULSA MECHANICAL", "MUSIC CITY AIR",
        "BORDER PLUMBING", "INFINITE POWER LLC",
    ):
        assert survivor not in names


def test_blank_state_is_left_alone():
    conn = _make_db()
    try:
        names = {row["business_name"] for row in _select_fragments(conn)}
    finally:
        conn.close()
    assert "AUSTIN CO" not in names


def test_predicate_is_a_noop_on_current_parser_output():
    """Post-fix data cannot be selected, BY CONSTRUCTION.

    Feeds the CURRENT parser the exact inputs that produced fragments under
    the old one (multi-word Texas cities, an out-of-state mailing address)
    and asserts the sweep would delete none of the results. This is the
    property that breaks the delete-then-restock-again loop.
    """
    base = {
        "business_telephone": "5125637173",
        "owner_name": "INFINITE POWER LLC",
        "business_name": "INFINITE POWER LLC",
        "license_type": "ELECTRICAL CONTRACTOR",
        "license_expiration_date_mmdyyyy": "10/06/2026",
    }
    city_state_zip = [
        "SAN ANTONIO TX 78204",
        "CORPUS CHRISTI, TX 78401",
        "EL PASO TX 79901",
        "AUSTIN 78701",
        "SHREVEPORT LA 71101",
        "TULSA OK 74103",
        "NASHVILLE TN 37201",
        "TEXARKANA AR 71854",
        "",
    ]
    conn = _empty_db()
    try:
        for index, cz in enumerate(city_state_zip):
            rec = soda._parse_tdlr_row(
                dict(base, business_city_state_zip=cz)
            )
            # A distinct phone per row: ``UNIQUE (phone, business_name)`` is
            # real schema and a fixture must not sidestep it.
            conn.execute(
                "INSERT INTO phone_leads "
                "(phone, business_name, city, state, source_url, "
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, '', '')",
                (
                    f"5125637{index:03d}", f"PARSED {index}", rec["city"],
                    rec["state"], rec["source_url"],
                ),
            )
        selected = _select_fragments(conn)
    finally:
        conn.close()
    assert selected == [], (
        "the current parser cannot produce a state outside the USPS set, so "
        f"this branch must select nothing — got {[dict(r) for r in selected]}"
    )

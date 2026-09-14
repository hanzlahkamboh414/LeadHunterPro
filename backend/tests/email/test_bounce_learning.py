"""P5-Lite bounce-learning store contracts (tmp SQLite — no real
outcomes, no live anything). The honesty core: only REAL events create
rows, the latest fact wins, and nothing is ever inferred here.
"""

from __future__ import annotations

from app.email.bounce_learning import BounceStore


def test_record_and_lookup_roundtrip(tmp_path):
    store = BounceStore(db_path=str(tmp_path / "outcomes.db"))
    assert store.lookup("Jane@Acme.com ") is None  # case/space normalized
    assert store.record("jane@acme.com", "delivered", "reply: hi") is True
    assert store.lookup("JANE@ACME.COM") == "delivered"
    store.close()


def test_bad_outcome_word_is_rejected(tmp_path):
    store = BounceStore(db_path=str(tmp_path / "outcomes.db"))
    assert store.record("jane@acme.com", "maybe") is False
    assert store.lookup("jane@acme.com") is None  # nothing written
    store.close()


def test_latest_fact_wins(tmp_path):
    """Delivered then bounced (mailbox closed since) — today it is dead,
    with the newest evidence kept."""
    store = BounceStore(db_path=str(tmp_path / "outcomes.db"))
    store.record("jane@acme.com", "delivered", "reply in May")
    store.record("jane@acme.com", "bounced", "DSN in September")
    assert store.lookup("jane@acme.com") == "bounced"
    store.close()


def test_one_row_per_email(tmp_path):
    store = BounceStore(db_path=str(tmp_path / "outcomes.db"))
    for _ in range(3):
        store.record("jane@acme.com", "bounced", "DSN")
    assert store.counts() == {"bounced": 1}
    store.close()


def test_counts_by_outcome(tmp_path):
    store = BounceStore(db_path=str(tmp_path / "outcomes.db"))
    store.record("a@acme.com", "bounced")
    store.record("b@acme.com", "delivered")
    store.record("c@acme.com", "delivered")
    assert store.counts() == {"bounced": 1, "delivered": 2}
    store.close()


def test_persistence_across_reopen(tmp_path):
    path = str(tmp_path / "outcomes.db")
    store = BounceStore(db_path=path)
    store.record("jane@acme.com", "bounced", "DSN")
    store.close()
    again = BounceStore(db_path=path)
    assert again.lookup("jane@acme.com") == "bounced"
    again.close()


def test_invalid_email_is_rejected(tmp_path):
    store = BounceStore(db_path=str(tmp_path / "outcomes.db"))
    assert store.record("not-an-email", "bounced") is False
    assert store.counts() == {}
    store.close()

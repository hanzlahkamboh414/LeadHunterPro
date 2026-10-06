"""A delivery notice must prove that a specific mailbox is permanently invalid."""

import pytest

from app.campaigns.hard_bounce import (hard_bounce_target,
                                       invalid_recipient_reason,
                                       policy_block_target)
from app.email.bounce_learning import BounceStore


@pytest.mark.parametrize("notice", [
    "550 5.1.1 user unknown: bad@example.com",
    "550 5.7.1 <bad@example.com>: Recipient address rejected: User email address is marked as invalid",
    "550 5.2.1 The email account that you tried to reach is inactive (bad@example.com)",
])
def test_confirmed_invalid_recipient(notice):
    assert hard_bounce_target(notice, {"bad@example.com"}) == "bad@example.com"


@pytest.mark.parametrize("notice", [
    "Delivery delayed for bad@example.com: 4.2.2 mailbox full",
    "550 5.4.1 Recipient address rejected: Access denied bad@example.com",
    "554 5.7.1 Relay access denied bad@example.com",
    "550 Please turn on SMTP Authentication; bad@example.com not permitted to relay",
    "550 Group only accepts senders from inside organization bad@example.com",
])
def test_policy_or_transient_failure_does_not_delete(notice):
    assert not invalid_recipient_reason(notice)
    assert hard_bounce_target(notice, {"bad@example.com"}) is None


def test_ambiguous_notice_does_not_delete_anyone():
    notice = "550 5.1.1 bad@example.com user unknown; other@example.com also failed"
    assert hard_bounce_target(notice, {"bad@example.com", "other@example.com"}) is None


def test_gmail_policy_block_is_not_an_invalid_recipient():
    notice = ("Message blocked. Your message to chip@example.com has been "
              "blocked. The response was: Message rejected.")
    assert policy_block_target(notice, {"chip@example.com"}) == "chip@example.com"
    assert hard_bounce_target(notice, {"chip@example.com"}) is None
    assert policy_block_target(notice, {"other@example.com"}) is None


def test_bounce_store_migrates_and_only_queues_confirmed(tmp_path):
    import sqlite3

    path = tmp_path / "outcomes.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE email_outcomes (email_hash TEXT PRIMARY KEY, email TEXT NOT NULL, "
                     "domain TEXT NOT NULL, outcome TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '', "
                     "created_at TEXT NOT NULL DEFAULT (datetime('now')))" )
    store = BounceStore(str(path))
    assert store.record("policy@example.com", "bounced")
    assert store.pending_hard_bounces() == []
    assert store.record("bad@example.com", "bounced", hard_bounce=True)
    assert store.pending_hard_bounces() == ["bad@example.com"]
    store.mark_cleaned("bad@example.com")
    assert store.pending_hard_bounces() == []
    store.close()

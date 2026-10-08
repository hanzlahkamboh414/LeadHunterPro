import sqlite3
from datetime import datetime, timedelta, timezone

from app.campaigns.deliverability_guard import DeliverabilityGuard
from scripts.reconcile_delivery_holds import reconcile


NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def test_three_consecutive_bounces_or_blocks_hold_sender(tmp_path):
    guard = DeliverabilityGuard(str(tmp_path / "guard.db"))
    for send_id in (1, 2, 3):
        guard.record_send_accepted(12, send_id, now=NOW + timedelta(minutes=send_id))
    assert not guard.record_delivery_failure(12, 1)
    assert not guard.record_delivery_failure(12, 1)  # repeated notice
    assert not guard.record_delivery_failure(12, 2)
    assert guard.record_delivery_failure(12, 3)
    assert guard.is_held(12)


def test_accepted_send_breaks_streak_even_with_late_notice(tmp_path):
    guard = DeliverabilityGuard(str(tmp_path / "guard.db"))
    for send_id in (1, 2, 3, 4):
        guard.record_send_accepted(12, send_id, now=NOW + timedelta(minutes=send_id))
    assert not guard.record_delivery_failure(12, 1)
    assert not guard.record_delivery_failure(12, 2)
    assert not guard.record_delivery_failure(12, 3)  # send 4 was accepted later
    assert not guard.is_held(12)
    assert guard.record_delivery_failure(12, 4)


def test_existing_hold_is_not_cleared_by_accepted_send(tmp_path):
    guard = DeliverabilityGuard(str(tmp_path / "guard.db"))
    guard.hold_account(12, "older Gmail policy rejection", now=NOW)
    guard.record_send_accepted(12, 1, now=NOW)
    assert guard.is_held(12)


def test_legacy_one_block_hold_is_released_and_campaign_resumed(tmp_path):
    campaigns = tmp_path / "campaigns.db"
    guard_path = tmp_path / "guard.db"
    with sqlite3.connect(campaigns) as conn:
        conn.execute("CREATE TABLE campaigns (id INTEGER, account_id INTEGER, "
                     "status TEXT, paused_reason TEXT, resume_at TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE campaign_accounts (campaign_id INTEGER, account_id INTEGER)")
        conn.execute("CREATE TABLE campaign_sends (id INTEGER, campaign_id INTEGER, "
                     "account_id INTEGER, sent_at TEXT, state TEXT)")
        conn.execute("INSERT INTO campaigns VALUES (40,12,'paused','deliverability','','')")
        conn.execute("INSERT INTO campaign_accounts VALUES (40,12)")
        for send_id, state in ((1, "sent"), (2, "failed"), (3, "sent")):
            sent_at = (NOW + timedelta(minutes=send_id)).isoformat()
            conn.execute("INSERT INTO campaign_sends VALUES (?,?,?,?,?)",
                         (send_id, 40, 12, sent_at, state))
    guard = DeliverabilityGuard(str(guard_path))
    guard.hold_account(12, "Gmail blocked message as suspicious or by policy")
    preview = reconcile(str(campaigns), str(guard_path))
    assert preview["released_account_ids"] == [12]
    assert guard.is_held(12)  # preview does not release it
    assert reconcile(str(campaigns), str(guard_path), apply=True)[
        "resumed_campaign_ids"] == [40]
    assert not guard.is_held(12)
    with sqlite3.connect(campaigns) as conn:
        assert conn.execute("SELECT status,paused_reason FROM campaigns "
                            "WHERE id=40").fetchone() == ("running", "")


def test_legacy_three_blocks_keep_hold(tmp_path):
    campaigns = tmp_path / "campaigns.db"
    guard_path = tmp_path / "guard.db"
    with sqlite3.connect(campaigns) as conn:
        conn.execute("CREATE TABLE campaigns (id INTEGER, account_id INTEGER, "
                     "status TEXT, paused_reason TEXT, resume_at TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE campaign_accounts (campaign_id INTEGER, account_id INTEGER)")
        conn.execute("CREATE TABLE campaign_sends (id INTEGER, campaign_id INTEGER, "
                     "account_id INTEGER, sent_at TEXT, state TEXT)")
        conn.execute("INSERT INTO campaigns VALUES (38,27,'paused','deliverability','','')")
        conn.execute("INSERT INTO campaign_accounts VALUES (38,27)")
        for send_id in (1, 2, 3):
            conn.execute("INSERT INTO campaign_sends VALUES (?,?,?,?,?)",
                         (send_id, 38, 27,
                          (NOW + timedelta(minutes=send_id)).isoformat(), "failed"))
    guard = DeliverabilityGuard(str(guard_path))
    guard.hold_account(27, "Gmail blocked message as suspicious or by policy")
    result = reconcile(str(campaigns), str(guard_path), apply=True)
    assert result["released_account_ids"] == []
    assert result["resumed_campaign_ids"] == []
    assert guard.is_held(27)

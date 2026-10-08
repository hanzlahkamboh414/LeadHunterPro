from datetime import datetime, timezone

from app.campaigns.deliverability_guard import DeliverabilityGuard


NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def test_three_consecutive_hard_bounces_hold_sender(tmp_path):
    guard = DeliverabilityGuard(str(tmp_path / "guard.db"))
    assert not guard.record_hard_bounce(12, "first@example.com", now=NOW)
    assert not guard.record_hard_bounce(12, "first@example.com", now=NOW)  # duplicate DSN
    assert not guard.record_hard_bounce(12, "second@example.com", now=NOW)
    assert guard.record_hard_bounce(12, "third@example.com", now=NOW)
    assert guard.is_held(12)


def test_accepted_send_breaks_hard_bounce_streak(tmp_path):
    guard = DeliverabilityGuard(str(tmp_path / "guard.db"))
    assert not guard.record_hard_bounce(12, "first@example.com", now=NOW)
    guard.record_send_accepted(12, now=NOW)
    assert not guard.record_hard_bounce(12, "second@example.com", now=NOW)
    assert not guard.record_hard_bounce(12, "third@example.com", now=NOW)
    assert guard.record_hard_bounce(12, "fourth@example.com", now=NOW)


def test_provider_hold_is_not_cleared_by_accepted_send(tmp_path):
    guard = DeliverabilityGuard(str(tmp_path / "guard.db"))
    guard.hold_account(12, "Gmail policy rejection", now=NOW)
    guard.record_send_accepted(12, now=NOW)
    assert guard.is_held(12)

"""One-time migration from immediate Gmail holds to three-outcome holds.

Run with the campaign service stopped and both SQLite files backed up.
Without --apply, all writes are rolled back after printing the proposed IDs.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone

from app.campaigns.deliverability_guard import DeliverabilityGuard


LEGACY_REASONS = (
    "Gmail blocked message as suspicious or by policy",
    "two hard bounces in 24 hours",
    "three consecutive hard bounces",
)


def reconcile(campaign_db: str, guard_db: str, *, apply: bool = False) -> dict:
    DeliverabilityGuard(guard_db)  # create the new attempt table if necessary
    campaigns = sqlite3.connect(
        campaign_db if apply else f"file:{campaign_db}?mode=ro",
        uri=not apply,
    )
    guard = sqlite3.connect(guard_db)
    resumed: list[int] = []
    try:
        guard.execute("BEGIN IMMEDIATE")
        rows = campaigns.execute(
            "SELECT CASE WHEN s.account_id>0 THEN s.account_id ELSE c.account_id END, "
            "s.id, s.sent_at, s.state FROM campaign_sends s "
            "JOIN campaigns c ON c.id=s.campaign_id "
            "WHERE s.sent_at IS NOT NULL AND s.sent_at<>'' "
            "AND s.state IN ('sent','failed')"
        )
        seeded = 0
        for account_id, send_id, sent_at, state in rows:
            if account_id <= 0:
                continue
            seeded += guard.execute(
                "INSERT OR IGNORE INTO delivery_attempts "
                "(account_id,send_id,sent_at,outcome) VALUES (?,?,?,?)",
                (account_id, send_id, sent_at,
                 "failed" if state == "failed" else "accepted"),
            ).rowcount

        legacy = guard.execute(
            "SELECT account_id FROM sender_holds WHERE reason IN (?,?,?)",
            LEGACY_REASONS,
        ).fetchall()
        released = []
        for (account_id,) in legacy:
            latest = [row[0] for row in guard.execute(
                "SELECT outcome FROM delivery_attempts WHERE account_id=? "
                "ORDER BY sent_at DESC, send_id DESC LIMIT 3", (account_id,),
            )]
            if len(latest) < 3 or latest != ["failed"] * 3:
                released.append(account_id)
                guard.execute("DELETE FROM sender_holds WHERE account_id=?",
                              (account_id,))

        for (campaign_id,) in campaigns.execute(
            "SELECT id FROM campaigns WHERE status='paused' "
            "AND paused_reason='deliverability'"
        ):
            accounts = [r[0] for r in campaigns.execute(
                "SELECT account_id FROM campaign_accounts WHERE campaign_id=?",
                (campaign_id,),
            )]
            if any(account_id in released for account_id in accounts):
                resumed.append(campaign_id)

        result = {"seeded_attempts": seeded,
                  "released_account_ids": released,
                  "resumed_campaign_ids": resumed}
        if apply:
            guard.commit()
            now = datetime.now(timezone.utc).isoformat()
            campaigns.executemany(
                "UPDATE campaigns SET status='running', paused_reason='', "
                "resume_at='', updated_at=? WHERE id=? AND status='paused' "
                "AND paused_reason='deliverability'",
                [(now, campaign_id) for campaign_id in resumed],
            )
            campaigns.commit()
        else:
            guard.rollback()
        return result
    finally:
        campaigns.close()
        guard.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_db")
    parser.add_argument("guard_db")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = reconcile(args.campaign_db, args.guard_db, apply=args.apply)
    print(result)


if __name__ == "__main__":
    main()

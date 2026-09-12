"""Campaigns (Phase E3) — email outreach with a safety-first scheduler.

A campaign is: one connected Gmail account + a subject/body template + a
list of lead emails + a schedule. The scheduler (scheduler.py) drains it
SLOWLY — one email per random 3–7 minute gap, a hard daily cap per ACCOUNT,
honest pauses on Gmail 429 / revoked tokens — because the #1 way cold-email
systems die is volume: Gmail suspends accounts that blast.

Separate from ``app/email_accounts/`` (the sending ACCOUNTS) the same way
that is separate from ``app/email/`` (address DISCOVERY).
"""

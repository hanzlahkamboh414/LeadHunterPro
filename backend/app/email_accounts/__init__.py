"""Email sending accounts (Phase E2) — Gmail OAuth connections.

A separate concern from ``app/email/`` (which DISCOVERS email addresses on
websites): this package owns the user's own SENDING accounts — OAuth tokens,
their encrypted storage, and the Gmail API client used to send.
"""

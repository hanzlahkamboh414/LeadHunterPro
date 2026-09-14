"""P9 — AI Source Scout: staged-trust discovery of NEW data sources.

Every source so far is hand-coded (WA SODA, TDLR, Overture, …). The scout
automates the supply side: an AI proposes candidate license-board /
directory sources, a MECHANICAL verifier checks them (error classify,
DNS fallback, alt endpoint, IP pin, shape/volume/recency), survivors go
to agnes probation (>= 70% pass rate over verified fetches), and only
then is a source auto-promoted into the harvest coverage — no admin
approval anywhere in the loop (founder directive, big-bang plan).

This package is additive by design: it never edits an existing store.
Promoted rows are CONSUMED by the phones lane; lead data stays in the
vertical stores.
"""

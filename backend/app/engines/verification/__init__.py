"""Accuracy-first lead verification (Phase 1).

Structured containers and source-quality tiers that later phases build on:

- :mod:`models`      — ``VerificationStatus``, ``FieldEvidence``, ``LeadRecord``
- :mod:`source_tiers` — Tier 1-4 source quality model

Phase 2 adds the actual verifiers (identity/industry/location) and the
deterministic acceptance gate.
"""

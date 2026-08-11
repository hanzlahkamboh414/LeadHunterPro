"""Prototype V1 — potential-lead contract (Phase target: 15-20 qualified leads).

Structured containers and the deterministic qualification gate that later
increments populate:

- :mod:`lead_models`  — ``Lead``, honest verification tiers, ``IntentEvidence``,
  ``LeadAI``, and the threshold gate
- :mod:`lead_samples` — canonical fixture samples pinning the contract shape

The gate is THRESHOLD-ONLY for V1 (founder agreement): a lead clears at
``ai_confidence`` >= QUALIFIED_THRESHOLD (90) only when real evidence is
present. Qualifying requires ALL hard rules: AcceptanceGate-verified company +
decision-maker whose role is estimation/bidding-relevant (``role_relevance``)
+ a ``person_bound`` email (generic info@/contact@ never qualifies) + at least
one intent signal with a source URL + a written justification citing that
evidence. Phones are optional and never block.
"""
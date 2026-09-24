"""The Phase 1 hook: WHERE the evidence intake runs, and where it does not.

The intake itself is tested in ``tests/research/test_intake.py``. What these
tests pin is the placement decision, because the placement is what protects
the credits:

* it runs for a lead that SURVIVED the Stage 1c not-a-client shortcut, and
* it does NOT run for one the shortcut dropped — those three plugins make
  live network calls, and spending them on companies the pipeline has
  already decided are not buyers is the leak the pre-verdict exists to close.

The feature flag is forced OFF repo-wide (``tests/conftest.py``), so every
test here turns it back on explicitly and injects stub plugins. That is the
only way the hook can run offline.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.discovery.sources.status import SourceStatus
from app.engines.lead.lead_models import IntentEvidence, IntentEvidenceType
from app.research.store import ResearchEvidenceStore
from tests.lead_research.test_agent import _make_agent


class _StubPlugin:
    """A stub intent plugin; records whether it was called at all."""

    name = "stub"

    def __init__(self, status=SourceStatus.SUCCESS, items=None):
        self._status = status
        self._items = items if items is not None else [_evidence()]
        self.calls: list[dict[str, str]] = []

    def collect_evidence(self, *, company_name, website="", location=""):
        self.calls.append({"company_name": company_name, "website": website})
        return self._status, list(self._items), {"source": self.name}


class _ExplodingPlugin:
    name = "exploding"

    def collect_evidence(self, *, company_name, website="", location=""):
        raise RuntimeError("endpoint down")


def _evidence():
    return IntentEvidence(
        type=IntentEvidenceType.project,
        source_url="https://acme.com/projects/riverside",
        snippet="Acme broke ground on the Riverside project.",
        date="2026-09-11",
        source="company_site",
    )


@pytest.fixture()
def enabled(monkeypatch):
    """Turn the Phase 1 intake on for one test (it is OFF repo-wide)."""
    monkeypatch.setattr(settings, "INTENT_EVIDENCE_ENABLED", True)


# --- the hook runs where it should ----------------------------------------


def test_a_kept_lead_gets_its_company_evidence_collected(enabled):
    plugin = _StubPlugin()
    dossier = _make_agent(intent_evidence_plugins=[plugin]).research(
        "jane@acme.com", "acme.com"
    )

    assert plugin.calls, "the intake must run for a surviving lead"
    assert "intent_evidence" in dossier.sources_checked
    assert dossier.source_errors.get("intent_evidence") is None, (
        "evidence was stored, so nothing should be reported as an error"
    )


def test_the_evidence_lands_in_the_store_against_the_company(enabled):
    """End to end: plugin item -> canonical row in research_evidence.db."""
    dossier = _make_agent(intent_evidence_plugins=[_StubPlugin()]).research(
        "jane@acme.com", "acme.com"
    )
    store = ResearchEvidenceStore()

    company = store.companies_for_domain("acme.com")
    assert company is not None
    rows = store.evidence_for_company(company.company_id)
    assert len(rows) == 1
    assert rows[0].source_url == "https://acme.com/projects/riverside"
    assert dossier.company.name, "the research path still completed normally"


def test_verified_evidence_is_extracted_and_stored_as_an_event(enabled):
    calls: list[str] = []

    def grounded_event_ai(prompt: str) -> str:
        calls.append(prompt)
        evidence_id = prompt.split('"evidence_id": "', 1)[1].split('"', 1)[0]
        return (
            '{"events":[{"event_type":"project_announced",'
            '"project_key":"","occurred_at":"2026-09-11",'
            f'"confidence":0.9,"evidence_ids":["{evidence_id}"]}}]}}'
        )

    dossier = _make_agent(
        intent_evidence_plugins=[_StubPlugin()], event_ai_ask=grounded_event_ai
    ).research("jane@acme.com", "acme.com")
    store = ResearchEvidenceStore()
    company = store.companies_for_domain("acme.com")

    assert company is not None
    assert len(calls) == 1
    assert len(store.events_for_company(company.company_id)) == 1
    signals = store.signals_for_company(company.company_id)
    assert signals, "Phase 3 must compute signals after storing events"
    assert any(signal.signal_type.value == "project_activity" for signal in signals)
    assert "signal_scoring" in dossier.sources_checked
    assert "signal_events" in dossier.sources_checked
    assert "signal_events" not in dossier.source_errors


def test_existing_event_coverage_skips_a_second_ai_call(enabled):
    calls: list[str] = []

    def event_ai(prompt: str) -> str:
        calls.append(prompt)
        evidence_id = prompt.split('"evidence_id": "', 1)[1].split('"', 1)[0]
        return (
            '{"events":[{"event_type":"project_announced",'
            '"project_key":"","occurred_at":"2026-09-11",'
            f'"confidence":0.9,"evidence_ids":["{evidence_id}"]}}]}}'
        )

    agent = _make_agent(
        intent_evidence_plugins=[_StubPlugin()], event_ai_ask=event_ai
    )
    agent.research("jane@acme.com", "acme.com")
    agent.research("john@acme.com", "acme.com")

    assert len(calls) == 1


def test_pain_call_runs_once_and_stores_the_gated_hypothesis(enabled):
    """One AI call, one stored hypothesis — and the gate now actually rules.

    THIS TEST USED TO ASSERT THE DEFECT. Until 2026-09-21 it required
    ``verdict == "BLOCKED"`` and ``"company identity" in blocked_by``, which
    was not a statement about this fixture's evidence — it was the
    ``company_match`` defect showing through. No production path ever set
    that field, so every supporting record read ``unknown`` and the gate's
    identity condition failed for EVERY hypothesis ever proposed. The old
    assertions pinned that as correct behaviour, which is why the fix had to
    change them rather than work around them.

    The evidence here genuinely names its company ("Acme broke ground on the
    Riverside project.", read off acme.com), so it now classifies
    ``CONFIRMED`` and the gate moves on to the conditions that are actually
    about this hypothesis. Its verdict is ``LIKELY``, not ``VERIFIED``:
    there is one signal and no direct pain evidence, so it does not clear the
    verified basis — the identity condition was never what made this thin.
    """
    import json

    event_calls: list[str] = []
    pain_calls: list[str] = []

    def event_ai(prompt: str) -> str:
        event_calls.append(prompt)
        evidence_id = prompt.split('"evidence_id": "', 1)[1].split('"', 1)[0]
        return (
            '{"events":[{"event_type":"project_announced",'
            '"project_key":"","occurred_at":"2026-09-11",'
            f'"confidence":0.9,"evidence_ids":["{evidence_id}"]}}]}}'
        )

    def pain_ai(prompt: str) -> str:
        pain_calls.append(prompt)
        signals = json.loads(prompt.split("Signals: ", 1)[1])
        project = next(
            item for item in signals if item["signal_type"] == "project_activity"
        )
        return json.dumps({
            "candidate_signals": [{
                "signal_id": project["signal_id"], "reasoning": "project activity"
            }],
            "candidate_pain_hypotheses": [{
                "pain_type": "project_volume",
                "confidence": 0.3,
                "signal_ids": [project["signal_id"]],
                "evidence_ids": project["evidence_ids"],
                "reasoning": "One recent project signal.",
            }],
        })

    agent = _make_agent(
        intent_evidence_plugins=[_StubPlugin()],
        event_ai_ask=event_ai,
        pain_ai_ask=pain_ai,
    )
    dossier = agent.research("jane@acme.com", "acme.com")
    agent.research("john@acme.com", "acme.com")
    store = ResearchEvidenceStore()
    company = store.companies_for_domain("acme.com")

    assert company is not None
    assert len(event_calls) == 1
    assert len(pain_calls) == 1
    hypotheses = store.pain_hypotheses_for_company(company.company_id)
    assert len(hypotheses) == 1
    assert hypotheses[0].pain_type.value == "project_volume"
    assert hypotheses[0].verdict.value == "LIKELY"
    assert not any(
        "company identity" in item for item in hypotheses[0].blocked_by
    ), (
        "the evidence names its company, so the identity condition must be "
        "satisfied — this assertion is the regression test for the field that "
        "nothing used to set"
    )
    assert "pain_inference" in dossier.sources_checked
    assert dossier.signal_intelligence["company_id"] == company.company_id

    # The downstream half of the same defect. A BLOCKED hypothesis is not
    # ``eligible`` in ``build_outreach_trigger``, so with every hypothesis
    # blocked the trigger fell through to its generic branch — angle "General
    # Estimating Support", basis "No licensed current pain", strength "none".
    # That is what this test used to assert, and it was the symptom, not the
    # contract: the whole signal -> pain -> outreach chain was ending every
    # company at the same fallback sentence. A LIKELY hypothesis IS eligible,
    # so the trigger now carries the angle its licensed pain actually implies.
    trigger = dossier.signal_intelligence["outreach_trigger"]
    assert trigger["strength"] == "weak_inference", (
        "LIKELY (not VERIFIED, no direct evidence) is exactly weak_inference"
    )
    assert trigger["basis"] == "project_volume"
    assert dossier.signal_intelligence["recommended_angle"] == "Project Estimating Support"
    assert trigger["wording"] == (
        "As project activity picks up, flexible estimating support can help."
    ), "the weak-inference wording variant, chosen by the same index"
    assert trigger["evidence_ids"], "a licensed trigger names the evidence it rests on"
    assert store.outreach_trigger_for_company(company.company_id) is not None


def test_empty_extraction_is_not_repeated_without_new_evidence(enabled):
    calls: list[str] = []

    def event_ai(prompt: str) -> str:
        calls.append(prompt)
        return '{"events":[]}'

    agent = _make_agent(
        intent_evidence_plugins=[_StubPlugin()], event_ai_ask=event_ai
    )
    agent.research("jane@acme.com", "acme.com")
    agent.research("john@acme.com", "acme.com")

    assert len(calls) == 1


def test_the_plugin_is_given_the_researched_company_not_the_email(enabled):
    plugin = _StubPlugin()
    _make_agent(intent_evidence_plugins=[plugin]).research("jane@acme.com", "acme.com")

    assert plugin.calls[0]["company_name"] == "Acme Construction"
    assert plugin.calls[0]["website"] == "https://acme.com"


# --- the hook does NOT run where it should not ----------------------------


def test_a_not_a_client_lead_never_spends_a_collection(enabled):
    """THE credit-control guarantee.

    Stage 1c's shortcut returns before the hook, so a company the pipeline
    has already rejected as not-a-buyer costs zero network calls. If the
    hook were placed above that shortcut, every rejected lead would pay for
    three live endpoint calls — the exact regression the placement exists
    to prevent.
    """
    plugin = _StubPlugin()
    agent = _make_agent(
        ai_response={
            "company_name": "Dallas Software Group",
            "industry": "Software Development",
            "location": "Dallas, TX",
            "website": "https://dallassoftware.com",
            # A CITED fact is required, or CompanyResearcher's identity guard
            # wipes the company (unreadable site, nothing to cite) and Stage 1c
            # has no industry left to judge.
            "facts": [{
                "claim": "Dallas Software Group builds estimating platforms",
                "source_url": "https://dallassoftware.com/about",
                "source_type": "website",
                "confidence": "verified",
            }],
        },
        intent_evidence_plugins=[plugin],
    )
    dossier = agent.research("jane@dallassoftware.com", "dallassoftware.com")

    assert dossier.recommendation == "skip", "fixture must produce a skip"
    assert plugin.calls == [], "a skipped lead must not cost a collection"
    assert "intent_evidence" not in dossier.sources_checked


def test_the_flag_off_means_no_collection_and_no_diagnostic(monkeypatch):
    """The kill-switch is silent by design: off means off, not 'off and noisy'."""
    monkeypatch.setattr(settings, "INTENT_EVIDENCE_ENABLED", False)
    plugin = _StubPlugin()
    dossier = _make_agent(intent_evidence_plugins=[plugin]).research(
        "jane@acme.com", "acme.com"
    )

    assert plugin.calls == []
    assert "intent_evidence" not in dossier.sources_checked
    assert "intent_evidence" not in dossier.source_errors


# --- failure isolation ----------------------------------------------------


def test_a_crashing_intake_is_recorded_and_never_fails_the_lead(enabled):
    """A third-party outage must not cost the lead its research."""
    dossier = _make_agent(intent_evidence_plugins=[_ExplodingPlugin()]).research(
        "jane@acme.com", "acme.com"
    )

    assert dossier.recommendation, "the lead still completed"
    assert "intent_evidence" in dossier.sources_checked
    reason = dossier.source_errors["intent_evidence"]
    assert "NOT_ACCESSIBLE" in reason, (
        "an unreachable provider is 'we could not look', and the state must "
        "say so rather than reporting a quiet empty result"
    )
    assert "exploding" in reason


def test_a_provider_that_answers_with_nothing_is_reported_as_not_found(enabled):
    """The other half of the distinction: we looked, and there was nothing."""
    plugin = _StubPlugin(status=SourceStatus.EMPTY, items=[])
    dossier = _make_agent(intent_evidence_plugins=[plugin]).research(
        "jane@acme.com", "acme.com"
    )

    assert "NOT_FOUND" in dossier.source_errors["intent_evidence"]
    assert "NOT_ACCESSIBLE" not in dossier.source_errors["intent_evidence"]

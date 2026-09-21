"""``collect_intent_evidence`` — the shared run seam (Phase 1).

The loop that drives the intent plugins used to exist only inside
``LeadPipeline._collect_intent``. Phase 1 needs the same behaviour from a
second caller (``app.research.intake``), so it was extracted rather than
copied. These tests pin the behaviour both callers now depend on, and
``tests/lead/test_lead_pipeline.py`` still pins the original caller.

The two rules that matter are the dedup key and the honest report: an item
with no ``source_url`` is not evidence, and a provider that failed must be
recorded against its own name rather than disappearing.
"""

from __future__ import annotations

from app.discovery.intent import (
    BaseIntentPlugin,
    collect_intent_evidence,
    default_intent_plugins,
    registered_intent_plugins,
)
from app.discovery.plugins.plugin_registry import PluginRegistry
from app.discovery.sources.status import SourceStatus
from app.engines.lead.lead_models import IntentEvidence, IntentEvidenceType


class _Plugin:
    def __init__(self, name, status=SourceStatus.SUCCESS, items=()):
        self.name = name
        self._status = status
        self._items = list(items)

    def collect_evidence(self, *, company_name, website="", location=""):
        return self._status, list(self._items), {"source": self.name}


class _Exploding:
    name = "exploding"

    def collect_evidence(self, *, company_name, website="", location=""):
        raise RuntimeError("connection reset")


def _item(url="https://example.gov/1", kind=IntentEvidenceType.project, source="p"):
    return IntentEvidence(type=kind, source_url=url, snippet="s", source=source)


def test_evidence_is_collected_from_every_plugin():
    result = collect_intent_evidence(
        [_Plugin("a", items=[_item("https://x.gov/1")]),
         _Plugin("b", items=[_item("https://x.gov/2")])],
        company_name="Acme",
    )

    assert len(result.evidence) == 2
    assert result.plugins_available == ["a", "b"]


def test_the_same_observation_from_two_plugins_is_one_item():
    """Dedup is by (type, source_url) — two sources citing one page is one fact."""
    result = collect_intent_evidence(
        [_Plugin("a", items=[_item("https://x.gov/1")]),
         _Plugin("b", items=[_item("https://x.gov/1")])],
        company_name="Acme",
    )

    assert len(result.evidence) == 1
    assert result.providers[1]["deduped"] == 1


def test_an_item_with_no_url_is_not_evidence():
    """Hard rule #5: an untraceable signal is dropped, and the drop is counted."""
    result = collect_intent_evidence(
        [_Plugin("a", items=[_item(url="   ")])], company_name="Acme"
    )

    assert result.evidence == []
    assert result.providers[0]["blank_url"] == 1
    assert result.providers[0]["accepted"] == 0


def test_a_broken_plugin_is_recorded_against_its_own_name():
    result = collect_intent_evidence(
        [_Exploding(), _Plugin("good", items=[_item()])], company_name="Acme"
    )

    assert len(result.evidence) == 1, "one bad plugin never kills the run"
    by_name = {p["provider"]: p for p in result.providers}
    assert by_name["exploding"]["status"] == SourceStatus.ERROR.value
    assert "RuntimeError" in by_name["exploding"]["error"]
    assert by_name["good"]["accepted"] == 1


def test_the_report_separates_answered_from_unreachable():
    """The basis for NOT_FOUND vs NOT_ACCESSIBLE — they must not collapse."""
    answered = collect_intent_evidence(
        [_Plugin("a", status=SourceStatus.EMPTY)], company_name="Acme"
    )
    assert answered.any_reachable is True
    assert answered.unreachable == []

    dark = collect_intent_evidence(
        [_Plugin("a", status=SourceStatus.UNAVAILABLE)], company_name="Acme"
    )
    assert dark.any_reachable is False
    assert dark.unreachable == ["a"]


def test_found_evidence_counts_as_reachable():
    result = collect_intent_evidence(
        [_Plugin("a", items=[_item()])], company_name="Acme"
    )
    assert result.any_reachable is True


# --- the registry seam ----------------------------------------------------


def test_only_intent_plugins_are_selected_from_the_registry():
    """Selected by TYPE, never by capability string.

    The discovery framework is open — a plugin may declare any capability —
    so an isinstance check is what guarantees the research path can never be
    handed a company-discovery plugin.
    """
    from app.discovery.intent import register_intent_plugins

    registry = PluginRegistry()
    assert registered_intent_plugins(registry) == []

    register_intent_plugins(registry)
    active = registered_intent_plugins(registry)
    assert sorted(p.name for p in active) == [
        "company_site", "google_news", "usaspending",
    ]
    assert all(isinstance(p, BaseIntentPlugin) for p in active)


def test_a_disabled_intent_plugin_is_not_selected():
    from app.discovery.intent import register_intent_plugins

    registry = PluginRegistry()
    register_intent_plugins(registry)
    registry.disable("usaspending")

    names = [p.name for p in registered_intent_plugins(registry)]
    assert "usaspending" not in names
    assert sorted(names) == ["company_site", "google_news"]


def test_the_built_in_list_matches_the_registry_names():
    """Two paths to the same three plugins — they must not drift apart."""
    registry = PluginRegistry()
    from app.discovery.intent import register_intent_plugins

    register_intent_plugins(registry)
    assert sorted(p.name for p in default_intent_plugins()) == sorted(
        p.name for p in registered_intent_plugins(registry)
    )

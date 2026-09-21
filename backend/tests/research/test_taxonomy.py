"""The event taxonomy's hard-coded prohibitions (``check.txt`` §10, §21).

These tests exist because of a specific, already-observed defect class: a
single ambiguous ``bid_award`` value let a company that merely BID reach
outreach as a company that WON. The founder froze two rules on 2026-09-18:

1. ``bid_submitted``, ``apparent_low_bidder``, ``recommended_for_award``,
   ``contract_awarded`` and ``contract_signed`` are five DIFFERENT facts, and
   an event must never silently become a stronger one further along.
2. ``permit_issued`` is project ACTIVITY — never a bid win, never a contract
   award.

A prompt cannot enforce either: a model under pressure will happily read a
permit as a win. So both are constants and guards in
:mod:`app.research.taxonomy`, and this module proves the guards actually
refuse. If someone deletes a guard, these tests fail — that is the point.
"""

from __future__ import annotations

import pytest

from app.research.taxonomy import (
    ACTIVITY_ONLY_EVENTS,
    BID_AWARD_CHAIN,
    CONTRACT_AWARD_EVENTS,
    UNRESOLVED_EVENTS,
    CompanyMatch,
    EventType,
    ResearchState,
    chain_rank,
    is_contract_award,
    is_unresolved_event,
    may_promote,
    normalize_event_type,
    promotion_violation,
)


# --- the procurement chain is five distinct facts -------------------------


def test_chain_is_ordered_weakest_first():
    """Order is semantic: rank 0 asserts least, the last rank asserts most."""
    assert chain_rank(EventType.BID_SUBMITTED) == 0
    assert chain_rank(EventType.CONTRACT_SIGNED) == len(BID_AWARD_CHAIN) - 1
    ranks = [chain_rank(stage) for stage in BID_AWARD_CHAIN]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(BID_AWARD_CHAIN)


def test_no_two_stages_share_a_value():
    """Distinct members, distinct strings — the conflation cannot re-enter."""
    values = [stage.value for stage in BID_AWARD_CHAIN]
    assert len(set(values)) == len(values)
    assert "bid_award" not in values, (
        "the ambiguous legacy value must never exist as an event type"
    )


@pytest.mark.parametrize("old", BID_AWARD_CHAIN)
@pytest.mark.parametrize("new", BID_AWARD_CHAIN)
def test_forward_promotion_is_refused_without_new_evidence(old, new):
    """THE rule: a weaker stage never silently becomes a stronger one.

    Covers every ordered pair, not just adjacent ones — a bid jumping
    straight to ``contract_signed`` is the same defect as a one-step hop.
    """
    if chain_rank(new) <= chain_rank(old):
        assert may_promote(old, new) is True
        return
    assert may_promote(old, new) is False
    reason = promotion_violation(old, new)
    assert reason, "a refusal must carry a reason, never a bare False (§6)"
    assert normalize_event_type(old) in reason
    assert normalize_event_type(new) in reason


@pytest.mark.parametrize("old", BID_AWARD_CHAIN)
@pytest.mark.parametrize("new", BID_AWARD_CHAIN)
def test_forward_promotion_is_allowed_with_new_evidence(old, new):
    """A genuinely new observation may carry a company up the chain.

    The guard is against SILENT promotion, not against ever learning more.
    """
    assert may_promote(old, new, new_evidence=True) is True


@pytest.mark.parametrize("old", BID_AWARD_CHAIN)
@pytest.mark.parametrize("new", BID_AWARD_CHAIN)
def test_backward_correction_is_always_allowed(old, new):
    """Under-claiming is never a fabrication, so it is never blocked.

    If a win turns out to have been only an apparent low bid, correcting it
    must not require new evidence — the correction IS the new information.
    """
    if chain_rank(new) >= chain_rank(old):
        return
    assert may_promote(old, new) is True
    assert promotion_violation(old, new) == ""


def test_ambiguous_legacy_value_is_not_a_stage():
    """``bid_award`` is not in the chain, so it can never be ordered as one."""
    assert chain_rank("bid_award") is None
    assert chain_rank("BID_AWARD") is None


# --- an unresolved stage is its own state (review correction 2026-09-18) ---


def test_needs_resolution_carries_no_chain_rank():
    """The corrected mistake: it is NOT the weakest stage.

    Mapping the ambiguous legacy value to ``bid_submitted`` looked like safe
    under-claiming, but ``bid_submitted`` is a claim in its own right. A
    procurement page does not prove the company submitted anything.
    """
    assert chain_rank(EventType.NEEDS_RESOLUTION) is None
    assert EventType.NEEDS_RESOLUTION not in BID_AWARD_CHAIN


@pytest.mark.parametrize("stage", BID_AWARD_CHAIN)
def test_leaving_an_unresolved_state_requires_new_evidence(stage):
    """The founder's condition: prove the stage from source text, then promote."""
    assert is_unresolved_event(EventType.NEEDS_RESOLUTION) is True
    assert may_promote(EventType.NEEDS_RESOLUTION, stage) is False
    reason = promotion_violation(EventType.NEEDS_RESOLUTION, stage)
    assert reason, "a refusal must carry a reason, never a bare False (§6)"
    assert normalize_event_type(stage) in reason
    assert may_promote(EventType.NEEDS_RESOLUTION, stage, new_evidence=True) is True


def test_nothing_established_counts_as_unresolved_too():
    """``None`` and ``needs_resolution`` are the same thing to a consumer."""
    assert is_unresolved_event(None) is True
    assert is_unresolved_event("") is True
    assert may_promote(None, EventType.CONTRACT_AWARDED) is False
    assert (
        may_promote(None, EventType.CONTRACT_AWARDED, new_evidence=True) is True
    )


@pytest.mark.parametrize("stage", BID_AWARD_CHAIN)
def test_retracting_a_stage_into_unresolved_is_always_allowed(stage):
    """Correcting an unsupported claim is the honest direction."""
    assert may_promote(stage, EventType.NEEDS_RESOLUTION) is True
    assert promotion_violation(stage, EventType.NEEDS_RESOLUTION) == ""


def test_resolved_events_are_not_unresolved():
    for event in (
        EventType.CONTRACT_AWARDED,
        EventType.PERMIT_ISSUED,
        EventType.HIRING,
        EventType.BID_SUBMITTED,
    ):
        assert is_unresolved_event(event) is False


def test_needs_resolution_is_neither_activity_nor_a_win():
    """It must not be countable as either, or it becomes a fabricated fact."""
    assert EventType.NEEDS_RESOLUTION not in ACTIVITY_ONLY_EVENTS
    assert EventType.NEEDS_RESOLUTION not in CONTRACT_AWARD_EVENTS
    assert is_contract_award(EventType.NEEDS_RESOLUTION) is False


def test_non_chain_events_are_not_this_guard_business():
    """Project/organisational events are not procurement stages."""
    assert chain_rank(EventType.PERMIT_ISSUED) is None
    assert chain_rank(EventType.HIRING) is None
    assert may_promote(EventType.HIRING, EventType.PERMIT_ISSUED) is True


# --- a permit is activity, never a win -----------------------------------


def test_permit_is_not_a_contract_award():
    """§21, founder-frozen: a $15M permit shows work, not a won bid."""
    assert is_contract_award(EventType.PERMIT_ISSUED) is False
    assert EventType.PERMIT_ISSUED not in CONTRACT_AWARD_EVENTS


@pytest.mark.parametrize(
    "stage",
    [
        EventType.BID_SUBMITTED,
        EventType.APPARENT_LOW_BIDDER,
        EventType.RECOMMENDED_FOR_AWARD,
    ],
)
def test_stages_before_award_are_not_awards(stage):
    """Everything short of ``contract_awarded`` is not a win."""
    assert is_contract_award(stage) is False


@pytest.mark.parametrize(
    "stage", [EventType.CONTRACT_AWARDED, EventType.CONTRACT_SIGNED]
)
def test_only_the_two_award_stages_count_as_won(stage):
    assert is_contract_award(stage) is True


def test_activity_only_events_exclude_the_award_stages():
    """The signal layer's vocabulary: activity is never reported as winning."""
    assert EventType.PERMIT_ISSUED in ACTIVITY_ONLY_EVENTS
    assert EventType.PROJECT_ANNOUNCED in ACTIVITY_ONLY_EVENTS
    assert EventType.BID_SUBMITTED in ACTIVITY_ONLY_EVENTS
    assert EventType.CONTRACT_AWARDED not in ACTIVITY_ONLY_EVENTS
    assert EventType.CONTRACT_SIGNED not in ACTIVITY_ONLY_EVENTS


def test_the_three_event_classes_partition_cleanly():
    """Activity, win, unresolved — and nothing in two of them at once."""
    assert ACTIVITY_ONLY_EVENTS & CONTRACT_AWARD_EVENTS == frozenset()
    assert UNRESOLVED_EVENTS & CONTRACT_AWARD_EVENTS == frozenset()
    assert UNRESOLVED_EVENTS & ACTIVITY_ONLY_EVENTS == frozenset()
    assert UNRESOLVED_EVENTS == frozenset({EventType.NEEDS_RESOLUTION})


def test_is_contract_award_tolerates_strings_and_unknowns():
    """Common string forms work; nonsense is False, never an exception."""
    assert is_contract_award("contract_awarded") is True
    assert is_contract_award("CONTRACT_AWARDED") is True
    assert is_contract_award("permit_issued") is False
    assert is_contract_award("something_new_from_a_future_source") is False
    assert is_contract_award("") is False


# --- the four honest states never collapse -------------------------------


def test_research_states_are_four_distinct_values():
    """Founder-frozen 2026-09-18 (``check.txt`` §31)."""
    assert {s.value for s in ResearchState} == {
        "NOT_FOUND",
        "NOT_ACCESSIBLE",
        "NOT_VERIFIED",
        "VERIFIED",
    }
    assert len({s.value for s in ResearchState}) == 4


def test_not_found_and_not_accessible_are_not_equal():
    """"We searched and found nothing" != "we could not look"."""
    assert ResearchState.NOT_FOUND is not ResearchState.NOT_ACCESSIBLE
    assert ResearchState.NOT_FOUND != ResearchState.NOT_ACCESSIBLE
    assert ResearchState.NOT_FOUND.value != ResearchState.NOT_ACCESSIBLE.value


def test_company_match_has_four_states_and_mismatch_is_its_own():
    assert {m.value for m in CompanyMatch} == {
        "confirmed",
        "inferred",
        "unknown",
        "mismatch",
    }


# --- normalization --------------------------------------------------------


def test_normalize_event_type_accepts_enum_and_string_forms():
    assert normalize_event_type(EventType.CONTRACT_AWARDED) == "contract_awarded"
    assert normalize_event_type("Contract_Awarded") == "contract_awarded"
    assert normalize_event_type("  BID_SUBMITTED  ") == "bid_submitted"


def test_normalize_event_type_rejects_blank_and_nonsense():
    with pytest.raises(ValueError):
        normalize_event_type("")
    with pytest.raises(ValueError):
        normalize_event_type("   ")
    with pytest.raises(TypeError):
        normalize_event_type(None)
    with pytest.raises(TypeError):
        normalize_event_type(42)


def test_undeclared_event_type_behaves_like_a_declared_one():
    """Open by design: a future source's new event needs no schema change."""
    assert normalize_event_type("bond_filed") == "bond_filed"
    assert chain_rank("bond_filed") is None
    assert is_contract_award("bond_filed") is False

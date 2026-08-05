"""Infrastructure tests for the Direct Website Discovery confidence model.

Phase 2.3A scope: the model only. No test asserts a *particular* score
for a *particular* extraction, because no scoring algorithm exists yet
and none belongs in this phase.
"""

from __future__ import annotations

import math

import pytest

from app.discovery.website.confidence import MAX_SCORE, MIN_SCORE, Confidence


class TestConstruction:
    def test_score_is_stored(self):
        assert Confidence(0.75).score == 0.75

    def test_bounds_are_accepted(self):
        assert Confidence(MIN_SCORE).score == 0.0
        assert Confidence(MAX_SCORE).score == 1.0

    def test_int_is_coerced_to_float(self):
        value = Confidence(1).score
        assert value == 1.0
        assert isinstance(value, float)


class TestValidation:
    @pytest.mark.parametrize("value", [1.7, -0.1, 2, -1, 100.0])
    def test_out_of_range_raises(self, value):
        # Deliberately not clamped: an out-of-range score means the
        # producer has a bug, and clamping would hide it.
        with pytest.raises(ValueError):
            Confidence(value)

    def test_nan_raises(self):
        with pytest.raises(ValueError):
            Confidence(float("nan"))

    @pytest.mark.parametrize("value", [float("inf"), float("-inf")])
    def test_infinity_raises(self, value):
        with pytest.raises(ValueError):
            Confidence(value)

    @pytest.mark.parametrize("value", ["high", None, [], {}, object()])
    def test_non_numeric_raises_type_error(self, value):
        with pytest.raises(TypeError):
            Confidence(value)

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_is_rejected(self, value):
        # bool passes isinstance(x, int); a flag passed where a score
        # belongs must not silently become 1.0 or 0.0.
        with pytest.raises(TypeError):
            Confidence(value)


class TestPredicates:
    def test_is_unknown_only_at_zero(self):
        assert Confidence(0.0).is_unknown is True
        assert Confidence(0.01).is_unknown is False

    def test_is_certain_only_at_one(self):
        assert Confidence(1.0).is_certain is True
        assert Confidence(0.99).is_certain is False

    def test_meets_is_inclusive(self):
        assert Confidence(0.8).meets(0.8) is True

    def test_meets_above_and_below(self):
        assert Confidence(0.9).meets(0.8) is True
        assert Confidence(0.7).meets(0.8) is False


class TestOrdering:
    def test_comparison(self):
        assert Confidence(0.9) > Confidence(0.4)
        assert Confidence(0.1) < Confidence(0.2)

    def test_equality_by_value(self):
        assert Confidence(0.5) == Confidence(0.5)

    def test_int_and_float_construction_compare_equal(self):
        assert Confidence(1) == Confidence(1.0)

    def test_sorting(self):
        scores = [Confidence(0.5), Confidence(0.1), Confidence(0.9)]
        assert [c.score for c in sorted(scores)] == [0.1, 0.5, 0.9]

    def test_max_picks_the_highest(self):
        assert max(Confidence(0.2), Confidence(0.8)) == Confidence(0.8)


class TestImmutability:
    def test_score_cannot_be_reassigned(self):
        confidence = Confidence(0.5)
        with pytest.raises(Exception):
            confidence.score = 0.9

    def test_is_hashable(self):
        assert len({Confidence(0.5), Confidence(0.5), Confidence(0.7)}) == 2

    def test_usable_as_a_dict_key(self):
        assert {Confidence(1.0): "certain"}[Confidence(1.0)] == "certain"


class TestSentinels:
    def test_certain_is_one(self):
        assert Confidence.CERTAIN.score == 1.0
        assert Confidence.CERTAIN.is_certain is True

    def test_unknown_is_zero(self):
        assert Confidence.UNKNOWN.score == 0.0
        assert Confidence.UNKNOWN.is_unknown is True

    def test_sentinels_equal_their_constructed_form(self):
        assert Confidence.CERTAIN == Confidence(1.0)
        assert Confidence.UNKNOWN == Confidence(0.0)

    def test_sentinels_are_not_confused_with_each_other(self):
        assert Confidence.CERTAIN != Confidence.UNKNOWN


class TestFromValue:
    def test_passes_through_an_existing_confidence(self):
        original = Confidence(0.5)
        assert Confidence.from_value(original) is original

    def test_builds_from_a_float(self):
        assert Confidence.from_value(0.5) == Confidence(0.5)

    def test_builds_from_an_int(self):
        assert Confidence.from_value(0) == Confidence.UNKNOWN

    def test_invalid_input_still_raises(self):
        with pytest.raises(ValueError):
            Confidence.from_value(1.7)
        with pytest.raises(TypeError):
            Confidence.from_value("high")


class TestSerialization:
    def test_to_dict(self):
        assert Confidence(0.75).to_dict() == {"score": 0.75}

    def test_str_is_the_bare_score(self):
        assert str(Confidence(0.5)) == "0.50"

    def test_repr_identifies_the_type(self):
        assert repr(Confidence(0.5)) == "<Confidence 0.50>"


class TestNoScoringLogic:
    """Phase 2.3A ships the model only — scoring arrives with extraction."""

    def test_model_exposes_no_combination_api(self):
        # How two observations of one field merge is a scoring decision
        # with several defensible answers; it is not pre-empted here.
        for name in ("combine", "merge", "aggregate", "average", "boost", "decay"):
            assert not hasattr(Confidence(0.5), name)

    def test_model_exposes_no_bands(self):
        # Where HIGH/MEDIUM/LOW cut-offs sit is ranking policy.
        for name in ("HIGH", "MEDIUM", "LOW", "band", "level"):
            assert not hasattr(Confidence, name)

    def test_bounds_are_the_documented_range(self):
        assert (MIN_SCORE, MAX_SCORE) == (0.0, 1.0)
        assert not math.isnan(MIN_SCORE)

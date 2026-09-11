"""Tests for app.company_profile — the single source of vertical boundary.

Covers:
1. Default profile loads correctly (company_profile.json absent / None)
2. is_off_vertical — excluded vs. in-vertical vs. empty industries
3. prompt_context — contains company name and location
4. is_target_trade — target trades vs. non-target
5. prompts.py — no hardcoded "The Best Estimator" string
"""

from __future__ import annotations

import inspect

from app.company_profile import CompanyProfile, get_profile, load_profile, set_profile_path
from app.lead_research import prompts as _prompts_mod


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fresh_profile() -> CompanyProfile:
    """Return a clean default profile (no company_profile.json)."""
    set_profile_path(None)          # clear singleton cache -> forces re-load
    return get_profile()


# ---------------------------------------------------------------------------
# 1  Default profile loads correctly
# ---------------------------------------------------------------------------

class TestDefaultProfileLoads:
    """get_profile() returns sensible defaults when no JSON file exists."""

    def test_returns_company_profile_instance(self) -> None:
        profile = _fresh_profile()
        assert isinstance(profile, CompanyProfile)

    def test_default_company_name(self) -> None:
        profile = _fresh_profile()
        assert profile.company_name == "The Best Estimator LLC"

    def test_default_location(self) -> None:
        profile = _fresh_profile()
        assert profile.location == "United States (nationwide)"

    def test_target_trades_not_empty(self) -> None:
        profile = _fresh_profile()
        assert len(profile.target_trades) > 0, "target_trades must have entries"

    def test_excluded_vernaculars_not_empty(self) -> None:
        profile = _fresh_profile()
        assert len(profile.excluded_vernaculars) > 0, "excluded_vernaculars must have entries"

    def test_what_we_sell_not_empty(self) -> None:
        profile = _fresh_profile()
        assert len(profile.what_we_sell.strip()) > 0

    def test_ideal_client_not_empty(self) -> None:
        profile = _fresh_profile()
        assert len(profile.ideal_client.strip()) > 0


    def test_missing_file_fallback_keeps_boundaries(self) -> None:
        """Regression: ``load_profile({})`` (what ``_read_config`` returns when
        company_profile.json is missing) must keep the excluded_vernaculars +
        target_trades defaults. An empty boundary here would silently disable
        the off-vertical gate — CLAUDE.md §6 forbids that silent degradation."""
        profile = load_profile({})
        assert len(profile.excluded_vernaculars) > 0
        assert len(profile.target_trades) > 0
        assert profile.is_off_vertical("Fiber Installation") is True
        assert profile.is_off_vertical("general contractor") is False

    def test_load_profile_none_keeps_boundaries(self) -> None:
        """``load_profile(None)`` (the module-level default) keeps the same
        boundaries — the gate must never start with empty lists."""
        profile = load_profile(None)
        assert len(profile.excluded_vernaculars) > 0
        assert len(profile.target_trades) > 0


# ---------------------------------------------------------------------------
# 2  is_off_vertical
# ---------------------------------------------------------------------------

class TestIsOffVertical:
    """is_off_vertical returns True only for explicitly excluded industries."""

    # -- excluded industries => True ------------------------------------

    def test_fiber(self) -> None:
        p = _fresh_profile()
        assert p.is_off_vertical("Fiber Optic Construction") is True

    def test_telecom(self) -> None:
        p = _fresh_profile()
        assert p.is_off_vertical("Telecommunications") is True

    def test_utility(self) -> None:
        p = _fresh_profile()
        assert p.is_off_vertical("Utility Infrastructure") is True

    def test_pipeline(self) -> None:
        p = _fresh_profile()
        assert p.is_off_vertical("Oil & Gas Pipeline") is True

    def test_road(self) -> None:
        p = _fresh_profile()
        assert p.is_off_vertical("Road Construction") is True

    def test_bridge(self) -> None:
        p = _fresh_profile()
        assert p.is_off_vertical("Bridge Construction") is True

    def test_rail(self) -> None:
        p = _fresh_profile()
        assert p.is_off_vertical("Railway Construction") is True

    def test_materials_supplier(self) -> None:
        p = _fresh_profile()
        assert p.is_off_vertical("Building Materials Supplier") is True

    def test_wholesale(self) -> None:
        p = _fresh_profile()
        assert p.is_off_vertical("Wholesale Distribution") is True

    # -- in-vertical industries => False ---------------------------------

    def test_general_contractor(self) -> None:
        p = _fresh_profile()
        assert p.is_off_vertical("General Contractor") is False

    def test_construction(self) -> None:
        p = _fresh_profile()
        assert p.is_off_vertical("Construction") is False

    def test_roofing(self) -> None:
        p = _fresh_profile()
        assert p.is_off_vertical("Roofing Contractor") is False

    def test_subcontractor(self) -> None:
        p = _fresh_profile()
        assert p.is_off_vertical("Subcontractor") is False

    # -- empty / blank => False (conservative: don't starve funnel) ------

    def test_empty_string(self) -> None:
        p = _fresh_profile()
        assert p.is_off_vertical("") is False

    def test_blank_whitespace(self) -> None:
        p = _fresh_profile()
        assert p.is_off_vertical("   ") is False

    def test_none_equivalent(self) -> None:
        """Passing '' to is_off_vertical is equivalent to None/missing."""
        p = _fresh_profile()
        assert p.is_off_vertical("") is False


# ---------------------------------------------------------------------------
# 3  prompt_context
# ---------------------------------------------------------------------------

class TestPromptContext:
    """prompt_context() embeds the company name and location."""

    def test_contains_company_name(self) -> None:
        p = _fresh_profile()
        ctx = p.prompt_context()
        assert p.company_name in ctx

    def test_contains_location(self) -> None:
        p = _fresh_profile()
        ctx = p.prompt_context()
        assert p.location in ctx

    def test_returns_non_empty_string(self) -> None:
        p = _fresh_profile()
        ctx = p.prompt_context()
        assert isinstance(ctx, str) and len(ctx) > 0

    def test_mentions_excluded_verticals(self) -> None:
        """The context should tell the AI which verticals are NOT targets."""
        p = _fresh_profile()
        ctx = p.prompt_context()
        # At least one excluded vernacular should appear
        assert any(v in ctx for v in p.excluded_vernaculars), (
            "prompt_context should mention at least one excluded vernacular"
        )


# ---------------------------------------------------------------------------
# 4  is_target_trade
# ---------------------------------------------------------------------------

class TestIsTargetTrade:
    """is_target_trade returns True for in-vertical trades."""

    def test_general_contractor(self) -> None:
        p = _fresh_profile()
        assert p.is_target_trade("General Contractor") is True

    def test_roofing(self) -> None:
        p = _fresh_profile()
        assert p.is_target_trade("Roofing") is True

    def test_electrical(self) -> None:
        p = _fresh_profile()
        assert p.is_target_trade("Electrical Subcontractor") is True

    def test_plumbing(self) -> None:
        p = _fresh_profile()
        assert p.is_target_trade("Plumbing") is True

    def test_hvac(self) -> None:
        p = _fresh_profile()
        assert p.is_target_trade("HVAC Mechanical") is True

    def test_construction_general(self) -> None:
        p = _fresh_profile()
        assert p.is_target_trade("Construction") is True

    # -- non-target trades => False -------------------------------------

    def test_fiber_not_target(self) -> None:
        p = _fresh_profile()
        assert p.is_target_trade("Fiber Optic Installation") is False

    def test_telecom_not_target(self) -> None:
        p = _fresh_profile()
        assert p.is_target_trade("Telecommunications Services") is False

    def test_software_not_target(self) -> None:
        p = _fresh_profile()
        assert p.is_target_trade("Software Development") is False

    # -- empty => False -------------------------------------------------

    def test_empty_string(self) -> None:
        p = _fresh_profile()
        assert p.is_target_trade("") is False

    def test_blank_whitespace(self) -> None:
        p = _fresh_profile()
        assert p.is_target_trade("   ") is False


# ---------------------------------------------------------------------------
# 5  No hardcoded "The Best Estimator" in prompts.py
# ---------------------------------------------------------------------------

class TestNoHardcodedBrandInPrompts:
    """prompts.py must use get_profile() for branding, never hardcode it."""

    def test_no_hardcoded_best_estimator_string(self) -> None:
        source = inspect.getsource(_prompts_mod)
        assert "The Best Estimator" not in source, (
            "prompts.py still contains hardcoded 'The Best Estimator' — "
            "use get_profile().company_name or get_profile().prompt_context() instead"
        )

    def test_no_hardcoded_texas(self) -> None:
        """'Texas' should also not appear as a literal in the prompt body."""
        source = inspect.getsource(_prompts_mod)
        # We only check for Texas appearing outside of get_profile() calls.
        # Strip lines that call get_profile() — those are correct.
        non_profile_lines = [
            line for line in source.splitlines()
            if "get_profile()" not in line
        ]
        clean = "\n".join(non_profile_lines)
        assert "Texas" not in clean, (
            "prompts.py has hardcoded 'Texas' outside of get_profile() calls"
        )

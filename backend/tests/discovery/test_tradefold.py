"""TradeFold tests (Phase P1) — every lane's real vocabulary folds to the
canonical 14, unknown stays honestly '', and the alias ORDER is load-bearing.

The input strings below are REAL labels from the live-verified sources
(memory lead-source-verification, 2026-09-13 sweeps): WA L&I
``specialtycode1desc`` values, TDLR ``license_type`` values, NYC/LA permit
``license_type`` values, CSLB C-codes, the ContractorClassifier's categories,
and free research industry strings / company names.
"""

from app.discovery.tradefold import (
    CANONICAL_TRADES,
    TRADE_LABELS,
    normalize_trade,
    trade_label,
)


class TestWashingtonLabels:
    """WA L&I specialtycode1desc — the trade volumes memory recorded."""

    def test_wa_exact_specialty_descriptions(self):
        assert normalize_trade("DRY WALL") == "drywall"
        assert normalize_trade("PAINTING") == "painting"
        assert normalize_trade("PAINTING/WALLCOVERING") == "painting"
        assert normalize_trade("GENERAL") == "gc"
        assert normalize_trade("CONSTRUCTION CONTRACTOR") == "gc"
        assert normalize_trade("FLOOR COVERING") == "flooring"
        assert normalize_trade("LANDSCAPING") == "landscaping"
        assert normalize_trade("LAWN") == "landscaping"
        assert normalize_trade("CONCRETE") == "concrete"
        assert normalize_trade("PLUMBING") == "plumbing"
        assert normalize_trade("ROOFING") == "roofing"
        assert normalize_trade("DEMOLITION") == "demolition"
        assert normalize_trade("HVAC") == "mechanical"

    def test_wa_real_but_unsold_trades_stay_unknown(self):
        """Masonry/glazing/steel are real WA trades we do NOT sell — an
        honest '' (never forced into a nearby bucket)."""
        assert normalize_trade("MASONRY") == ""
        assert normalize_trade("GLAZING") == ""
        assert normalize_trade("STEEL ERECTORS") == ""
        assert normalize_trade("FRAMING") == ""
        assert normalize_trade("WELDING") == ""


class TestCSLBCodes:
    """CSLB classification codes (P3/P8 connector will carry these)."""

    def test_well_established_c_codes(self):
        assert normalize_trade("C-9") == "drywall"
        assert normalize_trade("C-10") == "electrical"
        assert normalize_trade("C-4") == "mechanical"
        assert normalize_trade("C-20") == "mechanical"
        assert normalize_trade("C-21") == "demolition"
        assert normalize_trade("C-33") == "painting"
        assert normalize_trade("C-36") == "plumbing"
        assert normalize_trade("C-39") == "roofing"
        assert normalize_trade("C-15") == "flooring"
        assert normalize_trade("C-27") == "landscaping"

    def test_b_class_folds_to_gc(self):
        assert normalize_trade("B - General Building Contractor") == "gc"

    def test_la_style_code_without_dash(self):
        assert normalize_trade("C10") == "electrical"


class TestTDLRLicenseTypes:
    def test_tdlr_types(self):
        assert normalize_trade("Electrical Contractor") == "electrical"
        assert normalize_trade("Electrician") == "electrical"
        assert normalize_trade("A/C Contractor") == "mechanical"
        assert normalize_trade("Air Conditioning Contractor") == "mechanical"
        assert normalize_trade("Boiler") == "mechanical"

    def test_tdlr_noise_stays_unknown(self):
        """Cosmetology/barber/auctioneer — licensed in TX, never a client."""
        assert normalize_trade("Cosmetologist") == ""
        assert normalize_trade("Barber") == ""
        assert normalize_trade("Auctioneer") == ""
        assert normalize_trade("Elevator") == ""


class TestPermitAndClassifierLabels:
    def test_nyc_permit_license_type(self):
        assert normalize_trade("GC") == "gc"

    def test_classifier_categories(self):
        # The ContractorClassifier's real category slugs.
        assert normalize_trade("general_contractor") == "gc"
        assert normalize_trade("hvac") == "mechanical"
        assert normalize_trade("painting") == "painting"
        assert normalize_trade("flooring") == "flooring"
        assert normalize_trade("landscaping") == "landscaping"
        assert normalize_trade("concrete") == "concrete"
        assert normalize_trade("plumbing") == "plumbing"
        assert normalize_trade("electrical") == "electrical"
        assert normalize_trade("roofing") == "roofing"

    def test_classifier_steel_stays_unknown(self):
        assert normalize_trade("steel") == ""


class TestResearchAndCompanyStrings:
    def test_research_industry_strings(self):
        assert normalize_trade("Drywall Contractor") == "drywall"
        assert normalize_trade("General Contractor") == "gc"
        assert normalize_trade("Construction Company") == "gc"
        assert normalize_trade("HVAC Services") == "mechanical"
        assert normalize_trade("Plumbing and Heating") == "mechanical"
        assert normalize_trade("Interior Finishes") == "finishes"
        assert normalize_trade("MEP") == "mep"
        assert normalize_trade("m.e.p.") == "mep"
        assert normalize_trade("Lumber Supplier") == "lumber"

    def test_company_names(self):
        assert normalize_trade("XYZ Drywall Inc") == "drywall"
        assert normalize_trade("ABC Construction LLC") == "gc"
        assert normalize_trade("Smith Roofing & Exteriors") == "roofing"

    def test_none_and_blank(self):
        assert normalize_trade("") == ""
        assert normalize_trade(None) == ""
        assert normalize_trade("   ") == ""


class TestOrderSensitivity:
    """The alias ORDER is load-bearing (most-specific first, gc LAST)."""

    def test_drywall_wins_over_paint_family(self):
        assert normalize_trade("Drywall and Painting") == "drywall"

    def test_specific_trade_wins_over_gc(self):
        assert normalize_trade("general contractor specializing in drywall") == "drywall"
        assert normalize_trade("Electrical - General Contractor") == "electrical"

    def test_finish_carpentry_is_finishes_not_gc_noise(self):
        assert normalize_trade("Finish Carpentry") == "finishes"
        assert normalize_trade("Finished Carpentry and Millwork") == "finishes"

    def test_carpentry_alone_is_unknown(self):
        """Plain "carpentry" is NOT one of the 14 — honest ''."""
        assert normalize_trade("Carpentry") == ""


class TestLabelHelpers:
    def test_canonical_set_is_the_founders_fourteen(self):
        assert len(CANONICAL_TRADES) == 14
        assert "" not in CANONICAL_TRADES

    def test_every_slug_has_the_founder_spelling(self):
        assert set(TRADE_LABELS) == set(CANONICAL_TRADES)
        assert TRADE_LABELS["gc"] == "GC"
        assert TRADE_LABELS["drywall"] == "DryWall"
        assert TRADE_LABELS["landscaping"] == "LandScaping"
        assert TRADE_LABELS["mep"] == "MEP"

    def test_label_of_unknown_is_empty(self):
        assert trade_label("") == ""
        assert trade_label("masonry") == ""

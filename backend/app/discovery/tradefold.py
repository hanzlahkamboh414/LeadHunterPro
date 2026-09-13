"""TradeFold — the ONE normalization map from every source's trade label
to LeadHunter's canonical 14 trades (Phase P1 of the big-bang update).

Why this exists
---------------
The user's complaint: a Drywall search returned GC data. Every lane names
trades DIFFERENTLY — Washington says ``DRY WALL``, CSLB says ``C-9``, NYC
permits say ``GC``, the contractor classifier says ``general_contractor``,
the research prompt says ``general contractor`` — so before any filtering
can happen (P2's trade gate), every label must fold into ONE canonical
vocabulary. One module, one definition (CLAUDE.md §14).

The canonical set is the founder's exact 14-trade product list (the
frontend ``TRADES`` in ``frontend/src/data/locations.ts`` — do not re-order
or rename):

    gc · electrical · mechanical · plumbing · lumber · demolition · mep ·
    drywall · roofing · concrete · landscaping · flooring · painting ·
    finishes

Honesty rules (CLAUDE.md §7/§12, fail-open like every gate here):
* ``normalize_trade`` returns ``""`` for unknown/unmappable labels — an
  honest "trade unknown", NEVER a guess forced into a bucket. A source
  trade that is real but outside the 14 (masonry, glazing, steel, framing)
  also folds to ``""``: the product does not sell that trade, and serving
  it to one of the 14 would be the exact cross-trade leak this fixes.
* Normalization is pure substring matching over LOWERCASED input, ordered
  most-specific-first. The order is load-bearing: "DRY WALL" must be
  tested before any "WALL" pattern, and "finish carpentry" before plain
  "carpentry"-family noise.
* Known limitation, documented on purpose: the ContractorClassifier's
  ``painting`` category includes drywall page evidence (its regex matches
  both), so a classifier-labeled drywall company folds to ``painting``.
  Sources that natively separate the two (WA ``DRY WALL``, CSLB ``C-9``)
  are exact; the classifier's granularity is what it is.

Consumed by (P1 wiring):
* ``leads.pipeline.company_records_to_leads`` — discovery records' trade
  label folds into every lead dict (``trade`` key) before caching;
* ``PendingLeadsStore`` — new ``trade`` column, stocked and served;
* ``LeadResearchStore.save`` — dossier's own researched ``company.industry``
  string folds into the dossiers ``trade`` column (research evidence beats
  discovery labels when they disagree — a re-research updates it);
* boot-time lazy backfill — pre-P1 rows get a trade from the evidence they
  already carry (dossiers: industry string; pending: company name).
"""

from __future__ import annotations

import re

#: The canonical 14 — founder's product list, kebab-free slug form. '' is
#: NOT in the set: it is the honest "unknown", kept distinct from every
#: real trade.
CANONICAL_TRADES: tuple[str, ...] = (
    "gc", "electrical", "mechanical", "plumbing", "lumber", "demolition",
    "mep", "drywall", "roofing", "concrete", "landscaping", "flooring",
    "painting", "finishes",
)

#: Human labels for the UI (slug -> the founder's exact spelling).
TRADE_LABELS: dict[str, str] = {
    "gc": "GC",
    "electrical": "Electrical",
    "mechanical": "Mechanical",
    "plumbing": "Plumbing",
    "lumber": "Lumber",
    "demolition": "Demolition",
    "mep": "MEP",
    "drywall": "DryWall",
    "roofing": "Roofing",
    "concrete": "Concrete",
    "landscaping": "LandScaping",
    "flooring": "Flooring",
    "painting": "Painting",
    "finishes": "Finishes",
}

#: Alias table, ORDERED most-specific-first. Each row: (canonical, regex).
#: Verified against live data (2026-09-13 sweeps — see memory
#: lead-source-verification): WA specialty descriptions, TDLR license
#: types, NYC/LA permit license types, CSLB C-codes, the
#: ContractorClassifier's categories, and free research industry strings.
#: CSLB codes included are only the well-established ones (B, C-4, C-9,
#: C-10, C-15, C-20, C-21, C-27, C-33, C-36, C-39); uncertain codes are
#: left out rather than guessed.
_TRADE_ALIASES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # --- drywall BEFORE painting: WA "PAINTING/WALLCOVERING" must fold to
    # painting, but "DRY WALL"/"drywall" is its own trade in the 14.
    ("drywall", re.compile(r"\bdry\s?wall(ing)?\b|\bc-?9\b")),
    ("demolition", re.compile(r"\bdemolition\b|\bsalvage\b|\bc-?21\b")),
    # --- mechanical family BEFORE electrical: "Heating/Vent/Air" etc.
    # ("a/c" = TDLR's "A/C Contractor" license type; contractor domain only.)
    ("mechanical", re.compile(
        r"\bhvac\b|\bh\.?v\.?a\.?c\b|\bheating\b|\bcooling\b"
        r"|\bair\s+condition|\ba/c\b|\brefrig|\bboiler\b|\bsteam\s+fit"
        r"|\bmechanical\b|\bc-?4\b|\bc-?20\b")),
    ("electrical", re.compile(r"\belectric(al|ian)?\b|\bc-?10\b")),
    ("plumbing", re.compile(r"\bplumb(er|ing)?\b|\bc-?36\b")),
    ("roofing", re.compile(r"\broof(er|ing)?\b|\bc-?39\b")),
    ("flooring", re.compile(
        r"\bfloor(ing)?\s*(cover(ing)?)?\b|\bc-?15\b")),
    ("landscaping", re.compile(
        r"\blandscap(e|ing)\b|\blawn\b|\bc-?27\b")),
    ("concrete", re.compile(r"\bconcrete\b|\bcement\b")),
    ("finishes", re.compile(
        r"\bfinish(?:ed)?\s+carpentry\b|\bmillwork\b|\bcabinets?\b"
        r"|\bfinishes?\b")),
    ("painting", re.compile(
        r"\bpaint(er|ing)?\b|\bwallcover(ing)?\b|\bcoat(ing)?\b|\bc-?33\b")),
    ("mep", re.compile(r"\bm\.?e\.?p\.?\b")),
    ("lumber", re.compile(r"\blumber\b")),
    # --- gc LAST: it is the broadest label ("general contractor",
    # "construction company"); a record naming a specific trade above must
    # never fall through to it. Bare "general" = WA L&I's license category
    # (115k+ records say just "GENERAL") — in a contractor-license context
    # that IS a general contractor.
    ("gc", re.compile(
        r"\bgeneral\s+contract(or|ing)\b|\bgeneral_contractor\b"
        r"|\bgc\b|\bconstruction\s*(company|contract(or|ing))?\b"
        r"|\bbuilding\s+contractor\b|\bremodel(er|ing)\b|\brenovat(or|ion)\b"
        r"|\bgeneral\b")),
)


def normalize_trade(raw: str) -> str:
    """Fold ANY source's trade label to a canonical slug, or ``''``.

    Pure, cheap, total: never raises, never guesses. ``raw`` may be a WA
    specialty description, a CSLB code, a TDLR license type, a classifier
    category, an AI-researched industry string, or a company name — the
    same ordered alias table handles them all because every system's
    vocabulary is substring-representable in lowercase.
    """
    text = (raw or "").strip().lower()
    if not text:
        return ""
    for canonical, pattern in _TRADE_ALIASES:
        if pattern.search(text):
            return canonical
    return ""


def trade_label(trade: str) -> str:
    """The founder's exact UI spelling for a canonical slug ('' -> '')."""
    return TRADE_LABELS.get(trade, "")

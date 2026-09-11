"""AI query-surface expansion (Phase J): the durable answer to discovery exhaust.

WHAT IT SOLVES
--------------
The user's hard requirement: DON'T hand-write synonyms (a fixed list goes stale
and the universe exhausts again) — let the AI GENERATE the search queries. A
run for "General Contractors · San Antonio TX" today searches ONE literal
spelling of the trade and ONE literal location through 8 static dorks. That
fixed point is exactly why a 500-target run honestly exhausts at ~3-7 working
leads: the query space is a point, not a set.

This module is the ADD side for the QUERY SURFACE (Phase H already added new
dork ANGLES; this adds new trade/location WORDINGS). One bounded AI call per
job expands the user's trade + location into real, related search phrases; the
producer cycles through them so each pass searches a genuinely different query
string -> a different PDF set -> discovery keeps ADVANCING instead of grinding
the same dead pool. The AI refills the surface every job, so it never runs out
as a finite list — the durable behavior requested.

REUSE (CLAUDE.md §14): the LLM transport is Phase H's (``make_ai_ask`` on the
3rd lane, AI_API_KEY_3), the prompt/parse/sanitize discipline mirrors
``template_generation``, and the honest no-silent-skip contract (§6) is
identical. Nothing new is invented where Phase H already solved the problem.

HONEST LIMITS:
  * BOUNDED — ONE AI call per job (a couple of credits), never per pass;
    expansion is a per-job template, not a per-search inference loop.
  * SANITIZED — every variant is a short, plain phrase; dork/URL/operator
    syntax is rejected (a trade/location wording must never leak a ``filetype:``
    or ``site:`` into the query); dedupe against the base; blanks dropped.
  * FALLBACK — if the LLM fails or no key is configured, the run degrades to
    the exact pre-Phase-J behavior (singular/plural trade + literal location),
    LOUDLY logged — never a silent skip (§6).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Bounds — enough real surface to keep a pass advancing, few enough that one
#: job's expansion stays a handful of distinct query strings (credit-bounded).
#: Breadth targets 100-500 leading markets, so the geo surface needs more than
#: a couple of variant words to fill a buffer (Phase K discovery-breadth fix).
_MAX_TRADE = 8
_MAX_LOCATION = 8

#: Upper bound on a single variant string. A real trade/location phrase is
#: short; anything longer is a "sentence" the AI drifted into, rejected.
_MAX_VARIANT_LEN = 40

#: Dork/URL operators that must never leak into a plain query variant — a
#: trade/location wording is quoted/searchable only via the plan-holder dork
#: templates, never a raw search-operator payload (§12 sanity).
_FORBIDDEN = re.compile(
    r"filetype:|site:|intitle:|inurl:|intext:\s|\bor\b|\band\b|index of|\.pdf",
    re.IGNORECASE,
)

#: Collapse any run of whitespace (incl. newlines) to one space.
_WS = re.compile(r"\s+")


def _fold(text: str) -> str:
    """Canonical market key: lowercase, whitespace collapsed, commas stripped.

    'San Antonio, TX' and 'san antonio tx' are the SAME market — folding is how
    the literal-guard and the metro fallback tell 'same place' from 'new place'.
    """
    return _WS.sub(" ", (text or "").strip().lower()).replace(",", "")


def _sanitize_list(entries: list[str], base: str, cap: int) -> list[str]:
    """Validate/dedupe/cap an AI-proposed variant list; never trust it raw.

    Returns a clean, de-duplicated, bounded list in the AI's order. Any entry
    that is blank, oversized, operator-bearing, or a duplicate of the base (or
    an earlier entry) is dropped and logged — a bad proposal never reaches the
    producer's query rotation (§12).
    """
    out: list[str] = []
    seen: set[str] = {base.strip().lower()}
    for raw in entries:
        text = _WS.sub(" ", (raw or "").strip()).strip('"').strip()
        if not text:
            continue
        if len(text) > _MAX_VARIANT_LEN:
            logger.info("query-expansion: dropped %r (length %d)", text, len(text))
            continue
        if _FORBIDDEN.search(text):
            logger.info("query-expansion: dropped %r (search-operator shaped)", text)
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= cap:
            break
    return out


def _parse(reply: str) -> tuple[list[str], list[str]]:
    """Pull ``trade`` and ``location`` variant lists out of an LLM reply.

    The prompt asks for zero or more quoted strings after a ``TRADE:`` and a
    ``LOCATION:`` prefix. This parser is robust: it finds all quoted strings,
    then buckets each by which prefix's section it sat in, falling back to
    split-by-prefix if the LLM omitted quotes.
    """
    trades: list[str] = []
    locs: list[str] = []
    if not reply:
        return trades, locs
    # Split on the two section markers, keeping order.
    m = re.split(r"(?i)\b(TRADE|LOCATION)\s*:", reply)
    # m = [pre, 'TRADE', trades_sec, 'LOCATION', locs_sec, tail, ...]
    section: str | None = None
    for piece in m:
        if piece.upper() in ("TRADE", "LOCATION"):
            section = piece.upper()
            continue
        if section is None:
            continue
        (trades if section == "TRADE" else locs).extend(re.findall(r'"([^"]+)"', piece))
    # Fallback: if no section markers at all, treat ALL quoted strings as trades
    # (the most common drift) and leave locations empty (literal fallback).
    if not trades and not locs:
        trades = re.findall(r'"([^"]+)"', reply)
    return trades, locs


def _is_near_duplicate(candidate: str, accepted: str) -> bool:
    """Detect garbled near-duplicates like 'Forum Bend County TX' vs 'Fort Bend County TX'.

    Two locations are near-duplicates when their *core* tokens (the words
    that actually distinguish one place from another — everything except
    the state abbreviation and generic suffixes like "county") overlap
    with Jaccard ≥ 0.50.  For "Fort Bend County TX" vs "Forum Bend County
    TX" the cores are ["fort", "bend"] vs ["forum", "bend"] → intersection
    {bend} / union {fort, forum, bend} = 1/3 ≈ 0.33 — still ≥ 0.50 when
    using the distinguishing-token set; but more practically the shared
    suffix ("bend county") catches this class.

    The function uses two checks (union if ≥ 2 distinguishing tokens;
    longest-suffix match if ≥ 2 tokens) so that garbled one-word variants
    ("Forum Bend" vs "Fort Bend") are caught even when the raw Jaccard is
    diluted by the differing first word.

    >>> _is_near_duplicate("Fort Bend County TX", "Forum Bend County TX")
    True
    >>> _is_near_duplicate("Fort Bend County TX", "Fort Worth TX")
    False
    >>> _is_near_duplicate("Harris County TX", "Harris County TX")
    False  # exact dupes are caught by _fold, not this path
    """
    STOP_WORDS = {
        "tx", "texas", "ca", "california", "fl", "florida",
        "ny", "new york", "county", "counties",
    }

    def _core_tokens(loc: str) -> list[str]:
        """Lowercase, non-stop tokens preserving order."""
        return [
            t for t in re.sub(r"[^a-z0-9]+", " ", loc.lower()).split()
            if t and t not in STOP_WORDS
        ]

    def _sorted_tokens(loc: str) -> set[str]:
        return set(_core_tokens(loc))

    # Same canonical market (fold strips commas/case/space) → same place,
    # caught by _fold before this is called.
    if _fold(candidate) == _fold(accepted):
        return False

    tok_c = _core_tokens(candidate)
    tok_a = _core_tokens(accepted)
    if not tok_c or not tok_a:
        return False
    # Identical core tokens but different full strings (e.g. "Galveston
    # County TX" vs "Galveston Counties TX") — the suffix is garbled.
    if tok_c == tok_a:
        return True

    set_c, set_a = _sorted_tokens(candidate), _sorted_tokens(accepted)
    union = set_c | set_a
    intersection = set_c & set_a
    if not union:
        return False

    # Check 1: Jaccard on core (distinguishing) tokens.
    jaccard = len(intersection) / len(union)
    if jaccard >= 0.50:
        return True

    # Check 2: Longest common suffix of core tokens.
    # Catches "Fort Bend" vs "Forum Bend" (shared suffix "bend") when the
    # first word is garbled but the rest is identical.
    min_len = min(len(tok_c), len(tok_a))
    suffix_len = 0
    for i in range(1, min_len + 1):
        if tok_c[-i] == tok_a[-i]:
            suffix_len += 1
        else:
            break
    # Suffix must cover ≥ half the shorter location's core.
    if suffix_len > 0 and suffix_len / min_len >= 0.50:
        return True

    return False


def merge_locations(literal: str, ai_variants: list[str]) -> list[str]:
    """Build the location rotation a run actually SEARCHES.

    Discovery-breadth guard (Phase K): the pipeline previously replaced the
    user's literal location wholesale with whatever the AI expanded it into —
    which is how a "San Antonio TX" run searched ONLY "Bexar County TX" and
    never the city, starving the buffer. This folds the two together:

      1. the user's OWN market is ALWAYS searched first (never dropped to an AI
         rewrite), and
      2. every distinct AI metro variant is appended after it, so the run covers
         the city AND its surrounding counties / neighboring cities.

    AI variants that are the SAME market as the literal (modulo punctuation —
    "Houston, TX" vs "Houston TX" is one place, not breadth) are folded away;
    blanks are dropped. Distinct markets ("Harris County TX", "New Braunfels TX")
    are always kept.

    Garbled-location guard: AI-generated variants that are near-duplicates
    of an already-accepted variant (e.g. "Forum Bend County TX" vs
    "Fort Bend County TX") are dropped and logged — garbled names waste
    search passes on queries guaranteed to return nothing useful.
    """
    out: list[str] = []
    literal = (literal or "").strip()
    if literal:
        out.append(literal)
    needle = _fold(literal)
    for variant in ai_variants or []:
        v = (variant or "").strip()
        if not v:
            continue
        if _fold(v) == needle:
            continue
        # Garbled near-duplicate guard: skip if too similar to a
        # variant already accepted (e.g. "Forum Bend County TX" vs
        # "Fort Bend County TX" — 3 of 4 cleaned tokens match).
        if any(_is_near_duplicate(v, a) for a in out):
            logger.debug("merge_locations: dropped garbled near-duplicate %r (similar to accepted variants)", v)
            continue
        out.append(v)
    return out


#: Surface Expansion Guard (Inc 1) — how many DISTINCT markets the AI must
#: return before the deterministic metro fallback stays out of the run. The
#: old bar of 2 let a "passing" reply of 2-3 markets shrink the whole geo
#: surface (live proof: the 2026-09-11 Fort Worth run searched only 3 markets
#: while the fallback table held 6 more) — breadth is a UNION now, not an
#: either/or: fewer than this many AI markets and the fallback entries are
#: merged in alongside whatever the AI did give.
_MIN_BREADTH_MARKETS = 5

#: Deterministic geo breadth fallback for the metros this product actually
#: searches. Entries are genuinely SEPARABLE markets (neighboring cities,
#: distinct counties) — never re-spellings of the literal, so the rotation
#: always adds surface instead of narrowing it. The AI leads when it answers
#: (≥ _MIN_BREADTH_MARKETS distinct markets); this map keeps a run advancing
#: when it does not.
_METRO_FALLBACK: dict[str, list[str]] = {
    "san antonio tx": ["New Braunfels TX", "Seguin TX", "Boerne TX",
                       "Comal County TX", "Guadalupe County TX",
                       "Kendall County TX"],
    "houston tx": ["Harris County TX", "Fort Bend County TX",
                   "Montgomery County TX", "Galveston County TX",
                   "Katy TX", "Sugar Land TX", "Conroe TX"],
    "austin tx": ["Travis County TX", "Williamson County TX",
                  "Hays County TX", "Round Rock TX", "Cedar Park TX",
                  "Georgetown TX", "Pflugerville TX"],
    "dallas tx": ["Dallas County TX", "Collin County TX", "Denton County TX",
                  "Tarrant County TX", "Plano TX", "Irving TX",
                  "McKinney TX"],
    "fort worth tx": ["Tarrant County TX", "Denton County TX",
                      "Dallas County TX", "Arlington TX", "Grapevine TX",
                      "Burleson TX"],
    "cedar rapids ia": ["Linn County IA", "Johnson County IA",
                        "Benton County IA", "Marion IA", "Hiawatha IA",
                        "Coralville IA"],
}


def metro_fallback(location: str) -> list[str]:
    """Deterministic expansion markets for ANY US location (Phase 1 fix).

    Two tiers, ordered narrow-to-broad so the rotation tries the precise
    markets before the wide nets:

    1. ``_METRO_FALLBACK`` — counties/suburbs of the six hand-curated
       metros (highest precision).
    2. :func:`state_markets.state_markets` — the state's OTHER major
       metros plus a state-wide entry, so a metro outside the six (live
       proof: Honolulu HI / Wichita KS, 2026-09-12) still gets an
       expansion path instead of an empty list.

    Deduped by fold-key, never contains the literal itself. Returns []
    only when the location is genuinely un-expandable (non-US, or the
    literal already IS a state) — the honest "nothing left" signal.
    """
    out: list[str] = list(_METRO_FALLBACK.get(_fold(location)) or [])
    seen: set[str] = {_fold(location)}
    for m in out:
        seen.add(_fold(m))
    from app.discovery.state_markets import state_markets

    for m in state_markets(location):
        key = _fold(m)
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def generate_query_expansion(
    trade: str, location: str, ai_ask: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """The single entry: expand ``trade``/``location`` into search-word variant lists.

    Returns ``{"trade_variants": [...], "location_variants": [...], "reason": "",
    "raw_replies": [...], "retried": bool}``. On no-key / LLM failure / no usable
    output it returns EMPTY variant lists with a non-empty ``reason`` — the
    caller then uses the literal trade/location (the exact pre-Phase-J
    behavior). Always explainable, never a silent skip.

    RELIABILITY (Phase K2 — the 04:06 San Antonio run starved because ONE flash-
    model call returned no distinct locations): location BREADTH must never hang
    on a single LLM call, so:

      1. RETRY — if the call succeeds but yields fewer than TWO distinct markets,
         the SAME prompt is asked once more (flash-model drifts usually recover —
         probe evidence: 5/5 calls give 8 markets when the section is asked again).
      2. FALLBACK (union) — if the reply still yields fewer than
         :data:`_MIN_BREADTH_MARKETS` distinct markets (or is blank/unusable)
         and the metro is known, :data:`_METRO_FALLBACK` entries are MERGED IN
         alongside the AI's own (a union, never either/or — the Surface
         Expansion Guard). The user's literal market is ALWAYS part of the
         surface regardless (see :func:`merge_locations`).

    ``raw_replies`` carries every raw LLM reply (1 or 2) so the caller can
    surface WHAT the AI said into the job event stream — a weak expansion is
    then diagnosable from History instead of being a silent literal-only run.

    Args:
        trade: the user's trade phrase, e.g. "General Contractors".
        location: the user's location phrase, e.g. "San Antonio TX".
        ai_ask: ``(prompt) -> str`` LLM transport. Defaults to the 3rd AI lane
            (AI_API_KEY_3, falling back to 2 -> main), same as Phase H.
    """
    trade = (trade or "").strip()
    location = (location or "").strip()

    if ai_ask is None:
        from app.core.config import settings

        # ONLY build a live ask when a key is actually configured — otherwise a
        # provider would be constructed with an empty key and hit the network
        # pointlessly (breaks offline tests). No key -> honest fallback.
        if not (settings.AI_API_KEY_3 or settings.AI_API_KEY_2 or settings.AI_API_KEY):
            return {
                "trade_variants": [], "location_variants": [],
                "reason": "no AI key configured — using literal trade/location",
                "raw_replies": [], "retried": False,
            }
        try:
            from app.ai.gateway import make_ai_ask

            ai_ask = make_ai_ask(api_key=settings.AI_API_KEY_3)
        except Exception as exc:  # noqa: BLE001 — a broken lane is honest, not fatal
            logger.warning("query-expansion: AI lane unavailable: %s", exc)
            return {
                "trade_variants": [], "location_variants": [],
                "reason": f"AI lane unavailable: {exc}",
                "raw_replies": [], "retried": False,
            }

    prompt = (
        "You help a DISCOVERY engine find CONTRACTOR companies on public "
        "plan-holder / bid-roster PDFs. I give you ONE trade and ONE location. "
        "Your job is BREADTH: write the DIFFERENT real search phrases that "
        "surface a DIFFERENT set of plan-holder PDFs, so a run reaches the "
        "whole metro instead of grinding one spot.\n"
        f"TRADE: {trade!r}\n"
        f"LOCATION: {location!r}\n"
        "Respond with two sections.\n"
        "'TRADE' (up to 8): alternative real wordings of the trade — synonyms, "
        "what it is also called, common bid-roster headings.\n"
        "'LOCATION' (up to 8): DISTINCT geographic markets around the location "
        "that each host their OWN agencies and plan rooms — the main city, each "
        "surrounding county, each neighboring city/suburb. They MUST be genuinely "
        "separable markets so each surfaces a different PDF set. Do NOT give "
        "'contained' re-spellings of the one literal: 'San Antonio TX' and "
        "'Bexar County TX' cover the SAME market — instead list separate ones "
        "like 'San Antonio TX', 'New Braunfels TX', 'Seguin TX', 'Comal County TX', "
        "'Kendall County TX', 'Guadalupe County TX'. For a metro, name its "
        "distinct counties and nearby cities.\n"
        "Rules: each entry is ONE short plain phrase (no quotes inside), no "
        "search operators, no filetype, no website names, several words max, "
        "never repeat the exact TRADE/LOCATION I gave. Format each as a "
        "double-quoted string, one per line under its section.\n"
        "TRADE:\n\"...\"\nLOCATION:\n\"...\""
    )

    def _ask_once() -> str:
        """One bounded LLM call; failure is handled by the caller."""
        return ai_ask(prompt)

    raw_replies: list[str] = []
    retried = False
    try:
        raw_replies.append(_ask_once())
    except Exception as exc:  # noqa: BLE001 — a dead AI is honest, not fatal
        logger.warning("query-expansion: LLM call failed: %s", exc)
        return {
            "trade_variants": [], "location_variants": [],
            "reason": f"LLM call failed: {exc}",
            "raw_replies": [], "retried": False,
        }

    all_trades: list[str] = []
    all_locs: list[str] = []

    def _distinct_locs() -> list[str]:
        """Location variants that are DIFFERENT markets from the literal.

        DEDUPED by fold-key: the count that decides retry/fallback is DISTINCT
        markets, not occurrences — 'Bexar County TX' twice is ONE market, still
        too thin to fill a metro.
        """
        seen: set[str] = set()
        out: list[str] = []
        for v in all_locs:
            key = _fold(v)
            if key == _fold(location) or key in seen:
                continue
            seen.add(key)
            out.append(v)
        return out

    for reply in raw_replies:
        trades, locs = _parse(reply or "")
        all_trades.extend(trades)
        all_locs.extend(locs)
    if len(_distinct_locs()) < 2:
        # Flash-model drift surveillance: ONE retry before any fallback — the
        # 04:06 starvation was a single weak call, and a second ask usually
        # recovers (probe: 5/5 give the missing section on re-ask).
        try:
            raw_replies.append(_ask_once())
            retried = True
            trades, locs = _parse(raw_replies[-1] or "")
            all_trades.extend(trades)
            all_locs.extend(locs)
        except Exception as exc:  # noqa: BLE001 — retry failure is honest too
            logger.warning("query-expansion: retry call failed: %s", exc)

    clean_trades = _sanitize_list(all_trades, trade, _MAX_TRADE)
    clean_locs = _sanitize_list(all_locs, location, _MAX_LOCATION)
    fallback_used = False
    if len(_distinct_locs()) < _MIN_BREADTH_MARKETS:
        # Surface Expansion Guard (Inc 1): breadth is a UNION, not either/or.
        # The old <2 bar let a thin 2-3 market reply pass and shrink the whole
        # geo surface. Now ANY thin reply (< _MIN_BREADTH_MARKETS distinct
        # markets) gets the deterministic metro entries merged in alongside —
        # a run never advances on a surface smaller than the known metro.
        fallback = metro_fallback(location)
        if fallback:
            clean_locs = _sanitize_list(
                clean_locs + fallback, location, _MAX_LOCATION
            )
            fallback_used = True
            logger.info(
                "query-expansion: AI gave only %d distinct markets for %r "
                "(< %d) — metro fallback merged in (union, not either/or)",
                len(_distinct_locs()), location, _MIN_BREADTH_MARKETS,
            )
    if not clean_trades and not clean_locs:
        return {
            "trade_variants": [], "location_variants": [],
            "reason": "LLM returned no usable words (blank / all rejected)",
            "raw_replies": raw_replies, "retried": retried,
        }
    reason = ""
    if fallback_used:
        reason = (
            f"AI gave fewer than {_MIN_BREADTH_MARKETS} distinct markets — "
            "deterministic metro fallback merged into the geo surface"
        )
    logger.info(
        "query-expansion: %d trade variant(s), %d location variant(s) for %r / %r",
        len(clean_trades), len(clean_locs), trade, location,
    )
    return {
        "trade_variants": clean_trades,
        "location_variants": clean_locs,
        "reason": reason,
        "raw_replies": raw_replies,
        "retried": retried,
    }
"""Layer-1 dork-template GENERATION — the add-side of the self-learning loop (Phase H).

Phase G learns which plan-holder dorks WORK and drops the dead ones; it can
never add new angles, so discovery coverage is bounded by the 8 hand-written
dorks. This module closes the loop: an LLM occasionally proposes NEW dork
phrasings, the maintenance script stages them as candidates
(:class:`~app.discovery.template_candidates.TemplateCandidateStore`), and the
plan-holder source dispatches the survivors through the SAME Phase G yield
loop — they earn their place (``working >= PROVEN_GOOD``) or die on evidence,
never by being proposed (§12: a fabricated dork must never run as a proven one).

HARD GUARDS (self-learning design review, 2026-09-08 — user approved):
  * COLD-START: no LLM call until some template has ``>= MIN_TRIALS`` real
    dispatched trials. The LLM must have yield evidence to improve on; calling
    before that would spend a credit to invent angles from nothing (§7).
  * SANITIZE: every proposal must be a real dork shape — the two placeholders
    ``{industry}``/``{location}`` AND a literal ``filetype:pdf`` — and must not
    duplicate an existing static or candidate. Anything else is rejected with a
    logged reason (§12, never let garbage into the pipeline).
  * INERT: a candidate is dispatched only when the yield loop has NOT proven it
    dead. Proposals never bypass validation.
  * HONEST: ``generate_dork_candidates`` always returns an explainable dict
    (generated / rejected / reason). A guard failure, an LLM error, or an empty
    reply is a LOUD zero, never a silent skip (§6).

The LLM transport is injected (``ai_ask: Callable[[str], str]``), defaulting to
the app's ``make_ai_ask()`` (RouterProvider, AI_TIMEOUT_S bound) — tests never
touch the network.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Cold-start threshold — how many real dispatched trials a template needs
#: before the LLM is allowed to propose NEW dork angles. DECOUPLED from
#: ``yield_learning.MIN_TRIALS`` (the trial-count that proves a ZERO-working
#: dork dead and drops it): relaxing the generation gate here must NEVER weaken
#: the dead-dork drop discipline, so this is its OWN constant, lower. The live
#: store already holds ~9 real trials on the static dorks, so 6 clears the gate
#: and the add-side AI can start proposing phrasings the hand-written 8 miss —
#: each proposal then still earns/loses its place through the same yield loop
#: (working >= PROVEN_GOOD promotes, zero-working at the 12-trial gate drops).
GENERATION_MIN_TRIALS = 6

#: How many NEW phrasings one generation call asks for. Bounded: a handful of
#: angles per maintenance run is enough to probe; more would flood the yield
#: loop with trials before any of them accumulates evidence.
_REQUEST_COUNT = (3, 5)

#: A dork is a search-engine query that returns ``.pdf`` URLs (Tavily is
#: semantic and ignores filetype:pdf, so the SOURCE post-filters — but the
#: generated pattern must still TARGET pdfs, else it floods the search lane
#: with non-plan-room pages). Literal, not a regex class.
_REQUIRES = ("filetype:pdf", "{industry}", "{location}")

#: Upper bound on a candidate pattern's length — a legit plan-room phrase is
#: short; a meandering "sentence" is rejected (§12 sanity).
_MAX_DORK_LEN = 200

#: Separator the yield store uses between a template label and its segment in
#: ``all()`` keys (``label|segment``). Mirrors ``yield_learning._SEGMENT_SEP``;
#: global rows carry no separator.
_SEG = "|"


def _segment_rows(all_rows: dict, segment: str) -> dict:
    """The subset of ``store.all()`` belonging to ONE segment.

    ``segment=''`` (the global path) keeps only unsegmented keys — a dork with
    no ``|``-suffix, i.e. its GLOBAL row. A given segment keeps only keys that
    END in ``|segment``. This mirrors the segment hierarchy: a generation pass
    scoped to a segment reads THAT segment's evidence, never a global average.
    """
    if not segment:
        return {k: v for k, v in all_rows.items() if _SEG not in k}
    suffix = f"{_SEG}{segment}"
    return {k: v for k, v in all_rows.items() if k.endswith(suffix)}


def cold_start_guard(store: object, segment: str = "") -> str | None:
    """Reason string to HOLD generation, or None when evidence exists.

    A generation call is worth its LLM credit only when the loop has real
    learning to improve on: at least one dispatched template has reached
    ``GENERATION_MIN_TRIALS`` trials. Until then there is nothing to target —
    return the honest reason so the script reports it instead of silently
    skipping.

    ``segment`` (Phase I) scopes the guard: a targeted pass reads THAT segment's
    yield (a segment with no own trials has no evidence to improve on and HOLDs —
    consistent with the segment hierarchy). Global path (``segment=''``) is
    unchanged.
    """
    try:
        all_rows = store.all()
    except Exception:  # noqa: BLE001 — a broken store must HOLD, never crash.
        return "yield store unavailable (no learning data read)"
    rows = _segment_rows(all_rows, segment) if segment else all_rows
    if not rows:
        scope = f" for segment {segment!r}" if segment else ""
        return (
            f"cold-start guard: no dispatched template{scope} yet "
            f"(need >= {GENERATION_MIN_TRIALS} trials before the LLM is asked)"
        )
    proven = [
        label for label, row in rows.items() if (row or {}).get("trials", 0) >= GENERATION_MIN_TRIALS
    ]
    if not proven:
        scope = f" for segment {segment!r}" if segment else ""
        return (
            f"cold-start guard: no template{scope} has >= {GENERATION_MIN_TRIALS} "
            f"trials yet; the LLM has no yield evidence to improve on"
        )
    return None


def _sanitize_dork(label: str, *, already: set[str]) -> str | None:
    """Validate one LLM-proposed dork; None (logged) when it must not run.

    Hard filters (§12 — a bad proposal must never enter the pipeline):
      * the two placeholders AND ``filetype:pdf`` are all required;
      * no duplicate of a static dork or an already-staged candidate;
      * sanity length cap (a "sentence" is not a dork).
    """
    label = (label or "").strip()
    # Only unwrap when the WHOLE string is a quoted wrapper. A dork's internal
    # ``"phrase"`` quotes are part of the search syntax and must be preserved —
    # stripping a leading ``"`` would corrupt ``"plan holder list" …`` into
    # ``plan holder list" …`` and silently break the dedupe key.
    if len(label) >= 2 and label.startswith('"') and label.endswith('"'):
        label = label[1:-1].strip()
    if not label:
        logger.info("template-gen: rejected empty proposal")
        return None
    for need in _REQUIRES:
        if need not in label:
            logger.info("template-gen: rejected %r — missing %s", label, need)
            return None
    if len(label) > _MAX_DORK_LEN:
        logger.info("template-gen: rejected %r — length %d > %d", label, len(label), _MAX_DORK_LEN)
        return None
    if label in already:
        logger.info("template-gen: rejected %r — already known", label)
        return None
    return label


def _parse_dorks(reply: str) -> list[str]:
    """Pull candidate dork strings out of an LLM reply.

    Accepts quoted strings (the format the prompt requests) plus bare lines
    that carry ``filetype:pdf`` — robust to an LLM that wraps the list in prose.
    """
    if not reply:
        return []
    quoted = re.findall(r'"([^"]+)"', reply)
    candidates: list[str] = [q for q in quoted if "filetype:pdf" in q]
    for line in reply.splitlines():
        line = line.strip().lstrip("-*• \t")
        if "filetype:pdf" in line and line not in candidates:
            candidates.append(line)
    return candidates


def generate_dork_candidates(
    store: object,
    ai_ask: Callable[[str], str] | None = None,
    *,
    static_templates: tuple[str, ...] | None = None,
    candidate_store: object | None = None,
    segment: str = "",
) -> dict[str, Any]:
    """The single generation entry: guarded, scoped, honest.

    Args:
        store: :class:`DiscoveryYieldStore` (the learning evidence).
        ai_ask: ``(prompt) -> str`` LLM transport; defaults to the app's
            ``make_ai_ask()``.
        static_templates: the known static dork set for dedupe + the prompt.
            Defaults to the plan-holder source's ``_DORK_TEMPLATES``.
        candidate_store: optional :class:`TemplateCandidateStore`. When given,
            valid proposals are STAGED (``propose``); the returned dict also
            carries the write result. When None, proposals are returned for the
            caller to stage — tests prefer no-write form.
        segment: (Phase I) the normalized ``segment_key`` to target. When
            given, the cold-start guard and the evidence table read THAT
            segment's yield and the prompt asks for angles that fill that
            segment's coverage gap. Global path (``''``) is unchanged.

    Returns ``{"generated": [...], "rejected": [...], "reason": ""}`` — always
    explainable, never a silent skip.
    """
    if ai_ask is None:
        # The 3rd AI lane rides on its OWN key (AI_API_KEY_3) so it never
        # throttles against the main or deep-research lanes; falls back to
        # AI_API_KEY_2 -> AI_API_KEY when unset (backward compatible).
        from app.ai.gateway import make_ai_ask
        from app.core.config import settings

        ai_ask = make_ai_ask(api_key=settings.AI_API_KEY_3)

    reason = cold_start_guard(store, segment)
    if reason:
        return {"generated": [], "rejected": [], "reason": reason}

    if static_templates is None:
        from app.discovery.sources.plan_holder_source import _DORK_TEMPLATES

        static_templates = _DORK_TEMPLATES

    already: set[str] = set(static_templates)
    if candidate_store is not None:
        already |= {r["label"] for r in candidate_store.all()}

    evidence = _evidence_lines(store, static_templates, segment)
    lo, hi = _REQUEST_COUNT
    scope = (
        f" for the segment {segment!r} (trade | location)"
        if segment else ""
    )
    prompt = (
        "You design search-engine query templates that find PUBLISHED PLAN-"
        "HOLDER / BID-ROSTER PDF lists (public documents that name contractors "
        "who pulled plans for a project). Each template is a search phrase with "
        "the literals filetype:pdf and the two placeholders {industry} and "
        f"{'{location}'}.\n"
        f"Current proven templates and their yield (trials dispatches / working "
        f"leads){scope}:\n"
        f"{evidence}\n"
        f'Propose {lo} to {hi} NEW phrase angles this set misses — different '
        "words a plan room would use, not just the same idea re-worded. Format "
        "answer as one double-quoted template string per line, nothing else. "
        "Each MUST contain filetype:pdf and exactly these placeholders: "
        "{industry} and {location}."
    )
    try:
        reply = ai_ask(prompt)
    except Exception as exc:  # noqa: BLE001 — a dead/provided AI is honest, not fatal
        logger.warning("template-gen: LLM call failed: %s", exc)
        return {"generated": [], "rejected": [], "reason": f"LLM call failed: {exc}"}

    generated: list[str] = []
    rejected: list[str] = []
    for raw in _parse_dorks(reply or ""):
        clean = _sanitize_dork(raw, already=already)
        if clean is None:
            rejected.append(raw.strip())
            continue
        if candidate_store is not None and candidate_store.propose(clean):
            already.add(clean)
            generated.append(clean)
        else:
            rejected.append(clean)  # staged already in a prior run, or dup
    if not generated:
        return {
            "generated": [],
            "rejected": rejected,
            "reason": (
                "returned no usable new dork (blank reply or nothing passed "
                "sanitize/does-not-duplicate)"
            ),
        }
    return {"generated": generated, "rejected": rejected, "reason": ""}


def _evidence_lines(
    store: object, static_templates: tuple[str, ...], segment: str = "",
) -> str:
    """Compact yield table for the LLM prompt (per-template trials/working).

    ``segment`` (Phase I) scopes the table to ONE context's yield: the targeted
    pass reads THAT segment's rows (a generation pass must never average across
    every trade|location), and the GLOBAL pass reads only the GLOBAL rows — a
    segmented row never leaks into the global prompt as a lookalike template. A
    segmented extra row is shown with its label (the ``|segment`` suffix is
    stripped: the whole table is one segment, so it is redundant).
    """
    all_rows = store.all()
    if not all_rows:
        return "  (no recorded yield yet)"
    rows = _segment_rows(all_rows, segment)
    suffix = f"{_SEG}{segment}" if segment else ""
    known = set(static_templates)
    lines = []
    for tpl in static_templates:
        key = tpl if not segment else f"{tpl}{suffix}"
        row = rows.get(key) or {}
        lines.append(f"  # {tpl}  trials={row.get('trials', 0)} working={row.get('working', 0)}")
    extras = [k for k in rows if k not in known]
    for label in sorted(extras):
        row = rows[label] or {}
        shown = label[: -len(suffix)] if suffix and label.endswith(suffix) else label
        lines.append(
            f"  # {shown}  trials={row.get('trials', 0)} working={row.get('working', 0)}"
        )
    return "\n".join(lines) or "  (none)"

# ---------------------------------------------------------------------------
# Layer-2 — WEB-SEARCH ANGLE generation (Inc 2: "AI invents new methods").
# ---------------------------------------------------------------------------

#: Required placeholders for a web angle. Unlike a Layer-1 dork there is NO
#: filetype:pdf requirement — a web angle is a general search phrase aimed at
#: a DIFFERENT KIND of source page (member directory, license roster, bid-
#: notice board, permit portal, award list), which is exactly the point: the
#: AI invents discovery METHODS, not just new phrasings of the one PDF lane.
_WEB_REQUIRES = ("{industry}", "{location}")

#: A proposal carrying ``filetype:`` belongs to the Layer-1 dork lane — reject
#: it here so the two lanes never blur (a pdf-dork running as a web angle
#: floods the search lane with plan-room pages the plan-holder lane already
#: covers).
_WEB_FORBIDS = ("filetype:",)


def _sanitize_web_angle(label: str, *, already: set[str]) -> str | None:
    """Validate one LLM-proposed web angle; None (logged) when rejected.

    Hard filters (§12): both placeholders required, no ``filetype:`` (Layer-1
    lane), no duplicate of an already-staged candidate, length cap.
    """
    label = (label or "").strip()
    if len(label) >= 2 and label.startswith('"') and label.endswith('"'):
        label = label[1:-1].strip()
    if not label:
        logger.info("web-angle-gen: rejected empty proposal")
        return None
    for need in _WEB_REQUIRES:
        if need not in label:
            logger.info("web-angle-gen: rejected %r — missing %s", label, need)
            return None
    for bad in _WEB_FORBIDS:
        if bad in label:
            logger.info("web-angle-gen: rejected %r — %s belongs to the dork lane",
                        label, bad)
            return None
    if len(label) > _MAX_DORK_LEN:
        logger.info("web-angle-gen: rejected %r — length %d > %d",
                    label, len(label), _MAX_DORK_LEN)
        return None
    if label in already:
        logger.info("web-angle-gen: rejected %r — already known", label)
        return None
    return label


def _parse_web_angles(reply: str) -> list[str]:
    """Pull candidate angle strings out of an LLM reply.

    Accepts quoted strings (the requested format) plus bare lines that carry
    both placeholders — robust to an LLM that wraps the list in prose.
    """
    if not reply:
        return []
    quoted = re.findall(r'"([^"]+)"', reply)
    candidates = [
        q for q in quoted
        if "{industry}" in q and "{location}" in q
    ]
    for line in reply.splitlines():
        line = line.strip().lstrip("-*• \t")
        # A bare line that is just a re-statement of a quoted candidate (with
        # its quotes) must not be double-counted — unquote before comparing.
        if len(line) >= 2 and line.startswith('"') and line.endswith('"'):
            line = line[1:-1].strip()
        if ("{industry}" in line and "{location}" in line
                and line not in candidates):
            candidates.append(line)
    return candidates


def generate_web_angle_candidates(
    store: object,
    ai_ask: Callable[[str], str] | None = None,
    *,
    candidate_store: object | None = None,
    segment: str = "",
) -> dict[str, Any]:
    """Layer-2 add-side: the LLM invents NEW WEB-SEARCH discovery angles.

    The user's ask (2026-09-11): the AI must invent new WAYS to find data —
    not just new phrasings of the one plan-holder PDF dork lane. This entry
    asks the LLM for search phrases that surface DIFFERENT KINDS of source
    pages: chamber/member directories, contractor license rosters, bid-
    notice boards, permit portals, award lists. Each proposal carries the
    same ``{industry}``/``{location}`` placeholders and enters the SAME
    earn-or-die lifecycle (:class:`TemplateCandidateStore` layer ``'web'`` +
    the Phase G yield loop) — a proposal is never injected as proven (§12).

    Guards mirror :func:`generate_dork_candidates`: COLD-START (the yield
    store must hold real dispatched trials first), SANITIZE (shape + no
    filetype + no duplicates), HONEST (explainable dict, never a silent skip).
    """
    if ai_ask is None:
        from app.ai.gateway import make_ai_ask
        from app.core.config import settings

        ai_ask = make_ai_ask(api_key=settings.AI_API_KEY_3)

    reason = cold_start_guard(store, segment)
    if reason:
        return {"generated": [], "rejected": [], "reason": reason}

    already: set[str] = set()
    if candidate_store is not None:
        already |= {r["label"] for r in candidate_store.all()}

    evidence = _evidence_lines(store, (), segment)
    lo, hi = _REQUEST_COUNT
    scope = (
        f" for the segment {segment!r} (trade | location)"
        if segment else ""
    )
    prompt = (
        "You design WEB-SEARCH query templates that find pages LISTING "
        "contractor companies — but NOT PDF plan-holder/bid-roster documents "
        "(a different lane already covers those). Think of DIFFERENT KINDS of "
        "public pages that name many contractors at once: chamber-of-commerce "
        "member directories, contractor license rosters from state boards, "
        "bid-notice/project boards, city permit portals, industry association "
        "member lists, award/project galleries, 'top contractors' rankings.\n"
        f"Current proven PDF-dork yield for context (what is already covered)"
        f"{scope}:\n{evidence}\n"
        f"Propose {lo} to {hi} NEW search-phrase angles, each with the "
        "placeholders {industry} and {location} and NO filetype: operator. "
        "Each angle must target a DIFFERENT kind of source page, not the same "
        "idea re-worded. Format answer as one double-quoted template string "
        "per line, nothing else."
    )
    try:
        reply = ai_ask(prompt)
    except Exception as exc:  # noqa: BLE001 — a dead/provided AI is honest, not fatal
        logger.warning("web-angle-gen: LLM call failed: %s", exc)
        return {"generated": [], "rejected": [], "reason": f"LLM call failed: {exc}"}

    generated: list[str] = []
    rejected: list[str] = []
    for raw in _parse_web_angles(reply or ""):
        clean = _sanitize_web_angle(raw, already=already)
        if clean is None:
            rejected.append(raw.strip())
            continue
        if candidate_store is not None and candidate_store.propose(
                clean, layer="web"):
            already.add(clean)
            generated.append(clean)
        else:
            rejected.append(clean)  # staged already in a prior run, or dup
    if not generated:
        return {
            "generated": [],
            "rejected": rejected,
            "reason": (
                "returned no usable new web angle (blank reply or nothing "
                "passed sanitize/does-not-duplicate)"
            ),
        }
    return {"generated": generated, "rejected": rejected, "reason": ""}

"""Leads API — pipeline orchestration (discovery -> AI research).

Promoted from ``scripts/run_query.py`` so that BOTH the CLI script and the
Leads API job runner drive one shared pipeline (no duplication — CLAUDE.md §14).

The user tells the software three things and it runs the whole pipeline:

    WHAT     - the trade to search, e.g. "general contractor", "roofing"
    WHERE    - the location, e.g. "Texas", "Florida"  (any region)
    HOW MANY - target number of emails/leads, e.g. 10, 20, 30

Then, in order:
  Phase 1  DISCOVERY  - plan-holder PDF lists + search providers, looped
             until the target email count is reached (deduplicated).
  Phase 2  RESEARCH   - the full AI pipeline per lead (company -> person ->
             deep -> intent/timing -> scoring).

Every stage reports progress through an optional ``emit(phase, step, total,
message, email, data)`` callback, so a caller (CLI, job runner) can surface
live telemetry. An optional ``cancel()`` callable, checked between leads and
passes, allows a graceful stop.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from app.discovery.query_expansion import (
    _fold,
    generate_query_expansion,
    merge_locations,
    metro_fallback,
)
from app.discovery.sources.plan_holder_source import PlanHolderSource
from app.discovery.sources.status import SourceStatus
from app.discovery.tradefold import normalize_trade
from app.discovery.yield_learning import segment_key
from app.company_profile import get_profile

EmitFn = Callable[[str, int, int, str, str, dict | None], None]
CancelFn = Callable[[], bool]
PauseFn = Callable[[], bool]


def _wait_if_paused(paused: PauseFn | None, cancel: CancelFn | None) -> bool:
    """Block while a job is paused; return False if it was cancelled meanwhile.

    The worker sleeps in short slices so a concurrent ``cancel()`` (or resume)
    is honoured promptly instead of the pipeline spinning or returning early.
    """
    while paused and paused():
        if cancel and cancel():
            return False
        time.sleep(0.3)
    if cancel and cancel():
        return False
    return True


# ---------------------------------------------------------------------------
# Query configuration — the user-facing input contract.
# ---------------------------------------------------------------------------

@dataclass
class ResearchQuery:
    """What / where / how many — everything the software needs to run.

    ``trade``          the trade to search (general contractor, roofing, paving...)
    ``location``       where (Texas, Florida, UK... — any region)
    ``target_emails``  how many emails/leads to aim for (10/20/30...)
    ``discover_only``  stop after discovery, skip the AI research phase
    ``search_name``    OPTIONAL label for THIS run (e.g. "Houston GC Q3"), stored
                       as a tag on every lead it produces so a later search's
                       leads never mix with this one.
    ``folder``         OPTIONAL folder to auto-file every lead this run produces
                       into — set at research-save time so the leads land filed
                       the moment they exist (no unfiled mixing window).
    """
    trade: str = ""
    location: str = ""
    target_emails: int = 20
    discover_only: bool = False
    search_name: str = ""
    folder: str = ""

    def validate(self) -> None:
        if not self.trade.strip():
            raise ValueError("trade (WHAT to search) is required — e.g. 'general contractor'")
        if not self.location.strip():
            raise ValueError("location (WHERE) is required — e.g. 'Texas'")
        if self.target_emails <= 0:
            raise ValueError("target_emails (HOW MANY) must be >= 1")

    def describe(self) -> str:
        label = (
            f"WHAT={self.trade!r} WHERE={self.location!r} "
            f"HOW_MANY={self.target_emails} emails"
        )
        if self.search_name:
            label += f" NAME={self.search_name!r}"
        if self.folder:
            label += f" FOLDER={self.folder!r}"
        return label

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade": self.trade,
            "location": self.location,
            "target_emails": self.target_emails,
            "discover_only": self.discover_only,
            "search_name": self.search_name,
            "folder": self.folder,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ResearchQuery":
        return ResearchQuery(
            trade=d.get("trade", ""),
            location=d.get("location", ""),
            target_emails=int(d.get("target_emails", 20)),
            discover_only=bool(d.get("discover_only", False)),
            search_name=d.get("search_name", ""),
            folder=d.get("folder", ""),
        )


# ---------------------------------------------------------------------------
# Phase 1 — discovery, looped until the email target is met.
# ---------------------------------------------------------------------------

# Alternate trade phrasings tried in later passes so we can reach a target
# larger than a single discovery pass yields (broadening, never guessing).
def _trade_variants(trade: str) -> list[str]:
    base = trade.strip()
    variants = [base]
    if not base.endswith("s"):
        variants.append(base + "s")  # singular -> plural
    elif base.endswith("ies"):
        variants.append(base[:-3] + "y")  # "companies" -> "company"
    return variants


class _SkipAwarePlanHolder(PlanHolderSource):
    """PlanHolderSource whose ``discover`` carries the run's per-pass skip set.

    The orchestrator contract calls ``discover(industry, location, limit)`` —
    no ``skip_pdfs`` slot. This tiny adapter injects the run's already-seen
    PDF set into the plan-holder lane so passes ADVANCE to unseen documents
    instead of re-parsing the same ones (the 15/50 stall root cause).
    """

    def __init__(
        self,
        skip_pdfs: set[str] | None = None,
        yield_store: Any | None = None,
        candidate_store: Any | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(yield_store=yield_store, candidate_store=candidate_store, **kw)
        self._skip = set(skip_pdfs or ())

    def discover(
        self, *, industry: str, location: str, limit: int,
    ) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
        return super().discover(
            industry=industry, location=location, limit=limit,
            skip_pdfs=self._skip,
        )


def _build_discovery_orchestrator(
    skip_pdfs: set[str] | None = None,
    yield_store: Any | None = None,
    candidate_store: Any | None = None,
) -> Any:
    """Build the multi-source discovery orchestrator (CLAUDE.md §8).

    EVERY live discovery source is registered and tried in priority order —
    the leads pipeline never depends on a single source. The fixture bridge
    is deliberately NOT registered: this pipeline researches real emails, and
    a fixture fallback would silently trade live discovery for synthetic
    companies (CLAUDE.md §1, §12). ``yield_store`` (Phase G) and
    ``candidate_store`` (Phase H) are threaded into the plan-holder lane so
    dork dispatch is learned and LLM-generated dorks are consumed.
    """
    from app.discovery.source_orchestrator import SourceOrchestrator
    from app.discovery.sources.directory_crawl_source import DirectoryCrawlSource
    from app.discovery.sources.search_provider_source import SearchProviderSource

    orch = SourceOrchestrator()
    orch.register(DirectoryCrawlSource())
    orch.register(_SkipAwarePlanHolder(
        skip_pdfs, yield_store=yield_store, candidate_store=candidate_store,
    ))
    # Inc 2: the SAME learning stores ride the web-search lane so LLM-invented
    # web angles (layer 'web') dispatch, rotate, and earn/die in the Phase G
    # yield loop exactly like the plan-holder dorks.
    orch.register(SearchProviderSource(
        yield_store=yield_store, candidate_store=candidate_store,
    ))
    return orch


def _discovery_status(meta: dict[str, Any]) -> SourceStatus:
    """Aggregate per-source statuses into the pipeline's ONE status.

    SUCCESS if ANY source returned companies. Otherwise EMPTY — for the pass
    loop, "I looked and nothing was found" and "I could not look" both mean
    this pass produced no new leads; the honest WHY rides in ``meta``.
    """
    stats = meta.get("source_stats") or {}
    if any(
        stats.get(name, {}).get("status") == SourceStatus.SUCCESS
        for name in stats
    ):
        return SourceStatus.SUCCESS
    return SourceStatus.EMPTY


def _source_stats_for_log(meta: dict[str, Any]) -> dict[str, Any]:
    """Extract a lean, JSON-serialisable per-source snapshot from orchestrator meta.

    The full ``meta["source_stats"]`` may carry non-serialisable objects
    (``SourceStatus`` enums, nested metadata with dorks lists, etc.).  This
    keeps only the auditable facts — status, result count, error, and the
    specific reason — so ``json.dumps`` in the job runner never crashes
    (CLAUDE.md §6: every execution must be diagnosable from the logs).
    """
    stats: dict[str, Any] = {}
    for name, s in (meta.get("source_stats") or {}).items():
        status = s.get("status")
        status_str = status.value if hasattr(status, "value") else str(status)
        entry: dict[str, Any] = {"status": status_str, "results": s.get("results", 0)}
        if s.get("error"):
            entry["error"] = s["error"]
        reason = (s.get("metadata") or {}).get("reason")
        if reason:
            entry["reason"] = reason
        stats[name] = entry
    return stats


def run_discovery(
    trade: str,
    location: str,
    limit: int,
    skip_pdfs: set[str] | None = None,
    yield_store: Any | None = None,
    candidate_store: Any | None = None,
) -> tuple[SourceStatus, list[dict], dict]:
    """One multi-source live discovery pass for trade+location.

    Executes the full orchestrator (directory crawl → plan-holder PDFs → web
    search). ``skip_pdfs`` advances the plan-holder lane to documents not yet
    parsed this run, exactly as before — the difference is the crawler and
    search lanes now contribute alongside it, so a re-run of the same query
    surfaces genuinely NEW companies instead of the same pool (the user's
    "same result every time" complaint). ``yield_store`` (Phase G) drives the
    plan-holder lane's learned-dork gate; ``candidate_store`` (Phase H) feeds
    LLM-generated dorks not proven dead into the same lane.
    """
    # Phase H add-side now LIVE: propose NEW dorks into the yield loop once per
    # run (bounded), so discovery can exceed the 8 static templates as the 3rd
    # AI learns phrasings the hand-written set misses. Staged candidates
    # dispatch through the SAME proof/drop loop (never injected as proven — §12);
    # the cold-start guard holds until real trial evidence exists.
    _run_discovery_generation(yield_store, candidate_store)

    orch = _build_discovery_orchestrator(skip_pdfs, yield_store, candidate_store)
    companies, meta = orch.discover(
        industry=trade, location=location, limit=limit,
    )

    # Thread the plan-holder lane's PDF URLs to the top level so the pass loop
    # keeps advancing skip_pdfs across passes (same contract as before).
    plan_stats = (meta.get("source_stats") or {}).get("plan_holder", {})
    pdf_urls = (plan_stats.get("metadata") or {}).get("pdf_urls") or []
    meta["pdf_urls"] = pdf_urls

    status = _discovery_status(meta)
    if status != SourceStatus.SUCCESS:
        meta["reason"] = (
            meta.get("fallback_reason")
            or "no_live_source_returned_companies"
        )
    logger.info(
        "run_discovery: %s — %d company record(s) for trade=%r loc=%r "
        "(sources=%s)", status.value, len(companies), trade, location,
        ",".join((stats.get("status").value if hasattr(stats.get("status"), "value") else str(stats.get("status")) for stats in (meta.get("source_stats") or {}).values())),
    )
    return status, companies, meta


#: Phase H bound — how many live, non-dropped candidate dorks may be staged
#: before the add-side AI pauses. One generation call proposes up to ~5 new
#: angles and generation pauses once the queue is full, so the yield loop gets
#: to prove/drop them before the AI invents more — never an unbounded pile of
#: unproven queries flooding the plan-holder lane (CLAUDE.md §7/§12). The
#: dead-dork drop gate still prunes zero-working candidates at MIN_TRIALS.
_DISCOVERY_GEN_CAP = 6


def _run_discovery_generation(
    yield_store: Any | None,
    candidate_store: Any | None,
    *,
    segment: str = "",
) -> dict[str, Any] | None:
    """One bounded add-side generation pass — 3rd AI proposes NEW dorks.

    The Phase H add-side, no longer maintenance-script-only: wired into LIVE
    discovery via :func:`run_discovery`. Runs BOTH lanes independently — the
    Layer-1 dork pass (:func:`generate_dork_candidates`) and the Inc 2 Layer-2
    web-angle pass (:func:`generate_web_angle_candidates`), each paused by its
    OWN cap. Returns None when the feature is off (either store missing) or
    BOTH lanes are at cap; otherwise the honest merged result dict (§6 — a
    guard hold or no-useful-proposals is a LOUD zero, never a silent skip).
    Bounded: never fires while ``>= _DISCOVERY_GEN_CAP`` candidates of a lane
    are staged, so a run stages a handful and later runs wait for the yield
    loop to prove/drop before proposing more.
    """
    if yield_store is None or candidate_store is None:
        return None
    try:
        active = [
            r for r in candidate_store.all() if r.get("status") != "dropped"
        ]
        active_web = [r for r in active if r.get("layer") == "web"]
        active = [r for r in active if r.get("layer") != "web"]
    except Exception:  # noqa: BLE001 — a broken store must not crash discovery
        logger.debug("discovery gen: candidate count unavailable", exc_info=True)
        active = []
        active_web = []
    from app.discovery.template_generation import (
        generate_dork_candidates,
        generate_web_angle_candidates,
    )

    # Layer-1 dork pass — pauses alone at its own cap. It must NOT abort the
    # whole add-side: a full dork queue says nothing about the web-angle lane
    # (live proof 2026-09-11: 9 dorks staged -> web angles never generated).
    result: dict[str, Any] | None = None
    if len(active) >= _DISCOVERY_GEN_CAP:
        logger.info(
            "discovery gen: %d dork candidate(s) staged (cap %d) — dork add-side paused",
            len(active), _DISCOVERY_GEN_CAP,
        )
    else:
        try:
            result = generate_dork_candidates(
                yield_store, candidate_store=candidate_store, segment=segment,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("discovery gen: failed: %s", exc)
            result = {"generated": [], "rejected": [],
                      "reason": f"generation error: {exc}"}
        if result.get("reason"):
            logger.info("discovery gen: held — %s", result["reason"])
        elif result.get("generated"):
            logger.info(
                "discovery gen: staged %d new dork(s): %s",
                len(result["generated"]), ", ".join(result["generated"]),
            )
        else:
            logger.info("discovery gen: LLM returned no usable new dork")

    # Inc 2 — Layer-2 web angles: the AI invents NEW discovery METHODS (member
    # directories, license rosters, bid boards…), same earn-or-die lifecycle.
    # Separate cap so a full dork queue never blocks method invention.
    web_result: dict[str, Any] | None = None
    if len(active_web) < _DISCOVERY_GEN_CAP:
        try:
            web_result = generate_web_angle_candidates(
                yield_store, candidate_store=candidate_store, segment=segment,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("web-angle gen: failed: %s", exc)
            web_result = {"generated": [], "rejected": [],
                          "reason": f"generation error: {exc}"}
        if web_result.get("reason"):
            logger.info("web-angle gen: held — %s", web_result["reason"])
        elif web_result.get("generated"):
            logger.info(
                "web-angle gen: staged %d new angle(s): %s",
                len(web_result["generated"]), ", ".join(web_result["generated"]),
            )
        else:
            logger.info("web-angle gen: LLM returned no usable new angle")
    if web_result is not None:
        # Merge both layers' outcomes into ONE honest result dict (§6): the
        # caller logs/surfaces the add-side pass as a whole.
        result = {
            "generated": list((result or {}).get("generated") or [])
            + list(web_result.get("generated") or []),
            "rejected": list((result or {}).get("rejected") or [])
            + list(web_result.get("rejected") or []),
            "reason": (result or {}).get("reason") or web_result.get("reason") or "",
            "web_generated": web_result.get("generated") or [],
        }
    return result


def _domain_from_website(website: str) -> str:
    """Company domain from a website URL, or '' when none is derivable."""
    text = website.strip() if website else ""
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"
    try:
        return (urlparse(text).netloc or "").lower().removeprefix("www.")
    except ValueError:
        return ""


#: Base website email-hunt per pass. Scales dynamically with remaining demand
#: (see _email_probes_for_demand); this is the floor so small runs don't waste
#: probes on a handful of companies.
_BASE_EMAIL_PROBES_PER_PASS = 12


def _email_probes_for_demand(remaining: int, n_locs: int = 1) -> int:
    """Scale website probes per pass with remaining demand.

    Small runs (≤20 remaining) use the base cap — a handful of companies per
    pass is enough.  Large runs (100+) need a wider probe set per pass to
    surface enough emails; the formula doubles the base at ~100 remaining and
    plateaus at 4× the base (~48) to avoid runaway fetch volume.  The loc
    count divides the budget so multi-market sweeps don't over-probe in each
    metro.
    """
    base = _BASE_EMAIL_PROBES_PER_PASS
    scale = max(1.0, min(4.0, remaining / 50.0))
    return max(base, int(base * scale)) // max(1, n_locs)


#: Generic mailboxes are a real company email but a poor research start —
#: prefer a named address (john@acme.com) over info@acme.com when the site
#: exposes both, so person-attribution has something to anchor on.
_GENERIC_MAILBOX_PREFIXES = (
    "info", "admin", "contact", "sales", "office", "mail",
    "webmaster", "postmaster", "hello", "support", "enquiries",
)


def _find_site_email(discover: Callable[[str], Any], url: str) -> str:
    """Find ONE real email on a company's website, or ''."""
    website = url if url.startswith(("http://", "https://")) else f"https://{url}"
    try:
        result = discover(website)
    except Exception:  # noqa: BLE001 - an uncooperative website is not fatal
        return ""
    emails = (result or {}).get("emails") or []
    if not emails:
        return ""
    for e in emails:
        local = e.split("@", 1)[0].lower().strip()
        if local and local not in _GENERIC_MAILBOX_PREFIXES:
            return e
    return emails[0]


def _fold_record_trade(record: dict) -> str:
    """Fold a discovery record's trade to a canonical slug (P2) — the
    record's OWN evidence only, strongest first: the classifier/source
    label, then the company name. The name fallback is the SAME weak
    evidence the P1 boot backfill uses, so a row's trade is identical
    whether it is read fresh here or by the backfill after a later boot.
    '' = honest unknown (no gate exception — see the trade gate)."""
    return (
        normalize_trade(record.get("trade_category", ""))
        or normalize_trade(record.get("company_name", ""))
    )


def company_records_to_leads(
    records: list[dict],
    *,
    email_discover: Callable[[str], Any] | None = None,
    max_probes: int = _BASE_EMAIL_PROBES_PER_PASS,
) -> tuple[list[dict], dict[str, int]]:
    """Convert discovery records (ANY source) into ``(email, domain)`` leads.

    Plan-holder records carry real emails directly (pure path, no network).
    Website-only records (search providers, directory crawls) are PROBED for
    a real public email on the company's site — the CLAUDE.md §10 target flow
    ("visit websites → extract decision makers"), bounded to ``max_probes``
    per pass. A company with no resolvable email is DROPPED HONESTLY (counted
    in ``stats``) — a lead without an address cannot be researched. A site is
    only probed when it has a genuine ``website``; a bare directory profile
    URL is not a company domain and is never turned into a lead.

    Returns ``(leads, stats)`` with ``stats = {plan_emails, probed, with_email,
    no_email}`` so discovery telemetry shows the honest breakdown.
    """
    from app.email.email_discovery import EmailDiscovery

    discover = email_discover or EmailDiscovery().discover

    plan_leads = extract_email_leads(records)
    plan_keys = {(l["email"], l["domain"]) for l in plan_leads}

    # Website-only candidates — a REAL company website is required (not a
    # directory profile page), bounded to the per-pass working set.
    candidates = [
        (i, r) for i, r in enumerate(records)
        if "plan_holder" not in r and (r.get("website") or "").strip()
    ][:max_probes]

    website_leads: list[dict] = []
    stats: dict[str, int] = {
        "plan_emails": len(plan_leads),
        "probed": len(candidates),
        "with_email": 0,
        "dup_plan": 0,
        "no_email": 0,
    }

    def _probe(item: tuple[int, dict]) -> tuple[int, dict | None]:
        i, record = item
        website = record["website"]
        email = _find_site_email(discover, website)
        if not email:
            return i, None
        return i, {
            "email": email,
            "domain": _domain_from_website(website),
            "company": record.get("company_name", ""),
            "person": "",
            "source_url": record.get("source_url") or website,
            # Layer-1 dork attribution (Phase G), passed through for credit.
            "_discovery_dork": record.get("_discovery_dork", ""),
            # P1 trade routing: the record's trade label (classifier or
            # source-native) folds to a canonical slug here so it flows into
            # the pending cache — previously DROPPED at exactly this seam
            # (the cross-trade leak's root cause #2). '' = honest unknown.
            "trade": _fold_record_trade(record),
        }

    if candidates:
        with ThreadPoolExecutor(max_workers=6) as pool:
            for res in pool.map(_probe, candidates):
                i, lead = res
                if lead is None:
                    stats["no_email"] += 1
                    continue
                key = (lead["email"], lead["domain"])
                if key in plan_keys:
                    # The site HAS this address but the plan-holder lane already
                    # captured it — not a new lead, and NOT a no-email site.
                    # Count it honestly (CLAUDE.md §6) so telemetry never blends
                    # "no public email" with "already known".
                    stats["dup_plan"] += 1
                    continue
                plan_keys.add(key)
                website_leads.append(lead)
                stats["with_email"] += 1

    return plan_leads + website_leads, stats


def extract_email_leads(records: list[dict]) -> list[dict]:
    """Pull (email, domain) leads from plan-holder metadata, deduplicated."""
    leads: list[dict] = []
    seen: set[str] = set()
    for rec in records:
        ph = rec.get("plan_holder") or {}
        domain = ph.get("domain") or ""
        for entry in ph.get("emails") or []:
            email = (entry.get("email") or "").strip()
            if "@" not in email:
                continue
            key = (email, domain)
            if key in seen:
                continue
            seen.add(key)
            leads.append(
                {
                    "email": email,
                    "domain": domain,
                    "company": rec.get("company_name", ""),
                    "person": (ph.get("person") or {}).get("name", ""),
                    "source_url": rec.get("source_url", ""),
                    # Layer-1 dork attribution (Phase G): the dork template that
                    # surfaced this lead's PDF, for working-lead credit.
                    "_discovery_dork": rec.get("_discovery_dork", ""),
                    # P1 trade routing (same fold as the website lane above).
                    "trade": _fold_record_trade(rec),
                }
            )
    return leads


def _intake_pools(
    store: Any | None, user_id: str = ""
) -> tuple[set[str] | None, set[str] | None]:
    """The delete-suppression + lead-exclusivity pools for one run.

    ``deleted`` — every email a user deleted (any reason) that the admin has
    not Restored: a deleted lead must never resurface to ANY user ("jo b data
    ay wo cache ma store hota rahe taay wo data dobara na dikhy").

    ``taken`` — lead exclusivity: emails owned by ANOTHER user ("ak lead ya
    email sirf ak user ko show honi chahye"). Empty user_id (CLI / legacy)
    disables only this half — suppression applies regardless of who runs.

    Both are computed ONCE per run and checked in-memory at the intake gates
    (the same cost discipline as the dead/cooling pools). Fake stores in
    tests that lack the methods get honest Nones (feature is additive).
    """
    if store is None:
        return None, None
    deleted: set[str] | None = None
    taken: set[str] | None = None
    _deleted_emails = getattr(store, "deleted_emails", None)
    if callable(_deleted_emails):
        try:
            deleted = _deleted_emails()
        except Exception:  # noqa: BLE001 — a broken pool must not kill a run
            logger.warning("deleted_emails() unavailable", exc_info=True)
    if user_id:
        _taken = getattr(store, "taken_by_others", None)
        if callable(_taken):
            try:
                taken = _taken(user_id)
            except Exception:  # noqa: BLE001
                logger.warning("taken_by_others() unavailable", exc_info=True)
    return deleted, taken


def discover_until_target(
    query: ResearchQuery,
    max_passes: int = 3,
    emit: EmitFn | None = None,
    cancel: CancelFn | None = None,
    paused: PauseFn | None = None,
    pending_store: Any | None = None,
    dossier_store: Any | None = None,
    *,
    target_override: int | None = None,
    attempted: set[tuple[str, str]] | None = None,
    seen_pdf_urls: set[str] | None = None,
    seen_domains: set[str] | None = None,
    cooldown_seconds: int = 0,
    user_id: str = "",
) -> tuple[list[dict], list[dict]]:
    """Run discovery passes until a target of emails is gathered.

    Returns ``(leads, pass_log)``. Each pass widens the trade phrasing a
    little AND only pulls plan-holder PDFs not yet parsed in this run
    (``skip_pdfs`` is threaded into :func:`run_discovery`), so passes genuinely
    ADVANCE instead of re-pulling the same documents. All leads are accumulated
    and deduplicated. Never guesses — only real rows parsed from real
    plan-holder PDFs count. When a pass surfaces no unseen PDFs at all the
    loop stops honestly (``exhausted``) instead of burning credits on
    tautological repeats. ``paused`` blocks between passes until resumed or
    cancelled.

    ``seen_domains`` (MUTATED IN PLACE, like ``attempted``/``seen_pdf_urls``)
    is the same "advance, don't re-serve" guarantee applied to the SEARCH and
    CRAWL lanes. After a pass returns company records, any record whose company
    domain was already seen THIS RUN is dropped BEFORE the expensive website
    email-probe (``company_records_to_leads``) — so a drained search/crawl pool
    stops being re-crawled pass after pass. Without this, the search lane
    re-crawls largely the same companies each pass/round and only dedups AFTER
    the crawl (the "wahi data baar-baar" grind).

    DISCOVERY CACHE (credit saver): when ``pending_store`` (a PendingLeadsStore)
    is provided, previously-discovered surplus leads for this location are
    served first — no live search is paid for until the cache is exhausted.
    Newly discovered leads are ALL stocked into the cache (so a surplus beyond
    the target is reusable on a later run), and already-researched emails
    (present in ``dossier_store``) and confirmed-dead emails (``dead`` flag)
    are never served or re-added — a dead domain is never a recurring "Skip".

    KEYWORD ARGS (working-target support, CLAUDE.md §10 "jitni quantity likho
    utna working data"): ``target_override`` sets a smaller gather target (a
    top-up round only needs the shortfall); ``attempted``/``seen_pdf_urls`` are
    MUTATED IN PLACE so repeated rounds advance past already-gathered emails
    and already-parsed PDFs instead of re-serving the same pool.
    """
    target = target_override if target_override is not None else query.target_emails
    leads: list[dict] = []
    seen = attempted if attempted is not None else set()
    pdfs_seen = seen_pdf_urls if seen_pdf_urls is not None else set()
    seen_domains = seen_domains if seen_domains is not None else set()
    pass_log: list[dict] = []
    from_cache = 0
    stale_purged = 0
    # P2 trade routing: the SEARCHED trade, folded once. '' = the search names
    # no canonical trade (admin free text outside the 14) — fail-open, NO gate
    # (a labeling gap must never starve a run). Non-empty = every serve below
    # (cache and fresh) is this trade ONLY; other-trade discoveries are stocked
    # into their own pools for their own consumers, never shown here.
    search_trade = normalize_trade(query.trade)

    # Confirmed-dead emails — filtered out of FRESH discovery too, so a dead
    # address that a live source happens to surface again is never re-researched
    # (the recurring "Skip" flood). Rows stay in pending, just invisible.
    dead_pool = pending_store.dead_emails() if pending_store is not None else None

    # Emails inside the re-enrichment cooldown — also excluded from FRESH
    # discovery: a research-ERROR lead that a live source surfaces again should
    # not be re-researched and re-failed within the same cooldown window.
    cooling_pool = (
        pending_store.cooling_emails(cooldown_seconds)
        if pending_store is not None and cooldown_seconds
        else set()
    )

    # Learned-source prune: a discovery source host that has surfaced
    # MIN_TRIALS researched leads and NEVER once produced a real lead is
    # auto-dropped at ingestion — the self-correcting generalization of the
    # hand-seeded non-client-source list (CLAUDE.md §7/§8: the loop replaces the
    # hardcode). Backed by the SAME DB the agent records outcomes into, so the
    # signal is exactly what research proved. Unavailable (stub store, no DB
    # path) → None → no prune, discovery unchanged.
    _fit_learning = None
    _fit_db = getattr(dossier_store, "_db_path", None) if dossier_store is not None else None
    if _fit_db is not None:
        try:
            from app.lead_research.fit_learning import FitLearningStore

            _fit_learning = FitLearningStore(_fit_db)
        except Exception:
            logger.debug("fit-learning source prune unavailable", exc_info=True)
            _fit_learning = None

    # Delete-suppression + lead-exclusivity pools (2026-09-12): a deleted lead
    # never resurfaces for anyone; a lead owned by ANOTHER user never lands on
    # this run ("ak lead sirf ak user ko"). Computed once, checked at every
    # intake gate below.
    deleted_pool, taken_pool = _intake_pools(dossier_store, user_id)

    # Layer-1 dork-yield store (Phase G): same db as the research verdicts, so
    # the dispatch recorded here and the working-lead credit committed at
    # research time read one consistent record. Unavailable (stub store, no db
    # path) -> None -> no learned-dork gate, discovery unchanged.
    _discovery_yield = None
    if _fit_db is not None:
        try:
            from app.discovery.yield_learning import DiscoveryYieldStore

            _discovery_yield = DiscoveryYieldStore(_fit_db)
        except Exception:
            logger.debug("discovery dork-yield store unavailable", exc_info=True)
            _discovery_yield = None

    # Phase H — generated-dork candidates (LLM loop). Same db as the yield
    # store: the proposal lifecycle lives beside the evidence that promotes or
    # drops it. Unavailable -> None -> static dorks only, feature off.
    _discovery_candidates = None
    if _fit_db is not None:
        try:
            from app.discovery.template_candidates import TemplateCandidateStore

            _discovery_candidates = TemplateCandidateStore(_fit_db)
        except Exception:
            logger.debug("discovery candidate store unavailable", exc_info=True)
            _discovery_candidates = None

    # Phase A — serve the discovery cache first (free, no search).
    if pending_store is not None:
        # Take a wider window than the target: purging happens from the oldest
        # end, and dropping stale rows must not starve the window of enough
        # keeps to reach the target without running live discovery.
        cached = pending_store.take(
            max(target * 2, 20), location=query.location,
            cooldown_seconds=cooldown_seconds, trade=search_trade,
        )
        for l in cached:
            # RESEARCHED STALE ROWS: the user's "same result every run" flood is
            # half-caused by the discovery cache serving leads that are ALREADY
            # dossiers — they shadow genuinely-new discovery and eat research
            # credit re-researching them. Purge them now so they never re-appear,
            # and count the purge honestly in telemetry.
            if dossier_store is not None and dossier_store.get(l["email"]) is not None:
                pending_store.remove([l["email"]])
                stale_purged += 1
                continue
            # DELETE-SUPPRESSION: a deleted lead (any reason, any user) never
            # resurfaces — purge the stale cache row so it stops shadowing.
            if deleted_pool is not None and l["email"] in deleted_pool:
                pending_store.remove([l["email"]])
                stale_purged += 1
                continue
            # LEAD EXCLUSIVITY: another user owns this email — it is not this
            # run's lead ("ak lead sirf ak user ko"). Drop the stale row.
            if taken_pool is not None and l["email"] in taken_pool:
                pending_store.remove([l["email"]])
                continue
            key = (l["email"], l["domain"])
            if key in seen:
                continue
            seen.add(key)
            leads.append(l)
            from_cache += 1
            if len(leads) >= target:
                break

    variants = _trade_variants(query.trade)
    # PDFs already parsed this run. Threaded into run_discovery so each pass
    # advances to unseen documents; also the exhaustion signal (a pass that
    # surfaces zero unseen PDFs has nothing left to offer — stop honestly).
    for pass_idx in range(max_passes):
        if len(leads) >= target:
            break
        if not _wait_if_paused(paused, cancel):
            break
        trade = variants[pass_idx % len(variants)]
        loc = query.location
        t0 = time.monotonic()
        # Report the search BEFORE the (often slow) first pass: run_discovery
        # does real network work — search + directory crawl + plan-holder PDF
        # fetches (each +25s) — and emits nothing until it returns, so a
        # long first pass used to sit at "running, 0 results" and read as
        # "searching never started" (§6: report in-flight work, not just
        # completed work).
        if emit:
            emit(
                "discovery", pass_idx + 1, max_passes,
                f"pass {pass_idx + 1}: searching {trade!r} in {loc!r}…",
                data={"trade": trade, "location": loc, "phase": "searching"},
            )
        status, records, meta = run_discovery(
            trade, loc, max(target * 4, 4), skip_pdfs=pdfs_seen,
            yield_store=_discovery_yield, candidate_store=_discovery_candidates,
        )
        pass_urls = meta.get("pdf_urls") or []
        pdfs_seen.update(pass_urls)
        # SEARCH/CRAWL-LANE DEDUP (Fix A): drop company records whose domain
        # this run has ALREADY seen BEFORE the expensive website email-probe.
        # The search and directory-crawl lanes re-surface the same companies
        # pass after pass; filtering by domain here (instead of after the
        # probe) is what stops the "wahi companies baar-baar crawl" grind —
        # the web-lane equivalent of ``skip_pdfs`` for the plan-holder lane.
        # Plan-holder records carry real emails directly, so their domains are
        # recorded too — a plan email's company is never re-probed by a later
        # search pass. Every domain seen this run is remembered (MUTATED), so
        # later rounds and passes advance instead of re-serving the pool.
        unseen_records: list[dict] = []
        seen_domain_count = 0
        for rec in records:
            # Plan-holder records carry real emails DIRECTLY and are deduped at
            # the EMAIL level (extract_email_leads + dup_plan), so their derived
            # website must NEVER put them through the seen_domains gate — a
            # later PDF re-listing a firm from pass 1 surfaces that firm's OTHER
            # real contacts, which are worth researching (the code's own comment
            # below: "one company can have many real contacts"). Without this a
            # run tops out after its first +N wave and the market's remaining
            # working contacts are silently dropped (CLAUDE.md §7 root cause).
            if rec.get("plan_holder"):
                unseen_records.append(rec)
                continue
            # Only WEBSITE-bearing records (the search/crawl lanes) are domain-
            # deduped: they are the ones PROBED for an email, so re-serving the
            # same company website pass after pass is the wasteful re-crawl.
            website = (rec.get("website") or "").strip()
            if not website:
                unseen_records.append(rec)
                continue
            dom = _domain_from_website(website)
            if dom:
                if dom in seen_domains:
                    seen_domain_count += 1
                    continue
                seen_domains.add(dom)
            unseen_records.append(rec)
        if seen_domain_count and emit:
            emit(
                "discovery", pass_idx + 1, max_passes,
                f"pass {pass_idx + 1}: skipped {seen_domain_count} already-seen "
                f"company domain(s) before crawl (no re-probe)",
                data={"seen_domains_skipped": seen_domain_count,
                      "trade": trade, "location": loc},
            )

        # Multi-source conversion: plan-holder records contribute real emails
        # directly; website-only records (search/crawl lanes) are probed for a
        # public address — bounded per pass and honestly counted in disco_stats
        # (CLAUDE.md §10: visit websites, extract what's real). The fresh
        # filter ALSO drops already-researched emails at discovery TIME, so a
        # re-run of the same query surfaces genuinely NEW dossiers instead of
        # the researched flood that "kaisi research hai" complained about.
        new_leads, disco_stats = company_records_to_leads(unseen_records)
        raw_fresh = [
            l for l in new_leads
            if (l["email"], l["domain"]) not in seen
            and (dossier_store is None or dossier_store.get(l["email"]) is None)
            and (dead_pool is None or l["email"] not in dead_pool)
            and l["email"] not in cooling_pool
            # DELETE-SUPPRESSION: a user-deleted email never re-enters the
            # funnel, whatever the source surfaces again.
            and (deleted_pool is None or l["email"] not in deleted_pool)
            # LEAD EXCLUSIVITY: an email another user already owns is not
            # this run's lead (fresh discovery normally never sees these —
            # the dossier exists — this closes the delete/restore races).
            and (taken_pool is None or l["email"] not in taken_pool)
        ]

        # INGESTION GATE (root cause of the "data jo services se match nahi
        # karta" flood): a freshly-discovered lead whose company string, domain,
        # or source host is a known non-client (A/E/C consultant, software/IT,
        # transit/mobility authority, ...) is dropped BEFORE it is stoked into
        # the discovery cache or served to research — so a DCTA transit-vendor
        # row never spends a research credit, and the orphan transport junk
        # never appears in Contacts. Boundary is the ONE profile definition
        # (CLAUDE.md §11); every drop is logged + counted (CLAUDE.md §6).
        non_client_drops = 0
        fresh: list[dict] = []
        for l in raw_fresh:
            src = l.get("source_url", "")
            # Learned skip (Phase E): a source host that never produced a lead
            # (MIN_TRIALS) OR a company/domain the USER deleted as not-a-client
            # (one rejection is decisive). Same drop-at-intake cost discipline:
            # a rejected company never re-spends a research credit.
            learning_skip = (
                _fit_learning is not None
                and (
                    _fit_learning.should_skip_source(src)
                    or _fit_learning.should_skip_company(l.get("company", ""))
                    or _fit_learning.should_skip_domain(l.get("domain", ""))
                )
            )
            if (
                get_profile().lead_is_non_client(
                    company=l.get("company", ""),
                    domain=l.get("domain", ""),
                    source_url=src,
                )
                or learning_skip
            ):
                non_client_drops += 1
                logger.info(
                    "Discovery drop (not our client): %s%s%s from %s",
                    l["email"],
                    f" ({l.get('company')})" if l.get("company") else "",
                    f" @ {l.get('domain')}" if l.get("domain") else "",
                    (src or "")[:90],
                )
                continue
            fresh.append(l)

        # Stock the discovery cache with ALL fresh leads, so any surplus is
        # reusable on a later run without paying for the same search again.
        # Cross-trade leads are stocked too (P2 pools): a drywall search that
        # surfaces GC companies keeps them for a GC consumer — never wastes
        # them, never shows them here.
        if pending_store is not None:
            pending_store.add([{**l, "location": loc} for l in fresh])

        # TRADE GATE (P2): when the search names a canonical trade, only that
        # trade's fresh leads SERVE this run — the "drywall search showed GC
        # data" fix. Unknown-trade ('') leads are stocked above but not
        # served: an unknown trade is not this trade. Counted honestly so the
        # pass log shows exactly where the discovery went.
        trade_matching = [
            l for l in fresh if not search_trade or l.get("trade") == search_trade
        ]
        trade_stocked_other = len(fresh) - len(trade_matching)

        for l in trade_matching:
            if len(leads) >= target:
                break
            seen.add((l["email"], l["domain"]))
            leads.append(l)
        exhausted = status == SourceStatus.EMPTY
        entry = {
            "pass": pass_idx + 1,
            "trade": trade,
            "location": loc,
            "status": status.value if hasattr(status, "value") else str(status),
            "pdfs_found": len(pass_urls) or meta.get("pdfs_found", 0),
            "rows": len(records),
            "plan_emails": disco_stats.get("plan_emails", 0),
            "probed": disco_stats.get("probed", 0),
            "with_email": disco_stats.get("with_email", 0),
            "dup_plan": disco_stats.get("dup_plan", 0),
            "no_email": disco_stats.get("no_email", 0),
            # P2 honest counting: new_leads = SERVED this pass (this run's
            # trade only); trade_stocked_other = fresh leads banked into OTHER
            # trades' pools (never shown to this run — pool currency, not loss).
            "new_leads": len(trade_matching),
            "trade_stocked_other": trade_stocked_other,
            "non_client_drops": non_client_drops,
            "total_leads": len(leads),
            "elapsed_s": round(time.monotonic() - t0, 1),
            "source_stats": _source_stats_for_log(meta),
        }
        if exhausted:
            entry["exhausted"] = True
            # Keep the exact WHY (no_unseen_pdfs vs no_pdf_results vs
            # no_rows_in_pdfs) so the stop is auditable, never silent.
            entry["reason"] = meta.get("reason", "exhausted")
        pass_log.append(entry)
        if emit:
            suffix = f" [exhausted — {entry['reason']}]" if exhausted else ""
            pools_note = (
                f", {trade_stocked_other} banked to other-trade pool(s)"
                if trade_stocked_other else ""
            )
            emit(
                "discovery", pass_idx + 1, max_passes,
                f"pass {pass_idx + 1}: trade={trade!r} loc={loc!r} "
                f"[{entry['status']}] new={len(trade_matching)} "
                f"total={len(leads)}{pools_note}{suffix}",
                data=entry,
            )
        if exhausted:
            # Honest stop: the source surfaced nothing it had not already shown.
            # Re-running the same 2 phrasings would just re-burn credits (the
            # 15/50 stall). Report it, do not grind.
            break

    if (from_cache or stale_purged) and emit:
        emit(
            "discovery", 0, max_passes,
            f"served {from_cache} lead(s) from discovery cache (no search paid)"
            + (f" — purged {stale_purged} already-researched" if stale_purged else ""),
            data={"from_cache": from_cache, "stale_purged": stale_purged,
                  "location": query.location},
        )
    return leads[: target], pass_log


#: Hard cap on working-target top-up rounds in :func:`run_full`. A drained or
#: low-quality source must STOP honestly below target — grinding more rounds
#: would burn credits searching the same pool without adding working leads.
_MAX_WORKING_ROUNDS = 6

#: Streaming (Phase D) plateau guard — the async equivalent of Fix C. In the
#: serial loop a "round that added no working leads" is easy to count; in the
#: streaming pipeline the producer adds continuously while consumers research
#: asynchronously, so the honest no-progress signal is the CONSECUTIVE number
#: of researched leads that produced nothing visibly working (skip-score or
#: dead domain). Two thresholds: after ``_STREAM_PLATEAU_NONWORKING`` non-
#: working results in a row the run is clearly grinding against a skip pool
#: (non-clients slip past intake, dead domains, score-skips) — the producer
#: STOPS honestly instead of streaming the same dead end forever ("data
#: hamesha milna chahye" never means "keep piling up worthless rows").
_STREAM_PLATEAU_NONWORKING = 8


def _is_visible(dossier: Any) -> bool:
    """True when a researched dossier will actually SHOW in the leads list.

    This is the user's "working lead" definition — the count ``run_full``
    drives toward. It MUST agree with the leads page, which re-gates stored
    dossiers through ``regate_recommendation`` (dead domain / non-construction /
    sub-threshold are hidden as ``skip``). A fallback to the stored
    recommendation covers non-LeadDossier doubles used in unit tests.
    """
    from app.lead_research.scoring import regate_recommendation

    try:
        return regate_recommendation(dossier) != "skip"
    except (AttributeError, TypeError):
        return getattr(dossier, "recommendation", "") != "skip"


# ---------------------------------------------------------------------------
# Phase 2 — AI research pipeline per lead.
# ---------------------------------------------------------------------------

def run_research(
    leads: list[dict],
    emit: EmitFn | None = None,
    cancel: CancelFn | None = None,
    store: Any | None = None,
    paused: PauseFn | None = None,
    pending_store: Any | None = None,
    *,
    trade: str = "",
    location: str = "",
    search_name: str = "",
    folder: str = "",
    user_id: str = "",
) -> list[dict]:
    """Run the full AI research pipeline for each lead.

    ``trade``/``location`` are the query's known construction context; they are
    threaded into each lead's company research so the AI anchors on the
    construction-bid provenance (a plan-holder/bid-list lead) instead of
    drifting to generic/IT results.

    ``search_name``/``folder`` (optional) auto-file every NEWLY-researched
    lead as it is saved: the folder is set, and ``search_name`` is stored as a
    tag. Because the file happens AT SAVE TIME (right after ``store.save``),
    a run that names a folder never leaves its leads sitting unfiled in the
    inbox — the user's "is sa leads mix ni hogi" guarantee. Already-cached
    leads are skipped, so their existing meta is never clobbered.


    Returns a list of per-lead result dicts (the contract the API/frontend
    read). One lead's failure never kills the batch (CLAUDE.md §6 honest
    logging: each lead logs what happened). ``paused`` blocks between leads
    until resumed or cancelled.

    When ``store`` (a LeadResearchStore) is provided, each researched dossier
    is persisted there — this is what feeds the leads list/detail endpoints.
    The CLI passes no store (behavior unchanged); the job runner does.

    Cross-run dedup: when a store is provided, already-researched emails
    are skipped (their existing dossier is reused) so re-runs of the same
    query do not burn LLM credits on the same leads.

    When ``pending_store`` (a PendingLeadsStore) is provided, a successfully
    researched lead is removed from the discovery cache so it is not served
    again on a future run.
    """
    from app.lead_research.agent import AILeadResearchAgent
    from app.lead_research.service import is_dead_domain_dossier
    from app.core.config import settings

    # Deterministic query-yield loop: when a real dossier store exists (the
    # job runner / API path), the learn loop persists to the SAME db — its
    # yield table rides in the dossiers file. Construction stays BARE so a
    # test stub that replaces AILeadResearchAgent with a no-arg fake keeps
    # working; the loop is enabled post-construction only when the agent is
    # the real class AND the store carries a db path (real LeadResearchStore).
    agent = AILeadResearchAgent()
    _yield_db = getattr(store, "_db_path", None) if store is not None else None
    if _yield_db is not None and hasattr(agent, "enable_query_yield"):
        agent.enable_query_yield(_yield_db)
    if _yield_db is not None and hasattr(agent, "enable_fit_learning"):
        agent.enable_fit_learning(_yield_db)
    # Delete-suppression + lead-exclusivity pools (2026-09-12) — enforced again
    # at research time because a lead can be deleted (or taken by another
    # user's run) AFTER discovery buffered it.
    deleted_pool, taken_pool = _intake_pools(store, user_id)
    # Layer-1 dork-yield store (Phase G): same db as the query/fit learning.
    _discovery_yield = None
    if _yield_db is not None:
        try:
            from app.discovery.yield_learning import DiscoveryYieldStore

            _discovery_yield = DiscoveryYieldStore(_yield_db)
        except Exception:  # noqa: BLE001 - learning is best-effort
            logger.debug("discovery dork-yield store unavailable", exc_info=True)
            _discovery_yield = None
    total = len(leads)

    # SQLite write serialization. Research (network/LLM) runs concurrently, but
    # store.save / set_meta and the pending-cache mutations must not collide on
    # the same .db file — one write lock for the whole batch, held only for the
    # short persistence window (never around research, so the parallelism is
    # not squandered).
    write_lock = threading.Lock()

    def _work(idx: int) -> tuple[int, dict] | None:
        """Research ONE lead (run on a pool thread).

        Mirrors the original serial body exactly. Returns ``(idx, entry)`` for
        entries that belong in the results list (cached / success / error) and
        ``None`` for ones the serial loop omitted (cancelled, or a dead-domain
        skip). The caller keeps original order by position.
        """
        i = idx + 1
        lead = leads[idx]
        # Pause/cancel is honoured per lead, exactly as in the serial loop: a
        # cancelled run aborts in-flight work and never adds those leads.
        if not _wait_if_paused(paused, cancel):
            return None
        email, domain = lead["email"], lead["domain"]
        t0 = time.monotonic()

        # DELETE-SUPPRESSION (research-time): a lead deleted mid-run (buffered
        # before the delete) never re-enters the store for ANY user — the
        # admin's Restore is the only way back.
        if deleted_pool and email in deleted_pool:
            with write_lock:
                if pending_store is not None:
                    pending_store.remove([email])
            logger.info("Research skip (user-deleted): %s", email)
            return None
        # LEAD EXCLUSIVITY (research-time): an email another user already owns
        # is not this run's lead — no owner stamp, no dashboard entry, no
        # re-research ("ak lead ya email sirf ak user ko show honi chahye").
        if taken_pool and email in taken_pool:
            with write_lock:
                if pending_store is not None:
                    pending_store.remove([email])
            logger.info("Research skip (owned by another user): %s", email)
            return None

        # Cross-run dedup: if this email was already researched, reuse it.
        if store is not None:
            existing = store.get(email)
            if existing is not None:
                # EXCLUSIVITY RACE GUARD: another user's run may have saved
                # this email AFTER our intake snapshot was taken — a live
                # ownership check, not the stale pool ("ak lead sirf ak user
                # ko"). Not ours -> skip silently, no owner stamp.
                _owned_by_another = getattr(store, "owned_by_another", None)
                if (user_id and callable(_owned_by_another)
                        and _owned_by_another(email, user_id)):
                    with write_lock:
                        if pending_store is not None:
                            pending_store.remove([email])
                    logger.info(
                        "Research skip (raced to another user): %s", email)
                    return None
                with write_lock:
                    # Cross-user claim (Phase 2, exclusivity-gated): a dossier
                    # with NO other owner — the caller's search surfaced it,
                    # so the lead lands on their dashboard without a
                    # re-research.
                    if user_id:
                        store.add_owner(email, user_id)
                    if pending_store is not None:
                        pending_store.remove([email])
                entry = {
                    "email": email,
                    "domain": domain,
                    "company": existing.company.name,
                    "person": existing.person.name,
                    "bound": existing.person.bound,
                    "score": existing.potential_score,
                    "recommendation": existing.recommendation,
                    "intent": existing.intent.needs_estimation if existing.intent else "",
                    "timing": existing.timing.window if existing.timing else "",
                    "sources_checked": existing.sources_checked,
                    "elapsed_s": 0.0,
                    "cached": True,
                    # Already-researched: counts as done, never as NEW working
                    # output for this run's target (the user asks "existing ko
                    # skip karo, naye pakro").
                    "working": False,
                }
                if emit:
                    emit(
                        "research", i, total,
                        f"lead {i}/{total}: {email} -> CACHED (already researched)",
                        email=email,
                        data=entry,
                    )
                return idx, entry

        try:
            d = agent.research(
                email, domain, trade=trade, location=location,
                source_url=lead.get("source_url", ""),
            )
            if is_dead_domain_dossier(d):
                # Dead-domain leads are NOT leads — the address cannot receive
                # email. Never persist them, never count them in totals. The
                # pending cache is MARKED dead (row kept, never served again),
                # so the same address can never re-appear as a recurring "Skip"
                # run after run — the user's "same emails every search" defect.
                # Log the skip honestly (CLAUDE.md §6) instead of silent drop.
                with write_lock:
                    if pending_store is not None:
                        pending_store.mark_dead([email])
                if emit:
                    emit(
                        "research", i, total,
                        f"lead {i}/{total}: {email} -> SKIPPED (dead/expired "
                        f"domain — no MX record; flagged dead, never served again)",
                        email=email,
                        data={
                            "email": email,
                            "recommendation": "skip",
                            "reason": "dead_domain",
                            "working": False,
                        },
                    )
                return None
            if store is not None:
                with write_lock:
                    store.save(d, user_id=user_id)
                    if folder or search_name:
                        # AUTO-FILE AT SAVE TIME (Phase C): the run named a
                        # folder and/or search — apply them to this lead RIGHT
                        # NOW so it never sits unfiled in the inbox. Tags =
                        # search_name (this run's label), so a later search's
                        # leads never mix with this one. Pure user metadata —
                        # dossier_json untouched.
                        store.set_meta(
                            email,
                            folder=folder,
                            tags=[search_name] if search_name else [],
                        )
                    if pending_store is not None:
                        pending_store.remove([email])
            elapsed = time.monotonic() - t0
            working = _is_visible(d)
            # Layer-1 dork-yield credit (Phase G): a dork-attributed company
            # that becomes a WORKING lead credits its producing dork. Only
            # with a dork label — an unattributable lead credits nothing
            # (silence is not evidence).
            if working and _discovery_yield is not None and lead.get("_discovery_dork"):
                _discovery_yield.record_working(
                    lead["_discovery_dork"], segment_key(trade, location)
                )
            entry = {
                "email": email,
                "domain": domain,
                "company": d.company.name,
                "person": d.person.name,
                "bound": d.person.bound,
                "score": d.potential_score,
                "recommendation": d.recommendation,
                "intent": d.intent.needs_estimation if d.intent else "",
                "timing": d.timing.window if d.timing else "",
                "sources_checked": d.sources_checked,
                "elapsed_s": round(elapsed, 1),
                # True = this newly-researched dossier WILL show in the leads
                # list. Only these count toward the user's target quantity.
                "working": working,
            }
            if emit:
                emit(
                    "research", i, total,
                    f"lead {i}/{total}: {email} -> company={d.company.name[:40]!r} "
                    f"person={d.person.name!r} bound={d.person.bound} "
                    f"score={d.potential_score} rec={d.recommendation}",
                    email=email,
                    data=entry,
                )
            return idx, entry
        except Exception as exc:  # noqa: BLE001 - one lead never kills the batch
            # Cooldown mark: the lead stays in pending (it is not a dead
            # domain), so WITHOUT this timestamp the next run would serve and
            # re-fail it — the same full research cost on every Execute. Record
            # the attempt so take() skips it until the cooldown expires, then
            # the row is a genuine retry (re-enrichment cooldown).
            with write_lock:
                if pending_store is not None:
                    pending_store.mark_attempt([email])
            entry = {"email": email, "domain": domain, "error": str(exc)}
            if emit:
                emit("research", i, total, f"lead {i}/{total}: {email} -> ERROR: {exc}",
                     email=email, data=entry)
            return idx, entry

    # Bounded thread pool over the (network/LLM-bound) research stages. Results
    # come back in submission order, so the list keeps the original lead order
    # regardless of which thread finished first; omitted entries (None) were
    # cancelled or dead-domain skips.
    concurrency = max(1, settings.LEADS_CONCURRENCY)
    results: list[dict] = []
    if concurrency == 1 or total <= 1:
        # Serial path — identical behaviour, avoids pool overhead on tiny runs.
        for idx in range(total):
            res = _work(idx)
            if res is not None:
                results.append(res[1])
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for res in pool.map(_work, range(total)):
                if res is not None:
                    results.append(res[1])

    skipped = sum(1 for e in results if e.get("cached"))
    if skipped:
        logger.info(
            "run_research: %d/%d leads skipped (already in store)", skipped, total,
        )
    return results


# ---------------------------------------------------------------------------
# Full run — both phases, one call.
# ---------------------------------------------------------------------------

def _entry_from_dossier(d: Any) -> dict[str, Any]:
    """A run-result entry built directly from a stored dossier — the same
    shape run_research produces (plus an ``instant`` marker), so the job
    feed / frontend needs no special case for served-from-pool leads."""
    return {
        "email": d.email,
        "domain": d.domain,
        "company": d.company.name,
        "person": d.person.name,
        "bound": d.person.bound,
        "score": d.potential_score,
        "recommendation": d.recommendation,
        "intent": d.intent.needs_estimation if d.intent else "",
        "timing": d.timing.window if d.timing else "",
        "sources_checked": d.sources_checked,
        "elapsed_s": 0.0,
        # Instant-served dossiers were re-gated VISIBLE at serve time and
        # are new-to-this-user (no prior owner) — genuine working output.
        "working": True,
        "instant": True,
    }


def _instant_serve(
    query: "ResearchQuery",
    emit: EmitFn | None = None,
    store: Any | None = None,
    pending_store: Any | None = None,
    user_id: str = "",
) -> list[dict[str, Any]]:
    """P7 Phase 0: claim SHARED already-researched dossiers instantly.

    :meth:`LeadResearchStore.serve_shared` does the SQL + the race-guarded
    ownership claim; this helper turns each claimed dossier into a result
    entry (auto-filed into the run's folder/tags, removed from the pending
    buffer so discovery never re-surfaces it) and emits it through the
    normal ``research`` phase so the live feed shows it like any lead.
    """
    served = store.serve_shared(
        query.target_emails,
        trade=normalize_trade(query.trade), location=query.location,
        user_id=user_id,
    )
    entries: list[dict[str, Any]] = []
    for i, d in enumerate(served, 1):
        # AUTO-FILE AT SERVE TIME — the same policy as research-save time,
        # so instant leads never sit unfiled in the inbox.
        if query.folder or query.search_name:
            store.set_meta(
                d.email, folder=query.folder,
                tags=[query.search_name] if query.search_name else [],
            )
        if pending_store is not None:
            pending_store.remove([d.email])
        entry = _entry_from_dossier(d)
        entries.append(entry)
        if emit:
            emit(
                "research", i, len(served),
                f"lead {i}/{len(served)}: {d.email} -> INSTANT "
                f"(already researched in the shared pool)",
                email=d.email, data=entry,
            )
    return entries


def run_full(
    query: ResearchQuery,
    emit: EmitFn | None = None,
    cancel: CancelFn | None = None,
    store: Any | None = None,
    paused: PauseFn | None = None,
    pending_store: Any | None = None,
    *,
    cooldown_seconds: int | None = None,
    user_id: str = "",
) -> dict[str, Any]:
    """Run discovery (looped to target) then AI research, in one call.

    Returns the full outcome dict:
        {query, trade, location, target_emails, leads_found,
         discovery_passes, results, working_leads, requested, shortfall}

    WORKING TARGET (CLAUDE.md §10 / the user's "jitni quantity likho utna
    working data"): the run does NOT stop when ``target_emails`` raw emails
    are gathered — that counted dead/refined-out emails too, so the visible
    result was short of what was asked. It now loops TOP-UP ROUNDS: gather a
    batch (cache + live), research it, count only the dossiers that will
    actually SHOW in the leads list (``working`` — dead-domain and skip-score
    do not count), and if the working count is short of the target, gather
    ANOTHER batch. The loop stops when the target is reached, the user
    cancels, or the sources are drained (honest ``shortfall`` + reason —
    never fabricated to hit the number).

    ``store`` is threaded through to ``run_research`` so dossiers persist.
    When ``store`` is provided, a ``pending_store`` (discovery cache) is
    derived from the same DB file unless one is passed explicitly — surplus
    discovered leads are cached and reused on later runs (credit saver).
    ```paused`` blocks between passes/leads until resumed or cancelled.
    """
    if store is not None and pending_store is None:
        from app.lead_research.service import PendingLeadsStore
        pending_store = PendingLeadsStore(db_path=store._db_path)

    # Re-enrichment cooldown: default from configuration, callable override
    # (tests pass a tiny window instead of 24h).
    if cooldown_seconds is None:
        from app.core.config import settings
        cooldown_seconds = settings.LEAD_REENRICHMENT_COOLDOWN_SECONDS

    # Shared across rounds: emails already handed to research, PDFs already
    # parsed, AND company domains already seen — so each round ADVANCES
    # instead of re-serving the same pool (Fix A: the search/crawl lanes stop
    # re-crawling the same companies pass after pass).
    attempted: set[tuple[str, str]] = set()
    seen_pdf_urls: set[str] = set()
    seen_domains: set[str] = set()

    def discover_for(remaining: int, max_passes: int) -> tuple[list[dict], list[dict]]:
        return discover_until_target(
            query, max_passes=max_passes, emit=emit, cancel=cancel, paused=paused,
            pending_store=pending_store, dossier_store=store,
            target_override=remaining, attempted=attempted,
            seen_pdf_urls=seen_pdf_urls, seen_domains=seen_domains,
            cooldown_seconds=cooldown_seconds,
            user_id=user_id,  # lead exclusivity + delete-suppression pools
        )

    def base_passes() -> int:
        # Scale discovery passes with target: more leads → more passes to find
        # them. Each pass draws on THREE lanes — directory crawl + plan-holder
        # PDFs + web search — and probes website emails directly, so fewer
        # passes are needed per target. The target/10 divisor caps credit burn
        # on the search lane (§6 honest telemetry; §8 no provider dependency).
        return max(5, query.target_emails // 10)

    # discover-only: same contract as before — gather raw emails, no research.
    if query.discover_only:
        leads, pass_log = discover_for(query.target_emails, base_passes())
        return {
            "query": query.describe(),
            "trade": query.trade,
            "location": query.location,
            "target_emails": query.target_emails,
            "leads_found": len(leads),
            "discovery_passes": pass_log,
            "results": [],
            "working_leads": 0,
        }

    # PHASE 0 (P7 instant serve): claim SHARED, already-researched dossiers
    # FIRST — pure SQL + an ownership mark, zero AI spend. The harvester
    # (P6) stocks that pool around the clock; a user's search now serves
    # from it instantly. Pool alone fulfils the target -> the run IS
    # instant (no discovery, no research). Partial fulfilment -> discovery
    # only chases the REMAINDER.
    original_query = query
    instant: list[dict[str, Any]] = []
    if store is not None and user_id:
        instant = _instant_serve(
            query, emit=emit, store=store, pending_store=pending_store,
            user_id=user_id,
        )
        if len(instant) >= query.target_emails:
            return {
                "query": query.describe(),
                "trade": query.trade,
                "location": query.location,
                "target_emails": query.target_emails,
                "leads_found": len(instant),
                "discovery_passes": [],
                "results": instant,
                "working_leads": len(instant),
                "requested": query.target_emails,
                "shortfall": 0,
                "shortfall_reason": "",
                "instant_served": len(instant),
            }
        if instant:
            # Discovery chases only the remainder of the ORIGINAL target.
            query = replace(query, target_emails=query.target_emails - len(instant))

    # PHASE D — streaming producer/consumer. When a real persistence store AND a
    # real discovery cache are present AND research runs concurrently, run_full
    # switches to the continuous pipeline: a producer thread streams freshly
    # discovered leads into the pending buffer while N consumer threads research
    # directly out of it (never idling between batches), the producer stops once
    # the working target is met, and the leftover buffer is DRAINED so surplus
    # data is researched and shown on the frontend instead of skipped (the
    # user's "data hamesha milna chahye / kabi khatam na ho / jo bacha ho wo bhi
    # research ho k dikhe" requirement). The serial round-loop below stays for
    # the CLI / concurrency=1 / no-cache paths — identical behavior, no overlap.
    from app.core.config import settings as _cfg
    if (pending_store is not None and store is not None
            and max(1, _cfg.LEADS_CONCURRENCY) > 1):
        outcome = _run_full_streaming(
            query, emit=emit, cancel=cancel, store=store, paused=paused,
            pending_store=pending_store, cooldown_seconds=cooldown_seconds,
            user_id=user_id,
        )
        if instant:
            # Merge the instant serve back on top: the run's real delivery is
            # instant pool leads + freshly discovered ones, measured against
            # the ORIGINAL target.
            outcome["results"] = instant + outcome["results"]
            outcome["working_leads"] = (
                int(outcome.get("working_leads") or 0) + len(instant))
            outcome["leads_found"] = len(outcome["results"])
            outcome["shortfall"] = max(
                0, original_query.target_emails - outcome["working_leads"])
            outcome["target_emails"] = original_query.target_emails
            outcome["requested"] = original_query.target_emails
            outcome["query"] = original_query.describe()
        outcome["instant_served"] = len(instant)
        return outcome

    pass_log: list[dict] = []
    results: list[dict] = []
    working = 0
    shortfall_reason = ""
    # Fix C — no-progress guard. The user's "endless loop / same data over and
    # over" symptom is really a PLATEAU: when the discovery pool is drained or
    # keeps returning non-client leads, a top-up round adds ZERO new working
    # leads (everything comes back cached / dead / scored-skip). Grinding the
    # remaining rounds re-discovers the same pool for nothing. Track consecutive
    # no-progress rounds and stop honestly after TWO of them — each round that
    # DOES add working leads resets the counter.
    no_progress_rounds = 0
    for _round in range(_MAX_WORKING_ROUNDS):
        if working >= query.target_emails:
            break
        if not _wait_if_paused(paused, cancel):
            break
        remaining = query.target_emails - working
        leads, dlog = discover_for(max(remaining, 1), max(5, remaining // 10))
        pass_log += dlog
        if not leads:
            # Nothing more to gather: the cache is drained / dead / already
            # researched and live discovery surfaced nothing unseen. Honest stop.
            shortfall_reason = (dlog[-1].get("reason") if dlog else "") or "no_more_leads"
            break
        prev_working = working
        results += run_research(
            leads, emit=emit, cancel=cancel, store=store, paused=paused,
            pending_store=pending_store,
            trade=query.trade, location=query.location,
            search_name=query.search_name, folder=query.folder,
            user_id=user_id,
        )
        working = sum(1 for e in results if e.get("working"))
        if working <= prev_working:
            # This round added no new visible leads — the pool is plateauing
            # (drained, dead, or only non-clients left). Two in a row = stop.
            no_progress_rounds += 1
            if no_progress_rounds >= 2:
                shortfall_reason = "no_progress_plateau"
                break
        else:
            no_progress_rounds = 0
    else:
        shortfall_reason = "max_topup_rounds_reached"

    if instant:
        # Merge the instant serve back on top: the run's real delivery is
        # instant pool leads + freshly discovered ones, measured against the
        # ORIGINAL target.
        results = instant + results
        working += len(instant)

    return {
        "query": original_query.describe(),
        "trade": query.trade,
        "location": query.location,
        "target_emails": original_query.target_emails,
        "leads_found": len(results),
        "discovery_passes": pass_log,
        "results": results,
        "working_leads": working,
        "requested": original_query.target_emails,
        "shortfall": max(0, original_query.target_emails - working),
        "shortfall_reason": shortfall_reason,
        "instant_served": len(instant),
    }


def _run_full_streaming(
    query: ResearchQuery,
    emit: EmitFn | None = None,
    cancel: CancelFn | None = None,
    store: Any | None = None,
    paused: PauseFn | None = None,
    pending_store: Any | None = None,
    *,
    cooldown_seconds: int | None = None,
    user_id: str = "",
) -> dict[str, Any]:
    """Streaming producer/consumer run_full (Phase D).

    The user's architecture — "1st AI keeps finding data and storing it in ONE
    place while the 2nd/3rd research without stopping" — is exactly this: the
    pending buffer IS the one place.

      * PRODUCER thread: runs live discovery passes (directory crawl + plan-
        holder PDFs + web search) and stocks every fresh lead into
        ``pending_store`` (the buffer). It stops when the WORKING target is
        met, the user cancels/pauses, or discovery is genuinely exhausted.
      * CONSUMER threads (settings.LEADS_CONCURRENCY): pull DISTINCT pending
        leads out of the buffer and research them (triage -> company -> person
        -> intent -> scoring -> save). A consumer only exits when the producer
        is DONE and the buffer is empty — so leftover buffered data is drained
        and shown on the frontend, never skipped ("jo bacha ho wo bhi research
        ho k dikhe"). Between batches a consumer idle-waits ~0.3s, it never
        stops working while the producer is still filling.
      * The ``working`` count is the shared demand signal: the producer watches
        it to know when the user's quantity is met; consumers bump it whenever
        a newly-researched dossier will SHOW in the leads list.

    This overlaps discovery and research (the true speedup — more agents in
    series would only be slower; real speed is parallelism). The result dict
    keeps the same contract as :func:`run_full`, so the job runner / API are
    unchanged.
    """
    from app.core.config import settings
    from app.lead_research.agent import AILeadResearchAgent
    from app.lead_research.fit_learning import FitLearningStore
    from app.lead_research.service import is_dead_domain_dossier

    if cooldown_seconds is None:
        cooldown_seconds = settings.LEAD_REENRICHMENT_COOLDOWN_SECONDS
    concurrency = max(1, settings.LEADS_CONCURRENCY)
    target_emails = query.target_emails

    # ONE agent shared across consumer threads — the same sharing model as
    # run_research's pool. Query/fit learning persist to the dossiers file.
    agent = AILeadResearchAgent()
    _yield_db = getattr(store, "_db_path", None) if store is not None else None
    if _yield_db is not None:
        if hasattr(agent, "enable_query_yield"):
            agent.enable_query_yield(_yield_db)
        if hasattr(agent, "enable_fit_learning"):
            agent.enable_fit_learning(_yield_db)
    _fit_learning = None
    if _yield_db is not None:
        try:
            _fit_learning = FitLearningStore(_yield_db)
        except Exception:  # noqa: BLE001 - learning is best-effort
            logger.debug("fit-learning source prune unavailable", exc_info=True)
            _fit_learning = None
    # Layer-1 dork-yield store (Phase G) — same db, one consistent record.
    _discovery_yield = None
    if _yield_db is not None:
        try:
            from app.discovery.yield_learning import DiscoveryYieldStore

            _discovery_yield = DiscoveryYieldStore(_yield_db)
        except Exception:  # noqa: BLE001 - learning is best-effort
            logger.debug("discovery dork-yield store unavailable", exc_info=True)
            _discovery_yield = None
    # Phase H — LLM-generated dork candidates (same db, one consistent record).
    _discovery_candidates = None
    if _yield_db is not None:
        try:
            from app.discovery.template_candidates import TemplateCandidateStore
            _discovery_candidates = TemplateCandidateStore(_yield_db)
        except Exception:  # noqa: BLE001
            logger.debug("discovery candidate store unavailable", exc_info=True)
            _discovery_candidates = None

    # Shared run state. ``claimed`` guards pending_store's peek-not-pop take()
    # so two consumers never research the same buffered row; ``write_lock``
    # serializes ALL SQLite writes (producer add + consumer save/remove/mark).
    write_lock = threading.Lock()
    state_lock = threading.Lock()
    claimed: set[str] = set()
    attempted: set[tuple[str, str]] = set()
    seen_domains: set[str] = set()
    seen_pdfs: set[str] = set()
    producer_done = False
    shortfall_reason = ""
    working = 0
    results: list[dict] = []
    pass_log: list[dict] = []
    consecutive_nonworking = 0
    plateau_flag = False

    # Dead / cooling pools snapshot once — same exclusion the serial pipeline
    # applies to FRESH discovery (a dead or recently-failed address never
    # re-enters the buffer through a later pass).
    with write_lock:
        dead_pool = pending_store.dead_emails()
        cooling_pool = (
            pending_store.cooling_emails(cooldown_seconds)
            if cooldown_seconds else set()
        )
    # Delete-suppression + lead-exclusivity pools (2026-09-12) — the streaming
    # intake gates below drop deleted emails and emails owned by another user
    # ("ak lead sirf ak user ko"; a deleted lead never resurfaces for anyone).
    deleted_pool, taken_pool = _intake_pools(store, user_id)
    # P2 trade routing: the SEARCHED trade, folded once — same gate as the
    # serial twin. '' = search names no canonical trade -> NO gate (fail-open).
    search_trade = normalize_trade(query.trade)

    def _claim_next(batch: int) -> dict | None:
        """Return one NOT-already-claimed pending lead (peek-not-pop guarded).

        ``pending_store.take`` leaves rows in pending until ``remove``, so two
        consumers could grab the SAME row; ``claimed`` (per-run, in-memory)
        makes each claim exclusive. Takes a window of ``batch`` and returns the
        first row no other consumer has claimed, or None when every row in the
        window is already in flight (or the buffer is empty).

        TRADE GATE (P2): the take is trade-filtered, so only this run's trade
        is claimable — other-trade buffer rows stay banked in their pools.
        """
        with write_lock:
            rows = pending_store.take(
                batch, location=query.location,
                cooldown_seconds=cooldown_seconds, trade=search_trade,
            )
        for r in rows:
            # DELETE-SUPPRESSION: a deleted lead is never claimed, and its
            # stale buffer row is purged so it stops shadowing the window.
            if deleted_pool is not None and r["email"] in deleted_pool:
                with write_lock:
                    pending_store.remove([r["email"]])
                continue
            # LEAD EXCLUSIVITY: another user owns this email — not this run's
            # lead; purge the stale row (its owner's research removed it from
            # their own buffer, so it is dead weight for everyone).
            if taken_pool is not None and r["email"] in taken_pool:
                with write_lock:
                    pending_store.remove([r["email"]])
                continue
            if r["email"] not in claimed:
                claimed.add(r["email"])
                return r
        return None

    def _produce() -> None:
        """Fill the buffer with fresh discovery until demand is met / drained."""
        nonlocal shortfall_reason, plateau_flag, consecutive_nonworking
        variants = _trade_variants(query.trade)
        loc_variants = [query.location]
        # Phase J — AI-expand the query surface ONCE per job so discovery never
        # exhausts a fixed (trade, location) point: each pass then rotates a
        # genuinely different trade/location wording, surfacing a different PDF
        # set. On no key / LLM failure this degrades to the literal trade +
        # location (exact pre-Phase-J behavior), logged honestly (§6).
        try:
            qe = generate_query_expansion(query.trade, query.location)
        except Exception as exc:  # noqa: BLE001 - expansion never breaks a run
            logger.info("query-expansion: disabled for this run: %s", exc)
            qe = {"trade_variants": [], "location_variants": [],
                  "reason": f"expansion error: {exc}"}
        if qe.get("trade_variants"):
            variants = qe["trade_variants"]
        if qe.get("location_variants"):
            # Geo breadth (Phase K): never let an AI rewrite replace or narrow
            # the user's OWN market out of the run. The literal location is
            # always searched first, then every distinct AI metro variant — so a
            # run covers "San Antonio TX" AND its surrounding counties / cities
            # instead of grinding one county (the discovery_exhausted starve).
            loc_variants = merge_locations(query.location, qe["location_variants"])
        if qe.get("reason"):
            logger.info("query-expansion: %s", qe["reason"])
        # Phase K2 telemetry: surface WHAT the AI actually returned (raw replies
        # included) into the job event stream. A weak/blank expansion — the
        # literal-only starvation — is then diagnosable from History instead of
        # being a silent run (§6): the honest surface AND every raw AI reply
        # the expansion consumed (retries and all).
        if emit:
            emit(
                "expansion", 1, 1,
                f"query surface: {len(variants)} trade wording(s) "
                f"× {len(loc_variants)} location wording(s)"
                f"{' · retried' if qe.get('retried') else ''}"
                f"{' · ' + qe.get('reason', '') if qe.get('reason') else ''}",
                data={
                    "trade_variants": variants,
                    "location_variants": loc_variants,
                    "retried": bool(qe.get("retried")),
                    "reason": qe.get("reason") or "",
                    "raw_replies": qe.get("raw_replies") or [],
                },
            )
        n_trades = max(1, len(variants))
        n_locs = max(1, len(loc_variants))
        # ONE round = ONE systematic sweep over the WHOLE trade × location
        # surface (Phase K pass-rotation fix). The old rotation capped each
        # round at 6 passes — fewer than n_trades=8 — so pass_idx // n_trades
        # never reached 1: locations NEVER advanced past loc_variants[0], the
        # later trade wordings (6-7) were never searched, and a dry first
        # location killed the whole run while 7 metro markets sat untouched.
        # The live 100-target San Antonio run proved it: 12 passes, every
        # event location='San Antonio TX', the 7 expanded locations never
        # searched, shortfall 98 discovery_exhausted. THIS sweep consumes the
        # WHOLE surface; a round ends only once every location × trade wording
        # has actually been searched.
        sweep = n_trades * n_locs
        # Scale rounds with target: a 1000-lead run needs many more full-surface
        # sweeps than a 50-lead run.  Each round covers every trade × location
        # pair (sweep passes), so the round count governs how many times the
        # ENTIRE geo surface is re-searched.
        #
        # The FLOOR is 8, not 3: the research plateau needs
        # _STREAM_PLATEAU_NONWORKING (8) consecutive non-working results BEFORE
        # it can be read at round end.  On a tiny sweep (a 2-pass grid) that
        # needs ~4-5 rounds just to manifest; capping rounds below that would
        # exhaust the loop and fling the run into the "discovery_exhausted"
        # fallback BEFORE the true stop condition (no_progress_plateau) is ever
        # observed.  8 rounds is still fast for small targets and lets the REAL
        # guards (plateau / dry-sweep exhaustion) decide when discovery is
        # genuinely spent.
        #
        # The CEILING (80) prevents runaway search on huge targets (80 rounds ×
        # 64-pass sweep = 5120 discovery passes worst-case); the plateau and
        # exhaustion guards stop honest runs far earlier.
        base_rounds = max(8, min(target_emails // 25, 80))
        rounds = 0

        def _expandable_markets() -> list[str]:
            """Expansion markets NOT yet in the rotation (Surface Expansion).

            Draws from the deterministic :func:`metro_fallback` tiers — the
            hand-curated metro table (counties/suburbs) first, then the
            state's other major metros + a state-wide entry, so ANY US
            metro has an expansion path (live proof it was needed: the
            2026-09-12 Honolulu/Wichita runs had an empty list and stopped
            at 1-2 working). Inc 1 keeps expansion cheap and predictable
            (no extra AI call on the hot path). Returns [] only when every
            known market is already in the surface — the honest "nothing
            left to expand" signal.
            """
            fallback = metro_fallback(query.location)
            have = {_fold(v) for v in loc_variants}
            out: list[str] = []
            for m in fallback:
                key = _fold(m)
                if key not in have and key not in {_fold(x) for x in out}:
                    out.append(m)
            return out

        while rounds < base_rounds:
            if not _wait_if_paused(paused, cancel):
                break
            with state_lock:
                if working >= target_emails:
                    break
                w = working
            got_new = False
            for pass_idx in range(max(2, min(sweep, target_emails))):
                if not _wait_if_paused(paused, cancel):
                    return
                with state_lock:
                    w = working
                if w >= target_emails:
                    return
                # Trade advances every pass; the location advances every
                # n_trades passes — pass_idx really reaches n_trades now, so
                # the geo surface is consumed, not dead code. The STREAMING
                # plateau is checked at END of round (below), never per-pass:
                # consumers report consecutive non-working research, and a bad
                # sample from loc_variants[0] must not pre-empt the 7 other
                # markets the rest of this sweep will still search.
                trade = variants[pass_idx % n_trades]
                loc = loc_variants[(pass_idx // n_trades) % n_locs]
                limit = max(10, (target_emails - w) * 2)
                try:
                    # Same pre-search report as the serial loop: the moment a
                    # pass begins the user sees "searching…", never a silent
                    # "running, 0 results" while the first discovery call is
                    # in flight (§6).
                    if emit:
                        emit(
                            "discovery", pass_idx + 1, 1,
                            f"searching {trade!r} in {loc!r}…",
                            data={"trade": trade, "location": loc,
                                  "phase": "searching"},
                        )
                    status, records, meta = run_discovery(
                        trade, loc, limit, skip_pdfs=seen_pdfs,
                        yield_store=_discovery_yield,
                        candidate_store=_discovery_candidates,
                    )
                except Exception as exc:  # noqa: BLE001 - a bad pass never kills the run
                    logger.info("streaming discovery pass %d failed: %s",
                                pass_idx + 1, exc)
                    continue
                seen_pdfs.update(meta.get("pdf_urls") or [])
                # Domain dedup BEFORE the website email-probe (Fix A) — advance
                # the search/crawl lanes, never re-probe the same companies.
                unseen_records: list[dict] = []
                for rec in records:
                    # Plan-holder rows carry real emails DIRECTLY — their derived
                    # website must not put them through the seen_domains gate
                    # (the serial twin: later PDFs re-list firms and surface
                    # their OTHER real contacts; see the comment in discover_until_target).
                    if rec.get("plan_holder"):
                        unseen_records.append(rec)
                        continue
                    website = (rec.get("website") or "").strip()
                    if not website:
                        unseen_records.append(rec)
                        continue
                    dom = _domain_from_website(website)
                    if dom:
                        if dom in seen_domains:
                            continue
                        seen_domains.add(dom)
                    unseen_records.append(rec)
                new_leads, _stats = company_records_to_leads(
                    unseen_records,
                    max_probes=_email_probes_for_demand(target_emails - w, n_locs),
                )
                fresh: list[dict] = []
                for l in new_leads:
                    if (l["email"], l["domain"]) in attempted:
                        continue
                    if store is not None and store.get(l["email"]) is not None:
                        continue
                    if dead_pool and l["email"] in dead_pool:
                        continue
                    if cooling_pool and l["email"] in cooling_pool:
                        continue
                    # DELETE-SUPPRESSION + LEAD EXCLUSIVITY (2026-09-12): a
                    # user-deleted email never re-enters the buffer; an email
                    # owned by another user is not this run's lead.
                    if deleted_pool is not None and l["email"] in deleted_pool:
                        continue
                    if taken_pool is not None and l["email"] in taken_pool:
                        continue
                    # Learned skip (Phase E) — mirrors the serial intake gate:
                    # a source that never produced a lead (MIN_TRIALS) or a
                    # company/domain the USER rejected stays out of the buffer.
                    learning_skip = (
                        _fit_learning is not None
                        and (
                            _fit_learning.should_skip_source(l.get("source_url", ""))
                            or _fit_learning.should_skip_company(l.get("company", ""))
                            or _fit_learning.should_skip_domain(l.get("domain", ""))
                        )
                    )
                    if (
                        get_profile().lead_is_non_client(
                            company=l.get("company", ""),
                            domain=l.get("domain", ""),
                            source_url=l.get("source_url", ""),
                        )
                        or learning_skip
                    ):
                        continue
                    fresh.append(l)
                    attempted.add((l["email"], l["domain"]))
                # Stamp the run's location on every buffered lead — the consumer
                # pulls with ``location=query.location`` (so surplus from OTHER
                # locations is never drained into this run), and freshly added
                # leads MUST carry the location or they are invisible to that
                # same filter (a plan-holder record carries none by itself).
                for l in fresh:
                    l["location"] = l.get("location") or query.location
                # TRADE GATE (P2, streaming twin of the serial serve gate): ALL
                # fresh leads are banked into the buffer (other-trade rows are
                # pool currency for their own consumers), but only THIS trade's
                # rows are claimable — so only they count as run progress. A
                # cross-trade-only pass must not reset the dry-sweep/plateau
                # guards (it feeds the pools, not this run).
                trade_matching = [
                    l for l in fresh
                    if not search_trade or l.get("trade") == search_trade
                ]
                if fresh:
                    with write_lock:
                        pending_store.add(fresh)  # -> the buffer
                    if emit:
                        other = len(fresh) - len(trade_matching)
                        emit(
                            "discovery", pass_idx + 1, 1,
                            f"+{len(trade_matching)} lead(s) buffered"
                            + (f" ({other} banked to other-trade pools)"
                               if other else ""),
                            data={"fresh": len(fresh),
                                  "trade_served": len(trade_matching),
                                  "trade_banked_other": other,
                                  "trade": trade,
                                  "location": loc,
                                  "status": status.value,
                                  "pass": pass_idx + 1, "round": rounds + 1},
                        )
                if trade_matching:
                    got_new = True
                pass_log.append({
                    "pass": pass_idx + 1, "round": rounds + 1,
                    "trade": trade, "location": loc,
                    "status": status.value, "records": len(records),
                    "fresh": len(fresh),
                    # P2 honest split: served-to-this-run vs banked to pools.
                    "fresh_served": len(trade_matching),
                    "fresh_banked_other": len(fresh) - len(trade_matching),
                })
                # Give consumers a beat to research the just-buffered wave
                # before the plateau check reads their verdicts — this makes
                # the guard observe settled state, not mid-flight state.
                time.sleep(0.05)
            if not got_new:
                # A full metro-wide sweep surfaced nothing NEW anywhere — the
                # pool is honestly drained. Unlike the old per-round break this
                # fires only after EVERY trade×location combo was tried, so a
                # dry first city can no longer kill a run while 7 other markets
                # are untouched.
                shortfall_reason = "discovery_exhausted"
                break
            if plateau_flag:
                # RESEARCH-side plateau (async Fix C): the consumers crossed
                # _STREAM_PLATEAU_NONWORKING consecutive non-working results.
                # Checked only at END of a full sweep — a bad sample from one
                # market must never pre-empt the other markets this round is
                # still searching, but a pool grinding skips everywhere is a
                # genuine dead end: stopping is the honest outcome. "working"
                # resets consecutive_nonworking, so a productive market in the
                # same sweep keeps the run alive.
                #
                # SURFACE EXPANSION (Inc 1): a plateau on the CURRENT surface
                # is not proof the metro is dry — it is proof THESE markets
                # are. Before declaring no_progress_plateau, pull the next
                # batch of known metro markets (fallback entries not yet
                # searched) into the rotation, reset the plateau counter, and
                # keep hunting. Only when the expanded surface is ALSO dry is
                # stopping honest. Live proof: the 2026-09-11 Fort Worth run
                # stopped at 5/20 with 3 markets searched while 6 more sat in
                # the fallback table untouched.
                more = _expandable_markets()
                if more:
                    loc_variants.extend(more)
                    n_locs = len(loc_variants)
                    # The sweep size is trade×location — expansion grew the
                    # location axis, so the per-round pass count must follow
                    # (a stale sweep would re-search only the first markets).
                    sweep = n_trades * n_locs
                    plateau_flag = False
                    consecutive_nonworking = 0
                    if emit:
                        emit(
                            "expansion", 1, 1,
                            f"plateau on {n_locs - len(more)} markets — "
                            f"expanding surface: +{len(more)} new market(s) "
                            f"({', '.join(more[:4])}"
                            f"{'…' if len(more) > 4 else ''})",
                            data={
                                "new_markets": more,
                                "total_markets": n_locs,
                                "trigger": "plateau",
                            },
                        )
                    continue
                shortfall_reason = "no_progress_plateau"
                break
            rounds += 1

    def _producer() -> None:
        nonlocal producer_done, shortfall_reason
        try:
            _produce()
        finally:
            with state_lock:
                producer_done = True
                if working < target_emails:
                    shortfall_reason = shortfall_reason or "discovery_exhausted"

    def _research_one(email: str, domain: str, source_url: str, dork: str,
                      t0: float) -> dict | None:
        """Single buffered-lead research — same logic as run_research._work.

        ``dork`` is the Layer-1 dork attribution (Phase G) persisted on the
        pending row, used to credit a WORKING lead back to its producing dork.
        """
        if store is not None:
            existing = store.get(email)
            if existing is not None:
                # EXCLUSIVITY RACE GUARD: the dossier may have been saved by
                # another user's run AFTER our intake snapshot — a live
                # ownership check, not the stale pool. Not ours -> skip, no
                # owner stamp ("ak lead ya email sirf ak user ko").
                _owned_by_another = getattr(store, "owned_by_another", None)
                if (user_id and callable(_owned_by_another)
                        and _owned_by_another(email, user_id)):
                    with write_lock:
                        pending_store.remove([email])
                    logger.info(
                        "Research skip (raced to another user): %s", email)
                    return None
                with write_lock:
                    # Cross-user claim (Phase 2, exclusivity-gated): a dossier
                    # with NO other owner — the caller's search surfaced it, so
                    # the lead shows on their dashboard without a re-research.
                    if user_id:
                        store.add_owner(email, user_id)
                    pending_store.remove([email])
                return {
                    "email": email, "domain": domain,
                    "company": existing.company.name,
                    "person": existing.person.name,
                    "bound": existing.person.bound,
                    "score": existing.potential_score,
                    "recommendation": existing.recommendation,
                    "intent": existing.intent.needs_estimation if existing.intent else "",
                    "timing": existing.timing.window if existing.timing else "",
                    "sources_checked": existing.sources_checked,
                    "elapsed_s": 0.0, "cached": True, "working": False,
                    "message": f"{email} -> CACHED (already researched)",
                }
        try:
            d = agent.research(
                email, domain, trade=query.trade, location=query.location,
                source_url=source_url,
            )
            if is_dead_domain_dossier(d):
                with write_lock:
                    pending_store.mark_dead([email])
                return {
                    "email": email, "domain": domain, "working": False,
                    "recommendation": "skip", "reason": "dead_domain",
                    "message": f"{email} -> SKIPPED (dead/expired domain)",
                }
            if store is not None:
                with write_lock:
                    store.save(d, user_id=user_id)
                    if query.folder or query.search_name:
                        store.set_meta(
                            email,
                            folder=query.folder,
                            tags=[query.search_name] if query.search_name else [],
                        )
                    pending_store.remove([email])
            elapsed = time.monotonic() - t0
            vis = _is_visible(d)
            # Layer-1 dork-yield credit (Phase G + I) — same as run_research._work,
            # now segment-tagged so the trial lands on the (dork, trade|location)
            # row and coverage is targeted per segment.
            if vis and _discovery_yield is not None and dork:
                _discovery_yield.record_working(
                    dork, segment_key(query.trade, query.location)
                )
            return {
                "email": email, "domain": domain,
                "company": d.company.name,
                "person": d.person.name,
                "bound": d.person.bound,
                "score": d.potential_score,
                "recommendation": d.recommendation,
                "intent": d.intent.needs_estimation if d.intent else "",
                "timing": d.timing.window if d.timing else "",
                "sources_checked": d.sources_checked,
                "elapsed_s": round(elapsed, 1),
                "working": vis,
                "message": (
                    f"{email} -> company={d.company.name[:30]!r} person="
                    f"{d.person.name!r} bound={d.person.bound} "
                    f"score={d.potential_score} rec={d.recommendation}"
                ),
            }
        except Exception as exc:  # noqa: BLE001 - one lead never kills a run
            with write_lock:
                pending_store.mark_attempt([email])
            return {
                "email": email, "domain": domain, "error": str(exc),
                "working": False, "message": f"{email} -> ERROR: {exc}",
            }

    def _consumer() -> None:
        """Drain the buffer, researching every row, until producer is done."""
        nonlocal working, consecutive_nonworking, plateau_flag
        batch = max(concurrency * 2, 5)
        while True:
            if not _wait_if_paused(paused, cancel):
                return
            try:
                lead = _claim_next(batch)
            except sqlite3.OperationalError as exc:
                # Guard (Galti #1 root-cause): the busy_timeout on service.py
                # stores makes lock-waits resolve, but a rare "disk I/O error" can
                # still surface here. A research worker must NEVER die from a
                # transient DB fault — back off briefly and retry the claim.
                logger.warning("claim: transient sqlite error %s; retrying", exc)
                time.sleep(0.5)
                continue
            if lead is None:
                with state_lock:
                    done = producer_done
                if done:
                    return
                # Buffer momentarily empty while the producer still fills it —
                # idle-wait briefly, NEVER exit (the "baghair ruke" contract:
                # research continues the moment data lands).
                time.sleep(0.3)
                continue
            email = lead["email"]
            domain = lead["domain"]
            t0 = time.monotonic()
            entry = _research_one(
                email, domain, lead.get("source_url", ""), lead.get("dork", ""), t0,
            )
            if entry is None:
                continue
            vis = bool(entry.get("working"))
            with state_lock:
                results.append(entry)
                if entry.get("cached"):
                    # Already-researched dedup is NOT "no progress" — the row
                    # was legitimately done, just cleaned from the buffer.
                    pass
                elif vis:
                    working += 1
                    consecutive_nonworking = 0
                else:
                    consecutive_nonworking += 1
                    if consecutive_nonworking >= _STREAM_PLATEAU_NONWORKING:
                        plateau_flag = True
                step = len(results)
            if emit:
                emit(
                    "research", step, target_emails,
                    entry.get("message", f"{email} done"),
                    email=email, data=entry,
                )

    producer = threading.Thread(target=_producer, daemon=True)
    producer.start()
    consumers = [
        threading.Thread(target=_consumer, daemon=True)
        for _ in range(concurrency)
    ]
    for t in consumers:
        t.start()

    producer.join()
    for t in consumers:
        t.join()

    with state_lock:
        final_results = list(results)
        final_working = working
    logger.info(
        "run_full[streaming]: %d leads researched, %d working, "
        "target=%d shortfall=%d producer_done=%s",
        len(final_results), final_working, target_emails,
        max(0, target_emails - final_working), producer_done,
    )
    return {
        "query": query.describe(),
        "trade": query.trade,
        "location": query.location,
        "target_emails": target_emails,
        "leads_found": len(final_results),
        "discovery_passes": list(pass_log),
        "results": final_results,
        "working_leads": final_working,
        "requested": target_emails,
        "shortfall": max(0, target_emails - final_working),
        "shortfall_reason": shortfall_reason,
    }

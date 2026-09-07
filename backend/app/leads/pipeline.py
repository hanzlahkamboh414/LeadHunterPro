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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from app.discovery.sources.plan_holder_source import PlanHolderSource
from app.discovery.sources.status import SourceStatus

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

    def __init__(self, skip_pdfs: set[str] | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self._skip = set(skip_pdfs or ())

    def discover(
        self, *, industry: str, location: str, limit: int,
    ) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
        return super().discover(
            industry=industry, location=location, limit=limit,
            skip_pdfs=self._skip,
        )


def _build_discovery_orchestrator(skip_pdfs: set[str] | None = None) -> Any:
    """Build the multi-source discovery orchestrator (CLAUDE.md §8).

    EVERY live discovery source is registered and tried in priority order —
    the leads pipeline never depends on a single source. The fixture bridge
    is deliberately NOT registered: this pipeline researches real emails, and
    a fixture fallback would silently trade live discovery for synthetic
    companies (CLAUDE.md §1, §12).
    """
    from app.discovery.source_orchestrator import SourceOrchestrator
    from app.discovery.sources.directory_crawl_source import DirectoryCrawlSource
    from app.discovery.sources.search_provider_source import SearchProviderSource

    orch = SourceOrchestrator()
    orch.register(DirectoryCrawlSource())
    orch.register(_SkipAwarePlanHolder(skip_pdfs))
    orch.register(SearchProviderSource())
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
) -> tuple[SourceStatus, list[dict], dict]:
    """One multi-source live discovery pass for trade+location.

    Executes the full orchestrator (directory crawl → plan-holder PDFs → web
    search). ``skip_pdfs`` advances the plan-holder lane to documents not yet
    parsed this run, exactly as before — the difference is the crawler and
    search lanes now contribute alongside it, so a re-run of the same query
    surfaces genuinely NEW companies instead of the same pool (the user's
    "same result every time" complaint).
    """
    orch = _build_discovery_orchestrator(skip_pdfs)
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


#: Bounded website email-hunt per pass. A search/crawl batch can carry dozens
#: of companies; probing every site would explode into hundreds of fetches.
#: A small working set is probed per pass and the rest is honestly counted as
#: "no public email (not probed)" — CLAUDE.md §10 real discovery, bounded.
_MAX_EMAIL_PROBES_PER_PASS = 12

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


def company_records_to_leads(
    records: list[dict],
    *,
    email_discover: Callable[[str], Any] | None = None,
    max_probes: int = _MAX_EMAIL_PROBES_PER_PASS,
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
                }
            )
    return leads


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
    cooldown_seconds: int = 0,
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
    pass_log: list[dict] = []
    from_cache = 0
    stale_purged = 0

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

    # Phase A — serve the discovery cache first (free, no search).
    if pending_store is not None:
        # Take a wider window than the target: purging happens from the oldest
        # end, and dropping stale rows must not starve the window of enough
        # keeps to reach the target without running live discovery.
        cached = pending_store.take(
            max(target * 2, 20), location=query.location,
            cooldown_seconds=cooldown_seconds,
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
        status, records, meta = run_discovery(
            trade, loc, max(target * 4, 4), skip_pdfs=pdfs_seen
        )
        pass_urls = meta.get("pdf_urls") or []
        pdfs_seen.update(pass_urls)
        # Multi-source conversion: plan-holder records contribute real emails
        # directly; website-only records (search/crawl lanes) are probed for a
        # public address — bounded per pass and honestly counted in disco_stats
        # (CLAUDE.md §10: visit websites, extract what's real). The fresh
        # filter ALSO drops already-researched emails at discovery TIME, so a
        # re-run of the same query surfaces genuinely NEW dossiers instead of
        # the researched flood that "kaisi research hai" complained about.
        new_leads, disco_stats = company_records_to_leads(records)
        fresh = [
            l for l in new_leads
            if (l["email"], l["domain"]) not in seen
            and (dossier_store is None or dossier_store.get(l["email"]) is None)
            and (dead_pool is None or l["email"] not in dead_pool)
            and l["email"] not in cooling_pool
        ]

        # Stock the discovery cache with ALL fresh leads, so any surplus is
        # reusable on a later run without paying for the same search again.
        if pending_store is not None:
            pending_store.add([{**l, "location": loc} for l in fresh])

        for l in fresh:
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
            "new_leads": len(fresh),
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
            emit(
                "discovery", pass_idx + 1, max_passes,
                f"pass {pass_idx + 1}: trade={trade!r} loc={loc!r} "
                f"[{entry['status']}] new={len(fresh)} total={len(leads)}{suffix}",
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

    agent = AILeadResearchAgent()
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

        # Cross-run dedup: if this email was already researched, reuse it.
        if store is not None:
            existing = store.get(email)
            if existing is not None:
                with write_lock:
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
            d = agent.research(email, domain, trade=trade, location=location)
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
                    store.save(d)
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

def run_full(
    query: ResearchQuery,
    emit: EmitFn | None = None,
    cancel: CancelFn | None = None,
    store: Any | None = None,
    paused: PauseFn | None = None,
    pending_store: Any | None = None,
    *,
    cooldown_seconds: int | None = None,
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

    # Shared across rounds: emails already handed to research and PDFs already
    # parsed, so each round ADVANCES instead of re-serving the same pool.
    attempted: set[tuple[str, str]] = set()
    seen_pdf_urls: set[str] = set()

    def discover_for(remaining: int, max_passes: int) -> tuple[list[dict], list[dict]]:
        return discover_until_target(
            query, max_passes=max_passes, emit=emit, cancel=cancel, paused=paused,
            pending_store=pending_store, dossier_store=store,
            target_override=remaining, attempted=attempted,
            seen_pdf_urls=seen_pdf_urls, cooldown_seconds=cooldown_seconds,
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

    pass_log: list[dict] = []
    results: list[dict] = []
    working = 0
    shortfall_reason = ""
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
        results += run_research(
            leads, emit=emit, cancel=cancel, store=store, paused=paused,
            pending_store=pending_store,
            trade=query.trade, location=query.location,
            search_name=query.search_name, folder=query.folder,
        )
        working = sum(1 for e in results if e.get("working"))
    else:
        shortfall_reason = "max_topup_rounds_reached"

    return {
        "query": query.describe(),
        "trade": query.trade,
        "location": query.location,
        "target_emails": query.target_emails,
        "leads_found": len(results),
        "discovery_passes": pass_log,
        "results": results,
        "working_leads": working,
        "requested": query.target_emails,
        "shortfall": max(0, query.target_emails - working),
        "shortfall_reason": shortfall_reason,
    }

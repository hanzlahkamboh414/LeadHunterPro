"""AI Lead Research — Agent (pipeline orchestration).

Ties all stages into one pipeline:

  Stage 0  triage          → skip free-mail / placeholder domain
  Stage 0.7 pre-verdict    → homepage-only client-fit screen (Phase 3)
  Stage 1  refine/company  → CompanyResearcher (AI cited)
  Stage 2  person          → PersonResearcherAI (skipped for generic emails)
  Stage 3  intent/timing   → IntentTimingAnalyzer (AI reasoned)
  Stage 4  fit/score       → LeadScorer (AI + deterministic gate)

All seams are injectable for testing.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Sequence

from app.company_profile import get_profile
from app.core.config import settings
from app.lead_research.company_research import CompanyResearcher
from app.lead_research.fit_learning import FitLearningStore
from app.lead_research.intent_timing import IntentTimingAnalyzer
from app.lead_research.models import LeadDossier
from app.lead_research.person_research_ai import PersonResearcherAI
from app.lead_research.query_learning import QueryYieldPlanner, QueryYieldStore
from app.lead_research.scoring import LeadScorer

logger = logging.getLogger(__name__)

#: Substring that marks a dossier rejected at the dead-domain MX gate. Used by
#: the pipeline (don't persist/emit dead leads) and store cleanup (delete
#: already-stored dead dossiers), so the marker lives here — one definition.
DEAD_DOMAIN_MARKER = "no MX record"


def _default_domain_delivers_email(domain: str) -> bool:
    """Default dead-domain gate — real MX check via :func:`domain_delivers_email`.

    Lives at module level so the class references a stable callable
    (tests can still inject a stub through the constructor seam).
    """
    from app.lead_research.company_research import domain_delivers_email

    return domain_delivers_email(domain)

#: Industry keywords that flag a company as construction-related, triggering
#: the deep-dive (growth/need) research stage.
_CONSTRUCTION_KEYWORDS = (
    "contractor", "construction", "general contractor", "subcontractor",
    "gc", "builder", "building", "estimating", "preconstruction",
    "develop", "developer", "site work", "paving", "concrete", "asphalt",
    "excavat", "earthwork", "demolition", "framing", "roofing", "electrical",
    "plumbing", "mechanical", "hvac", "masonry", "steel", "civil",
)


def _is_construction(industry: str) -> bool:
    """Return True if an industry string indicates construction-related work."""
    ind = (industry or "").lower()
    return any(kw in ind for kw in _CONSTRUCTION_KEYWORDS)


def _is_free_mail(domain: str) -> bool:
    """Check if domain is a free email provider."""
    free_mail = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "aol.com", "icloud.com", "mail.com", "protonmail.com",
        "zoho.com", "yandex.com", "gmx.com", "live.com",
    }
    return domain.lower() in free_mail


def _is_generic_local_part(email: str) -> bool:
    """Check if email local-part is generic (info@, office@, etc.)."""
    local = email.split("@")[0].lower()
    generic = {
        "info", "office", "admin", "contact", "hello", "support",
        "sales", "help", "team", "mail", "email", "webmaster",
        "postmaster", "abuse", "noreply", "no-reply",
    }
    return local in generic


#: Reserved / placeholder domains that never host a real, deliverable company
#: mailbox. RFC 2606 reserves example.* and the .test/.invalid/.localhost TLDs;
#: scraped discovery data also carries test/placeholder addresses. An address
#: on one of these is junk — never know a real company for it, never contact
#: it, and never spend research credit on it (CLAUDE.md §12 fake-domain rule).
_PLACEHOLDER_DOMAINS = frozenset(
    {
        "example.com", "example.org", "example.net", "example.edu",
        "test.com", "test.org", "test.net",
        "invalid", "localhost", "local", "localdomain", "test",
    }
)


#: TLDs that are file/asset extensions, not mail domains. An address like
#: ``logo@3x-1-236x60.png`` — local-part@filename — is an extractor artifact
#: (the plan-holder/crawler latched onto a logo or document name), never a
#: contactable mailbox. Fails closed: any address whose LAST label is one of
#: these is rejected in triage before ANY research.
_FILE_TLDS = frozenset(
    {
        "png", "jpg", "jpeg", "jpe", "gif", "webp", "svg", "bmp", "ico",
        "tif", "tiff", "avif", "heic",
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "tsv",
        "zip", "tar", "gz", "rar", "7z",
        "css", "js", "mjs", "html", "htm", "json", "xml", "yaml", "yml",
        "mp3", "mp4", "webm", "mov", "avi", "wav",
        "woff", "woff2", "ttf", "otf", "eot",
        "apk", "exe", "dll", "bin", "iso",
    }
)


def _is_placeholder_or_fake_domain(domain: str) -> bool:
    """True when a mail domain is a reserved placeholder or a file-extension
    artifact — never a real, deliverable mailbox (e.g. ``example.com`` or the
    ``3x-1-236x60.png`` inside ``logo@3x-1-236x60.png``). Pure string check, no
    network: runs in Stage-0 triage BEFORE free-mail / MX / company research so
    junk costs nothing and never reaches results (CLAUDE.md §12)."""
    d = (domain or "").strip().lower().rstrip(".")
    if not d:
        return True
    if d in _PLACEHOLDER_DOMAINS:
        return True
    # Only the LAST label is the TLD. "png.com" is a real .com domain and is
    # never caught; a filename artifact ending ".png" (.pdf / .svg / …) is.
    # Both observed junk cases (yourname@example.com, logo@3x-1-236x60.png)
    # land here.
    if "." in d and d.rsplit(".", 1)[1] in _FILE_TLDS:
        return True
    return False


class AILeadResearchAgent:
    """Pipeline orchestration for researching one email+domain."""

    def __init__(
        self,
        *,
        company_researcher: CompanyResearcher | None = None,
        person_researcher: PersonResearcherAI | None = None,
        intent_analyzer: IntentTimingAnalyzer | None = None,
        scorer: LeadScorer | None = None,
        domain_delivers_email: Callable[[str], bool] | None = None,
        query_yield_store: QueryYieldStore | None = None,
        fit_learning_store: FitLearningStore | None = None,
        intent_evidence_plugins: Sequence[Any] | None = None,
        event_ai_ask: Callable[[str], str] | None = None,
        pain_ai_ask: Callable[[str], str] | None = None,
    ) -> None:
        self._company = company_researcher or CompanyResearcher()
        self._person = person_researcher or PersonResearcherAI()
        self._intent = intent_analyzer or IntentTimingAnalyzer()
        self._scorer = scorer or LeadScorer()
        # Dead-domain gate seam (defaults to the real MX check). Tests inject
        # a stub so no network is hit offline.
        self._domain_delivers_email = domain_delivers_email or _default_domain_delivers_email
        # Query-yield learn loop backing store (None = loop disabled; the agent
        # is shared across concurrent research threads, so only a fresh planner
        # per research() call is created from this store — never mutable state).
        self._query_yield_store = query_yield_store
        # Fit-learning loop backing store (None = disabled). Read + written
        # directly (its own writes are lock-serialized); records each completed
        # run's fit outcome per industry / source host and auto-skips a class
        # the pipeline has proven, by its own repeated verdicts, is not a buyer.
        self._fit_learning = fit_learning_store
        # Phase 1 evidence-intake seam. Same rule as the stages above: a
        # network-touching collaborator is INJECTED, so tests run fully
        # offline. ``None`` means "use whatever is registered at runtime,
        # falling back to the three built-ins" — the production behaviour.
        # An empty list is a legitimate value and means "collect nothing".
        self._intent_evidence_plugins = intent_evidence_plugins
        # Phase 2 AI Call #1 seam. Production resolves the configured deep
        # lane lazily; tests inject a deterministic offline response.
        self._event_ai_ask = event_ai_ask
        # Phase 4 AI Call #2 seam. The model may propose correlations only;
        # the deterministic PAIN_GATE decides every stored verdict.
        self._pain_ai_ask = pain_ai_ask

    def enable_query_yield(self, db_path: str) -> None:
        """Enable the deterministic query-yield loop against ``db_path``.

        Post-construction seam so callers that build the agent BARE (the
        pipeline runner, which some test suites replace with a no-arg stub)
        can turn the loop on without changing their construction call.
        """
        from app.lead_research.query_learning import QueryYieldStore

        self._query_yield_store = QueryYieldStore(db_path)

    def enable_fit_learning(self, db_path: str) -> None:
        """Enable the fit-learning loop against ``db_path`` (same DB as yield).

        Post-construction seam, mirroring :meth:`enable_query_yield`: the
        pipeline runner turns the loop on with the dossier store's own DB path,
        so learning persists beside the dossiers it learned from.
        """
        self._fit_learning = FitLearningStore(db_path)

    def _record_fit(self, dossier: LeadDossier, source_url: str, *, kept: bool) -> None:
        """Record one completed run's fit outcome into the learning loop.

        ``kept`` = did this run end as a REAL lead (contact_now / nurture)?
        Industry comes from the researched label; source from the discovery URL
        that surfaced the lead. An empty industry / source is silently ignored
        by the store (silence is not evidence). No-op when the loop is disabled;
        a store error never breaks research (the loop is additive — CLAUDE.md §4).
        """
        if self._fit_learning is None:
            return
        try:
            self._fit_learning.record_industry(dossier.company.industry, kept=kept)
            if source_url:
                self._fit_learning.record_source(source_url, kept=kept)
        except Exception:
            logger.debug("fit-learning record failed for %s", dossier.email, exc_info=True)

    def research(
        self, email: str, domain: str, *, trade: str = "", location: str = "",
        source_url: str = "",
    ) -> LeadDossier:
        """Run the full pipeline for one email+domain.

        ``trade``/``location`` (optional) are the known construction context
        from the discovery layer (plan-holder/bid list) — threaded into the
        company research so the AI anchors on the construction-bid provenance
        instead of drifting to generic (e.g. IT) results.

        ``source_url`` (optional) is the discovery URL that surfaced this lead.
        It is not researched — it only keys the source side of the fit-learning
        loop (a source host that never once produces a real lead is auto-pruned
        at discovery). Absent for the single-lead CLI/service path.

        Returns a complete LeadDossier.
        """
        dossier = LeadDossier(email=email, domain=domain)
        # Deterministic query-yield loop: a per-call accumulator (never stored
        # on the shared agent — concurrency-safe). Research stages note which
        # templates returned which URLs; commit() below writes yield once.
        planner = QueryYieldPlanner(self._query_yield_store)
        sources_checked: list[str] = []
        source_errors: dict[str, str] = {}

        # Stage 0: triage
        # Derive the domain from the email when the lead's registered-domain
        # field is empty (plan-holder rows often omit it for personal/free-mail
        # boxes). Without this, free-mail and generic triage below would
        # silently miss those leads and run the full (expensive) research
        # pipeline on a gmail/aol address — and worse, let a name be bound from
        # an unverifiable local-part (e.g. "bonwaterinc" -> "Bonadiman Water").
        if not (domain or "").strip() and "@" in (email or ""):
            domain = email.split("@", 1)[1].strip()
            dossier.domain = domain

        # Junk/placeholder gate (CLAUDE.md §12 — fake domains must never reach
        # results): an address whose OWN domain is a reserved placeholder
        # (yourname@example.com) or a file-extension artifact
        # (logo@3x-1-236x60.png) is not a real mailbox. Check the address's own
        # @-domain — NOT the caller's registered-domain field, which the
        # extractor may set to a plausible company domain — so the junk email
        # string itself is caught regardless of what registered-domain was
        # passed. Pure string, no network: catches both observed cases before
        # free-mail / MX / any research, saving the credit junk would spend.
        addr_domain = (email or "").rsplit("@", 1)[-1].strip().lower()
        if _is_placeholder_or_fake_domain(addr_domain):
            dossier.fit = (
                f"Junk/placeholder address — {addr_domain} is not a real mailbox "
                f"domain (reserved placeholder or file-extension artifact). "
                f"Rejected before research."
            )
            dossier.recommendation = "skip"
            logger.info("Triage: %s has junk/placeholder domain %s → skip", email, addr_domain)
            return dossier

        if _is_free_mail(domain):
            # Free-mail domains (gmail/yahoo/hotmail) are NOT deleted — they
            # are kept at second priority (nurture) so a person can still be
            # chased, but no company pipeline is run (no business domain).
            dossier.fit = "Free mail domain — kept at second priority"
            dossier.recommendation = "nurture"
            logger.info("Triage: %s is free mail → nurture (2nd priority)", domain)
            return dossier

        # Generic email flag: info@/admin@/contact@ on a REAL business domain
        # still runs company research (Stage 1) — the company may be a valid
        # construction firm worth keeping for outreach even without a named
        # person.  Person research (Stage 2) is skipped for generic addresses
        # because there is no specific individual to find.
        is_generic_email = _is_generic_local_part(email)
        if is_generic_email:
            logger.info("Triage: %s has generic local part — company research will run, person research skipped", email)

        # Stage 0.5: dead-domain gate. A domain that resolves no MX record
        # cannot receive email, so the address is undeliverable — such leads
        # are skipped outright (never contact_now), and the expensive AI
        # company/person pipeline is never started for them (credit saver).
        if not self._domain_delivers_email(domain):
            dossier.fit = f"Dead/expired domain — {DEAD_DOMAIN_MARKER} (undeliverable)"
            dossier.recommendation = "skip"
            logger.info("Triage: %s has dead/expired domain %s → skip", email, domain)
            return dossier

        # Stage 0.7: homepage-first client-fit pre-verdict (Phase 3 demand
        # fix). The measured leak: 61% of leads burned the 4 screening
        # search queries before the verdict said "not our client" — the
        # verdict needs the industry, the industry came from the Stage-1 AI
        # call, and that call needed the searches. The company's OWN site
        # (fetched directly, ZERO search-engine load) carries the industry
        # signal by itself: a magazine / telecom / software homepage says so
        # loudly. Skip the searches ONLY on a GROUNDED AI "no" (the same
        # signal Stage 1c trusts) or the deterministic off-vertical backstop
        # on the pre-verdict industry; "unsure" or an unreadable homepage
        # falls through to full research — default-keep, never a silent
        # skip (CLAUDE.md §6). Kept leads reuse the pre-fetched crawl, so
        # their only extra cost is the (cheap, flash-lane) pre-verdict call.
        pre = None
        if settings.HOMEPAGE_PRE_VERDICT:
            # Learned-domain shortcut first: a domain the fit loop has proven
            # (by its own repeated verdicts) never produces a lead needs no
            # pre-verdict either — the same check Stage 1c runs on the refined
            # domain, moved before ANY network work.
            if self._fit_learning is not None and self._fit_learning.should_skip_domain(domain):
                dossier.fit = (
                    f"Not our client — {domain}. This domain has never produced "
                    f"a lead across repeated research; auto-skipped."
                )
                dossier.recommendation = "skip"
                self._record_fit(dossier, source_url, kept=False)
                dossier.sources_checked = sources_checked
                dossier.source_errors = source_errors
                logger.info("Pre-Stage skip: %s domain proven non-buyer → skip", email)
                return dossier
            try:
                pre = self._company.pre_verdict(
                    email, domain, trade=trade, location=location,
                )
            except Exception as exc:  # noqa: BLE001 — pre-verdict is best-effort
                logger.info("pre-verdict failed for %s: %s", email, exc)
                pre = None
        if pre is not None:
            dossier.refined_domain = pre["refined_domain"]
            dossier.refined_company = pre["name"]
            prof = get_profile()
            det_reason = (
                pre["industry"]
                and (prof.is_off_vertical(pre["industry"])
                     or prof.is_non_client(pre["industry"]))
            )
            learned = (
                self._fit_learning is not None
                and self._fit_learning.should_skip_industry(pre["industry"])
            )
            if pre["verdict"] == "no" or det_reason or learned:
                from app.lead_research.models import CompanyProfile
                dossier.company = CompanyProfile(
                    name=pre["name"], industry=pre["industry"],
                    website=f"https://{pre['refined_domain']}",
                    is_our_client=pre["verdict"],
                    client_reason=pre["reason"],
                )
                ind = pre["industry"] or "(unknown industry)"
                if pre["verdict"] == "no" and pre["reason"]:
                    dossier.fit = f"Not our client — {pre['reason']}"
                elif learned:
                    dossier.fit = (
                        f"Not our client — {pre['name'] or pre['refined_domain']} "
                        f"({ind}). This class has never produced a lead across "
                        f"repeated research; auto-skipped."
                    )
                else:
                    dossier.fit = (
                        f"Not our client — {pre['name'] or pre['refined_domain']} "
                        f"({ind}). Estimating services are not for them."
                    )
                dossier.recommendation = "skip"
                sources_checked.append("company_pre_verdict")
                # Reinforce the loop with this proven non-buyer, then finalize.
                self._record_fit(dossier, source_url, kept=False)
                dossier.sources_checked = sources_checked
                dossier.source_errors = source_errors
                logger.info(
                    "Pre-verdict skip: %s industry=%r verdict=%r → skip "
                    "(0 search queries spent)",
                    email, ind, pre["verdict"],
                )
                return dossier

        # Stage 1: company research
        try:
            company_result = self._company.research_with_domain(
                email, domain, trade=trade, location=location,
                query_planner=planner,
                # Reuse the pre-verdict's page crawl — a kept lead pays one
                # extra AI call for the pre-verdict, never a second crawl.
                site_content=(pre or {}).get("site_content"),
            )
            dossier.refined_domain = company_result.get("refined_domain", domain)
            dossier.refined_company = company_result.get("name", "")

            from app.lead_research.models import CompanyProfile, AIEvidence
            dossier.company = CompanyProfile(
                name=company_result.get("name", ""),
                industry=company_result.get("industry", ""),
                location=company_result.get("location", ""),
                website=company_result.get("website", ""),
                facts=[AIEvidence.from_dict(f) for f in company_result.get("facts", [])],
                # Client-fit verdict from the SAME Stage-1 call (no extra credit):
                # the AI's honest yes/no/unsure + one-line reason, consumed by the
                # Stage 1c shortcut below.
                is_our_client=company_result.get("is_our_client", ""),
                client_reason=company_result.get("client_reason", ""),
            )
            sources_checked.append("company_research")
        except Exception as exc:
            logger.error("Stage 1 failed for %s: %s", email, exc)
            source_errors["company_research"] = str(exc)
            dossier.refined_domain = domain

        # Stage 1c: not-a-client shortcut. THREE independent signals, all
        # pointing at the same decision — this company is not a buyer, so stop
        # before the expensive person / deep / intent / scoring lanes run
        # (credits + UI noise). The reason is ALWAYS stored — never a hidden
        # skip (CLAUDE.md §6/§12):
        #   * deterministic — off-vertical (fiber/telecom/utility/road/pipeline/
        #     materials) or a non-client class (A/E/C consultant, association,
        #     software/IT, transit/mobility). The hand-written BACKSTOP list.
        #   * AI verdict — the Stage-1 LLM judged is_our_client == "no", with a
        #     grounded one-line client_reason (the fix for "AI ki reasoning weak").
        #   * learned — this industry has completed MIN_TRIALS research runs and
        #     never once produced a real lead (the self-correcting fit loop:
        #     "khud improvement kare, kabi wahi ghalti na kare").
        #   * user_rejected — you DELETED this company/domain as irrelevant/not
        #     our client (Phase E). ONE human verdict is decisive: it outranks
        #     every AI score, so a purged company stays purged on re-discovery.
        if dossier.company.name:
            prof = get_profile()
            det_reason = (
                prof.is_off_vertical(dossier.company.industry)
                or prof.is_non_client(dossier.company.industry)
            )
            ai_no = dossier.company.is_our_client == "no"
            learned = (
                self._fit_learning is not None
                and self._fit_learning.should_skip_industry(dossier.company.industry)
            )
            user_rejected = (
                self._fit_learning is not None
                and (
                    self._fit_learning.should_skip_company(dossier.refined_company)
                    or self._fit_learning.should_skip_company(dossier.company.name)
                    or self._fit_learning.should_skip_domain(dossier.refined_domain)
                    or self._fit_learning.should_skip_domain(domain)
                )
            )
            if det_reason or ai_no or learned or user_rejected:
                ind = dossier.company.industry or "(unknown industry)"
                name = dossier.refined_company or dossier.company.name
                if ai_no and dossier.company.client_reason:
                    # The AI's own grounded justification is the most specific.
                    dossier.fit = f"Not our client — {dossier.company.client_reason}"
                elif user_rejected and not det_reason:
                    dossier.fit = (
                        f"Not our client — {name} ({ind}). You marked this "
                        f"company as not-a-client; auto-skipped."
                    )
                elif learned and not det_reason:
                    dossier.fit = (
                        f"Not our client — {name} ({ind}). This class has never "
                        f"produced a lead across repeated research; auto-skipped."
                    )
                else:
                    dossier.fit = (
                        f"Not our client — {name} ({ind}). "
                        f"Estimating services are not for them."
                    )
                dossier.recommendation = "skip"
                sources_checked.append("scoring")
                # Reinforce the loop with this proven non-buyer, then finalize.
                self._record_fit(dossier, source_url, kept=False)
                dossier.sources_checked = sources_checked
                dossier.source_errors = source_errors
                logger.info(
                    "Stage skip: %s industry=%r verdict=%r learned=%s user_rejected=%s → skip (not our client)",
                    email, ind, dossier.company.is_our_client, learned, user_rejected,
                )
                return dossier

        # Signal-intelligence evidence intake (Phase 1 of the Company Signal
        # Intelligence engine). Company-scoped, and placed HERE on purpose:
        # after the Stage 1c not-a-client shortcut above, so the live network
        # calls are spent only on leads that survived every client-fit gate.
        # Running it earlier would spend them on the non-client leads the
        # pre-verdict exists to drop.
        #
        # No AI is involved — this is evidence collection only. Each plugin's
        # IntentEvidence is projected onto the canonical record and stored in
        # research_evidence.db against a stable company_id, so the next lead
        # at the same company reuses today's evidence instead of re-fetching
        # it. The outcome rides in sources_checked / source_errors, which the
        # UI already renders.
        self._collect_signal_evidence(
            dossier, domain, sources_checked, source_errors
        )

        # Stage 2: person research
        # Generic emails (info@/admin@/contact@) skip person research — there
        # is no specific individual to find; spending an AI call here would
        # always return empty.  The dossier stays person-unbound, and scoring
        # treats "company identified, person unbound" as a valid nurture tier.
        if is_generic_email:
            logger.info("Stage 2 skipped for %s: generic email, no person to research", email)
        else:
            try:
                person_result = self._person.research(
                    email=email,
                    refined_domain=dossier.refined_domain or domain,
                    company_name=dossier.company.name,
                    company_industry=dossier.company.industry,
                    company_facts=dossier.company.facts,
                    query_planner=planner,
                )
                dossier.person = person_result
                sources_checked.append("person_research")
            except Exception as exc:
                logger.error("Stage 2 failed for %s: %s", email, exc)
                source_errors["person_research"] = str(exc)

        # Stage 1b: deep-dive (only for qualifying leads)
        # If the company is construction-related AND on-vertical (a fiber/
        # telecom/utility/materials company never spends deep-research credits —
        # it is not our client, root-cause fix for the fiber leak) AND we have a
        # company name, run the growth/need queries. A bound decision-maker is
        # NOT a prerequisite: deep research examines the COMPANY (contractor
        # licence / newsworthy growth / hiring / expansion / bid wins), not the
        # person — gating it on person.bound silently starved the lane on runs
        # where discovery surfaced a company but no decision-maker, so the
        # second AI key never fired (the "sirf ek AI chala" gap).
        if (
            dossier.company.name
            and _is_construction(dossier.company.industry)
            and not get_profile().is_off_vertical(dossier.company.industry)
        ):
            # Recorded as checked whether or not it finds facts, so a run where
            # the deep lane found no growth signals is distinguishable from one
            # where it never ran (CLAUDE.md §6 — honest logging).
            sources_checked.append("deep_research")
            try:
                deep_facts = self._company.research_deep(
                    domain=dossier.refined_domain or domain,
                    company_name=dossier.company.name,
                    # Stage 1 already researched the company's location; pass it
                    # so the contractor-licence query hits the RIGHT state
                    # registry instead of a hardcoded one.
                    location=dossier.company.location,
                    query_planner=planner,
                )
                if deep_facts:
                    dossier.company.facts.extend(deep_facts)
                else:
                    logger.info("Deep research for %s returned no growth signals", email)
            except Exception as exc:
                logger.error("Deep research failed for %s: %s", email, exc)
                source_errors["deep_research"] = str(exc)

        # Stage 3: intent + timing
        try:
            intent, timing = self._intent.analyze(
                email=email,
                company=dossier.company,
                person=dossier.person,
            )
            dossier.intent = intent
            dossier.timing = timing
            sources_checked.append("intent_timing")
        except Exception as exc:
            logger.error("Stage 3 failed for %s: %s", email, exc)
            source_errors["intent_timing"] = str(exc)

        # Stage 4: scoring
        try:
            fit, score, rec = self._scorer.score(
                email=email,
                company=dossier.company,
                person=dossier.person,
                intent=dossier.intent,
                timing=dossier.timing,
            )
            dossier.fit = fit
            dossier.potential_score = score
            dossier.recommendation = rec
            sources_checked.append("scoring")
        except Exception as exc:
            logger.error("Stage 4 failed for %s: %s", email, exc)
            source_errors["scoring"] = str(exc)

        # Query-yield loop: write this run's template outcomes against the
        # citations the completed dossier actually made. Run only after every
        # stage so deep/person/intent URLs are all accounted for (a template is
        # credited when one of its returned URLs is cited as evidence).
        if planner.enabled:
            _all_facts = list(dossier.company.facts) + list(dossier.person.evidence)
            _all_facts += list(dossier.intent.evidence) + list(dossier.timing.events)
            cited_urls = {f.source_url for f in _all_facts if f.source_url}
            verified_urls = {
                f.source_url for f in _all_facts
                if f.source_url and f.confidence == "verified"
            }
            planner.commit(cited_urls, verified_urls)

        dossier.sources_checked = sources_checked
        dossier.source_errors = source_errors

        # Fit-learning: record how this fully-researched run ended. A dossier
        # that scored into contact_now / nurture is a REAL lead for its
        # industry + source; a skip is a non-buyer. Over MIN_TRIALS runs this is
        # what lets an always-skipping class be pruned at Stage 1c next time.
        self._record_fit(
            dossier,
            source_url,
            kept=dossier.recommendation in ("contact_now", "nurture"),
        )

        logger.info(
            "Research complete: %s → score=%.1f rec=%s",
            email, dossier.potential_score, dossier.recommendation,
        )
        return dossier

    def _collect_signal_evidence(
        self,
        dossier: LeadDossier,
        domain: str,
        sources_checked: list[str],
        source_errors: dict[str, str],
    ) -> None:
        """Store buying-intent evidence for this lead's company (Phase 1).

        Calls :func:`app.research.intake.collect_company_evidence`, which is
        best-effort by construction — a third-party endpoint having a bad day
        must never fail a lead. The outcome is recorded against the existing
        diagnostic fields rather than a new one:

        * ``sources_checked += "intent_evidence"`` when the intake ran,
        * ``source_errors["intent_evidence"]`` when it came back with
          anything other than VERIFIED, carrying the honest reason.

        That second part is the point: a thin evidence result is reported as
        *why* it is thin — "no provider answered" and "every provider
        answered and had nothing" are different facts and are stored as
        different strings (CLAUDE.md §6/§12).
        """
        if not settings.INTENT_EVIDENCE_ENABLED:
            return
        try:
            from app.research.intake import collect_company_evidence
            from app.research.taxonomy import ResearchState

            company_name = dossier.refined_company or dossier.company.name
            website = dossier.company.website or ""
            location = dossier.company.location or ""
            # The lead's own registered domain is the identity handle; the
            # refined domain is the same company under its resolved name.
            handle = dossier.refined_domain or domain

            result = collect_company_evidence(
                company_name=company_name,
                domain=handle,
                website=website,
                location=location,
                plugins=self._intent_evidence_plugins,
            )
            sources_checked.append("intent_evidence")
            if result.state != ResearchState.VERIFIED.value:
                source_errors["intent_evidence"] = (
                    f"{result.state}: {result.honest_reason or result.error}"
                )
            logger.info(
                "intent evidence for %s: state=%s stored=%d known=%d "
                "(company_id=%s)",
                dossier.email, result.state, result.evidence_stored,
                result.evidence_known, result.company_id or "-",
            )
            if result.state == ResearchState.VERIFIED.value and result.company_id:
                if result.evidence_stored > 0:
                    self._extract_signal_events(
                        result.company_id,
                        company_name,
                        dossier.email,
                        sources_checked,
                        source_errors,
                    )
                else:
                    sources_checked.append("signal_events")
                    logger.info(
                        "signal events for %s: skipped AI because evidence "
                        "was unchanged (company_id=%s)",
                        dossier.email,
                        result.company_id,
                    )
                    self._compute_company_signals(
                        result.company_id,
                        dossier.email,
                        sources_checked,
                        source_errors,
                    )
                self._attach_company_intelligence(
                    dossier, result.company_id, sources_checked
                )
        except Exception as exc:  # noqa: BLE001 — never fail a lead over this
            source_errors["intent_evidence"] = f"{type(exc).__name__}: {exc}"
            logger.exception("intent evidence intake failed for %s", dossier.email)

    def _extract_signal_events(
        self,
        company_id: str,
        company_name: str,
        email: str,
        sources_checked: list[str],
        source_errors: dict[str, str],
    ) -> None:
        """Run Phase 2 once for evidence not already represented by events."""
        from app.ai.gateway import make_ai_ask
        from app.research.events import extract_company_events
        from app.research.store import ResearchEvidenceStore

        sources_checked.append("signal_events")
        try:
            store = ResearchEvidenceStore()
            covered = {
                evidence_id
                for event in store.events_for_company(company_id)
                for evidence_id in event.evidence_ids
            }
            pending = [
                item for item in store.evidence_for_company(company_id)
                if item.evidence_id not in covered
            ]
            if not pending:
                logger.info(
                    "signal events for %s: no uncovered evidence (company_id=%s)",
                    email, company_id,
                )
                self._compute_company_signals(
                    company_id, email, sources_checked, source_errors
                )
                return
            ask = self._event_ai_ask or make_ai_ask()
            result = extract_company_events(
                pending, company_name=company_name, ai_ask=ask
            )
            stored = sum(1 for event in result.events if store.add_event(event))
            if result.rejections:
                source_errors["signal_events"] = (
                    f"{len(result.rejections)} proposal(s) rejected: "
                    + "; ".join(result.rejections)
                )
            logger.info(
                "signal events for %s: evidence=%d accepted=%d stored=%d "
                "rejected=%d (company_id=%s)",
                email, len(pending), len(result.events), stored,
                len(result.rejections), company_id,
            )
            self._compute_company_signals(
                company_id, email, sources_checked, source_errors
            )
        except Exception as exc:  # noqa: BLE001 — intelligence is additive
            source_errors["signal_events"] = f"{type(exc).__name__}: {exc}"
            logger.exception("signal event extraction failed for %s", email)

    def _compute_company_signals(
        self,
        company_id: str,
        email: str,
        sources_checked: list[str],
        source_errors: dict[str, str],
    ) -> None:
        """Compute and persist the deterministic Phase 3 signal snapshot."""
        from app.research.signals import compute_company_signals
        from app.research.store import ResearchEvidenceStore

        sources_checked.append("signal_scoring")
        try:
            store = ResearchEvidenceStore()
            result = compute_company_signals(
                store.events_for_company(company_id),
                store.evidence_for_company(company_id),
            )
            store.replace_signals(company_id, result.signals)
            if result.contradictions:
                source_errors["signal_scoring"] = (
                    f"{len(result.contradictions)} contradiction(s) excluded: "
                    + "; ".join(result.contradictions)
                )
            logger.info(
                "signal scoring for %s: signals=%d contradictions=%d "
                "excluded_events=%d (company_id=%s)",
                email, len(result.signals), len(result.contradictions),
                len(result.excluded_event_ids), company_id,
            )
            if result.signals:
                self._infer_company_pain(
                    company_id, email, sources_checked, source_errors
                )
        except Exception as exc:  # noqa: BLE001 — intelligence is additive
            source_errors["signal_scoring"] = f"{type(exc).__name__}: {exc}"
            logger.exception("signal scoring failed for %s", email)

    def _infer_company_pain(
        self,
        company_id: str,
        email: str,
        sources_checked: list[str],
        source_errors: dict[str, str],
    ) -> None:
        """Run cached AI Call #2 and persist deterministic PAIN_GATE verdicts."""
        from app.ai.gateway import make_ai_ask
        from app.research.pain import (
            infer_company_pain,
            pain_basis_hash,
        )
        from app.research.store import ResearchEvidenceStore

        sources_checked.append("pain_inference")
        try:
            store = ResearchEvidenceStore()
            events = store.events_for_company(company_id)
            signals = store.signals_for_company(company_id)
            evidence = store.evidence_for_company(company_id)
            basis = pain_basis_hash(events, signals)
            if store.pain_basis_hash(company_id) == basis:
                logger.info(
                    "pain inference for %s: skipped AI because event/signal "
                    "snapshot was unchanged (company_id=%s)",
                    email, company_id,
                )
                return
            ask = self._pain_ai_ask or make_ai_ask()
            result = infer_company_pain(
                events, signals, evidence, ai_ask=ask
            )
            store.replace_pain_hypotheses(
                company_id, result.hypotheses, basis_hash=basis
            )
            if result.rejections:
                source_errors["pain_inference"] = (
                    f"{len(result.rejections)} proposal(s) rejected: "
                    + "; ".join(result.rejections)
                )
            logger.info(
                "pain inference for %s: proposed=%d stored=%d rejected=%d "
                "(company_id=%s)",
                email, len(result.hypotheses) + len(result.rejections),
                len(result.hypotheses), len(result.rejections), company_id,
            )
        except Exception as exc:  # noqa: BLE001 — intelligence is additive
            source_errors["pain_inference"] = f"{type(exc).__name__}: {exc}"
            logger.exception("pain inference failed for %s", email)

    def _attach_company_intelligence(
        self,
        dossier: LeadDossier,
        company_id: str,
        sources_checked: list[str],
    ) -> None:
        """Attach and persist the Phase 5 evidence-licensed UI snapshot."""
        from app.research.outreach import (
            build_company_intelligence,
            build_outreach_trigger,
        )
        from app.research.store import ResearchEvidenceStore

        store = ResearchEvidenceStore()
        company = store.get_company(company_id)
        if company is None:
            raise ValueError(f"unknown company_id {company_id!r}")
        pains = store.pain_hypotheses_for_company(company_id)
        payload = build_company_intelligence(
            company,
            store.events_for_company(company_id),
            store.signals_for_company(company_id),
            pains,
            store.evidence_for_company(company_id),
            coverage=store.latest_coverage_for_company(company_id),
        )
        store.replace_outreach_trigger(
            company_id,
            build_outreach_trigger(company, pains),
        )
        dossier.signal_intelligence = payload
        sources_checked.append("company_intelligence")

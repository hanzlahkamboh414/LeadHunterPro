"""Phase 1 intake: the existing intent plugins → canonical evidence (L0→L5).

This is the module that switches the dormant layer ON. Everything it calls
already existed and was already tested — the three intent plugins
(:mod:`app.discovery.intent`), the canonical record
(:mod:`app.research.models`), the legacy→canonical adapter
(:mod:`app.research.adapters`) and the evidence store
(:mod:`app.research.store`). What was missing was the bridge between them and
a caller on the production path. That bridge is this file.

What one call does, in the frozen layer order::

    L0  identity    resolve_company(domain) -> a STABLE company_id
    L4  plugins     usaspending / google_news / company_site, run for the company
    L5  store       each IntentEvidence -> canonical -> research_evidence.db

Three properties this module is built to hold:

**Company-keyed, not lead-keyed.** Evidence is stored against ``company_id``,
so the second lead at the same company reuses the first one's evidence
instead of paying for it again — the reason the store is separate from
``dossiers.db`` in the first place.

**Honest outcomes, never collapsed.** The run is closed with one of the four
:class:`~app.research.taxonomy.ResearchState` values, and the distinction
that matters most here is ``NOT_FOUND`` ("every provider answered, none had
anything") versus ``NOT_ACCESSIBLE`` ("at least one provider could not be
reached"). Merging them is how a coverage hole silently becomes a confident
negative, which is the defect the four states exist to prevent.

**It never raises.** This runs inline in the research path; a third-party
endpoint having a bad day must not fail a lead's research. Failures are
recorded in the result and logged with their reason (CLAUDE.md §6 — never a
bare "live=False").

No AI is involved anywhere in this module: Phase 1 is evidence collection.
Reading events off this evidence is Phase 2 and needs its own approval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.discovery.intent import (
    collect_intent_evidence,
    default_intent_plugins,
    registered_intent_plugins,
)
from app.discovery.sources.status import SourceReason, SourceStatus
from app.research.adapters import from_intent_evidence
from app.research.models import CanonicalEvidence
from app.research.store import ResearchEvidenceStore, normalize_company_key
from app.research.taxonomy import ResearchState

logger = logging.getLogger(__name__)

#: A provider "answered" when it ran and returned a result — SUCCESS (found
#: something) or EMPTY (ran correctly, found nothing). Anything else means we
#: could not look, which is a different fact and must not be filed as a
#: negative about the company.
_ANSWERED_STATUSES = (SourceStatus.SUCCESS.value, SourceStatus.EMPTY.value)


@dataclass(frozen=True)
class CompanyIntakeResult:
    """What one company's evidence collection produced, and why.

    Every field is diagnostic on purpose: the caller logs this, and the
    verification script reads it. A caller that only wants the verdict reads
    :attr:`state`; a caller debugging a thin result reads :attr:`providers`.
    """

    company_id: str = ""
    domain_key: str = ""
    run_id: str = ""
    state: str = ResearchState.NOT_ACCESSIBLE.value
    honest_reason: str = ""
    #: Provider names the registry/config offered.
    plugins_available: list[str] = field(default_factory=list)
    #: Per-provider honest record: status, returned, accepted, deduped, error.
    providers: list[dict[str, Any]] = field(default_factory=list)
    #: Raw ``IntentEvidence`` items the plugins returned (post-dedup).
    evidence_collected: int = 0
    #: Canonical rows newly written (not previously known).
    evidence_stored: int = 0
    #: Canonical rows already present — the dedup working, not a failure.
    evidence_known: int = 0
    #: Items that could not be stored, each with its reason.
    rejected: list[dict[str, str]] = field(default_factory=list)
    #: Set only when the intake itself failed unexpectedly.
    error: str = ""

    @property
    def verified(self) -> bool:
        """True only when evidence exists and was stored (``VERIFIED``)."""
        return self.state == ResearchState.VERIFIED.value


def _plugins_for_run(plugins: Sequence[Any] | None) -> tuple[list[Any], str]:
    """The plugins to run, and an honest note on where they came from.

    Registry first: the plugins registered at startup are the ones that
    should actually run, which is what makes that registration meaningful
    rather than decorative. A process that never ran the app lifespan (a CLI
    script, a test) has an empty registry, so the built-ins are constructed
    directly — and the fallback is named in the log rather than happening
    silently.
    """
    if plugins is not None:
        return list(plugins), "caller-supplied"
    registered = registered_intent_plugins()
    if registered:
        return list(registered), "registry"
    return list(default_intent_plugins()), "built-in (registry empty)"


def collect_company_evidence(
    *,
    company_name: str,
    domain: str,
    website: str = "",
    location: str = "",
    store: ResearchEvidenceStore | None = None,
    plugins: Sequence[Any] | None = None,
) -> CompanyIntakeResult:
    """Collect and store buying-intent evidence for ONE company.

    Args:
        company_name: The company's name, as the research path knows it. May
            be empty — the plugins tolerate it and the store keeps whatever
            name it already had.
        domain: The company's registered domain (or a URL). REQUIRED: it is
            the identity lookup handle. An empty domain is refused rather
            than guessed, because evidence must attach to a real company.
        website: The company's website. When empty, ``https://<domain>`` is
            used — the company's own domain, never an invented one.
        location: Optional city/state context that sharpens a search.
        store: The evidence store. Defaults to the process-wide
            ``research_evidence.db``.
        plugins: The intent plugins to run. Defaults to the registered ones,
            falling back to the three built-ins.

    Returns:
        A :class:`CompanyIntakeResult`. Never raises — a failure is reported
        in the result and its reason is logged.
    """
    target = store if store is not None else ResearchEvidenceStore()
    key = normalize_company_key(domain)
    if not key:
        reason = (
            f"no company domain for {company_name or '(unnamed)'} — identity "
            f"cannot be resolved, and evidence stored without an owner would "
            f"be attributed to the wrong company"
        )
        logger.warning("intent evidence skipped: %s", reason)
        return CompanyIntakeResult(
            state=ResearchState.NOT_ACCESSIBLE.value, honest_reason=reason
        )

    chosen, origin = _plugins_for_run(plugins)
    # L0 — identity. ``resolve_company`` is idempotent by domain, so this
    # returns the SAME company_id on every later run for this company.
    identity = target.resolve_company(key, name=company_name)
    company_id = identity.company_id
    if company_name:
        # Fills a blank name only; never overwrites what the record already
        # knows (the store decides, not this caller).
        target.set_name(company_id, company_name)

    run_id = target.start_run(company_id)
    logger.info(
        "intent evidence: company=%s (%s) providers found=%s selected=%s "
        "source=%s",
        company_id, key, [getattr(p, "name", "?") for p in chosen],
        [getattr(p, "name", "?") for p in chosen], origin,
    )

    result = _run_collection(
        target,
        run_id=run_id,
        company_id=company_id,
        domain_key=key,
        company_name=company_name,
        website=website or f"https://{key}",
        location=location,
        plugins=chosen,
        plugins_origin=origin,
    )
    _close_run(target, run_id, result)
    _log_outcome(result)
    return result


def _run_collection(
    store: ResearchEvidenceStore,
    *,
    run_id: str,
    company_id: str,
    domain_key: str,
    company_name: str,
    website: str,
    location: str,
    plugins: Sequence[Any],
    plugins_origin: str,
) -> CompanyIntakeResult:
    """Run the plugins, adapt, store. Extracted so the caller can guarantee
    the run is closed even when this raises."""
    try:
        collection = collect_intent_evidence(
            plugins,
            company_name=company_name,
            website=website,
            location=location,
        )
    except Exception as exc:  # noqa: BLE001 — the intake never fails a lead
        logger.exception("intent evidence collection crashed for %s", company_id)
        return CompanyIntakeResult(
            company_id=company_id, domain_key=domain_key, run_id=run_id,
            state=ResearchState.NOT_ACCESSIBLE.value,
            plugins_available=[getattr(p, "name", "?") for p in plugins],
            error=f"{type(exc).__name__}: {exc}",
            honest_reason=f"collection crashed: {type(exc).__name__}: {exc}",
        )

    stored = 0
    known = 0
    rejected: list[dict[str, str]] = []
    for item in collection.evidence:
        try:
            # ``domain_key`` is passed so the adapter can classify
            # ``company_match``: it is the company's identity handle, and the
            # one fact that lets a record on the company's own site be tied
            # to it when the snippet does not happen to say the name.
            record: CanonicalEvidence = from_intent_evidence(
                item,
                company_id=company_id,
                company_name=company_name,
                domain=domain_key,
            )
            if store.add_evidence(record):
                stored += 1
            else:
                known += 1
        except Exception as exc:  # noqa: BLE001 — one bad item is not fatal
            rejected.append({
                "source_url": str(getattr(item, "source_url", "")),
                "reason": f"{type(exc).__name__}: {exc}",
            })

    state, reason = _classify(
        collection=collection,
        stored=stored,
        known=known,
        rejected=len(rejected),
        plugins_origin=plugins_origin,
    )
    return CompanyIntakeResult(
        company_id=company_id,
        domain_key=domain_key,
        run_id=run_id,
        state=state,
        honest_reason=reason,
        plugins_available=collection.plugins_available,
        providers=collection.providers,
        evidence_collected=len(collection.evidence),
        evidence_stored=stored,
        evidence_known=known,
        rejected=rejected,
    )


def _classify(
    *,
    collection: Any,
    stored: int,
    known: int,
    rejected: int,
    plugins_origin: str,
) -> tuple[str, str]:
    """The honest run state, and the reason that belongs with it.

    The order of these checks IS the never-collapse rule: evidence wins,
    then "we could not look", and only last "we looked and found nothing".
    """
    total = stored + known
    if total:
        reason = (
            f"{total} evidence item(s) from "
            f"{_answered(collection)} provider(s) "
            f"({stored} new, {known} already known)"
        )
        if collection.unreachable:
            # A VERIFIED run can still be a PARTIAL look. Naming only the
            # providers that answered would let a dead source read as
            # "covered, nothing more to find" — the same quiet-negative
            # failure the four states exist to prevent, just one level up.
            reason += (
                f" — partial look: {', '.join(collection.unreachable)} did "
                f"not answer"
            )
        return ResearchState.VERIFIED.value, reason
    if collection.evidence and rejected:
        # Items arrived but none could be stored. Not "nothing exists" —
        # something is wrong with the mapping, and a quiet negative would
        # hide it.
        return ResearchState.NOT_VERIFIED.value, (
            f"{len(collection.evidence)} item(s) arrived but none could be "
            f"stored — see the rejected list for the mapping failure"
        )
    if not collection.any_reachable:
        return ResearchState.NOT_ACCESSIBLE.value, (
            f"no provider answered ({_did_not_answer(collection)}); plugins "
            f"from {plugins_origin} — this is 'we could not look', NOT 'there "
            f"is nothing'"
        )
    if collection.unreachable:
        # PARTIAL coverage. At least one provider answered and had nothing,
        # but another could not be reached and might have had something.
        # Reporting the whole look as NOT_FOUND would overstate a negative
        # that the run does not support — the exact collapse the four states
        # exist to prevent. The reason names both halves.
        return ResearchState.NOT_ACCESSIBLE.value, (
            f"partial look: {_answered(collection)} provider(s) answered with "
            f"no evidence, but {_did_not_answer(collection)} — 'not found' is "
            f"not supported by a partial search"
        )
    return ResearchState.NOT_FOUND.value, (
        f"all {_answered(collection)} provider(s) answered and returned no "
        f"evidence for this company"
    )


#: How a failed provider's ``SourceReason`` reads in a summary line.
#: The wording matters: "could not be reached" is a claim about the network,
#: and it is FALSE for a 4xx — we reached the source and it rejected what we
#: sent. That wording is what let a permanently-malformed USAspending request
#: read as a flaky endpoint for its whole life, so a rejected request is
#: never described as an outage here.
_FAILURE_PHRASES = {
    SourceReason.REQUEST_ERROR.value: "rejected our request",
    SourceReason.SOURCE_ERROR.value: "failed",
    SourceReason.ACCESS_ERROR.value: "could not be reached",
}


def _did_not_answer(collection: Any) -> str:
    """The providers that did not answer, each with whose fault that was."""
    reasons = collection.provider_reasons()
    parts = [
        f"{name} {_FAILURE_PHRASES.get(reasons.get(name, ''), 'did not answer')}"
        for name in collection.unreachable
    ]
    return ", ".join(parts) or "none registered"


def _answered(collection: Any) -> str:
    """Names of the providers that actually answered (for the reason text)."""
    names = [
        str(entry.get("provider", ""))
        for entry in collection.providers
        if entry.get("status") in _ANSWERED_STATUSES
    ]
    return ", ".join(n for n in names if n) or "0"


def _close_run(
    store: ResearchEvidenceStore, run_id: str, result: CompanyIntakeResult
) -> None:
    """Close the run ledger row. A bookkeeping failure is logged, not raised."""
    try:
        store.finish_run(
            run_id,
            result.state,
            honest_reason=result.honest_reason or result.error,
            coverage={
                str(entry.get("provider", "")): {
                    "status": entry.get("status"),
                    "returned": entry.get("returned"),
                    "accepted": entry.get("accepted"),
                }
                for entry in result.providers
            },
        )
    except Exception:  # noqa: BLE001 — never fail a lead over bookkeeping
        logger.exception("could not close intent-evidence run %s", run_id)


def _log_outcome(result: CompanyIntakeResult) -> None:
    """The §6 line: every number a reader needs, and the reason.

    Provider-level detail stays at DEBUG so a healthy run is one line — but
    every provider's status, count and error is logged either way, so a thin
    result is always diagnosable without re-running anything.
    """
    for entry in result.providers:
        logger.debug("intent provider %s: %s", entry.get("provider"), entry)
    logger.info(
        "intent evidence: company=%s state=%s collected=%d stored=%d "
        "known=%d rejected=%d providers=%s reason=%s",
        result.company_id or "(none)",
        result.state,
        result.evidence_collected,
        result.evidence_stored,
        result.evidence_known,
        len(result.rejected),
        ",".join(
            f"{e.get('provider')}:{e.get('status')}"
            for e in result.providers
        ) or "none",
        result.honest_reason or result.error,
    )

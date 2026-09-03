"""Inc11 Step A — per-company evidence dump: why 0 qualified on the live run.

Drives the SAME stages the live coverage run drives (discovery ->
LeadershipDiscovery -> intent plugins -> CompanyScorer -> Lead gate) but dumps
the RAW evidence each stage produced, per gate-accepted company, so the three
0-qualified blockers can be traced to their source:

- "no named decision-maker": what did LeadershipDiscovery actually find on the
  site's candidate pages (CANDIDATE_PAGES: /, /about, /team, /contact, ...)?
  Names? roles? role_relevance?
- "no person_bound email": what emails + tiers did leadership + domain
  verification produce? Generic-only (info@/contact@)? Or none at all?
- "ai_confidence < 90": what did the scorer actually receive (emails, intent
  evidence, description) and did AI run (ai_used) or error out (malformed)?

Read-only: every stage is the REAL production class — no production code is
touched or modified. No secrets/keys printed. Run from ``backend/``:

    python diagnose_lead_evidence.py
    python diagnose_lead_evidence.py 7                  # limit discovery
    python diagnose_lead_evidence.py 7 --no-intent      # skip intent (faster)
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from app.ai.scorer import CompanyScorer
from app.discovery.leadership_discovery import LeadershipDiscovery
from app.engines.lead.lead_models import (
    EmailVerificationTier,
    IntentEvidence,
    Lead,
)
from app.engines.lead.lead_pipeline import (
    LeadPipeline,
    default_intent_plugins,
)
from run_leads import discover, to_company_discovery


def _status_value(status: Any) -> str:
    """Stringify an enum/status value without importing the enum type."""
    return status.value if hasattr(status, "value") else str(status)


def _dump_leadership(records: list[dict[str, Any]]) -> None:
    """Print every leadership record: person + every email with its tier."""
    if not records:
        print(
            "    [leadership] 0 records on candidate pages "
            "(/, /about, /team, /contact, ...) — pages non-200 or no people parsed"
        )
        return
    for i, rec in enumerate(records, 1):
        person = rec.get("person") or {}
        print(
            f"    [leadership {i}] name={person.get('name')!r} "
            f"role={person.get('role')!r} role_relevance={person.get('role_relevance')} "
            f"tier={person.get('tier')} page={person.get('source_url')}"
        )
        for email in rec.get("emails") or []:
            print(
                f"        email={email.get('email')} tier={email.get('tier')} "
                f"page={email.get('source_url')}"
            )


def _dump_intent(
    plugins: list[Any],
    company_name: str,
    website: str,
    location: str,
) -> list[IntentEvidence]:
    """Run the intent plugins live and print per-plugin evidence found."""
    evidence: list[IntentEvidence] = []
    for plugin in plugins:
        name = getattr(plugin, "name", type(plugin).__name__)
        try:
            status, items, _ = plugin.collect_evidence(
                company_name=company_name, website=website, location=location
            )
        except Exception as exc:  # one bad plugin never kills the dump
            print(f"    [intent:{name}] ERROR {type(exc).__name__}: {exc}")
            continue
        print(f"    [intent:{name}] status={_status_value(status)} evidence={len(items)}")
        for item in items[:3]:
            print(
                f"        {item.type.value}: {item.source_url}  "
                f"snippet={item.snippet[:70]!r}"
            )
        evidence.extend(items)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inc11 Step A evidence dump")
    parser.add_argument(
        "limit",
        nargs="?",
        type=int,
        default=7,
        help="Max discovery results to inspect (default 7).",
    )
    parser.add_argument(
        "--no-intent",
        action="store_true",
        help="Skip the intent plugins (faster; scorer then sees no intent evidence).",
    )
    args = parser.parse_args(argv)

    industry, location = "Roofing", "Dallas Texas"
    print("=" * 70)
    print("Inc11 Step A — per-company evidence dump")
    print("=" * 70)
    print(f"Query: {industry} / {location} | limit={args.limit}")

    results, metadata = discover(industry, location, args.limit)
    print(f"data_source={metadata.get('data_source')}  returned={len(results)}")

    leadership = LeadershipDiscovery()
    scorer = CompanyScorer(use_ai=True)
    plugins: list[Any] = [] if args.no_intent else default_intent_plugins()
    query = {"industry": industry, "location": location}

    summary: list[tuple[str, int, int, int, int, float, bool, str | None, list[str]]] = []
    start = time.monotonic()
    inspected = 0
    for idx, result in enumerate(results, 1):
        company = to_company_discovery(result)
        name = company.company_name
        if not company.metadata.get("gate_accepted"):
            print(f"\n## {idx}. {name} — gate REJECTED (not sent through pipeline)")
            continue
        inspected += 1
        print(f"\n## {idx}. {name} | {company.website}")
        print(
            f"    source={company.source} gate_accepted=True "
            f"city={company.city!r} state={company.state!r}"
        )
        company_start = time.monotonic()

        records = leadership.discover(company.website)
        _dump_leadership(records)
        person, emails = LeadPipeline._select_decision_maker(records)
        print(
            f"    [selected] person={person.name if person else None!r} "
            f"emails={[f'{e.email}:{e.tier.value}' for e in emails]}"
        )

        evidence = (
            _dump_intent(plugins, name, company.website, location) if plugins else []
        )

        data = LeadPipeline()._company_dict(company, evidence, emails)
        print(
            f"    [scorer input] description={data['description'][:60]!r} "
            f"emails={data['emails']} intent_evidence={len(data['intent_evidence'])}"
        )

        try:
            result = scorer.qualify(data, query)
        except Exception as exc:  # a failed score is itself diagnostic
            print(f"    [scorer] ERROR {type(exc).__name__}: {exc}")
            summary.append((name, len(records), 0, 0, len(evidence), 0.0, False, str(exc), []))
            continue
        print(
            f"    [scorer] ai_used={result.get('ai_used')} "
            f"ai_confidence={result.get('ai_confidence')} "
            f"ai_score={result.get('ai_score')} "
            f"deterministic={result.get('deterministic_score')} "
            f"error={result.get('error')!r}"
        )
        if result.get("justification"):
            print(f"    [scorer] justification={str(result['justification'])[:120]!r}")
        if result.get("reasons"):
            print(f"    [scorer] reasons={result.get('reasons')[:4]}")

        lead = Lead(
            company=company,
            person=person,
            emails=emails,
            intent_evidence=evidence,
            ai=LeadPipeline._lead_ai_from(result),
        )
        blockers = lead.qualification_gate().blocked_by
        print(f"    [gate] blocked_by={blockers}")

        role_relevant = sum(
            1 for r in records if (r.get("person") or {}).get("role_relevance")
        )
        person_bound = sum(
            1 for e in emails if e.tier is EmailVerificationTier.person_bound
        )
        summary.append(
            (
                name,
                len(records),
                role_relevant,
                person_bound,
                len(evidence),
                float(result.get("ai_confidence") or 0.0),
                bool(result.get("ai_used")),
                result.get("error"),
                blockers,
            )
        )
        print(f"    (company stage took {time.monotonic() - company_start:.1f}s)")

    print("\n" + "=" * 70)
    print("SUMMARY — per gate-accepted company")
    print("  company | records | role_rel | person_bound | evidence | conf | ai_used | error")
    for row in summary:
        name, recs, role_rel, pb, ev, conf, ai_used, error, _ = row
        print(
            f"  {name[:32]:<32} recs={recs} role_rel={role_rel} "
            f"pb_emails={pb} evidence={ev} conf={conf:.0f} "
            f"ai_used={ai_used} error={error!r}"
        )
    print(f"\nGate-accepted companies inspected: {inspected}")
    print(f"Total stage time: {time.monotonic() - start:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

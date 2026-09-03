#!/usr/bin/env python3
"""CLI runner for AI Lead Research Agent.

Usage:
    python scripts/research_lead.py email domain
    python scripts/research_lead.py --batch records.json
    python scripts/research_lead.py --list
"""

from __future__ import annotations

import argparse
import json
import sys
import os

# Ensure backend is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Load .env (TAVILY_SEARCH_API_KEY, AI_API_KEY, etc.)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass


def cmd_research(args: argparse.Namespace) -> int:
    """Research a single email+domain."""
    from app.lead_research.service import LeadResearchService

    svc = LeadResearchService()
    dossier = svc.research(args.email, args.domain)

    print(json.dumps(dossier.to_dict(), indent=2))

    # Summary
    print("\n--- Summary ---")
    print(f"Email:        {dossier.email}")
    print(f"Domain:       {dossier.domain} -> {dossier.refined_domain}")
    print(f"Company:      {dossier.company.name or '(unknown)'}")
    print(f"Person:       {dossier.person.name or '(unknown)'} (bound={dossier.person.bound})")
    print(f"Intent:       {dossier.intent.needs_estimation} -- {dossier.intent.signal}")
    print(f"Timing:       {dossier.timing.window} -- {dossier.timing.reason}")
    print(f"Score:        {dossier.potential_score}")
    print(f"Recommendation: {dossier.recommendation}")
    print(f"Sources:      {', '.join(dossier.sources_checked)}")
    if dossier.source_errors:
        print(f"Errors:       {dossier.source_errors}")

    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    """Research a batch of records from a JSON file."""
    from app.lead_research.service import LeadResearchService

    with open(args.file, "r") as f:
        records = json.load(f)

    if not isinstance(records, list):
        print("Error: JSON file must contain an array of {email, domain} objects", file=sys.stderr)
        return 1

    svc = LeadResearchService()
    results = svc.research_batch(records)

    print(f"\n--- Batch Results ({len(results)}/{len(records)} succeeded) ---")

    # Summary table
    contact_now = [r for r in results if r.recommendation == "contact_now"]
    nurture = [r for r in results if r.recommendation == "nurture"]
    skip = [r for r in results if r.recommendation == "skip"]

    print(f"\nContact Now: {len(contact_now)}")
    for r in contact_now:
        print(f"  [+] {r.email} -- {r.company.name} -- {r.person.name} -- score={r.potential_score}")

    print(f"\nNurture: {len(nurture)}")
    for r in nurture:
        print(f"  [~] {r.email} -- {r.company.name} -- score={r.potential_score}")

    print(f"\nSkip: {len(skip)}")
    for r in skip:
        print(f"  [-] {r.email} -- {r.fit}")

    # Save full results
    output_path = args.file.rsplit(".", 1)[0] + "_results.json"
    with open(output_path, "w") as f:
        json.dump([r.to_dict() for r in results], f, indent=2)
    print(f"\nFull results saved to: {output_path}")

    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List all stored research results."""
    from app.lead_research.service import LeadResearchService

    svc = LeadResearchService()
    leads = svc.list_leads()

    if not leads:
        print("No research results stored yet.")
        return 0

    print(f"\n--- Stored Leads ({len(leads)}) ---")
    for lead in leads:
        rec_icon = {"contact_now": "[+]", "nurture": "[~]", "skip": "[-]"}.get(lead.recommendation, "?")
        print(f"  {rec_icon} {lead.email} -- {lead.company.name or '(unknown)'} -- score={lead.potential_score} -- {lead.recommendation}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Lead Research Agent CLI")
    sub = parser.add_subparsers(dest="command")

    # research
    p_research = sub.add_parser("research", help="Research a single email+domain")
    p_research.add_argument("email", help="Email address to research")
    p_research.add_argument("domain", help="Company domain")
    p_research.set_defaults(func=cmd_research)

    # batch
    p_batch = sub.add_parser("batch", help="Research a batch from JSON file")
    p_batch.add_argument("file", help="JSON file with [{email, domain}, ...]")
    p_batch.set_defaults(func=cmd_batch)

    # list
    p_list = sub.add_parser("list", help="List stored research results")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

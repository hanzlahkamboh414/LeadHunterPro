"""Person Attribution Research — CLI runner (Stages 0-2 + persist).

Researches one or more company emails via :class:`ResearchService` and persists
each job to the SQLite store. Also supports re-scoring a stored job from its
evidence (no external calls) and explicit crash recovery.

Usage, from backend/:
    python scripts/research_queue.py info@acme.com acme.com
    python scripts/research_queue.py --emails file.txt
    python scripts/research_queue.py --rescore info@acme.com
    python scripts/research_queue.py --recover
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))
sys.path.insert(0, str(_HERE.parent))

from app.person_research.service import ResearchService  # noqa: E402
from app.person_research.store import ResearchStore  # noqa: E402


def _default_db() -> str:
    return str(pathlib.Path(_HERE.parents[1]) / "data" / "person_research.sqlite3")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email_domain", nargs="*", help="EMAIL DOMAIN pairs to research")
    parser.add_argument("--emails", help="file with one 'EMAIL DOMAIN' per line")
    parser.add_argument("--rescore", metavar="EMAIL", help="re-score a stored job (no external calls)")
    parser.add_argument("--recover", action="store_true", help="requeue any interrupted jobs")
    parser.add_argument("--db", default=None, help="sqlite path (default data/person_research.sqlite3)")
    parser.add_argument("--json", action="store_true", help="print raw JSON result")
    args = parser.parse_args(argv)

    db = args.db or _default_db()
    pathlib.Path(db).parent.mkdir(parents=True, exist_ok=True)
    store = ResearchStore(db)
    store.recover_interrupted()
    service = ResearchService(store)

    if args.recover:
        n = len(store.pending_jobs())
        print(f"recovered: {n} job(s) back to pending")
        return 0

    if args.rescore:
        result = service.rescore(args.rescore)
        if result is None:
            print(f"no stored job for {args.rescore}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(service.orchestrator.research_result_dict(result), indent=2))
        else:
            _print_human(args.rescore, result.domain, result, prefix="[rescore]")
        return 0

    pairs: list[tuple[str, str]] = []
    if args.email_domain:
        if len(args.email_domain) % 2 != 0:
            print("error: pass EMAIL and DOMAIN in pairs", file=sys.stderr)
            return 2
        it = iter(args.email_domain)
        pairs = list(zip(it, it, strict=True))
    if args.emails:
        for line in pathlib.Path(args.emails).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                pairs.append((parts[0], parts[1]))

    if not pairs:
        parser.print_help()
        return 0

    for email, domain in pairs:
        result = service.research(email, domain)
        if args.json:
            print(json.dumps(service.orchestrator.research_result_dict(result), indent=2))
        else:
            _print_human(email, domain, result)

    return 0


def _print_human(email: str, domain: str, result, prefix: str = "") -> None:
    tag = f" {prefix}" if prefix else ""
    print(f"\n=={tag} {email} ({domain}) ==  verdict={result.verdict.value}")
    if result.source_errors:
        for k, v in result.source_errors.items():
            print(f"   skip[{k}]: {v}")
    for c in result.candidates:
        mark = "BOUND" if c.bound else ("(contradiction)" if c.contradictory else "")
        print(
            f"   - {c.name:<24} role={c.role!r} local={c.local_part_match_level} "
            f"score={c.confidence} corrob={c.corroboration_count} {mark}"
        )


if __name__ == "__main__":
    raise SystemExit(main())

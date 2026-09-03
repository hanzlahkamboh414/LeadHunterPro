"""Probe: do the founder's plan-holder-list dorks return PDF URLs?

READ-ONLY DIAGNOSTIC. Not a production module and adds no dependency.

WHY THIS EXISTS
    The sourcing redesign moves to the "Plan Holder List" / "Bid Holders
    List" / "Prequalified Contractors List" PDF pattern: when a public
    project goes to bid, the agency publishes a PDF listing every
    contractor that pulled the plans -- company, contact person, email,
    phone, project. That one artifact carries proven participation + a
    decision maker + a public email + a project + a timing signal, which is
    exactly the LeadPipeline input we want.

    Before building ANY PDF extractor or parser, one question must be
    answered empirically from THIS machine: do the dorks actually surface
    plan-holder-list PDFs through the search providers we already have, and
    which provider honors the `filetype:pdf` / `intitle:` operators?

WHAT IT DOES
    Runs each dork through every ENABLED search provider (auto-registered
    from backend/.env) and prints the returned URLs, separating .pdf URLs
    (the candidates) from the rest. It reports a pdf ratio per provider so a
    semantic engine that ignores `filetype:pdf` (e.g. Tavily) shows up as a
    low ratio rather than being mistaken for "no PDFs exist".

WHAT IT DOES NOT DO / DOES NOT KNOW
    It does not fetch or parse any PDF. It reuses ONLY existing production
    code (app.search_providers). An empty result means "this provider
    returned no PDF URL for this dork from this machine right now" -- never
    "no plan-holder PDF exists". No API key is ever printed.

Usage, from backend/:
    python scripts/probe_search_dork.py
    python scripts/probe_search_dork.py '"Plan Holder List" filetype:pdf texas'
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
from urllib.parse import urlparse

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))  # backend/ on path for `import app`

# Importing the package auto-registers providers from backend/.env (config
# loads the env file via pydantic-settings). This is the ONLY reused entry
# point -- no provider is instantiated here, so each keeps the API key that
# auto-registration injected from settings.
import app.search_providers  # noqa: E402,F401
from app.search_providers.models import SearchQuery  # noqa: E402
from app.search_providers.registry import get_registry  # noqa: E402

# The founder's exact dork set. Overridable on the command line (each arg is
# one dork) so a new trade/state can be probed without editing this file.
_DEFAULT_DORKS = [
    '"Plan Holder List" filetype:pdf roofing',
    '"Plan Holders List" filetype:pdf',
    '"List of Plan Holders" filetype:pdf',
    '"Bid Holders List" filetype:pdf',
    '"Prequalified Contractors List" filetype:pdf DOT',
    'intitle:"plan holder list" pdf',
]

_NUM = 20


def _is_pdf(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".pdf") or ".pdf" in url.lower()


async def _run() -> int:
    dorks = sys.argv[1:] or _DEFAULT_DORKS
    registry = get_registry()
    providers = registry.get_enabled()

    print("=" * 100)
    print("PLAN-HOLDER DORK PROBE  (read-only; reuses app.search_providers)")
    print("=" * 100)

    if not providers:
        # CLAUDE.md section 5: never go silent. Say it, and say WHY.
        print("\n  NO SEARCH PROVIDERS REGISTERED")
        print("  Live dork discovery cannot run. Reason: no provider had its")
        print("  configuration set in backend/.env:")
        print("    - SEARXNG_URL           (SearXNG)")
        print("    - BRAVE_SEARCH_API_KEY  (Brave)")
        print("    - TAVILY_SEARCH_API_KEY (Tavily)")
        print("  Set at least one and re-run.")
        return 1

    print(f"\n  Enabled providers : {', '.join(p.provider_name for p in providers)}")
    print(f"  Dorks             : {len(dorks)}")
    print(f"  Results per dork  : {_NUM}")

    tally: dict[str, dict[str, int]] = {
        p.provider_name: {"urls": 0, "pdfs": 0, "dorks_with_pdf": 0}
        for p in providers
    }

    try:
        for provider in providers:
            name = provider.provider_name
            print("\n" + "=" * 100)
            print(f"PROVIDER: {name}")
            print("=" * 100)
            for dork in dorks:
                try:
                    resp = await provider.search(
                        SearchQuery(keywords=dork, num_results=_NUM)
                    )
                except Exception as exc:  # diagnostic must not die on one dork
                    print(f"\n  DORK: {dork}")
                    print(f"    EXCEPTION: {type(exc).__name__}: {exc}")
                    continue

                results = resp.results
                pdfs = [r for r in results if _is_pdf(r.url)]
                tally[name]["urls"] += len(results)
                tally[name]["pdfs"] += len(pdfs)
                if pdfs:
                    tally[name]["dorks_with_pdf"] += 1

                print(f"\n  DORK: {dork}")
                print(
                    f"    status={resp.status}  results={len(results)}  "
                    f"pdf={len(pdfs)}  err={resp.error or '-'}"
                )
                if pdfs:
                    print("    PDF URLs (plan-holder-list candidates):")
                    for r in pdfs:
                        print(f"      + {r.url}")
                        if r.title:
                            print(f"          {r.title[:80]}")
                non_pdf = [r for r in results if not _is_pdf(r.url)]
                if non_pdf:
                    print(f"    non-PDF URLs ({len(non_pdf)}):")
                    for r in non_pdf[:5]:
                        print(f"      - {r.url}")
                    if len(non_pdf) > 5:
                        print(f"      ... {len(non_pdf) - 5} more")
    finally:
        for provider in providers:
            close = getattr(provider, "close", None)
            if close:
                try:
                    await close()
                except Exception:
                    pass

    print("\n" + "=" * 100)
    print("SUMMARY  (pdf_ratio shows whether the provider honored filetype:pdf)")
    print("=" * 100)
    for name, t in tally.items():
        ratio = (t["pdfs"] / t["urls"] * 100) if t["urls"] else 0.0
        print(
            f"  {name:<10}  urls={t['urls']:<4} pdfs={t['pdfs']:<4} "
            f"dorks_with_pdf={t['dorks_with_pdf']}/{len(dorks)}  "
            f"pdf_ratio={ratio:.0f}%"
        )
    print("\n  Reading this:")
    print("   - High pdf_ratio -> provider honors filetype:pdf (Brave/SearXNG expected).")
    print("   - Low pdf_ratio but some pdfs -> semantic engine (Tavily): keep it, but")
    print("     post-filter to .pdf URLs downstream.")
    print("   - pdfs=0 everywhere -> dorks/providers don't surface these from this")
    print("     machine; does NOT prove no plan-holder PDF exists. Try Brave/CSE or")
    print("     probe a known-good PDF directly.")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_run()))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(1) from None

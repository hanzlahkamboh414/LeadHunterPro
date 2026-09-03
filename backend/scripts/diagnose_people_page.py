"""Roadmap D25 — why did the people scan find nobody on pages that LOADED?

D20's instrumentation narrowed a fruitless people-scan to the parsing stage:
the 2026-08-19 live run returned ``parser`` on 5 of 5 visible scans, with pages
fetched everywhere (``ok=1..3``), ``candidates=0`` everywhere and ``errors={}``
everywhere. That rules out missing paths, client blocking and transport — but
``candidates=0`` is still consistent with two situations that have OPPOSITE
fixes, and guessing between them is exactly what CLAUDE.md §7 forbids:

  (a) PARSER GAP  — named people ARE on the page, but some rule rejects them.
                    Fixed with code, in a known place.
  (b) NO PEOPLE   — the site publishes no named staff at all. The parser is
                    behaving CORRECTLY and no parser work will ever help; the
                    answer is additional people-bearing sources (licensing
                    boards, registries, permits).

This tool decides (a) vs (b) with evidence, by running the REAL production
constants and the REAL :class:`PeopleParser` (§14 — nothing is reimplemented
here) over the REAL candidate pages, and reporting a per-stage FUNNEL. Whatever
stage the funnel collapses at IS the answer:

    regions found -> length ok -> role keyword -> name shape -> name accepted

When a region has a role keyword but yields no name, every capitalized phrase in
it is re-tested against each acceptance rule individually, and the FIRST rule
that rejects it is named. So the output does not merely say "no name found" — it
says *which rule* discarded *which phrase*, e.g.::

    Chris Arrington     REJECTED by: given-name whitelist

That distinction is the whole point: a phrase rejected by the given-name
whitelist is a real person we threw away (a), while a page whose regions carry
no role keyword and no name-shaped phrase at all has nobody to find (b).

There is a THIRD outcome, and omitting it made this tool able to answer (b) when
the truth was (a). Acceptance is decided by ``_find_name_near_role``, which is
**positional**: it walks tokens backwards from the role and breaks on a boundary
character, stopword or lead-in, and considers only what sits immediately before
or after the role. The rule mirror below models ``_is_plausible_name`` and knows
nothing about position. So a perfectly good full name that every rule accepts,
sitting one boundary character too far from its role, used to be found, silently
dropped by the attribution loop, and then reported as "no region carries a
name-shaped phrase alongside a role" — the opposite of what the page contained,
pointing the reader at new data sources when a fix to the walk was what was
needed. Such phrases are now reported in their own ``bypos`` bucket::

    Chris Arrington     declined by: position/boundary

Read the two buckets as two different repairs: ``byrule`` is fixed in the word
lists, ``bypos`` is fixed in the token walk. Both are (a).

Why this lives in ``scripts/`` and not at the ``backend/`` root: the root already
holds 16 one-off probe/diagnose scripts from previous investigations, none of
which could be reused for the next one (and three of which, being named
``test_*``, broke pytest collection until ``pytest.ini`` grew a ``testpaths``
workaround). This is a permanent, re-runnable diagnostic, not a scratch file.

Read-only: production classes are imported and called, never modified, and no
page is written to except the optional HTML cache. Fetched HTML is cached under
``backend/output/diagnostics/`` (gitignored, so no site content enters the repo)
so the analysis can be re-run and iterated on WITHOUT re-hitting the site.

Usage, from ``backend/``::

    python scripts/diagnose_people_page.py boldroofing.com
    python scripts/diagnose_people_page.py https://bertroofing.com --verbose
    python scripts/diagnose_people_page.py arringtonroofing.com --cached
    python scripts/diagnose_people_page.py a.com b.com c.com

Flags:
    --cached    Re-analyse previously saved HTML only; makes zero requests.
    --verbose   Also dump every role-bearing region's text (truncated).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Make ``app`` importable. Without this line the imports below raise
# ``ModuleNotFoundError: No module named 'app'``, because `python
# scripts/diagnose_people_page.py` puts **scripts/** on ``sys.path`` — not
# ``backend/`` — and nothing installs this package (there is no `[project]`
# table; see backend/pyproject.toml). The same bootstrap is in
# ``scripts/smoke_ai.py``; `pyproject.toml`'s permanent E402 ignore exists
# precisely so a scripts/ entrypoint may adjust the path before importing `app`,
# so the import order below is deliberate rather than an oversight.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.discovery import people_parser as pp
from app.discovery.leadership_discovery import LeadershipDiscovery

CACHE_DIR = Path("output/diagnostics")

#: Same transport shape as production (LeadershipDiscovery.discover), so a page
#: that loads here loads there too — otherwise the diagnosis is about a
#: different request than the one that actually failed.
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TIMEOUT = 10


@dataclass
class PageFunnel:
    """Per-page counts for each stage a region must survive to yield a person."""

    path: str
    status: int = 0
    error: str = ""
    regions_total: int = 0
    length_ok: int = 0
    role_found: int = 0
    name_shape_found: int = 0
    accepted: int = 0
    #: (phrase, rule that rejected it) for regions that had a role but no name.
    rejections: list[tuple[str, str]] = field(default_factory=list)
    #: Name-shaped phrases that every name-plausibility rule ACCEPTS, and which
    #: the real parser still declined. These cannot be explained by rule
    #: mirroring, so they are kept in their own bucket rather than dropped: the
    #: refusal must then come from `_find_name_near_role`'s POSITIONAL logic —
    #: the backward token walk that breaks on a boundary character, stopword or
    #: lead-in, or a phrase that sits neither immediately before nor immediately
    #: after the role. Without this bucket such a page reports "no name-shaped
    #: phrase alongside a role", which is the opposite of what is true, and the
    #: verdict then sends the reader off to find new data sources when a
    #: positional fix would have worked.
    unexplained: list[str] = field(default_factory=list)
    #: Role keywords actually seen on the page, for context.
    roles_seen: set[str] = field(default_factory=set)
    #: Role-bearing region texts, only kept for --verbose.
    region_samples: list[str] = field(default_factory=list)


def _reject_reason(phrase: str, company_name: str) -> str:
    """Name the FIRST production rule that rejects ``phrase`` as a person name.

    Mirrors :meth:`PeopleParser._is_plausible_name` rule-for-rule so the report
    can attribute a rejection instead of only reporting absence. Kept in the
    same order as the production method; if that method changes, this reads as
    a stale diagnostic rather than a silent wrong answer, because the final
    ``accepted`` line below is computed by the REAL parser, not by this.

    SCOPE LIMIT, load-bearing: this models name *plausibility* only. It does not
    model ``_find_name_near_role``'s positional logic, so ``""`` does NOT mean
    "the parser would accept this phrase" — it means "no word-list rule objects".
    Callers must therefore treat ``""`` on a phrase the parser declined as a
    finding in its own right (the ``unexplained`` bucket), never as nothing.

    Returns:
        A short rule name, or ``""`` if every name-plausibility rule accepts the
        phrase — which is a positive finding, not an absence. See above.
    """
    tokens = phrase.strip().split()
    if not 2 <= len(tokens) <= 4:
        return f"token-count ({len(tokens)}, need 2-4)"
    lower = phrase.strip().lower()
    words = set(lower.split())
    if lower in pp.NAME_NOISE:
        return "noise-heading list"
    hit = words & pp.NAME_STOPWORDS
    if hit:
        return f"stopword {sorted(hit)}"
    hit = words & pp.NAME_PROSE_WORDS
    if hit:
        return f"prose-word blacklist {sorted(hit)}"
    hit = words & pp._ROLE_TOKEN_LOWER
    if hit:
        return f"role-token {sorted(hit)}"
    if not (set(re.findall(r"[a-z]+", lower)) & pp.COMMON_GIVEN_NAMES):
        return "given-name whitelist"
    if company_name:
        company_lower = company_name.strip().lower()
        if company_lower in lower or lower in company_lower:
            return "company-name guard"
    return ""


def _cache_path(website: str, path: str) -> Path:
    """Return the on-disk cache location for one fetched page."""
    host = urlparse(website).netloc or website
    slug = path.strip("/").replace("/", "_") or "index"
    return CACHE_DIR / _safe(host) / f"{_safe(slug)}.html"


def _safe(text: str) -> str:
    """Reduce a host/path fragment to a filesystem-safe token."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)


def _load_page(website: str, path: str, cached_only: bool) -> tuple[int, str, str]:
    """Fetch (or read from cache) one candidate page.

    Returns:
        ``(status, html, error)``. ``status`` is 0 when nothing was reachable;
        ``error`` carries the exception class name, matching how
        :class:`LeadershipScanStats` counts transport failures.
    """
    cache = _cache_path(website, path)
    if cached_only:
        if cache.exists():
            return 200, cache.read_text(encoding="utf-8", errors="replace"), ""
        return 0, "", "NotCached"

    url = urljoin(website, path)
    try:
        response = requests.get(
            url, timeout=_TIMEOUT, headers=_HEADERS, allow_redirects=True
        )
    except requests.RequestException as exc:
        return 0, "", type(exc).__name__

    if response.status_code == 200:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(response.text, encoding="utf-8", errors="replace")
    return response.status_code, response.text, ""


def analyse_page(
    website: str, path: str, cached_only: bool, parser: pp.PeopleParser
) -> PageFunnel:
    """Run the production region filters over one page and record the funnel."""
    funnel = PageFunnel(path=path)
    funnel.status, html, funnel.error = _load_page(website, path, cached_only)
    if funnel.status != 200 or not html:
        return funnel

    soup = BeautifulSoup(html, "html.parser")
    company = parser._site_name(soup)

    for tag in soup.find_all(pp.REGION_TAGS):
        funnel.regions_total += 1
        text = tag.get_text(" ", strip=True)
        if not (pp.MIN_REGION_LEN <= len(text) <= pp.MAX_REGION_LEN):
            continue
        funnel.length_ok += 1

        role = parser._detect_role(text)
        if not role:
            continue
        funnel.role_found += 1
        funnel.roles_seen.add(role)
        funnel.region_samples.append(text)

        if not pp.NAME_PATTERN.search(text):
            continue
        funnel.name_shape_found += 1

        # The real parser decides acceptance. Only when it declines do we
        # attribute the refusal, phrase by phrase. A phrase that no rule
        # rejects must NOT be dropped here — that silence is what used to turn
        # a positional parser gap into a "no people" verdict.
        if parser._find_name_near_role(text, role.lower(), company):
            funnel.accepted += 1
            continue
        for phrase in pp.NAME_PATTERN.findall(text):
            reason = _reject_reason(phrase, company)
            if reason:
                funnel.rejections.append((phrase, reason))
            else:
                funnel.unexplained.append(phrase)

    return funnel


def _verdict(funnels: list[PageFunnel]) -> str:
    """Turn the collected funnels into the (a)-vs-(b) answer this tool exists for."""
    fetched = [f for f in funnels if f.status == 200]
    if not fetched:
        return (
            "INCONCLUSIVE — no page returned 200, so this is a fetch problem "
            "(D20's blocked/missing/transport buckets), not a parser question."
        )
    if any(f.accepted for f in fetched):
        return (
            "PARSER WORKS on this site — at least one name was accepted. "
            "If the live run still reported no decision-maker, the loss is "
            "downstream of parsing (role_relevance, email binding, or the gate)."
        )
    rejected = [r for f in fetched for r in f.rejections]
    unexplained = [p for f in fetched for p in f.unexplained]
    if rejected or unexplained:
        parts: list[str] = []
        if rejected:
            by_rule: dict[str, int] = {}
            for _phrase, rule in rejected:
                by_rule[rule] = by_rule.get(rule, 0) + 1
            worst = sorted(by_rule.items(), key=lambda kv: -kv[1])
            top = ", ".join(f"{rule} x{count}" for rule, count in worst[:3])
            parts.append(
                f"{len(rejected)} phrase(s) rejected by a NAME RULE "
                f"(dominant: {top}) — the fix is in those word lists"
            )
        if unexplained:
            parts.append(
                f"{len(unexplained)} phrase(s) that every name rule ACCEPTS and "
                "the parser still declined, so the refusal is POSITIONAL rather "
                "than rule-based: `_find_name_near_role` walks backwards from "
                "the role and breaks on a boundary character, stopword or "
                "lead-in, and looks only immediately before/after it — the fix "
                "is in that walk, not in the word lists"
            )
        return (
            "(a) PARSER GAP — name-shaped phrases were found next to a role and "
            "then discarded: " + "; ".join(parts) + ". These are candidate real "
            "people being thrown away; read the phrases listed above before "
            "changing anything."
        )
    if not any(f.role_found for f in fetched):
        return (
            "(b) NO PEOPLE — not one region on any fetched page contains a role "
            "keyword. There is nobody on this website to extract, so no parser "
            "change can help. This site needs a different source (licensing "
            "board, registry, permits) or must be dropped."
        )
    return (
        "(b) NO PEOPLE (weaker form) — role keywords appear, but no region "
        "carries a name-shaped phrase alongside one. Likely generic copy "
        '("our project managers are..."), not a named individual.'
    )


def report(website: str, cached_only: bool, verbose: bool) -> list[PageFunnel]:
    """Analyse every production candidate page of one site and print the funnel."""
    if not website.startswith(("http://", "https://")):
        website = f"https://{website}"

    print("=" * 78)
    print(f"D25 people-page diagnosis: {website}")
    print("=" * 78)

    parser = pp.PeopleParser()
    funnels = [
        analyse_page(website, path, cached_only, parser)
        for path in LeadershipDiscovery.CANDIDATE_PAGES
    ]

    header = (
        f"{'path':<14}{'status':>7}{'regions':>9}{'len-ok':>8}"
        f"{'role':>6}{'name?':>7}{'OK':>5}{'byrule':>8}{'bypos':>7}"
    )
    print(header)
    print("-" * len(header))
    for f in funnels:
        status = f.error or str(f.status)
        print(
            f"{f.path:<14}{status:>7}{f.regions_total:>9}{f.length_ok:>8}"
            f"{f.role_found:>6}{f.name_shape_found:>7}{f.accepted:>5}"
            f"{len(f.rejections):>8}{len(f.unexplained):>7}"
        )

    roles = sorted({r for f in funnels for r in f.roles_seen})
    print(f"\nRole keywords seen on this site: {roles or '(none)'}")

    rejections: dict[tuple[str, str], int] = {}
    for f in funnels:
        for phrase, rule in f.rejections:
            rejections[(phrase, rule)] = rejections.get((phrase, rule), 0) + 1
    if rejections:
        print("\nName-shaped phrases found next to a role, then DISCARDED:")
        for (phrase, rule), count in sorted(
            rejections.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            seen = f"  (x{count})" if count > 1 else ""
            print(f"  {phrase:<38} REJECTED by: {rule}{seen}")

    positional: dict[str, int] = {}
    for f in funnels:
        for phrase in f.unexplained:
            positional[phrase] = positional.get(phrase, 0) + 1
    if positional:
        print(
            "\nName-shaped phrases every name rule ACCEPTS, which the parser "
            "still declined\n(so the refusal is POSITIONAL — the backward walk "
            "in _find_name_near_role):"
        )
        for phrase, count in sorted(positional.items(), key=lambda kv: (-kv[1], kv[0])):
            seen = f"  (x{count})" if count > 1 else ""
            print(f"  {phrase:<38} declined by: position/boundary{seen}")

    if verbose:
        print("\nRole-bearing regions (truncated to 200 chars):")
        for f in funnels:
            for text in f.region_samples:
                print(f"  [{f.path}] {text[:200]}")

    print(f"\nVERDICT: {_verdict(funnels)}\n")
    return funnels


def main(argv: list[str]) -> int:
    """Entry point. Returns a process exit code."""
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    unknown = flags - {"--cached", "--verbose"}
    if unknown or not args:
        print(f"unknown flag(s): {sorted(unknown)}" if unknown else "no site given")
        print(__doc__)
        return 2

    for website in args:
        report(website, "--cached" in flags, "--verbose" in flags)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

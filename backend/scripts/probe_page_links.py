"""What is actually ON a page -- links, downloads and forms, categorised.

WHY THIS EXISTS (roadmap D33, D25 sourcing decision)
    ``scripts/network_diagnostics.py`` proved on 2026-08-20 that
    ``comptroller.texas.gov/purchasing/vendor/cmbl/`` returns HTTP 200 with
    125,078 bytes through CloudFront, while the CMBL application host
    ``mycpa.cpa.state.tx.us`` times out at TCP connect from this machine. So a
    reachable route to CMBL *material* exists. That leaves the question the
    reachability tool structurally cannot answer: is there a BULK DOWNLOAD on
    the reachable host, or does every route lead back to the unreachable one?

    A 500-character body preview cannot answer it -- on that page the first 500
    characters are ``<!DOCTYPE html>`` and two ``<head>`` tags. The answer is in
    the anchors, and the anchor TEXT is most of the signal: "Download the
    complete CMBL" and "Search the CMBL" are the same shape of URL and
    completely different findings.

    This is written argv-driven and permanent for the same reason
    ``network_diagnostics.py`` was made argv-driven: the next registry (ESBD,
    SAM.gov, TDLR, a county permit portal) raises exactly this question again,
    and the operator must not have to open the backend to ask it.

WHAT IS REUSED, AND WHAT IS NOT (CLAUDE.md 14)
    Link discovery is NOT reimplemented here. ``app.crawlers.html_parser``
    is live production code -- ``app/discovery/sources/directory_crawl_source.py``
    (the API-free source) parses every crawled page with it -- and its
    ``HTMLParser.parse()`` already skips ``#``/``javascript:`` hrefs, resolves
    relative paths against a base URL and de-duplicates. That method owns the
    URL set here; this script only sorts and labels what it returns, and also
    takes the page title and any emails from the same single parse.

    ONE overlapping pass is unavoidable and is deliberately scoped:
    ``ParsedPage.links`` is ``list[str]``, so anchor text cannot survive it.
    Rather than re-discover the links, :func:`_anchor_texts` walks the anchors
    only to ATTACH text to URLs ``HTMLParser`` already found, joined on the
    resolved URL; a URL the parser did not return is not introduced here. That
    information loss is a real production defect, not just an inconvenience for
    this script -- ``DirectoryCrawlSource._member_links`` has to guess which
    links are company profiles from URL path tokens alone, so a directory whose
    entries are ``/profile.aspx?id=123`` with the company name in the anchor
    text is invisible to it. Roadmap D34 carries that.

WHAT THIS TOOL DOES NOT KNOW
    It reads ``<a href>`` and ``<form action>`` from server-rendered HTML. A
    download built by JavaScript at click time leaves no anchor and will not
    appear. So "no bulk-data link listed" means EXACTLY that, and never "no
    bulk download exists" -- the report says so, in those words, because three
    diagnostics in this repo have now printed confident verdicts that were
    wrong while every line that merely reported was right (roadmap D31).

    For the same reason the exit code reports whether the FETCH worked, never
    whether a download was found. Finding none is a legitimate result, not a
    failure.

Usage, from ``backend/``::

    python scripts/probe_page_links.py comptroller.texas.gov/purchasing/vendor/cmbl/
    python scripts/probe_page_links.py https://www.txsmartbuy.gov/esbd --all
    python scripts/probe_page_links.py site.gov/a site.gov/b

Flags:
    --all       List every same-host link, not only the interesting ones.

Exit code 0 when every URL was fetched, 1 when any fetch failed, 2 on an
unexpected exception.
"""

from __future__ import annotations

import pathlib
import sys
from collections import defaultdict
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Two bootstraps, for two different import problems.
#
# `python scripts/probe_page_links.py` puts **scripts/** on sys.path, not
# `backend/`, and nothing installs this package (there is no [project] table in
# backend/pyproject.toml). So `app` needs the parent, and the sibling
# `doh_resolver` needs this directory. `pyproject.toml`'s permanent E402 ignore
# exists precisely so a scripts/ entrypoint may fix its path before importing.
_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))
sys.path.insert(0, str(_HERE.parent))

import doh_resolver  # noqa: E402

from app.crawlers.html_parser import HTMLParser  # noqa: E402

#: Same transport shape as the reachability tool, so a page that loads there
#: loads here -- otherwise this is a report about a different request.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

#: (connect, read) rather than one number. The 2026-08-20 19:40 diagnostic run
#: spent 30 of its 44 seconds inside two 15-second CONNECT timeouts on hosts
#: that were never going to answer, while every host that did answer connected
#: in under 2 seconds. Connecting and reading fail for different reasons and
#: deserve different patience: a slow report may legitimately take 20 seconds to
#: generate, but a TCP handshake that has not completed in 6 is not going to.
#: Roadmap D33 requires this split at the production fetch boundary too -- at
#: the 20,000-request/day target a fail-slow path is the entire budget.
_TIMEOUT = (6, 25)

#: Extensions that mean "the whole dataset in one file" -- the outcome that
#: turns a registry from a page to be scraped into a file to be downloaded.
#: ``.txt``/``.dat`` are here because state registries of CMBL's generation
#: commonly publish fixed-width text, and ``.mdb``/``.accdb`` because several
#: still publish Access databases.
_BULK_EXT = (
    ".csv", ".tsv", ".txt", ".dat", ".xls", ".xlsx", ".zip",
    ".json", ".xml", ".mdb", ".accdb", ".gz",
)

#: Extensions that are a document, not a dataset. Worth listing separately: a
#: PDF vendor list is still a route to the data, just an expensive one.
_DOC_EXT = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".rtf")

#: Words that mark a link as worth reading even when its URL carries no
#: extension at all -- ``/Download.aspx?id=9``, ``/reports/vendors`` and
#: ``/data/export`` are all downloads with nothing for the extension test to
#: match, and matching on extension alone would report "nothing found" on a page
#: whose entire purpose is distributing a file.
#:
#: Split into two strengths on purpose. A government purchasing page has
#: hundreds of links and words like "list", "vendor" and "search" appear all over
#: its navigation, so one broad bucket would bury the three links that matter
#: under a hundred that do not -- and a report nobody can read is a report that
#: gets skimmed, which is how a finding gets missed. STRONG words name the act of
#: obtaining a file; WEAK words merely name the subject matter.
_STRONG_WORDS = (
    "download", "export", "bulk", "dataset", "data set", "csv", "excel",
    "spreadsheet", "extract", "raw data", "full list", "complete list",
    "open data", "ftp", "api",
)

_WEAK_WORDS = (
    "file", "report", "list", "directory", "vendor", "bidder", "cmbl",
    "search", "registry", "database", "archive", "publication", "data",
)

#: Hosts already PROVEN unreachable from this machine, so a link leading to one
#: is labelled rather than silently listed as a route. Not a claim that the host
#: is down -- a connect timeout is symmetric and cannot tell "my route drops
#: packets" from "their firewall drops my region" (roadmap D33).
_KNOWN_UNREACHABLE = ("mycpa.cpa.state.tx.us",)


def _norm(target: str) -> str:
    """Accept a bare hostname/path the way the reachability tool does."""
    if target.startswith(("http://", "https://")):
        return target
    return f"https://{target}"


def _anchor_texts(soup: BeautifulSoup, base_url: str) -> dict[str, str]:
    """Map resolved URL -> anchor text, for links HTMLParser already found.

    This exists ONLY because ``ParsedPage.links`` is ``list[str]`` and drops the
    text. It deliberately does not filter, de-duplicate or decide anything: the
    caller intersects it with the parser's link list, so a URL this pass sees and
    the parser rejected is never introduced into the report. Where two anchors
    share a URL the longer text wins, since a nav icon and a described link
    routinely point at the same page and the described one is the informative one.
    """
    texts: dict[str, str] = {}
    for tag in soup.find_all("a", href=True):
        href = str(tag["href"])
        if href.startswith(("#", "javascript:")):
            continue
        url = urljoin(base_url, href)
        text = " ".join(tag.get_text(" ", strip=True).split())
        if not text:
            # An image-only link. Its alt text is the nearest thing to a label.
            img = tag.find("img")
            if img is not None:
                text = " ".join(str(img.get("alt", "")).split())
        if len(text) > len(texts.get(url, "")):
            texts[url] = text
    return texts


def _forms(soup: BeautifulSoup, base_url: str) -> list[tuple[str, str, list[str]]]:
    """Return (method, action URL, field names) for every form on the page.

    Forms are reported because on registries of this generation the data is
    routinely BEHIND one -- CMBL's own search is a POST -- so a page with no
    download anchor and one search form is a completely different finding from a
    page with neither, and the extension test alone cannot tell them apart.
    """
    found: list[tuple[str, str, list[str]]] = []
    for form in soup.find_all("form"):
        method = str(form.get("method", "get")).upper()
        action = urljoin(base_url, str(form.get("action", "")))
        fields = [
            str(field.get("name"))
            for field in form.find_all(["input", "select", "textarea"])
            if field.get("name")
        ]
        found.append((method, action, fields))
    return found


def _classify(url: str, text: str, page_host: str) -> str:
    """Bucket one link. Returns a bucket key, never a verdict."""
    path = urlparse(url).path.lower()
    host = (urlparse(url).netloc or "").lower()
    if path.endswith(_BULK_EXT):
        return "bulk"
    if path.endswith(_DOC_EXT):
        return "doc"
    if any(known in host for known in _KNOWN_UNREACHABLE):
        return "unreachable"
    haystack = f"{text} {url}".lower()
    if any(word in haystack for word in _STRONG_WORDS):
        return "strong"
    if any(word in haystack for word in _WEAK_WORDS):
        return "weak"
    if host and host != page_host:
        return "offsite"
    return "other"


def _show(label: str, rows: list[tuple[str, str]], limit: int = 0) -> None:
    """Print one bucket. ``limit`` 0 means print everything."""
    print(f"\n  {label}  ({len(rows)})")
    if not rows:
        print("    (none)")
        return
    shown = rows if limit <= 0 else rows[:limit]
    for url, text in shown:
        print(f"    {text[:58] or '(no text)':<58} {url}")
    if len(rows) > len(shown):
        print(f"    ... {len(rows) - len(shown)} more (pass --all to list them)")


def probe(url: str, show_all: bool) -> bool:
    """Fetch one URL and print its categorised links. Returns fetch success."""
    print("=" * 100)
    print(f"PAGE: {url}")
    print("=" * 100)

    try:
        response = requests.get(
            url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True
        )
    except requests.RequestException as exc:
        print(f"  FETCH FAILED: {type(exc).__name__}: {exc}")
        print("    Nothing was parsed, so this page's links are UNKNOWN, not absent.")
        print(f"    Next: python scripts/network_diagnostics.py {url}")
        return False

    html = response.text
    print(f"  Status      : HTTP {response.status_code}")
    print(f"  Final URL   : {response.url}")
    print(f"  Body size   : {len(response.content):,} bytes")
    print(f"  Server      : {response.headers.get('Server', '-')}")
    print(f"  Content-type: {response.headers.get('Content-Type', '-')}")

    if response.status_code != 200 or not html:
        print("    Non-200 or empty body -- link extraction skipped.")
        return False

    # Production parser owns the link set, the title and the emails.
    parsed = HTMLParser().parse(html, base_url=str(response.url))
    soup = BeautifulSoup(html, "html.parser")
    texts = _anchor_texts(soup, str(response.url))
    page_host = (urlparse(str(response.url)).netloc or "").lower()

    print(f"  Title       : {parsed.title or '(none)'}")
    print(f"  Links found : {len(parsed.links)}")
    if parsed.emails:
        print(f"  Emails      : {', '.join(parsed.emails[:8])}")

    buckets: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for link in parsed.links:
        text = texts.get(link, "")
        buckets[_classify(link, text, page_host)].append((link, text))

    _show("BULK DATA FILES (.csv/.xlsx/.txt/.zip/...)", buckets["bulk"])
    _show("DOCUMENTS (.pdf/.doc/...)", buckets["doc"], 0 if show_all else 15)
    _show("LINKS TO KNOWN-UNREACHABLE HOSTS", buckets["unreachable"])
    _show("STRONG SIGNAL (wording names a file or an export)", buckets["strong"])
    _show("WEAK SIGNAL (wording names the subject)", buckets["weak"], 0 if show_all else 20)
    _show("OFF-SITE", buckets["offsite"], 0 if show_all else 10)
    _show("EVERYTHING ELSE (same host)", buckets["other"], 0 if show_all else 8)

    forms = _forms(soup, str(response.url))
    print(f"\n  FORMS ON PAGE  ({len(forms)})")
    if not forms:
        print("    (none)")
    for method, action, fields in forms:
        print(f"    {method:<5} {action}")
        if fields:
            print(f"          fields: {', '.join(fields[:12])}")

    off_hosts: dict[str, int] = defaultdict(int)
    for link, _text in [*buckets["offsite"], *buckets["unreachable"]]:
        off_hosts[(urlparse(link).netloc or "?").lower()] += 1
    if off_hosts:
        print("\n  OFF-SITE HOSTS (where this page sends you):")
        for host, count in sorted(off_hosts.items(), key=lambda kv: -kv[1]):
            flag = ""
            if any(known in host for known in _KNOWN_UNREACHABLE):
                flag = "   <-- unreachable from this machine"
            print(f"    {count:>4}  {host}{flag}")

    return True


def main(argv: list[str]) -> int:
    """Entry point. Returns a process exit code."""
    show_all = "--all" in argv
    targets = [a for a in argv if not a.startswith("--")]
    unknown = {a for a in argv if a.startswith("--")} - {"--all"}
    if unknown or not targets:
        print(f"unknown flag(s): {sorted(unknown)}" if unknown else "no URL given")
        print(__doc__)
        return 2

    # Reuse the D32 repair path rather than adding another one. It tries the OS
    # resolver first and consults DoH only on gaierror, so on a healthy machine
    # it is inert; the 2026-08-20 19:36 run confirmed that. The resolver failure
    # it repairs was INTERMITTENT, which is exactly the case a repair path
    # handles and configuring a machine once cannot.
    doh_resolver.install()

    # Every target is probed before the result is reduced. `all(generator)`
    # short-circuits, so one unreachable first URL would have silently skipped
    # the rest -- and a run that quietly probed 1 of 3 pages reads exactly like a
    # run that probed all 3 and found nothing.
    results = [probe(_norm(target), show_all) for target in targets]
    ok = all(results)

    repaired = doh_resolver.repaired_hosts()
    print("\n" + "=" * 100)
    if repaired:
        print(f"  DNS via DoH : {', '.join(repaired)} (OS resolver FAILED for these)")
    else:
        print("  DNS via DoH : not needed (OS resolver answered)")

    # Report, and name the next command. Do not conclude: this tool read
    # server-rendered anchors, so an empty BULK DATA bucket means no such anchor
    # was PUBLISHED on this page -- not that no download exists. A link built by
    # JavaScript, a file behind the search form, or a download on a page not
    # probed would all look identical from here.
    print(
        "  Empty BULK DATA bucket means no <a href> on THIS page named a data\n"
        "  file. It is not evidence that no bulk download exists: a JS-built\n"
        "  link, a file behind a form POST, or a download one page deeper all\n"
        "  look the same from here. Next, probe the pages the STRONG SIGNAL and\n"
        "  FORMS sections point at, and pass --all to see the full link list."
    )
    print("=" * 100)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(1) from None

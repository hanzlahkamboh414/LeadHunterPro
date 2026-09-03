"""Inc11 Step B-2b — email data-availability probe.

The Step B-3 live run still showed ``emails=[]`` on every gate-accepted
company, even though the parser's ``mailto:`` binding is unit-proven. Before
writing more code, we need to KNOW whether real Texas roofing sites publish any
email the parser can reach, or whether person-bound email is genuinely absent
from the website channel.

This probe fetches every :class:`LeadershipDiscovery` candidate page of one
site and reports, WITHOUT any role-region filter:

- HTTP status per page,
- how many ``mailto:`` links the page has,
- every email found anywhere in the raw HTML (text + hrefs), with its tier,
- how many of those emails the CURRENT parser actually binds (the gap).

Read-only diagnostic — no production code touched, no secrets written. Run
from ``backend/``:

    python probe_page_emails.py arringtonroofing.com
    python probe_page_emails.py alpineroofingconstruction.com
"""

from __future__ import annotations

import argparse
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from app.discovery.leadership_discovery import LeadershipDiscovery
from app.discovery.people_parser import PeopleParser
from app.email.email_cleaner import EMAIL_CLEAN_PATTERN
from app.engines.lead.lead_models import is_generic_email_local_part

_UA = {"User-Agent": "Mozilla/5.0"}


def _tier(email: str) -> str:
    """person_bound vs format — the two tiers a page can reach."""
    local = email.split("@", 1)[0]
    if is_generic_email_local_part(local):
        return "format"
    return "person_bound"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="email availability probe")
    parser.add_argument("website", help="site root, e.g. arringtonroofing.com")
    args = parser.parse_args(argv)

    base = args.website if args.website.startswith("http") else f"https://{args.website}"
    base = base.rstrip("/")

    print(f"Probing email availability on {base}")
    print("=" * 70)

    people_parser = PeopleParser()
    site_present: set[str] = set()
    site_bound: set[str] = set()

    for page in LeadershipDiscovery.CANDIDATE_PAGES:
        url = urljoin(base, page)
        try:
            resp = requests.get(url, timeout=10, headers=_UA, allow_redirects=True)
        except requests.RequestException as exc:
            print(f"  {url}  ERROR {type(exc).__name__}")
            continue
        if resp.status_code != 200:
            print(f"  {url}  HTTP {resp.status_code}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        mailtos = [
            a.get("href", "").strip()
            for a in soup.find_all("a", href=True)
            if a.get("href", "").strip().lower().startswith("mailto:")
        ]
        raw = soup.get_text(" ", strip=True) + " " + " ".join(mailtos)
        page_emails = sorted({e.lower() for e in EMAIL_CLEAN_PATTERN.findall(raw)})

        records = people_parser.extract_candidates(soup, page_url=url)
        bound = {e.email for r in records for e in r.emails}

        site_present.update(page_emails)
        site_bound.update(bound)

        print(f"\n  {url}  HTTP 200  mailto={len(mailtos)}  regions={len(records)}")
        if page_emails:
            for email in page_emails:
                mark = "  [parser-bound]" if email in bound else ""
                print(f"      present  {email:40s} tier={_tier(email):6s}{mark}")
        else:
            print("      NO email found anywhere on this page")

    print("\n" + "=" * 70)
    print(
        f"Emails present on site: {len(site_present)}  "
        f"parser-bound: {len(site_bound)}"
    )
    print(f"Unreachable (present but never bound): {sorted(site_present - site_bound)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
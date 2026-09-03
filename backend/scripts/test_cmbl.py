"""POC: Scrape Texas CMBL (Centralized Master Bidders List) for construction companies.

This script is a PROOF OF CONCEPT ONLY. It does NOT integrate with any part
of the LeadHunter Pro application. It exists solely to verify that the
public Texas CMBL website can be scraped to extract real construction
company contact information without any API key or authentication.

Website: https://mycpa.cpa.state.tx.us/tpasscmblsearch/index.jsp

Usage:
    python scripts/test_cmbl.py [trade_code] [county] [limit]

Examples:
    python scripts/test_cmbl.py           # Default: code 874 (roofing), DALLAS county, 10 results
    python scripts/test_cmbl.py 812       # Concrete work
    python scripts/test_cmbl.py 83010     # Plumbing/HVAC
    python scripts/test_cmbl.py 861 HOUSTON 20  # General contracting in Houston

Exit codes: 0 when at least one vendor record was parsed, 1 when the search
returned no vendor links (a FAILED probe -- read the response dump it prints),
2 on an unexpected exception.

This script has no recorded live result, so treat it as unproven until one run
is pasted back. It also prints every label the registry publishes on the first
record (see :func:`dump_labels`), because whether CMBL names a contact PERSON --
not just a company, phone and email -- is the question that decides how much of
roadmap D25 this source can answer.

DNS: the 2026-08-20 attempt died at ``[Errno 11002] getaddrinfo failed`` without
sending a request, because this machine's resolver will not answer for the
Texas host even though a public resolver returns an address for it in seconds
(roadmap D32). ``scripts/doh_resolver.py`` is installed at the top of
:func:`main` to repair exactly that: the OS resolver is still tried first and
DNS-over-HTTPS is consulted only after it raises, so on a healthy network this
changes nothing. Whether the fallback was actually used is printed in the
summary -- a repair that hides itself would let a broken resolver ship.
"""

from __future__ import annotations

import pathlib
import re
import sys
import time
import urllib.parse
from datetime import datetime
from html.parser import HTMLParser

# Make the sibling `doh_resolver` importable however this script is invoked.
# `python scripts/test_cmbl.py` already puts scripts/ on sys.path, but
# `python -m scripts.test_cmbl` and an absolute-path invocation do not, and the
# failure mode would be a ModuleNotFoundError that looks unrelated to CMBL.
# E402 is a permanent ignore in backend/pyproject.toml precisely so a scripts/
# entrypoint may adjust the path before importing, so the order below is
# deliberate rather than an oversight.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import doh_resolver

BASE_URL = "https://mycpa.cpa.state.tx.us"
SEARCH_URL = f"{BASE_URL}/tpasscmblsearch/index.jsp"
VIEW_VENDOR_URL = f"{BASE_URL}/tpasscmblsearch/viewVendor.do"

# NIGP codes mapped to construction trades
TRADE_CODES = {
    "roofing": "874",        # Roofing/Shingle work
    "concrete": "812",       # Concrete work
    "electrical": "820",     # Electrical work
    "plumbing": "83010",    # Plumbing/Heating/AC
    "hvac": "83010",         # Same as plumbing
    "general_contractor": "861",  # Construction services NEC
    "874": "roofing",
    "812": "concrete",
    "820": "electrical",
    "83010": "plumbing",
    "861": "general_contractor",
}


class SimpleHTMLParser(HTMLParser):
    """Lightweight HTML parser — no external dependencies."""

    def __init__(self):
        super().__init__()
        self._in_tag = None
        self._tag_depth = 0
        self._text_parts = []
        self._collecting = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        self._in_tag = tag
        self._tag_depth += 1
        if tag == "a":
            href = attrs_dict.get("href", "")
            if href and "viewVendor.do" in href:
                # Absolute URL
                if href.startswith("/"):
                    href = BASE_URL + href
                print(f"  → Vendor link: {href}")

    def handle_endtag(self, tag):
        if tag == self._in_tag:
            self._tag_depth -= 1
            if self._tag_depth == 0:
                self._in_tag = None

    def handle_data(self, data):
        if self._in_tag in ("td", "th", "span", "div", "p", "li", "a", "font", "b", "strong"):
            text = data.strip()
            if text:
                self._text_parts.append(text)


def fetch_page(url: str, method: str = "GET", post_data: dict | None = None, timeout: int = 30) -> tuple[int, str, str]:
    """Fetch a URL and return (status_code, final_url, html)."""
    import http.client
    from urllib.parse import urlparse, urlencode

    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path
    if parsed.query:
        path += "?" + parsed.query

    headers = {
        "Host": host,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "identity",
        "Connection": "close",
        "Referer": SEARCH_URL,
    }

    conn = None
    try:
        if parsed.scheme == "https":
            import ssl
            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)

        if method == "POST" and post_data:
            body = urlencode(post_data)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Content-Length"] = str(len(body))
            conn.request("POST", path, body=body, headers=headers)
        else:
            conn.request("GET", path, headers=headers)

        resp = conn.getresponse()
        status = resp.status
        location = resp.getheader("Location", "")

        # Handle redirects
        final_url = url
        if status in (301, 302, 303, 307, 308) and location:
            if location.startswith("/"):
                final_url = f"{parsed.scheme}://{host}{location}"
            else:
                final_url = location
            # Follow redirect with GET
            conn.close()
            conn = None
            return fetch_page(final_url, method="GET", timeout=timeout)

        html = resp.read().decode("utf-8", errors="replace")
        return status, final_url, html

    except Exception as exc:
        raise RuntimeError(f"HTTP request failed: {exc}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def parse_vendor_table(html: str) -> list[dict]:
    """Parse the vendor listing table from CMBL search results.

    The search results page uses an HTML table where each row contains
    vendor info with links to individual vendor pages.

    Returns list of dicts with at minimum 'name' and 'detail_url'.
    """
    vendors = []
    lines = html.split("\n")

    # Strategy: find rows containing viewVendor.do links
    i = 0
    while i < len(lines):
        line = lines[i]
        # Look for anchor tags with viewVendor.do
        match = re.search(r'href=["\']([^"\']*viewVendor\.do[^"\']*)["\']', line)
        if match:
            href = match.group(1)
            if href.startswith("/"):
                href = BASE_URL + href

            # Extract company name from same row or nearby cells
            # The table structure typically has: <td>link</td><td>name</td><td>city</td>...
            # Look backwards in the same line for text before the link
            before_link = line[: match.start()]
            after_link = line[match.end():]

            # Try to get text content from surrounding lines (table rows)
            row_text = ""
            for j in range(max(0, i - 2), min(len(lines), i + 5)):
                row_text += lines[j] + " "

            # Extract all text between tags
            text_only = re.sub(r"<[^>]+>", " ", row_text)
            text_only = re.sub(r"\s+", " ", text_only).strip()

            # Clean up the URL
            href = href.replace("&amp;", "&").replace("&#39;", "'")

            # Try to pull out the company name — usually the first meaningful text
            # after the link or before it in the same cell
            vendor_name = ""
            # Look for text patterns that look like company names
            name_match = re.search(
                r'(?:>|&gt;)\s*([A-Z][A-Za-z\s&\'\-]{3,50}?)\s*(?=<|&lt;|$)',
                line
            )
            if name_match:
                candidate = name_match.group(1).strip()
                if candidate and len(candidate) > 3:
                    vendor_name = candidate

            # Fallback: use text from adjacent cells
            if not vendor_name:
                # Find text in td elements near the link
                td_matches = re.findall(r'<td[^>]*>([^<]*)</td>', line)
                for td in td_matches:
                    td = td.strip()
                    if td and len(td) > 3 and "viewVendor" not in td.lower():
                        vendor_name = td
                        break

            if not vendor_name:
                # Broader search in the row
                td_matches = re.findall(r'<td[^>]*>([^<]*)</td>', row_text)
                for td in td_matches:
                    td = td.strip()
                    if td and len(td) > 3 and "viewVendor" not in td.lower():
                        vendor_name = td
                        break

            if vendor_name:
                vendors.append({
                    "name": vendor_name,
                    "detail_url": href,
                })
        i += 1

    return vendors


def parse_vendor_detail(html: str) -> dict:
    """Parse a single vendor detail page.

    Expected fields:
    - company_name
    - address, city, state, zip
    - phone, fax
    - email
    - website
    - category_code, category_description
    - vendor_id
    - status
    """
    info: dict = {}

    # Extract all text content with tag context
    # Pattern: label-value pairs in tables
    lines = html.split("\n")

    current_label = None
    for line in lines:
        line_stripped = line.strip()

        # Look for label patterns like "<b>Company Name:</b>" or "<td>Company Name:</td>"
        label_match = re.search(
            r'<(?:b|td|th)[^>]*>\s*([A-Za-z\s&\-]+?):\s*</(?:b|td|th)>',
            line,
            re.IGNORECASE
        )
        if label_match:
            current_label = label_match.group(1).strip().lower()

        # Look for value after label — could be in <td>, <b>, or plain text
        value_match = re.search(
            r'</(?:b|td|th)>\s*(<[^>]+>|[^<]*)',
            line,
            re.IGNORECASE
        )
        if value_match and current_label:
            raw_value = value_match.group(1).strip()
            # Strip HTML tags from value
            clean_value = re.sub(r"<[^>]+>", "", raw_value).strip()
            # Decode HTML entities
            clean_value = (
                clean_value.replace("&amp;", "&")
                .replace("&#39;", "'")
                .replace("&apos;", "'")
                .replace("&quot;", '"')
                .replace("&lt;", "<")
                .replace("&gt;", ">")
            )

            # Map labels to fields
            if "company" in current_label and "name" in current_label:
                info["company_name"] = clean_value
            elif current_label == "address":
                info["address"] = clean_value
            elif "city" in current_label:
                info["city"] = clean_value
            elif "state" in current_label and "code" not in current_label:
                info["state"] = clean_value
            elif "zip" in current_label:
                info["zip"] = clean_value
            elif "phone" in current_label and "fax" not in current_label:
                info["phone"] = clean_value
            elif "fax" in current_label:
                info["fax"] = clean_value
            elif "email" in current_label:
                info["email"] = clean_value
            elif "website" in current_label or "url" in current_label:
                info["website"] = clean_value
            elif "category" in current_label:
                info["category_description"] = clean_value
            elif "code" in current_label and "category" in current_label:
                info["category_code"] = clean_value
            elif "vendor" in current_label and "id" in current_label:
                info["vendor_id"] = clean_value
            elif "status" in current_label:
                info["status"] = clean_value
            else:
                # Anything this mapping does not recognise is KEPT, not dropped.
                # The chain above is a GUESS at CMBL's field list (note the
                # docstring says "Expected fields"), and a guess with no else
                # branch cannot report what it failed to anticipate: a
                # `Contact Name:` or `Principal:` label would set current_label,
                # fall through every test, and vanish without trace. The run
                # would then look like proof that CMBL names no person, when in
                # fact nothing ever looked for one. Roadmap D31 is this same
                # defect in the D25 diagnostic, and D25's whole conclusion was
                # that a person's NAME is the one thing contractor websites do
                # not publish -- so silently discarding one here would be the
                # most expensive bug this file could contain.
                info.setdefault("_unmapped", {})[current_label] = clean_value

        # Handle multi-line label/value patterns
        if current_label and not value_match:
            # Check if next non-empty line has the value
            pass

    # Additional extraction: find emails anywhere in the page
    if "email" not in info:
        emails = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", html)
        if emails:
            info["email"] = emails[0]

    # Find phones
    if "phone" not in info:
        phones = re.findall(
            r"(?:\+?1[-.\s]?)?\(?[0-9]{3}\)?[-.\s]?[0-9]{3}[-.\s]?[0-9]{4}",
            html,
        )
        if phones:
            info["phone"] = phones[0]

    # Find website URLs
    if "website" not in info:
        urls = re.findall(
            r'https?://[a-zA-Z0-9\-._~:/?#\[\]@!$&\'()*+,;=%]+',
            html,
        )
        # Filter to likely company websites (not state tx.gov URLs)
        company_urls = [u for u in urls if ".com" in u or ".org" in u or ".net" in u]
        if company_urls:
            info["website"] = company_urls[0]

    return info


def dump_labels(html: str) -> list[str]:
    """Every label CMBL actually prints on a vendor record, in page order.

    This exists to answer ONE question with evidence rather than assumption:
    does a CMBL vendor record name a PERSON, or only a company plus phone and
    email? Roadmap D25 established that contractor websites do not publish
    staff names, which is why the sourcing strategy now turns on registries --
    so whether this registry carries a contact person decides how much of the
    decision-maker problem CMBL can solve at all.

    ``parse_vendor_detail`` cannot answer it, by construction: it only ever
    looks for labels somebody already expected. This function looks for labels
    of ANY name, unfiltered, deduplicated in page order, so a field nobody
    predicted still appears in the output.
    """
    labels: list[str] = []
    pattern = r"<(?:b|td|th)[^>]*>\s*([A-Za-z][A-Za-z\s&/'\-]{1,40}?)\s*:\s*</"
    for match in re.finditer(pattern, html, re.IGNORECASE):
        label = re.sub(r"\s+", " ", match.group(1)).strip()
        if label not in labels:
            labels.append(label)
    return labels


def main() -> int:
    """Run the CMBL scraping POC."""
    start_time = time.monotonic()

    # Repair failed hostname lookups over DNS-over-HTTPS before the first
    # request. This is ON by default and needs no flag, because it is a REPAIR
    # PATH, not a replacement: the OS resolver is tried first every time, and
    # DoH is consulted only after `socket.gaierror`. On a machine whose DNS
    # works, nothing here changes -- same addresses, same latency. On this one
    # it is the difference between a probe and `[Errno 11002] getaddrinfo
    # failed`, which is where the 2026-08-20 run died before sending a byte.
    #
    # Whether it was actually needed is REPORTED at the end of the run. A
    # fallback that hides itself is the mistake CLAUDE.md 1 exists to prevent.
    doh_resolver.install()

    # Parse arguments
    args = sys.argv[1:]
    if len(args) >= 1:
        raw_code = args[0].upper()
    else:
        raw_code = "874"  # Roofing default

    # Resolve trade code
    trade_name = TRADE_CODES.get(raw_code, raw_code)
    trade_code = raw_code if raw_code in TRADE_CODES.values() else raw_code
    county = args[1].upper() if len(args) >= 2 else "DALLAS"
    limit = int(args[2]) if len(args) >= 3 else 10

    print("=" * 70)
    print("TEXAS CMBL SCRAPING POC")
    print(f"  Trade code : {trade_code} ({trade_name})")
    print(f"  County     : {county}")
    print(f"  Max results: {limit}")
    print(f"  Timestamp  : {datetime.now().isoformat()}")
    print("=" * 70)

    # Step 1: Submit search form
    print("\n[1/4] Submitting search form...")
    try:
        status, final_url, html = fetch_page(
            SEARCH_URL,
            method="POST",
            post_data={
                "vendorCategoryCode": trade_code,
                "vendorStatus": "",
                "countyName": county,
            },
            timeout=30,
        )
        print(f"  Status      : {status}")
        print(f"  Final URL   : {final_url}")
        print(f"  Response    : {len(html):,} chars")
    except Exception as exc:
        print(f"  ERROR: {exc}")
        # Say WHICH layer failed. The 2026-08-20 run printed only
        # "[Errno 11002] getaddrinfo failed" here, and that message names
        # neither DNS nor a remedy -- so the next hour went into the form, the
        # headers and the session, none of which had been reached, because the
        # request was never sent. `getaddrinfo` failing after DoH was installed
        # is a genuinely different finding from the search form rejecting us,
        # and the two must not print the same line.
        if isinstance(exc, OSError) and "getaddrinfo" in str(exc):
            host = urllib.parse.urlparse(BASE_URL).hostname or BASE_URL
            print(
                "\n  This is a NAME RESOLUTION failure, not an HTTP one: no\n"
                "  request left this machine, so the form fields, the missing\n"
                "  cookie jar and the table parser below are all still\n"
                "  UNTESTED. DNS-over-HTTPS was already installed and also\n"
                f"  could not resolve {host}.\n"
                "\n  Settle which it is before touching this script:\n"
                f"      python scripts/doh_resolver.py {host}\n"
                f"      nslookup {host} 8.8.8.8\n"
                "  Resolves there but not here -> a network/deployment issue.\n"
                "  Fails there too -> the host is gone; find the successor\n"
                "  portal, and note that roadmap D32 lists two Texas sources\n"
                "  that DID return HTTP 200 on the same day."
            )
        elif isinstance(exc, OSError) and any(
            token in str(exc).lower() for token in ("10060", "timed out", "timeout")
        ):
            # A CONNECT timeout, which is a third thing again: the name
            # resolved, a SYN was sent, and nothing came back. Distinguishing
            # this from the DNS case above matters because the remedies share
            # nothing -- and neither one is anywhere in this file, so a reader
            # who starts editing the form fields here has already lost.
            #
            # It is also the one failure this script must NOT interpret. A
            # connect timeout is symmetric: "my route drops packets to that
            # netblock" and "their firewall drops packets from my region"
            # produce byte-identical evidence, and no amount of retrying from
            # the same machine separates them.
            host = urllib.parse.urlparse(BASE_URL).hostname or BASE_URL
            print(
                "\n  This is a CONNECT TIMEOUT, not DNS and not HTTP: the name\n"
                f"  resolved and {host} accepted no connection on port 443.\n"
                "  So the form fields, the cookie jar and the table parser\n"
                "  below are STILL untested -- do not edit them on this signal.\n"
                "\n  What is known as of 2026-08-20 (roadmap D32): every host in\n"
                "  this project's probe set that sits behind a CDN answered 200,\n"
                "  and every host on a State-of-Texas origin IP timed out --\n"
                "  including comptroller.texas.gov succeeding while this host,\n"
                "  which is a Comptroller application, did not. That is a\n"
                "  property of the PATH, not proof the service is down.\n"
                "\n  Next, in this order:\n"
                "      python scripts/network_diagnostics.py\n"
                "  Every origin host timing out while CDN hosts return 200 means\n"
                "  no code change in this repo reaches it; that is a deployment\n"
                "  decision. Verify from a different network before concluding\n"
                "  anything about the registry itself."
            )
        return 1

    # Step 2: Parse vendor links from results page
    print("\n[2/4] Parsing vendor listing page...")
    vendors = parse_vendor_table(html)
    print(f"  Found {len(vendors)} vendor(s) on listing page")

    if not vendors:
        print("\n  No vendor links found. Dumping first 500 chars of response:")
        print("  " + "=" * 66)
        print("  " + html[:500].replace("\n", "\n  "))
        print("  " + "=" * 66)
        # Exit NON-ZERO: zero vendors is a failed probe, not a successful run
        # that happened to find nothing. The distinction is load-bearing here
        # because the POST above is a guess at the form's field names
        # (`vendorCategoryCode`, `countyName`) sent with no cookie jar at all --
        # and a JSP search form that wants a session answers by re-serving the
        # search page, which parses to zero vendors. Under the old `return 0`
        # that total failure reported success. The dump above is what tells the
        # two apart, so read it before changing any code.
        return 1

    # Step 3: Visit each vendor detail page
    print(f"\n[3/4] Fetching vendor detail pages (up to {limit})...")
    companies = []
    failures = []

    for idx, vendor in enumerate(vendors[:limit]):
        url = vendor["detail_url"]
        try:
            vstatus, vurl, vhtml = fetch_page(url, timeout=15)
            if vstatus != 200:
                failures.append((vendor["name"], f"HTTP {vstatus}"))
                continue

            info = parse_vendor_detail(vhtml)
            if idx == 0:
                # First record only: the field list is a property of the
                # REGISTRY, not of this one vendor, so a single dump settles it.
                print("      --- labels CMBL prints on this record ---")
                for label in dump_labels(vhtml):
                    print(f"      | {label}")
                unmapped = sorted(info.get("_unmapped", {}))
                print(f"      --- not mapped by this parser: {unmapped or '(none)'}")
            info["source_url"] = url
            info["trade_category"] = trade_name
            info["source"] = "texas_cmbl"

            companies.append(info)
            print(f"  [{idx+1}] {vendor['name']}")
            if info.get("phone"):
                print(f"      Phone : {info['phone']}")
            if info.get("email"):
                print(f"      Email : {info['email']}")
            if info.get("website"):
                print(f"      Web   : {info['website']}")
            if info.get("city"):
                print(f"      City  : {info['city']}, {info.get('state','')} {info.get('zip','')}")

        except Exception as exc:
            failures.append((vendor["name"], str(exc)))
            print(f"  [{idx+1}] {vendor['name']} — ERROR: {exc}")

    # Step 4: Print summary
    elapsed = time.monotonic() - start_time
    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)
    print(f"  Companies extracted : {len(companies)}")
    print(f"  Fetch failures      : {len(failures)}")
    if failures:
        for name, err in failures[:5]:
            print(f"    - {name}: {err}")
    print(f"  Total execution time: {elapsed:.2f}s")

    # Was the DoH fallback load-bearing? Print it either way. "It worked" and
    # "it worked because a fallback carried it" are different facts, and only
    # the second one means the host resolver still needs a decision (D32).
    repaired = doh_resolver.repaired_hosts()
    if repaired:
        print(f"  DNS via DoH         : {', '.join(repaired)}")
        print("    ^ the OS resolver FAILED on the above and dns.google")
        print("      answered instead. This run is real, but it did not use")
        print("      this machine's DNS -- see roadmap D32 before deploying.")
    else:
        print("  DNS via DoH         : not needed (OS resolver answered)")

    # The question that decides how much of D25 this source can answer is
    # whether CMBL names a PERSON, and `parse_vendor_detail` maps no such field,
    # so it cannot report one directly. What it CAN do is surface every label
    # the registry printed that this parser did not anticipate: a `Contact
    # Name:` or `Principal:` would appear here. Silence here plus silence in the
    # first-record label dump above is the real answer to Bar 2 -- company,
    # phone and email only.
    unmapped_labels = sorted(
        {label for comp in companies for label in comp.get("_unmapped", {})}
    )
    print(f"  Labels not mapped   : {unmapped_labels or '(none)'}")
    person_like = [
        label
        for label in unmapped_labels
        if any(word in label.lower() for word in ("contact", "name", "principal", "owner", "officer"))
    ]
    if person_like:
        print(f"    ^ POSSIBLE PERSON FIELD(S): {person_like}")
        print("      Read the values in the dump above before believing this;")
        print("      `Company Name` matches the same test and is not a person.")
    print("=" * 70)

    # Print full company details
    if companies:
        print(f"\nFirst {min(len(companies), 10)} companies:")
        for idx, comp in enumerate(companies[:10], 1):
            print(f"\n  {idx}. {comp.get('company_name', 'N/A')}")
            print(f"     City    : {comp.get('city', 'N/A')}")
            print(f"     State   : {comp.get('state', 'N/A')}")
            print(f"     Zip     : {comp.get('zip', 'N/A')}")
            print(f"     Website : {comp.get('website', 'N/A')}")
            print(f"     Phone   : {comp.get('phone', 'N/A')}")
            print(f"     Email   : {comp.get('email', 'N/A')}")
            print(f"     Category: {comp.get('category_description', comp.get('category_code', 'N/A'))}")
            print(f"     Source  : {comp.get('source_url', 'N/A')}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
    except Exception as exc:
        print(f"\nUNEXPECTED ERROR: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(2)

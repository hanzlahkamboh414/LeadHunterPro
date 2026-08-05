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
"""

from __future__ import annotations

import re
import sys
import time
import urllib.parse
from datetime import datetime
from html.parser import HTMLParser

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


def main() -> int:
    """Run the CMBL scraping POC."""
    start_time = time.monotonic()

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
        return 0

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

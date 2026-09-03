"""Network diagnostics for Texas government web sources.

This is a STANDALONE diagnostic script — it does NOT import any
LeadHunterPro modules. It uses only the Python standard library
and the `requests` package.

Purpose:
  Determine whether the current execution environment can reach
  US state government DNS/HTTP endpoints, or whether failures are
  caused by sandbox/network restrictions.

Usage:
    python scripts/network_diagnostics.py

    # Or probe any host/URL without editing this file. Bare hostnames get
    # https:// prepended; full URLs are used exactly as given, so a specific
    # path can be tested rather than only a site root:
    python scripts/network_diagnostics.py comptroller.texas.gov/purchasing/
    python scripts/network_diagnostics.py https://example.gov/a https://example.gov/b

Taking targets on the command line is not a convenience. This is a PERMANENT
diagnostic, and the version that hardcoded its target list could not answer a
new reachability question without a source edit -- which means every such
question became a code change, a review and a commit. That is the failure mode
this project has explicitly ruled out: the operator should not have to open the
backend to ask whether a host answers.
"""

from __future__ import annotations

import platform
import socket
import sys
import time
from typing import Any
from urllib.parse import urlparse

# Domains to probe
#
# The last two were added on 2026-08-20, when `scripts/test_cmbl.py` died with
# `[Errno 11002] getaddrinfo failed` on `mycpa.cpa.state.tx.us`. They exist to
# separate two explanations that look identical from inside one failed script,
# and which imply COMPLETELY different work:
#
#   * `www.tdlr.texas.gov` — the other registry the roadmap is considering. If
#     both Texas hosts fail together, the problem is not "CMBL was retired".
#   * `sam.gov` — a FEDERAL .gov. If Texas .gov fails while this one resolves,
#     the "our DNS filters .gov" theory is dead and the Texas host really is
#     gone. If every .gov fails while google.com resolves, the blocker is this
#     MACHINE's resolver, no source is reachable from here, and the fix is a
#     deployment decision (run discovery from a US host) rather than a source
#     decision. One run tells the two apart; guessing between them would be
#     exactly the CLAUDE.md §7 mistake.
DOMAINS = [
    "google.com",                              # Internet baseline
    "mycpa.cpa.state.tx.us",                  # CMBL (Centralized Master Bidders List)
    "comptroller.texas.gov",                  # Texas Comptroller homepage
    "txsmartbuy.gov",                         # Electronic State Business Daily
    "texas.gov",                              # State portal
    "www.tdlr.texas.gov",                     # TDLR licensed-contractor registry
    "sam.gov",                                # Federal baseline (see note above)
]

#: The URL probed for each default domain. Kept beside DOMAINS so the two lists
#: cannot drift apart unnoticed, and overridden entirely when targets are passed
#: on the command line.
DEFAULT_PROBES: list[tuple[str, str]] = [
    ("google.com", "https://www.google.com"),
    ("mycpa.cpa.state.tx.us", "https://mycpa.cpa.state.tx.us/tpasscmblsearch/index.jsp"),
    ("comptroller.texas.gov", "https://comptroller.texas.gov"),
    ("txsmartbuy.gov", "https://www.txsmartbuy.gov/esbd"),
    ("texas.gov", "https://www.texas.gov"),
    ("www.tdlr.texas.gov", "https://www.tdlr.texas.gov/"),
    ("sam.gov", "https://sam.gov"),
]


def _targets(argv: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Resolve command-line arguments into (domains to resolve, URLs to fetch).

    A bare hostname becomes an ``https://`` URL. A full URL is passed through
    untouched, so a specific PATH can be probed -- which matters, because a site
    root returning 200 says nothing about whether the page holding the data
    exists. Several URLs may share a host; that host is resolved once and
    fetched once per URL.
    """
    if not argv:
        return DOMAINS, DEFAULT_PROBES

    domains: list[str] = []
    probes: list[tuple[str, str]] = []
    for target in argv:
        url = target if target.startswith(("http://", "https://")) else f"https://{target}"
        host = urlparse(url).hostname or target
        if host not in domains:
            domains.append(host)
        probes.append((host, url))
    return domains, probes


def print_separator(title: str) -> None:
    print(f"\n{'='*70}")
    print(f" {title}")
    print("=" * 70)


def get_dns_info() -> dict[str, Any]:
    """Return local DNS configuration."""
    info: dict[str, Any] = {}

    # Platform-specific DNS config
    system = platform.system()
    if system == "Windows":
        import ctypes
        # Get DNS servers from Windows networking config
        try:
            adptrs = ctypes.windll.dnsapi.DnsQuery_A(
                "localhost", 1, 0x00000010, None, None  # A record, DO_NOT_ADD_TO_CACHE
            )
            if adptrs:
                info["dns_servers_windows"] = "queried via DnsQuery"
        except Exception:
            pass
    elif system == "Linux":
        try:
            with open("/etc/resolv.conf", "r") as f:
                info["resolv_conf"] = f.read().strip()
        except OSError:
            pass

    # Use socket to resolve localhost to find default resolver
    try:
        localhost_ip = socket.gethostbyname("localhost")
        info["localhost_ip"] = localhost_ip
    except Exception as e:
        info["localhost_ip_error"] = str(e)

    # System info
    info["python_version"] = sys.version
    info["platform"] = platform.platform()
    info["machine"] = platform.machine()
    info["node"] = platform.node()

    return info


def dns_resolve(domain: str) -> dict[str, Any]:
    """Perform DNS resolution and return results."""
    result: dict[str, Any] = {"domain": domain, "status": "unknown", "records": []}
    try:
        # socket.getaddrinfo returns all resolved addresses
        infos = socket.getaddrinfo(domain, None, socket.AF_INET, socket.SOCK_STREAM)
        ips = sorted(set(info[4][0] for info in infos))
        result["status"] = "OK"
        result["ips"] = ips
        result["record_count"] = len(infos)
        # Also try to get canonical name via gethostbyaddr on first IP
        if ips:
            try:
                hostname = socket.gethostbyaddr(ips[0])[0]
                result["canonical_name"] = hostname
            except socket.herror:
                pass
    except socket.gaierror as exc:
        result["status"] = "DNS_FAIL"
        result["exception"] = f"socket.gaierror: {exc}"
    except Exception as exc:
        result["status"] = "ERROR"
        result["exception"] = f"{type(exc).__name__}: {exc}"
    return result


def http_get(url: str, timeout: int = 15) -> dict[str, Any]:
    """Perform an HTTP GET and return detailed results."""
    result: dict[str, Any] = {"url": url, "status": "unknown"}
    start = time.monotonic()

    try:
        import requests

        resp = requests.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
        )

        elapsed = time.monotonic() - start
        result["status"] = f"HTTP_{resp.status_code}"
        result["elapsed_ms"] = round(elapsed * 1000, 1)
        result["final_url"] = resp.url
        result["response_size_bytes"] = len(resp.content)
        result["content_type"] = resp.headers.get("Content-Type", "")
        result["server_header"] = resp.headers.get("Server", "")
        result["is_https"] = resp.url.startswith("https://")

        # Redirect chain
        if resp.history:
            redirects = []
            for r in resp.history:
                redirects.append({
                    "status": r.status_code,
                    "url": r.url,
                    "final_url": r.headers.get("Location", ""),
                })
            result["redirect_chain"] = redirects
        else:
            result["redirect_chain"] = []

        # Key headers
        selective_headers: dict[str, str] = {}
        for key in ("Content-Type", "Content-Length", "Server", "Cache-Control",
                     "X-Frame-Options", "Strict-Transport-Security", "Set-Cookie"):
            val = resp.headers.get(key)
            if val:
                selective_headers[key] = val
        result["headers"] = selective_headers

        # First 500 chars of body (for content inspection)
        try:
            text_preview = resp.text[:500]
            result["body_preview"] = text_preview
        except Exception:
            result["body_preview"] = "<binary response>"

    except requests.exceptions.ConnectionError as exc:
        elapsed = time.monotonic() - start
        result["status"] = "CONNECTION_ERROR"
        result["elapsed_ms"] = round(elapsed * 1000, 1)
        result["exception"] = f"requests.ConnectionError: {exc}"
    except requests.exceptions.Timeout as exc:
        elapsed = time.monotonic() - start
        result["status"] = "TIMEOUT"
        result["elapsed_ms"] = round(elapsed * 1000, 1)
        result["exception"] = f"requests.Timeout: {exc}"
    except requests.exceptions.HTTPError as exc:
        elapsed = time.monotonic() - start
        result["status"] = "HTTP_ERROR"
        result["elapsed_ms"] = round(elapsed * 1000, 1)
        result["exception"] = f"requests.HTTPError: {exc}"
    except Exception as exc:
        elapsed = time.monotonic() - start
        result["status"] = "UNEXPECTED_ERROR"
        result["elapsed_ms"] = round(elapsed * 1000, 1)
        result["exception"] = f"{type(exc).__name__}: {exc}"

    return result


def main(argv: list[str] | None = None) -> int:
    overall_start = time.monotonic()
    domains, probes = _targets(argv or [])

    print_separator("NETWORK DIAGNOSTICS")
    print(f"Timestamp : {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"Python    : {sys.version.split()[0]}")
    print(f"Platform  : {platform.platform()}")
    print(f"Machine   : {platform.machine()}")
    print(f"Hostname  : {platform.node()}")
    print(f"Targets   : {'command line' if argv else 'built-in default set'}")

    # --- Section 1: Local DNS Configuration ---
    print_separator("1. LOCAL DNS CONFIGURATION")
    dns_info = get_dns_info()
    for key, value in dns_info.items():
        if key not in ("python_version", "platform", "machine", "node"):
            print(f"  {key}: {value}")

    # Check if we can resolve localhost (baseline)
    print("\n  [baseline] Resolving 'localhost' ...")
    try:
        ip = socket.gethostbyname("localhost")
        print(f"         OK -> {ip}")
    except Exception as exc:
        print(f"         FAIL -> {exc}")

    # --- Section 2: DNS Resolution for Target Domains ---
    print_separator("2. DNS RESOLUTION")
    dns_results: dict[str, dict] = {}
    for domain in domains:
        print(f"\n  Resolving {domain} ...")
        result = dns_resolve(domain)
        dns_results[domain] = result
        if result["status"] == "OK":
            ips = ", ".join(result["ips"])
            cname = f" (CNAME: {result['canonical_name']})" if result.get("canonical_name") else ""
            print(f"         OK -> {len(result['ips'])} address(es): {ips}{cname}")
        else:
            exc = result.get("exception", "unknown error")
            print(f"         FAIL -> {exc}")

    # --- Section 3: HTTP GET Requests ---
    print_separator("3. HTTP GET REQUESTS")

    # Collected so Section 4 can summarise what the HTTP probes actually did.
    # An earlier version summarised DNS only, and on the 2026-08-20 19:40 run it
    # therefore printed "internet access appears normal" while two of the seven
    # hosts were timing out at TCP connect. See the note in that section.
    #
    # Keyed by URL, not by host: several targets may share a host (probing three
    # paths on one site is the normal case), and keying by host would silently
    # keep only the last of them -- losing exactly the comparison the run was
    # made for.
    http_results: dict[str, dict] = {}
    url_host: dict[str, str] = {}

    for label, url in probes:
        print(f"\n  GET {url}")
        print(f"         (DNS: {dns_results.get(label, {}).get('status', '?')})")
        http_result = http_get(url)
        http_results[url] = http_result
        url_host[url] = label
        print(f"         Status      : {http_result.get('status', '?')}")
        print(f"         Elapsed     : {http_result.get('elapsed_ms', '?')} ms")
        final_url = http_result.get("final_url", "")
        print(f"         Final URL   : {final_url}")
        size = http_result.get("response_size_bytes")
        if size is not None:
            print(f"         Body size   : {size:,} bytes")
        ct = http_result.get("content_type", "")
        if ct:
            print(f"         Content-type: {ct}")
        server = http_result.get("server_header", "")
        if server:
            print(f"         Server      : {server}")
        redirects = http_result.get("redirect_chain", [])
        if redirects:
            print(f"         Redirects   : {len(redirects)} hop(s)")
            for i, r in enumerate(redirects, 1):
                print(f"           [{i}] {r['status']} → {r['url']}")
        exc = http_result.get("exception", "")
        if exc:
            print(f"         Exception   : {exc}")
        preview = http_result.get("body_preview", "")
        if preview:
            # Show first line that looks meaningful
            first_lines = [l.strip() for l in preview.split("\n") if l.strip()][:5]
            if first_lines:
                print(f"         Preview     : {' | '.join(first_lines[:3])}")

    # --- Section 4: Summary ---
    elapsed_total = (time.monotonic() - overall_start) * 1000
    print_separator("4. SUMMARY")

    dns_ok = sum(1 for r in dns_results.values() if r["status"] == "OK")
    dns_fail = len(domains) - dns_ok
    http_ok = [u for u, r in http_results.items() if r.get("status", "").startswith("HTTP_2")]
    http_bad = [u for u in http_results if u not in http_ok]
    print(f"\n  DNS resolutions  : {dns_ok} OK / {dns_fail} FAILED (out of {len(domains)})")
    print(f"  HTTP responses   : {len(http_ok)} OK / {len(http_bad)} FAILED (out of {len(http_results)})")
    print(f"  Total time       : {elapsed_total:.1f} ms")
    print()

    # Section 4 used to count DNS ONLY, and then print a verdict about "internet
    # access" from that half of the run. On the 2026-08-20 19:40 run every domain
    # resolved, so it announced "internet access appears normal" while
    # mycpa.cpa.state.tx.us and www.tdlr.texas.gov were both timing out at TCP
    # connect -- the reader had to scroll back through Section 3 to find the two
    # failures the summary existed to surface. That is roadmap D31's defect shape
    # a fourth time, in a branch of this very file: the lines that REPORT were
    # right, the line that CONCLUDED was wrong, and wrong in the direction that
    # costs time. HTTP outcomes are now counted above, before any verdict.
    if dns_ok == len(domains) and not http_bad:
        print("  ALL domains resolved and ALL HTTP probes returned 2xx.")
    elif dns_ok == len(domains) and http_bad:
        print("  DNS is fine everywhere, but HTTP FAILED on:")
        for url in http_bad:
            print(f"     - {url}  ({http_results[url].get('status', '?')})")
        print("     Resolving and connecting are different layers: a name that")
        print("     resolves proves a DNS record exists, not that anything is")
        print("     listening or that packets can reach it.")
    elif dns_ok == 0:
        print("  NO domains resolved - this environment has NO external DNS access.")
        print("     Likely cause: sandbox/container with restricted network.")
    else:
        print(f"  PARTIAL - {dns_ok}/{len(domains)} domains resolved.")
        failed = [d for d, r in dns_results.items() if r["status"] != "OK"]
        print(f"     Failed: {', '.join(failed)}")
        # DO NOT print a verdict here. A DNS failure seen through ONE resolver
        # cannot distinguish "this host no longer exists" from "this resolver
        # will not answer for it" -- and an earlier version of this block
        # asserted the first, on a run where `texas.gov` itself had failed. That
        # domain plainly exists, so the assertion was false, and false in the
        # EXPENSIVE direction: it sent the reader hunting for a successor portal
        # that was never missing. Roadmap D31 records the same defect shape in
        # the D25 diagnostic -- the lines that REPORT were right every time, the
        # lines that INFER were wrong in both directions. So this block now
        # reports the pattern and names the one command that settles it.
        print("\n     To settle it, ask a public resolver directly:")
        for domain in failed[:2]:
            print(f"       nslookup {domain} 8.8.8.8")
        print("     Resolves there but not here -> this machine's resolver is")
        print("     the blocker, which is a deployment decision. Fails there")
        print("     too -> the host really is gone; find the successor portal.")

    # The one correlation this tool CAN report, and the reason it exists: which
    # side of a CDN a host sits on. A CDN-fronted host answers from an edge near
    # the caller, while an origin-hosted one requires reaching the operator's own
    # netblock -- so these two groups can behave completely differently on the
    # same network, in the same second. Reported as a grouping, never as a cause:
    # a connect timeout is symmetric and cannot tell "my route drops packets to
    # that netblock" apart from "their firewall drops packets from my region".
    def _route(domain: str) -> str:
        cname = dns_results.get(domain, {}).get("canonical_name", "") or ""
        if "cloudfront" in cname or "awsglobalaccelerator" in cname:
            return "CDN/edge"
        if cname and cname != domain:
            return "aliased"
        return "origin"

    if http_results:
        print("\n  Route vs outcome (grouping, NOT a cause):")
        for url, result in http_results.items():
            host = url_host[url]
            status = result.get("status", "?")
            ips = ", ".join(dns_results.get(host, {}).get("ips", [])[:1]) or "-"
            print(f"     {_route(host):<9} {status:<17} {ips:<16} {url}")
        print("     If every CDN/edge row succeeded and every origin row timed")
        print("     out, that is a property of the PATH from this machine, not")
        print("     evidence about whether those services are running. Confirm")
        print("     from a different network before acting on it.")

    print()
    # Non-zero when anything failed, so this can gate a script or a CI step
    # instead of only being read by a human. Previously it returned 0 even on a
    # run where four of seven hosts were unreachable.
    return 1 if (http_bad or dns_fail) else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
    except Exception as exc:
        print(f"\nFATAL: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(2)

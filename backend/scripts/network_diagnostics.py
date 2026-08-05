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
"""

from __future__ import annotations

import platform
import socket
import sys
import time
from typing import Any

# Domains to probe
DOMAINS = [
    "google.com",                              # Internet baseline
    "mycpa.cpa.state.tx.us",                  # CMBL (Centralized Master Bidders List)
    "comptroller.texas.gov",                  # Texas Comptroller homepage
    "txsmartbuy.gov",                         # Electronic State Business Daily
    "texas.gov",                              # State portal
]


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


def main() -> int:
    overall_start = time.monotonic()

    print_separator("NETWORK DIAGNOSTICS")
    print(f"Timestamp : {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"Python    : {sys.version.split()[0]}")
    print(f"Platform  : {platform.platform()}")
    print(f"Machine   : {platform.machine()}")
    print(f"Hostname  : {platform.node()}")

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
    for domain in DOMAINS:
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

    # Map domains to their primary HTTP URLs
    probes: list[tuple[str, str]] = [
        ("google.com", "https://www.google.com"),
        ("mycpa.cpa.state.tx.us", "https://mycpa.cpa.state.tx.us/tpasscmblsearch/index.jsp"),
        ("comptroller.texas.gov", "https://comptroller.texas.gov"),
        ("txsmartbuy.gov", "https://www.txsmartbuy.gov/esbd"),
        ("texas.gov", "https://www.texas.gov"),
    ]

    for label, url in probes:
        print(f"\n  GET {url}")
        print(f"         (DNS: {dns_results.get(label, {}).get('status', '?')})")
        http_result = http_get(url)
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
    dns_fail = len(DOMAINS) - dns_ok
    print(f"\n  DNS resolutions  : {dns_ok} OK / {dns_fail} FAILED (out of {len(DOMAINS)})")
    print(f"  Total time       : {elapsed_total:.1f} ms")
    print()

    if dns_ok == len(DOMAINS):
        print("  ALL domains resolved - internet access appears normal.")
        print("     If specific sites still fail, they may be down or blocking us.")
    elif dns_ok == 0:
        print("  NO domains resolved - this environment has NO external DNS access.")
        print("     Likely cause: sandbox/container with restricted network.")
    else:
        print(f"  PARTIAL - {dns_ok}/{len(DOMAINS)} domains resolved.")
        failed = [d for d, r in dns_results.items() if r["status"] != "OK"]
        print(f"     Failed: {', '.join(failed)}")
        print("     This may be a DNS filtering issue for .gov domains specifically.")

    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
    except Exception as exc:
        print(f"\nFATAL: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(2)

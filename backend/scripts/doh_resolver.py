"""Resolve hostnames over DNS-over-HTTPS when the host resolver refuses to.

WHY THIS EXISTS
    On 2026-08-20 ``scripts/test_cmbl.py`` died with ``[Errno 11002]
    getaddrinfo failed`` before sending a single byte, and the same run showed
    four Texas government hosts unresolvable while ``google.com``,
    ``txsmartbuy.gov`` and ``sam.gov`` resolved fine. ``nslookup
    www.tdlr.texas.gov 8.8.8.8`` then returned ``168.44.252.184`` in seconds.
    So those hosts exist; this machine's resolver simply will not answer for
    them. Roadmap D32 carries the full evidence table.

    The obvious remedy -- point the OS at a public resolver -- needs
    Administrator (``Set-DnsClientServerAddress`` -> ``PermissionDenied``) and
    is defeated anyway by ``fe80::1``, the router's IPv6 link-local address,
    which Windows prefers over any IPv4 resolver you configure. A fix that
    needs elevation cannot live in the repo, cannot run in CI, cannot ride
    along in a container build, and silently re-breaks on the next machine.
    That is the class of problem this module removes.

WHY IT IS NOT NEW ARCHITECTURE (CLAUDE.md 14)
    This project ALREADY resolves DNS over HTTPS in production:
    ``app/email/domain_verifier.py`` queries ``https://dns.google/resolve``
    through ``requests`` for MX records, keyless and with no extra dependency,
    and its docstring states that rationale explicitly. The mechanism below is
    not a new idea being introduced -- it is the SAME mechanism, asked for an A
    record instead of an MX record.

    That asymmetry is itself the defect D32 records: this platform resolves
    EMAIL domains over DoH, immune to the host resolver, while resolving CRAWL
    targets through the host resolver, which on this machine fails. Two DNS
    strategies in one pipeline, the weaker one sitting on the path that feeds
    every later stage. ``domain_has_mx`` is hardcoded to MX and returns
    ``bool``, so it cannot be called for an A record as written; generalising it
    is the permanent fix, and this module proves the generalisation works
    before any production code is touched.

WHY IT IMPORTS NO LEADHUNTERPRO MODULE
    Deliberate, and copied from ``scripts/network_diagnostics.py``, whose
    docstring makes the same promise. A tool that diagnoses the network must
    keep working when the application cannot import, or it fails exactly when
    it is needed. Importing the production constant would cost one line and buy
    a circular dependency between the diagnostic and the thing diagnosed.

WHAT IT DELIBERATELY DOES NOT DO
    :func:`install` tries the REAL resolver first and consults DoH only after
    ``socket.gaierror``. Hosts that already resolve are untouched -- same
    addresses, same ordering, same latency -- so no new failure mode is
    introduced to the majority of lookups that work. It also does not touch
    TLS: the hostname the caller asked for stays the hostname used for SNI and
    certificate validation, because only the address lookup is replaced.
    Rewriting URLs to a bare IP, the other obvious approach, would have broken
    both.

Usage as a standalone diagnostic, from ``backend/``::

    python scripts/doh_resolver.py mycpa.cpa.state.tx.us www.tdlr.texas.gov

Usage from another script, before any network call::

    import doh_resolver
    doh_resolver.install()

Exit code 0 when every requested hostname resolved by one route or the other,
1 when at least one resolved by neither, 2 on an unexpected exception.

This module has no recorded live result yet, so treat it as unproven until one
run is pasted back.
"""

from __future__ import annotations

import socket
from typing import Any

import requests

#: Primary DoH endpoint. Identical to ``domain_verifier.MX_LOOKUP_URL``; see the
#: module docstring for why the value is repeated rather than imported.
DOH_URL = "https://dns.google/resolve"

#: Fallback for the one case the primary cannot cover: ``dns.google`` is itself
#: a hostname, so reaching it needs the very resolver that may be broken. Google
#: publishes 8.8.8.8 as an IP SAN on that certificate, so an HTTPS request
#: straight to the address still validates -- nothing is disabled here. If this
#: endpoint is ever reached with certificate errors, fix the cause; do NOT add
#: ``verify=False``, which would turn a DNS workaround into a
#: man-in-the-middle hole on every lookup the process makes.
DOH_URL_BY_IP = "https://8.8.8.8/resolve"

#: DNS record type for an IPv4 address, per RFC 1035. The DoH JSON response
#: mixes record types in one "Answer" list, so this constant is what separates
#: an address from a CNAME.
_A_RECORD = 1

_TIMEOUT = 10

#: hostname -> resolved addresses. Without it, a single page fetch can trigger
#: several identical DoH round trips; at the 20,000-lookups-per-day target in
#: the roadmap that difference is the entire budget.
_cache: dict[str, list[str]] = {}

#: Captured at import time, before :func:`install` can replace it. Also the
#: escape hatch the diagnostic below needs in order to ask "would the OS have
#: resolved this?" after the patch is active.
_real_getaddrinfo = socket.getaddrinfo

#: Hostnames the OS failed on and DoH then resolved. Kept so a caller can SAY
#: that it fell back, instead of reporting a clean success and leaving the
#: broken resolver undiscovered. CLAUDE.md 1 forbids a silent fallback for
#: fixture data for the same reason it is wrong here: a repair that hides
#: itself gets shipped as if nothing were wrong, and the next operator on a
#: working network cannot reproduce whatever this one saw.
_repaired: set[str] = set()


def repaired_hosts() -> list[str]:
    """Return the hostnames that only resolved because DoH was consulted.

    Empty means the OS resolver answered everything and this module changed
    nothing -- which is a result worth printing too, since it distinguishes
    "the network is fine here" from "the fallback carried the run".
    """
    return sorted(_repaired)



def resolve_a(hostname: str, *, timeout: int = _TIMEOUT) -> list[str]:
    """Return the IPv4 addresses for ``hostname``, or an empty list.

    An empty list means UNRESOLVED, never "no such host". A timeout, a non-200
    from the resolver, malformed JSON and a genuine NXDOMAIN all land here and
    this function cannot tell them apart. Callers must therefore report ``[]``
    as unknown, exactly as ``domain_verifier.domain_has_mx`` treats its own
    failures as unverified rather than as negative.
    """
    if hostname in _cache:
        return _cache[hostname]

    params = {"name": hostname, "type": "A"}
    headers = {"Accept": "application/dns-json"}
    addresses: list[str] = []

    for url in (DOH_URL, DOH_URL_BY_IP):
        try:
            response = requests.get(
                url, params=params, headers=headers, timeout=timeout
            )
            if response.status_code != 200:
                continue
            data = response.json()
        except (requests.RequestException, ValueError):
            continue

        # Only type-1 answers hold an address. CNAME answers (type 5) arrive in
        # the same list with a hostname in "data"; handing one to a socket call
        # would fail later, far from here, with an error that names neither DNS
        # nor this module.
        for answer in data.get("Answer") or []:
            if answer.get("type") == _A_RECORD:
                value = str(answer.get("data", "")).strip()
                if value:
                    addresses.append(value)
        if addresses:
            break

    _cache[hostname] = addresses
    return addresses


def _patched_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
    """Stand in for :func:`socket.getaddrinfo`, falling back to DoH on failure.

    The real resolver is always tried first, so this is a repair path rather
    than a replacement: a working lookup keeps its own answer, its own ordering
    and its own IPv6 records, and only ``gaierror`` -- the exact failure D32
    documents -- reaches the DoH branch.

    ``*args``/``**kwargs`` rather than the real six-parameter signature: the
    stdlib's fourth parameter is named ``type``, and spelling that out here
    would shadow a builtin for no gain. Passing the arguments through
    untouched also means this wrapper cannot drift from the signature it
    stands in for, whichever way a caller chooses to pass them.
    """
    try:
        return _real_getaddrinfo(host, port, *args, **kwargs)
    except socket.gaierror:
        family = kwargs.get("family", args[0] if len(args) > 0 else 0)
        requested_type = kwargs.get("type", args[1] if len(args) > 1 else 0)
        requested_proto = kwargs.get("proto", args[2] if len(args) > 2 else 0)

        # An AF_INET6 request cannot be satisfied with the A records below, and
        # a non-string host is already an address or an unsupported input. Both
        # re-raise rather than guess.
        if family == socket.AF_INET6 or not isinstance(host, str):
            raise
        addresses = resolve_a(host)
        if not addresses:
            raise
        _repaired.add(host)

    # Re-raising above rather than returning an empty list is deliberate:
    # getaddrinfo's contract is "addresses or gaierror", and a caller handed an
    # empty list would fail further downstream with an error that no longer
    # mentions name resolution -- which is the wrong turn D32 already watched a
    # reader take once.
    sock_type = requested_type or socket.SOCK_STREAM
    sock_proto = requested_proto or socket.IPPROTO_TCP
    resolved_port = port if isinstance(port, int) else 0
    return [
        (socket.AF_INET, sock_type, sock_proto, "", (address, resolved_port))
        for address in addresses
    ]


def install() -> None:
    """Route FAILED hostname lookups through DoH, process-wide.

    Idempotent. This affects every library that resolves through the socket
    module, which is the point: ``http.client`` (as used by
    ``scripts/test_cmbl.py``) and ``requests`` (as used by everything else) are
    both repaired without either being edited.
    """
    if socket.getaddrinfo is not _patched_getaddrinfo:
        socket.getaddrinfo = _patched_getaddrinfo


def uninstall() -> None:
    """Restore the real resolver. Exists so a test can prove install() acted."""
    socket.getaddrinfo = _real_getaddrinfo


def main(argv: list[str]) -> int:
    """Report, per hostname, whether the OS resolves it and whether DoH does."""
    hostnames = argv or ["mycpa.cpa.state.tx.us", "www.tdlr.texas.gov", "google.com"]

    print("=" * 74)
    print("DoH resolver check -- OS resolver vs dns.google (roadmap D32)")
    print("=" * 74)
    header = f"{'hostname':<34}{'OS':>8}{'DoH':>8}   verdict"
    print(header)
    print("-" * 74)

    unresolved: list[str] = []
    for hostname in hostnames:
        try:
            _real_getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
            os_ok = True
        except socket.gaierror:
            os_ok = False

        addresses = resolve_a(hostname)
        if os_ok:
            verdict = "fine already"
        elif addresses:
            verdict = f"REPAIRED -> {addresses[0]}"
        else:
            verdict = "unresolved by BOTH"
            unresolved.append(hostname)

        print(
            f"{hostname:<34}{'OK' if os_ok else 'FAIL':>8}"
            f"{'OK' if addresses else 'FAIL':>8}   {verdict}"
        )

    # Report only, and name the next command. Deciding what the pattern PROVES
    # is left to the reader on purpose: three diagnostics in this repo have now
    # stated confident verdicts that were wrong, while every line that merely
    # reported was right (roadmap D31).
    print(
        "\nOS FAIL + DoH OK means the host exists and this machine's resolver\n"
        "will not answer for it, so install() unblocks the fetch with no\n"
        "elevation and no change to the machine. FAIL on BOTH sides is a\n"
        "different finding -- DoH is a public resolver, so the name most likely\n"
        "does not exist. Confirm with `nslookup <host> 8.8.8.8` before acting."
    )
    return 1 if unresolved else 0


if __name__ == "__main__":
    import sys

    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(1) from None

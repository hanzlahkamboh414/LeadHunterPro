"""P5-Lite heuristic verifier contracts (hermetic: injected MX lookups
and outcome lookups — no live DNS, no live bounce store). The honesty
rules under test: "dead" only on AUTHORITATIVE evidence, resolver
failures are "unknown", confidence NEVER claims verification.
"""

from __future__ import annotations

from app.email.heuristic_verifier import (
    DISPOSABLE_DOMAINS,
    ROLE_LOCALS,
    classify_email,
    classify_emails,
    get_email_classifier,
    mx_status,
)


def _mx(state: str, hosts: list[str] | None = None):
    """An injected MX lookup pinned to one answer."""
    def lookup(domain: str):
        if state == "boom":
            raise OSError("resolver down")
        return (state, hosts if hosts is not None else [])
    return lookup


# ---------------------------------------------------------------------------
# syntax + disposable (no DNS needed)
# ---------------------------------------------------------------------------

def test_bad_syntax_is_dead():
    for bad in ("", "no-at-sign", "a@b", "x@localhost", "@x.com"):
        v = classify_email(bad, mx_lookup=_mx("hosts", ["mx.x.com"]))
        assert v["confidence"] == "dead"
        assert v["reasons"] == ["bad_syntax"]


def test_disposable_domain_is_dead():
    domain = sorted(DISPOSABLE_DOMAINS)[0]
    v = classify_email(f"jane@{domain}", mx_lookup=_mx("hosts", ["mx.a.com"]))
    assert v["confidence"] == "dead"
    assert v["reasons"] == ["disposable"]


# ---------------------------------------------------------------------------
# MX states — the honesty core
# ---------------------------------------------------------------------------

def test_authoritative_no_mx_is_dead():
    v = classify_email("jane@acme.com", mx_lookup=_mx("none"))
    assert v["confidence"] == "dead"
    assert v["reasons"] == ["no_mx"]


def test_resolver_failure_is_unknown_never_dead():
    """An unreachable DNS resolver must never mark a lead dead — offline
    is not a verdict (the pipeline's DOA gate relies on this)."""
    v = classify_email("jane@acme.com", mx_lookup=_mx("boom"))
    assert v["confidence"] == "unknown"
    assert v["reasons"] == ["mx_unresolvable"]


def test_null_mx_is_dead():
    """A null MX (RFC 7505 — 'we accept no mail') surfaces as an empty
    host list from the authoritative path and is a dead address."""
    v = classify_email("jane@acme.com", mx_lookup=_mx("none", []))
    assert v["confidence"] == "dead"


def test_person_on_major_provider_is_high():
    v = classify_email(
        "jane@acme.com",
        mx_lookup=_mx("hosts", ["aspmx.l.google.com", "alt1.aspmx.l.google.com"]),
    )
    assert v["confidence"] == "high"
    assert v["reasons"] == ["provider_google"]
    assert v["provider"] == "google"
    assert v["role"] is False


def test_person_on_selfhosted_mx_is_medium():
    v = classify_email(
        "jane@acme.com", mx_lookup=_mx("hosts", ["mail.acme.com"]),
    )
    assert v["confidence"] == "medium"
    assert v["reasons"] == ["selfhosted_mx"]


def test_role_account_is_medium_even_on_google():
    local = sorted(ROLE_LOCALS)[0]
    v = classify_email(
        f"{local}@acme.com",
        mx_lookup=_mx("hosts", ["aspmx.l.google.com"]),
    )
    assert v["confidence"] == "medium"
    assert v["reasons"] == ["role_account"]
    assert v["role"] is True
    assert v["provider"] == "google"


def test_longest_provider_suffix_wins():
    """protection.outlook.com is Microsoft even though it also ends in
    .com — the longest fingerprint match decides."""
    v = classify_email(
        "jane@acme.com",
        mx_lookup=_mx("hosts", ["acme-com.mail.protection.outlook.com"]),
    )
    assert v["provider"] == "microsoft"


# ---------------------------------------------------------------------------
# bounce-outcome evidence (real send/reply ground truth)
# ---------------------------------------------------------------------------

def test_bounced_before_is_dead():
    v = classify_email(
        "jane@acme.com", mx_lookup=_mx("hosts", ["mx.acme.com"]),
        outcome_lookup=lambda e: "bounced",
    )
    assert v["confidence"] == "dead"
    assert v["reasons"] == ["bounced_before"]


def test_reply_confirmed_is_high_outranking_role():
    """A real reply proves the mailbox even for a role address — the
    strongest evidence wins."""
    v = classify_email(
        "info@acme.com", mx_lookup=_mx("hosts", ["mx.acme.com"]),
        outcome_lookup=lambda e: "delivered",
    )
    assert v["confidence"] == "high"
    assert v["reasons"] == ["reply_confirmed"]
    assert v["role"] is True


def test_unknown_outcome_falls_through_to_mx():
    v = classify_email(
        "jane@acme.com", mx_lookup=_mx("hosts", ["mx.acme.com"]),
        outcome_lookup=lambda e: None,
    )
    assert v["confidence"] == "medium"


# ---------------------------------------------------------------------------
# batch + wiring
# ---------------------------------------------------------------------------

def test_batch_shares_one_mx_check_per_domain():
    calls: list[str] = []

    def lookup(domain: str):
        calls.append(domain)
        return ("hosts", ["mail.acme.com"])

    verdicts = classify_emails(
        ["a@acme.com", "b@acme.com", "c@other.com", "not-an-email"],
        mx_lookup=lookup,
    )
    assert calls == ["acme.com", "other.com"]  # one per UNIQUE domain
    assert [v["confidence"] for v in verdicts] == [
        "medium", "medium", "medium", "dead"]


def test_batch_never_leaks_live_dns_on_bad_rows():
    """A bad-syntax row must not trigger the default (live) MX path —
    classify_emails only wraps domains it actually cached."""
    verdicts = classify_emails(["garbage"])  # no mx_lookup injected
    assert verdicts[0]["confidence"] == "dead"


def test_get_email_classifier_degrades_to_heuristic_only(monkeypatch):
    """A broken bounce store must not break classification — the factory
    falls back to heuristic-only (outcome_lookup=None)."""
    import app.email.heuristic_verifier as hv

    monkeypatch.setattr(hv, "_store", None)
    monkeypatch.setattr(
        "app.email.bounce_learning.BounceStore",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db broken")),
    )
    classify = hv.get_email_classifier()
    v = classify("jane@mailinator.com")  # disposable: no DNS needed
    assert v["confidence"] == "dead"


def test_bounce_store_cross_thread_lookup(tmp_path):
    """The cached classifier is used from a NEW thread every run_full pass
    (observed live 2026-09-15: the consumer thread died with
    sqlite3.ProgrammingError and every emails-lane pass researched 0 despite
    discovery succeeding). The shared connection must allow that."""
    import threading

    import app.email.heuristic_verifier as hv
    from app.email.bounce_learning import BounceStore

    store = BounceStore(db_path=str(tmp_path / "outcomes.db"))
    store.record("someone@acme.com", "bounced", "dsn")

    errors: list[str] = []

    def use_from_other_thread() -> None:
        try:
            assert store.lookup("someone@acme.com") == "bounced"
            assert store.lookup("unknown@acme.com") is None
        except Exception as exc:  # pragma: no cover — the failure report
            errors.append(repr(exc))

    t = threading.Thread(target=use_from_other_thread)
    t.start()
    t.join()
    assert errors == []
    store.close()


def test_classifier_lookup_failure_degrades_to_none(monkeypatch):
    """A lookup that RAISES at call time (not build time) must degrade to
    heuristic-only, never kill the research consumer."""
    import sqlite3

    import app.email.heuristic_verifier as hv

    monkeypatch.setattr(hv, "_store", None)

    class _ExplodingStore:
        def lookup(self, email):  # pragma: no cover — the explosion
            raise sqlite3.ProgrammingError("wrong thread")

    monkeypatch.setattr(
        "app.email.bounce_learning.BounceStore", lambda *a, **k: _ExplodingStore(),
    )
    classify = hv.get_email_classifier()
    v = classify("jane@mailinator.com")  # disposable: no DNS needed
    assert v["confidence"] == "dead"


# ---------------------------------------------------------------------------
# mx_status (the real dnspython path, with injected resolver errors)
# ---------------------------------------------------------------------------

def test_mx_status_lookup_exception_is_unknown():
    assert mx_status("acme.com", lookup=_mx("boom")) == ("unknown", None)


def test_mx_status_hosts_passthrough():
    assert mx_status("acme.com", lookup=_mx("hosts", ["mx.acme.com"])) == (
        "hosts", ["mx.acme.com"])


def test_public_resolvers_down_falls_back_to_system(monkeypatch):
    """Networks that block UDP/53 to public resolvers (the EC2 test VPS)
    still get verdicts: the SYSTEM resolver is tried last, and once it
    answers it is remembered so later domains skip the dead fallbacks."""
    import dns.resolver

    import app.email.heuristic_verifier as hv

    class _Rec:
        preference, exchange = 10, "mx.acme.com."

    class _FakeResolver:
        def __init__(self):
            self.nameservers = []  # [] = the system default config

        def resolve(self, domain, rdtype):
            if self.nameservers:
                # A public resolver — simulate the network block.
                raise OSError("UDP/53 blocked")
            return [_Rec()]

    monkeypatch.setattr(hv, "_preferred_ns", [])
    monkeypatch.setattr(dns.resolver, "Resolver", _FakeResolver)
    assert hv.mx_status("acme.com") == ("hosts", ["mx.acme.com"])
    # The system config (None) is now preferred — the second call never
    # touches the blocked public resolvers.
    assert hv._preferred_ns == [None]
    assert hv.mx_status("other.com") == ("hosts", ["mx.acme.com"])

"""P5 — the email pattern-inference engine.

Hermetic: the two network seams (``mx_lookup`` / ``probe``) are injected
fakes, so no test ever talks to a DNS resolver or an SMTP host.
"""

from __future__ import annotations

from app.email.pattern_inference import (
    _name_parts,
    domain_of,
    email_permutations,
    infer_verified_email,
)


# -- name parsing -------------------------------------------------------------


def test_name_parts_board_flip_and_plain_forms():
    assert _name_parts("SMITH, JANE") == ("jane", "smith")
    assert _name_parts("Smith, Jane") == ("jane", "smith")
    assert _name_parts("Jane Smith") == ("jane", "smith")
    # Middle names are ignored (first token, last token).
    assert _name_parts("Jane Anne Smith") == ("jane", "smith")
    # Suffixes never reach a local-part.
    assert _name_parts("Robert Jones Jr.") == ("robert", "jones")
    assert _name_parts("SMITH, JANE III") == ("jane", "smith")


def test_name_parts_punctuation_and_single_names():
    # Apostrophes / hyphens are stripped to email-safe fragments.
    assert _name_parts("O'Brien, Connor") == ("connor", "obrien")
    assert _name_parts("Jean-Luc Picard") == ("jeanluc", "picard")
    # A single token is a last name only — honest, no first invented.
    assert _name_parts("Cher") == ("", "cher")
    assert _name_parts("") == ("", "")


# -- permutations ---------------------------------------------------------------


def test_permutations_ordered_deduped_lowercase():
    got = email_permutations("Jane Smith", "acmegc.com")
    assert got == [
        "jane.smith@acmegc.com", "janesmith@acmegc.com",
        "jane_smith@acmegc.com", "jane@acmegc.com",
        "jsmith@acmegc.com", "j.smith@acmegc.com", "janes@acmegc.com",
        "smith.jane@acmegc.com",
    ]


def test_permutations_single_name_and_limit():
    assert email_permutations("Cher", "acmegc.com") == ["cher@acmegc.com"]
    assert len(email_permutations("Jane Smith", "acmegc.com")) == 8
    assert email_permutations("", "acmegc.com") == []
    assert email_permutations("Jane Smith", "") == []


# -- domain_of -----------------------------------------------------------------


def test_domain_of_takes_url_or_bare_domain():
    assert domain_of("https://www.acmegc.com/contact") == "acmegc.com"
    assert domain_of("http://acmegc.com") == "acmegc.com"
    assert domain_of("www.acmegc.com") == "acmegc.com"
    assert domain_of("acmegc.com") == "acmegc.com"
    assert domain_of("") == ""


# -- the engine -----------------------------------------------------------------


def _engine(person, site, *, mx=None, replies=None):
    """infer_verified_email with faked MX + probe. ``replies`` maps each
    probed email to the server's answer (True/False/None); default False."""
    mx = mx if mx is not None else ["mx1.acmegc.com"]
    replies = replies or {}
    seen: list[str] = []

    def fake_probe(host, email):
        seen.append(email)
        return replies.get(email, False)

    result = infer_verified_email(
        person, site, mx_lookup=lambda d: mx, probe=fake_probe,
    )
    return result, seen


def test_engine_verifies_first_accepted_permutation():
    result, seen = _engine(
        "Jane Smith", "https://www.acmegc.com",
        replies={"jane.smith@acmegc.com": True},
    )
    assert result == {"email": "jane.smith@acmegc.com", "reason": "verified"}
    # The catch-all probe ran first, then the candidates in order.
    assert seen[0].endswith("@acmegc.com") and "-noexist" in seen[0]
    assert seen[1] == "jane.smith@acmegc.com"


def test_engine_stops_at_first_verify_never_probes_all():
    result, seen = _engine(
        "Jane Smith", "acmegc.com",
        replies={"jsmith@acmegc.com": True},
    )
    assert result["reason"] == "verified"
    assert result["email"] == "jsmith@acmegc.com"
    # catch-all probe + 5 candidates (jane.smith … j.smith) = 6 probes,
    # never the whole list once one is confirmed.
    assert len(seen) == 6


def test_engine_catch_all_never_returns_a_maybe():
    # The server accepts EVERY address — its 250 proves nothing, so the
    # engine reports catch_all instead of stocking a maybe.
    result = infer_verified_email(
        "Jane Smith", "acmegc.com",
        mx_lookup=lambda d: ["mx1.acmegc.com"],
        probe=lambda host, email: True,
    )
    assert result == {"email": "", "reason": "catch_all"}


def test_engine_honest_miss_reasons():
    # No person name to infer from.
    result, _ = _engine("", "acmegc.com")
    assert result == {"email": "", "reason": "no_name"}
    # No website / domain.
    result, _ = _engine("Jane Smith", "")
    assert result == {"email": "", "reason": "no_domain"}
    # Domain resolves to no MX host.
    result, _ = _engine("Jane Smith", "acmegc.com", mx=[])
    assert result == {"email": "", "reason": "no_mx"}
    # Every candidate rejected — honest all_rejected, never a maybe.
    result, _ = _engine("Jane Smith", "acmegc.com")
    assert result == {"email": "", "reason": "all_rejected"}


def test_engine_greylisting_is_an_honest_unknown():
    result, _ = _engine(
        "Jane Smith", "acmegc.com",
        replies={e: None for e in
                 ["jane.smith@acmegc.com", "janesmith@acmegc.com",
                  "jane_smith@acmegc.com", "jane@acmegc.com",
                  "jsmith@acmegc.com", "j.smith@acmegc.com",
                  "janes@acmegc.com", "smith.jane@acmegc.com"]},
    )
    assert result == {"email": "", "reason": "greylisted"}


def test_engine_unreachable_when_even_the_catch_all_probe_fails():
    # Every SMTP answer is inconclusive (4xx / timeouts) — honest unknown.
    result = infer_verified_email(
        "Jane Smith", "acmegc.com",
        mx_lookup=lambda d: ["mx1.acmegc.com"],
        probe=lambda host, email: None,
    )
    assert result == {"email": "", "reason": "unreachable"}

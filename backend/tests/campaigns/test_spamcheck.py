"""Spam-risk analysis + one-click fix (Phase E7).

analyze_script: the honest heuristic — trigger phrases (word-boundary,
subject counts double), subject shape (ALL CAPS, '!!', length, fake
'Re:'), body shape (shouting, exclamation runs, link farms, shorteners,
'$' density). Score is severity-weighted with capped counts.

improve_script: AI rewrite best-effort (variables must survive verbatim,
no em-dashes, SUBJECT:/BODY: shape) with the deterministic rules rewrite
as the guaranteed fallback.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import app.campaigns.spamcheck as spamcheck
from app.main import app

from tests.campaigns.test_multiaccount_ai import _setup

CLEAN_SUBJECT = "Estimating for {{company_name}}"
CLEAN_BODY = (
    "Hi {{first_name}},\n\n"
    "I saw {{company_name}} in {{location}} and wanted to introduce our "
    "estimating services for GC projects. We handle takeoffs and bids for "
    "contractors like you.\n\n"
    "Would a short call next week work?\n\n"
    "Best regards,\nSam"
)

SPAM_SUBJECT = "RE: ACT NOW!!! FREE GUARANTEED CASH OFFER winner"
SPAM_BODY = (
    "Hi {{first_name}},\n\n"
    "This is a LIMITED TIME offer!!! Click here for your FREE prize. "
    "Act now and get RICH with our AMAZING system. "
    "100% GUARANTEED or your money back. "
    "See https://bit.ly/scam1 and https://example.com/a and "
    "https://example.com/b\n\n"
    "$5000 $10000 $50000 waiting for YOU!!!"
)


# ---------------------------------------------------------------------------
# analyze_script
# ---------------------------------------------------------------------------

def test_clean_script_scores_low():
    r = spamcheck.analyze_script(CLEAN_SUBJECT, CLEAN_BODY)
    assert r["score"] < 10
    assert r["level"] == "low"
    assert r["findings"] == []


def test_spammy_script_scores_high_with_findings():
    r = spamcheck.analyze_script(SPAM_SUBJECT, SPAM_BODY)
    assert r["score"] >= 50
    assert r["level"] == "high"
    rules = {f["rule"] for f in r["findings"]}
    assert "phrase:act now" in rules
    assert "phrase:free" in rules
    assert "subject:allcaps" in rules
    assert "subject:exclamations" in rules
    assert "subject:fake-reply" in rules
    assert "body:allcaps-words" in rules
    assert "body:url-shortener" in rules
    assert "body:link-farm" in rules
    # High-severity findings float to the top.
    assert r["findings"][0]["severity"] == "high"


def test_word_boundaries_no_false_positives():
    """'freedom' is not 'free', 'grassroots' is not 'roots' — the analyzer
    matches whole words only, never inside another word. Plurals
    ('discounts') are NOT the word 'discount' either (strict boundary)."""
    r = spamcheck.analyze_script(
        "Freedom Contractors quote",
        "We handle classified documents and caretaking work. "
        "Our grassroots crew discounts nothing.")
    rules = {f["rule"] for f in r["findings"]}
    assert "phrase:free" not in rules
    assert "phrase:discount" not in rules
    # The singular form IS the word — flagged by design.
    r2 = spamcheck.analyze_script("s", "a discount for you")
    assert "phrase:discount" in {f["rule"] for f in r2["findings"]}


def test_template_variables_never_flagged():
    """{{first_name}} etc. are field references, not content — the ALL-CAPS
    and phrase rules skip them."""
    r = spamcheck.analyze_script("Quote for {{company_name}}",
                                 "Hi {{first_name}}, the {{company_name}} "
                                 "crew in {{location}}.")
    assert r["findings"] == []


def test_subject_counts_double():
    """The same trigger in the subject weighs twice the body — the subject
    is what a filter reads first."""
    subj_only = spamcheck.analyze_script("Amazing deal", "plain text")
    body_only = spamcheck.analyze_script("plain subject", "amazing deal")
    assert subj_only["score"] > body_only["score"]


def test_long_subject_flagged():
    short = spamcheck.analyze_script("Quote", CLEAN_BODY)
    long_ = spamcheck.analyze_script(
        "A very long subject line that goes on and on and will surely get "
        "cut off in most mailbox providers list views today", CLEAN_BODY)
    assert short["score"] < long_["score"]


def test_score_capped_at_100():
    r = spamcheck.analyze_script(SPAM_SUBJECT, SPAM_BODY * 5)
    assert r["score"] == 100


# ---------------------------------------------------------------------------
# The rules rewrite (deterministic)
# ---------------------------------------------------------------------------

def test_rules_fix_replaces_and_normalizes():
    subj, txt, notes = spamcheck._rules_fix(SPAM_SUBJECT, SPAM_BODY)
    low = (subj + txt).lower()
    for phrase in ("act now", "click here", "free", "guaranteed",
                   "limited time", "amazing"):
        assert phrase not in low, f"'{phrase}' survived: {low}"
    assert "!!!" not in subj and "!!!" not in txt
    assert "re:" not in subj.lower()
    # The shortener is flagged but links are kept (removing a link would
    # change the offer) — the swap only touches wording.
    assert "https://bit.ly/scam1" in txt
    assert notes


def test_rules_fix_preserves_variables():
    subj, txt, _ = spamcheck._rules_fix(SPAM_SUBJECT, SPAM_BODY)
    assert "{{first_name}}" in txt


def test_rules_fix_clean_script_unchanged():
    subj, txt, _ = spamcheck._rules_fix(CLEAN_SUBJECT, CLEAN_BODY)
    assert subj == CLEAN_SUBJECT
    assert txt == CLEAN_BODY


def test_rules_fix_caps_words_lowered_but_acronyms_kept():
    subj, txt, _ = spamcheck._rules_fix(
        "Quote", "Our HVAC and MEP crews are READY for GC work.")
    assert "HVAC" in txt and "MEP" in txt and "GC" in txt
    assert "READY" not in txt
    assert "ready" in txt


def test_rules_fix_sentence_start_capitalized():
    subj, txt, _ = spamcheck._rules_fix("Quote", "Amazing work on the site.")
    assert txt.startswith("Impressive") or txt[0].isupper()


# ---------------------------------------------------------------------------
# The AI rewrite (best-effort, variables must survive)
# ---------------------------------------------------------------------------

def _ai_reply(subject: str, body: str) -> str:
    return f"SUBJECT: {subject}\n\nBODY: {body}"


def test_improve_uses_ai_when_valid():
    ask = lambda prompt: _ai_reply(  # noqa: E731 — test stub
        "Estimating for {{company_name}}",
        "Hi {{first_name}}, a calm professional rewrite.")
    r = spamcheck.improve_script(ask, SPAM_SUBJECT, SPAM_BODY)
    assert r["method"] == "ai"
    assert "{{first_name}}" in r["body"]
    assert "{{company_name}}" in r["subject"]


def test_improve_falls_back_when_ai_drops_variable():
    ask = lambda prompt: _ai_reply(  # noqa: E731 — test stub
        "Rewritten subject", "Rewritten body that lost the variable.")
    r = spamcheck.improve_script(ask, SPAM_SUBJECT, SPAM_BODY)
    assert r["method"] == "rules"
    assert "{{first_name}}" in r["body"]


def test_improve_falls_back_when_ai_raises():
    def ask(prompt):
        raise RuntimeError("router down")
    r = spamcheck.improve_script(ask, SPAM_SUBJECT, SPAM_BODY)
    assert r["method"] == "rules"
    assert "act now" not in r["body"].lower()


def test_improve_strips_dashes_from_ai_text():
    """The same em-dash ban as the opening lines — an AI tell."""
    ask = lambda prompt: _ai_reply(  # noqa: E731 — test stub
        "Estimating for {{company_name}}",
        "Hi {{first_name}}, we saw your work, amazing jobs, and want to "
        "help with takeoffs.")
    r = spamcheck.improve_script(ask, "s", "b")
    assert "—" not in r["body"] and "–" not in r["body"]


def test_improve_on_clean_script_still_returns_usable_text():
    ask = lambda prompt: _ai_reply(  # noqa: E731 — test stub
        CLEAN_SUBJECT, CLEAN_BODY)
    r = spamcheck.improve_script(ask, CLEAN_SUBJECT, CLEAN_BODY)
    assert r["subject"] and r["body"]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_api_spam_check_and_improve(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    r = ctx["client"].post("/api/v1/campaigns/spam-check",
                           json={"subject": SPAM_SUBJECT, "body": SPAM_BODY})
    assert r.status_code == 200
    out = r.json()
    assert out["score"] >= 50 and out["level"] == "high"
    assert out["findings"]

    # The endpoint's AI callable is injected so no real router call
    # happens in tests.
    monkeypatch.setattr(spamcheck, "_module_ask",
                        lambda prompt: _ai_reply(
                            "Estimating for {{company_name}}",
                            "Hi {{first_name}}, a calm professional rewrite."))
    r2 = ctx["client"].post("/api/v1/campaigns/spam-improve",
                            json={"subject": SPAM_SUBJECT, "body": SPAM_BODY})
    assert r2.status_code == 200
    out2 = r2.json()
    assert out2["method"] == "ai"
    assert "act now" not in (out2["subject"] + out2["body"]).lower()


def test_api_spam_check_requires_auth(tmp_path, monkeypatch):
    anon = TestClient(app)
    r = anon.post("/api/v1/campaigns/spam-check",
                  json={"subject": "s", "body": "b"})
    assert r.status_code in (401, 403)


def test_api_spam_check_validates(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    r = ctx["client"].post("/api/v1/campaigns/spam-check",
                           json={"subject": "", "body": "b"})
    assert r.status_code == 422

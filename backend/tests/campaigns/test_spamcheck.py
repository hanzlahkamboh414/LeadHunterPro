"""Spam-risk analysis + one-click fix (Phase E7).

analyze_script: the honest rules heuristic — trigger phrases (word-boundary,
subject counts double), subject shape (ALL CAPS, '!!', length, fake
'Re:'), body shape (shouting, exclamation runs, link farms, shorteners,
'$' density), structure (greeting, personalization, opt-out line,
wall of text, sentence length) and the cold-email rulebook (template
filler / AI-tell phrases with neutral swaps, generic openers, the
120-150 word target, sign-off + signature with contact info, hard-sell
CTAs). Score is severity-weighted with capped counts.

The AI layer: the LLM reads the RENDERED email as a deliverability
expert and answers a JSON verdict (score, plain-words summary, six
category scores, findings with concrete fixes); it merges with the
rules (AI 60 / rules 40) and is cached per script. Bad JSON, a risky
score with nothing behind it, or a dead router -> rules-only fallback.

improve_script: AI rewrite best-effort (variables must survive verbatim,
no em-dashes, SUBJECT:/BODY: shape) with the deterministic rules rewrite
as the guaranteed fallback.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.campaigns.spamcheck as spamcheck
from app.main import app

from tests.campaigns.test_multiaccount_ai import _setup


@pytest.fixture(autouse=True)
def _fresh_cache():
    """The per-script AI cache is process state — tests start clean."""
    spamcheck._AI_CACHE.clear()
    yield
    spamcheck._AI_CACHE.clear()


CLEAN_SUBJECT = "Estimating for {{company_name}}"
# A fully-compliant cold email per the rulebook: greeting + variables,
# a how-I-found-you line, 120-150 words of content, an opt-out line, a
# 'Best regards' sign-off and a signature with company name + contact.
# The analyzer must find NOTHING.
CLEAN_BODY = (
    "Hi {{first_name}},\n\n"
    "I found your company while researching commercial contractors in "
    "{{location}}, and I saw that {{company_name}} is bidding on fitout "
    "work. I wanted to introduce our estimating services. We handle "
    "takeoffs, material counts, and full bid packages for contractors "
    "like you, and our team has been preparing GC and MEP estimates "
    "across the United States for over ten years. Most of our clients "
    "send us their drawings on a Monday and have a priced takeoff back "
    "before they bid on Friday.\n\n"
    "Would a short call next week work? I am happy to walk through a "
    "sample takeoff so you can judge the quality yourself.\n\n"
    "Just let me know if you'd rather not hear from me, and I will not "
    "follow up.\n\n"
    "Best regards,\nSam Carter\nThe Best Estimator LLC\n"
    "sam@thebestestimator.com | +1 555 014 2211"
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
# The cold-email rulebook (template filler, opener, shape)
# ---------------------------------------------------------------------------

def test_template_phrases_and_ai_tells_flagged_and_swapped():
    body = ("I hope this email finds you well. We are revolutionizing the "
            "takeoff process with seamless technology and industry-leading "
            "accuracy.")
    r = spamcheck.analyze_script("Quote", body)
    rules = {f["rule"] for f in r["findings"]}
    assert "phrase:i hope this email finds you well" in rules
    assert "phrase:revolutionizing" in rules
    assert "phrase:seamless" in rules
    assert "phrase:industry-leading" in rules
    # The one-click rewrite swaps them out.
    _, txt, _ = spamcheck._rules_fix("Quote", body)
    low = txt.lower()
    for gone in ("finds you well", "revolutionizing", "seamless",
                 "industry-leading"):
        assert gone not in low, f"'{gone}' survived: {low}"


def test_generic_opener_flagged():
    """The first real line after the greeting is the opener — 'I wanted
    to reach out' there means no specific research fact."""
    body = ("Hi {{first_name}},\n\n"
            "I wanted to reach out because I saw your website and think we "
            "could help your team with takeoffs.")
    r = spamcheck.analyze_script("Quote", body)
    assert "body:generic-opener" in {f["rule"] for f in r["findings"]}
    # The same wording mid-email is not the opener — not flagged as one.
    body2 = ("Hi {{first_name}},\n\nYour team bids on fitouts. I wanted to "
             "reach out because we do the takeoffs behind bids like those.")
    r2 = spamcheck.analyze_script("Quote", body2)
    assert "body:generic-opener" not in {f["rule"] for f in r2["findings"]}


def test_word_count_target():
    """Too short or too long is flagged; the 120-150 range is not."""
    short = ("Hi {{first_name}},\n\nWe prepare cost estimates for GC "
             "projects. Our team handles takeoffs, material counts, and "
             "bid packages for contractors. We have been doing this work "
             "for many years across several states. Our clients send us "
             "drawings and we return a full priced takeoff within two "
             "business days every single week without any delays at all.")
    assert "body:word-count" in {
        f["rule"] for f in spamcheck.analyze_script("Quote", short)["findings"]}
    long_ = "filler sentence here. " * 60
    assert "body:word-count" in {
        f["rule"] for f in spamcheck.analyze_script("Quote", long_)["findings"]}
    # CLEAN_BODY sits inside the range (covered by the clean test too).
    assert "body:word-count" not in {
        f["rule"] for f in spamcheck.analyze_script(
            CLEAN_SUBJECT, CLEAN_BODY)["findings"]}


def test_signoff_and_signature_checks():
    no_signoff = ("Hi {{first_name}}, we do estimating work for GC "
                  "projects every day.")
    assert "body:no-signoff" in {
        f["rule"] for f in spamcheck.analyze_script("Quote", no_signoff)["findings"]}
    bare = "Hi {{first_name}},\n\nWe do estimating.\n\nBest regards,\nSam"
    rules = {f["rule"] for f in spamcheck.analyze_script("Quote", bare)["findings"]}
    assert "body:no-signoff" not in rules
    assert "body:no-contact-info" in rules
    full = ("Hi {{first_name}},\n\nWe do estimating.\n\n"
            "Best regards,\nSam Carter\n+1 555 014 2211")
    rules2 = {f["rule"] for f in spamcheck.analyze_script("Quote", full)["findings"]}
    assert "body:no-signoff" not in rules2
    assert "body:no-contact-info" not in rules2


def test_hard_sell_cta_flagged():
    body = ("Hi {{first_name}},\n\nDon't miss this offer, and sign up "
            "today for our service package.")
    r = spamcheck.analyze_script("Quote", body)
    hits = [f for f in r["findings"] if f["rule"] == "body:hard-sell-cta"]
    assert hits and hits[0]["count"] == 3


def test_landscape_trade_word_not_flagged():
    """Landscaping is a real trade we email — only the marketing-speak
    'the X landscape' is flagged, never 'landscape projects'."""
    trade = ("Hi {{first_name}},\n\nWe support landscaping crews and "
             "landscape projects across your region.")
    assert "body:marketing-landscape" not in {
        f["rule"] for f in spamcheck.analyze_script("Quote", trade)["findings"]}
    marketing = ("Hi {{first_name}},\n\nWe are leaders in the construction "
                 "landscape of today.")
    assert "body:marketing-landscape" in {
        f["rule"] for f in spamcheck.analyze_script("Quote", marketing)["findings"]}


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


def _ai_verdict(score: int = 85, summary: str = "Pushy hype throughout.",
                 categories: dict | None = None,
                 findings: list | None = None) -> str:
    """A JSON deliverability verdict the way the model is told to answer."""
    return json.dumps({
        "score": score,
        "summary": summary,
        "categories": categories if categories is not None else {
            "content": 80, "urgency": 90, "tone": 70,
            "structure": 20, "personalization": 10, "links": 60},
        "findings": findings if findings is not None else [
            {"severity": "high",
             "message": "'get RICH' promises wealth to the reader",
             "fix": "State the service plainly without a wealth promise",
             "category": "content"}],
    })


def _dual_ask(prompts: list[str]) -> "callable":
    """A fake _module_ask that answers BOTH shapes: the analysis prompt
    (JSON verdict) and the improve prompt (SUBJECT:/BODY:). Records every
    prompt so tests can assert on what the model was shown. The improve
    reply uses ONLY variables the original SPAM text has ({{first_name}})
    — invented variables get rejected by design."""
    def ask(prompt: str) -> str:
        prompts.append(prompt)
        if "deliverability expert" in prompt:
            return _ai_verdict()
        return _ai_reply("Estimating support",
                         "Hi {{first_name}}, a calm professional rewrite.")
    return ask


def test_improve_uses_ai_when_valid():
    ask = lambda prompt: _ai_reply(  # noqa: E731 — test stub
        "Estimating support",
        "Hi {{first_name}}, a calm professional rewrite.")
    r = spamcheck.improve_script(ask, SPAM_SUBJECT, SPAM_BODY)
    assert r["method"] == "ai"
    assert "{{first_name}}" in r["body"]


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
    # The fallback also gets the completed skeleton.
    assert "best regards" in r["body"].lower()


def test_improve_cleans_reintroduced_filler_and_completes_structure():
    """The AI rewrite can bring banned filler back ('I noticed', 'no cost')
    and skip the skeleton — the deterministic cleanup removes the filler
    and the sign-off / signature / compliance footer are added."""
    long_body = (
        "Hi {{first_name}}, I noticed that your team handles many "
        "projects at no cost to the owner during preconstruction reviews, "
        "and I wanted to share how our takeoff process can reduce manual "
        "counting and speed up proposal timelines for busy contractors "
        "who bid every week across several states and keep winning new "
        "work throughout the year.")
    ask = lambda prompt: _ai_reply(  # noqa: E731 — test stub
        "Estimating support", long_body)
    monkey_company = "The Best Estimator LLC"
    import app.campaigns.spamcheck as sc
    old = sc._COMPANY_NAME
    sc._COMPANY_NAME = monkey_company
    try:
        r = spamcheck.improve_script(ask, "s", "Hi {{first_name}}, short.")
    finally:
        sc._COMPANY_NAME = old
    assert r["method"] == "ai"
    low = r["body"].lower()
    assert "i noticed" not in low
    assert "no cost" not in low
    assert "best regards" in low
    assert monkey_company.lower() in low
    assert "unsubscribe" in low  # >= 60 words -> footer added
    # The completed script scores far below the original problem level.
    after = spamcheck.analyze_script(r["subject"], r["body"])
    assert after["score"] < 15, [f["rule"] for f in after["findings"]]


def test_improve_strips_dashes_from_ai_text():
    """The same em-dash ban as the opening lines — an AI tell."""
    ask = lambda prompt: _ai_reply(  # noqa: E731 — test stub
        "Estimating support",
        "We saw your work, amazing jobs, and want to help with takeoffs.")
    r = spamcheck.improve_script(ask, "s", "b")
    assert "—" not in r["body"] and "–" not in r["body"]


def test_improve_rejects_ai_invented_variables():
    """'{{company}}' is not a real field — if the model invents one, the
    reply is rejected and the deterministic rewrite is used instead (an
    invented variable would go out literally to every lead)."""
    ask = lambda prompt: _ai_reply(  # noqa: E731 — test stub
        "Estimating for {{company_name}}", "Hi!")
    r = spamcheck.improve_script(ask, "s", "b")
    assert r["method"] == "rules"
    assert "{{" not in r["body"]


def test_improve_prompt_forbids_invented_facts():
    """The rewrite must never pretend to know the recipient — a verified
    research fact is added per lead at send time, so the template stays
    honest (the reference cold-email standard: never invent facts)."""
    prompt = spamcheck.build_improve_prompt("s", "b", [])
    assert "NEVER invent claims" in prompt
    assert "never add new ones" in prompt


def test_improve_on_clean_script_still_returns_usable_text():
    ask = lambda prompt: _ai_reply(  # noqa: E731 — test stub
        CLEAN_SUBJECT, CLEAN_BODY)
    r = spamcheck.improve_script(ask, CLEAN_SUBJECT, CLEAN_BODY)
    assert r["subject"] and r["body"]


# ---------------------------------------------------------------------------
# The AI analysis layer (merged verdict)
# ---------------------------------------------------------------------------

def test_analyze_with_ai_merges_and_blends():
    r = spamcheck.analyze_with_ai(
        lambda prompt: _ai_verdict(),
        SPAM_SUBJECT, SPAM_BODY, "Estimating for Acme", "Hi John, ...")
    assert r is not None and r["method"] == "ai"
    rules_score = spamcheck.analyze_script(SPAM_SUBJECT, SPAM_BODY)["score"]
    assert r["score"] == round(0.6 * 85 + 0.4 * rules_score)
    assert r["summary"] == "Pushy hype throughout."
    # All six categories survive the clamp.
    assert set(r["categories"]) == set(spamcheck.CATEGORIES)
    # Both layers' findings are present, high severity first.
    rules = {f["rule"] for f in r["findings"]}
    assert "phrase:free" in rules            # the rules engine
    assert any(f["rule"].startswith("ai:") for f in r["findings"])  # the AI
    assert r["findings"][0]["severity"] == "high"
    # AI findings carry the fix + category, rules findings don't need to.
    ai = next(f for f in r["findings"] if f["rule"].startswith("ai:"))
    assert ai["fix"] and ai["category"] == "content"


def test_analyze_with_ai_sees_rendered_not_raw():
    """The model judges the real email: variables are filled in before the
    prompt, and the rules findings come from the RAW editor text."""
    seen: list[str] = []
    r = spamcheck.analyze_with_ai(
        lambda prompt: (seen.append(prompt), _ai_verdict())[1],
        CLEAN_SUBJECT, CLEAN_BODY, "Estimating for Acme", "Hi John, ...")
    assert r is not None
    assert "{{first_name}}" not in seen[0]
    assert "Acme" in seen[0]


def test_analyze_with_ai_falls_back_on_bad_json():
    for bad in ("", "I cannot judge this", "{\"score\": "):
        assert spamcheck.analyze_with_ai(
            lambda prompt, b=bad: b,
            SPAM_SUBJECT, SPAM_BODY, "s", "b") is None


def test_analyze_with_ai_falls_back_when_ask_raises():
    def ask(prompt):
        raise RuntimeError("router down")
    assert spamcheck.analyze_with_ai(
        ask, SPAM_SUBJECT, SPAM_BODY, "s", "b") is None


def test_analyze_with_ai_rejects_risky_score_with_nothing_behind():
    """Score 90 with no summary and no findings is not a verdict — a
    number alone must not drive the meter."""
    assert spamcheck.analyze_with_ai(
        lambda prompt: _ai_verdict(score=90, summary="", findings=[]),
        SPAM_SUBJECT, SPAM_BODY, "s", "b") is None


def test_analyze_with_ai_clamps_out_of_range_values():
    r = spamcheck.analyze_with_ai(
        lambda prompt: _ai_verdict(
            score=250, categories={"content": 500, "urgency": -40}),
        CLEAN_SUBJECT, CLEAN_BODY, "s", "b")
    assert r is not None
    assert r["score"] == round(0.6 * 100 + 0.4 * 0)
    assert r["categories"]["content"] == 100
    assert r["categories"]["urgency"] == 0


def test_analyze_with_ai_fenced_json_still_parses():
    """Models love ```json fences despite instructions — survive them."""
    r = spamcheck.analyze_with_ai(
        lambda prompt: f"```json\n{_ai_verdict()}\n```",
        CLEAN_SUBJECT, CLEAN_BODY, "s", "b")
    assert r is not None and r["method"] == "ai"


# ---------------------------------------------------------------------------
# check_endpoint (cache + guaranteed rules fallback)
# ---------------------------------------------------------------------------

def test_check_endpoint_caches_per_script(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(spamcheck, "_module_ask", _dual_ask(calls))
    r1 = spamcheck.check_endpoint(SPAM_SUBJECT, SPAM_BODY)
    r2 = spamcheck.check_endpoint(SPAM_SUBJECT, SPAM_BODY)
    assert r1["method"] == "ai" and r2 == r1
    # One analysis prompt total — the second check was a cache hit.
    assert sum("deliverability expert" in p for p in calls) == 1


def test_check_endpoint_rules_only_when_ai_down(monkeypatch):
    def ask(prompt):
        raise RuntimeError("router down")
    monkeypatch.setattr(spamcheck, "_module_ask", ask)
    r = spamcheck.check_endpoint(SPAM_SUBJECT, SPAM_BODY)
    assert r["method"] == "rules"
    assert r["summary"] == "" and not r.get("categories")
    assert r["score"] == spamcheck.analyze_script(SPAM_SUBJECT, SPAM_BODY)["score"]


def test_check_endpoint_bad_verdict_falls_back_to_rules(monkeypatch):
    monkeypatch.setattr(spamcheck, "_module_ask",
                        lambda prompt: "garbage not json")
    r = spamcheck.check_endpoint(SPAM_SUBJECT, SPAM_BODY)
    assert r["method"] == "rules"
    assert r["findings"]  # the user still gets the full rules report


def test_check_endpoint_cache_evicts_fifo(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(spamcheck, "_module_ask", _dual_ask(calls))
    spamcheck._AI_CACHE.clear()
    spamcheck._AI_CACHE_MAX = 2
    for i in range(4):
        spamcheck.check_endpoint(SPAM_SUBJECT, f"body {i}")
    assert len(spamcheck._AI_CACHE) == 2
    assert sum("deliverability expert" in p for p in calls) == 4
    spamcheck._AI_CACHE_MAX = 300


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_api_spam_check_and_improve(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    # The endpoint's AI callable is injected BEFORE any call — both
    # spam-check (analysis) and spam-improve (rewrite) hit it, and no real
    # router call ever happens in tests.
    calls: list[str] = []
    monkeypatch.setattr(spamcheck, "_module_ask", _dual_ask(calls))

    r = ctx["client"].post("/api/v1/campaigns/spam-check",
                           json={"subject": SPAM_SUBJECT, "body": SPAM_BODY})
    assert r.status_code == 200
    out = r.json()
    assert out["method"] == "ai"
    assert out["score"] >= 50 and out["level"] == "high"
    assert out["findings"]
    assert out["summary"]
    assert out["categories"]["content"] == 80

    r2 = ctx["client"].post("/api/v1/campaigns/spam-improve",
                            json={"subject": SPAM_SUBJECT, "body": SPAM_BODY})
    assert r2.status_code == 200
    out2 = r2.json()
    assert out2["method"] == "ai"
    assert "act now" not in (out2["subject"] + out2["body"]).lower()


def test_api_spam_check_falls_back_to_rules(tmp_path, monkeypatch):
    ctx = _setup(tmp_path, monkeypatch, accounts=1)
    monkeypatch.setattr(spamcheck, "_module_ask",
                        lambda prompt: (_ for _ in ()).throw(
                            RuntimeError("router down")))
    r = ctx["client"].post("/api/v1/campaigns/spam-check",
                           json={"subject": SPAM_SUBJECT, "body": SPAM_BODY})
    assert r.status_code == 200
    out = r.json()
    assert out["method"] == "rules"
    assert out["score"] >= 50 and out["findings"]


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

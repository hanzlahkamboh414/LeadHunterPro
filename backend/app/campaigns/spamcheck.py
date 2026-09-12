"""Spam-risk analysis + one-click fix for campaign scripts (Phase E7).

Before a user schedules a campaign, the builder shows an honest estimate
of how spammy the script looks, a plain-words list of WHAT in the text is
risky, and a one-click "fix" that rewrites the pitch without those
triggers.

Everything here is self-hosted rules (FREE rule, provider-agnostic): the
classic content signals mailbox providers weigh — hype words, fake
urgency, ALL CAPS, exclamation runs, deceptive "Re:" subjects, link
farms. The score is a heuristic, NOT a Gmail oracle: it is a guide that
makes the usual mistakes visible, and the UI says so.

The one-click fix is AI-best-effort with a deterministic fallback (the
Phase-E5 pattern): the configured LLM rewrites the script keeping its
meaning, tone and {{variables}}; if the AI is down or mangles the
variables, a pure-rules rewrite (word swaps + punctuation + caps
normalization) produces a guaranteed-usable script. Either way the
result lands back in the editor for the user to review — nothing is sent
anywhere until they schedule it.
"""

from __future__ import annotations

import re

from app.campaigns.personalize import Ask, default_ask

# ---------------------------------------------------------------------------
# Trigger phrases: (phrase, severity, plain-words message, neutral swap)
# Sorted longest-first at match time so "for a limited time" wins over
# "limited time". A "" swap deletes the phrase (a flagged lead-in usually
# reads better gone than reworded).
# ---------------------------------------------------------------------------

_PHRASES: list[tuple[str, str, str, str]] = [
    ("act now", "high", "Fake urgency ('act now') is a classic spam phrase", "when you are ready"),
    ("apply now", "medium", "'Apply now' reads as mass marketing", "feel free to reach out"),
    ("best deal", "medium", "'Best deal' is marketing hype", "a good fit"),
    ("best price", "medium", "'Best price' claims read as marketing hype", "competitive pricing"),
    ("bargain", "medium", "'Bargain' reads as mass marketing", ""),
    ("buy now", "high", "'Buy now' is a classic spam call-to-action", ""),
    ("call now", "medium", "'Call now' reads as telemarketing", "give me a call"),
    ("cash", "medium", "'Cash' is a money-scam marker in cold email", ""),
    ("cheap", "medium", "'Cheap' lowers trust in a professional pitch", "affordable"),
    ("click here", "high", "'Click here' is a top spam phrase", "see our website"),
    ("congratulations", "high", "'Congratulations' is a lottery-scam marker", ""),
    ("discount", "medium", "'Discount' reads as mass marketing", "a better rate"),
    ("don't delay", "high", "Fake urgency ('don't delay')", ""),
    ("double your", "high", "'Double your ...' is a get-rich scam marker", ""),
    ("earn cash", "high", "'Earn cash' is a scam marker", ""),
    ("exclusive deal", "high", "'Exclusive deal' is marketing hype", ""),
    ("extra income", "high", "'Extra income' is a get-rich scam marker", ""),
    ("financial freedom", "high", "'Financial freedom' is a get-rich scam marker", ""),
    ("for a limited time", "high", "Fake urgency ('for a limited time')", "this quarter"),
    ("free", "medium", "'Free' is a heavily-weighed spam word; 'complimentary' reads professional", "complimentary"),
    ("guaranteed", "high", "'Guaranteed' is a top spam word — nobody can guarantee an outcome", "confident"),
    ("guarantee", "high", "'Guarantee' is a top spam word — nobody can guarantee an outcome", "stand behind our"),
    ("hurry", "medium", "Fake urgency ('hurry')", ""),
    ("incredible", "low", "'Incredible' is hype language", "strong"),
    ("investment opportunity", "high", "'Investment opportunity' is a scam marker", ""),
    ("last chance", "high", "Fake urgency ('last chance')", ""),
    ("limited time", "high", "Fake urgency ('limited time')", "this month"),
    ("lowest price", "medium", "'Lowest price' claims read as marketing hype", "competitive pricing"),
    ("make money", "high", "'Make money' is a get-rich scam marker", ""),
    ("miracle", "high", "'Miracle' is a scam marker", ""),
    ("no credit check", "high", "'No credit check' is a scam marker", ""),
    ("no cost", "medium", "'No cost' is a spam-weighed money phrase", "complimentary"),
    ("no obligation", "medium", "'No obligation' is a spam-weighed phrase", ""),
    ("no risk", "medium", "'No risk' promises read as spam", ""),
    ("offer expires", "high", "Fake urgency ('offer expires')", ""),
    ("100%", "medium", "'100%' claims read as overpromise", ""),
    ("order now", "high", "'Order now' is a classic spam call-to-action", ""),
    ("pre-approved", "high", "'Pre-approved' is a scam marker", ""),
    ("risk free", "high", "'Risk free' is a top spam phrase", ""),
    ("reply now", "medium", "'Reply now' reads as mass marketing", "reply whenever suits you"),
    ("save big", "high", "'Save big' is marketing hype", "save on"),
    ("save money", "medium", "'Save money' reads as mass marketing", "reduce cost"),
    ("see for yourself", "low", "'See for yourself' is filler hype", ""),
    ("special offer", "medium", "'Special offer' reads as mass marketing", "an offer"),
    ("this is not spam", "high", "Saying 'this is not spam' is the loudest spam marker there is", ""),
    ("today only", "high", "Fake urgency ('today only')", ""),
    ("while supplies last", "high", "Fake scarcity ('while supplies last')", ""),
    ("winner", "high", "'Winner' is a lottery-scam marker", ""),
    ("work from home", "high", "'Work from home' is a get-rich scam marker", ""),
    ("you have been selected", "high", "'You have been selected' is a scam marker", ""),
    ("amazing", "low", "'Amazing' is hype language", "impressive"),
]

#: Allowed to stay uppercase — real industry acronyms, not shouting.
_ACRONYM_OK = {
    "GC", "MEP", "HVAC", "USA", "PDF", "API", "URL", "ROI", "BIM", "OSHA",
    "LEED", "ISO", "DB", "LP", "LLC", "INC", "CM", "PM", "RFI", "RFP",
    "AIA", "CSI", "T&M",
}

_VAR = re.compile(r"\{\{[a-z_]+\}\}", re.IGNORECASE)
_CAPS_WORD = re.compile(r"\b[A-Z]{4,}\b")
_EXCLAM_RUN = re.compile(r"!{2,}")
_QUESTION_RUN = re.compile(r"\?{2,}")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_SHORTENER = re.compile(
    r"https?://(?:bit\.ly|tinyurl\.com|is\.gd|goo\.gl|t\.co|rb\.gy|cutt\.ly)/\S+",
    re.IGNORECASE)
_DECEPTIVE_PREFIX = re.compile(r"^\s*(?:re|fw|fwd)\s*:", re.IGNORECASE)

_SEVERITY_WEIGHT = {"high": 12, "medium": 7, "low": 3}


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    tail = r"\b" if phrase[-1].isalnum() else ""
    return re.compile(r"\b" + re.escape(phrase) + tail, re.IGNORECASE)


_PHRASE_RES = [
    (p, s, m, swap, _phrase_pattern(p))
    for (p, s, m, swap) in sorted(_PHRASES, key=lambda t: -len(t[0]))
]


def _finding(rule: str, severity: str, message: str, count: int) -> dict:
    return {"rule": rule, "severity": severity, "message": message,
            "count": count}


def analyze_script(subject: str, body: str) -> dict:
    """The spam-risk report for one pitch: an estimated 0-100 risk score,
    a level, and the findings behind it (rule, severity, plain words,
    occurrence count). Template variables ({{first_name}} etc.) are never
    analyzed — they are field references, not content."""
    subj = _VAR.sub(" ", subject or "")
    txt = _VAR.sub(" ", body or "")
    findings: list[dict] = []

    # -- Trigger phrases (subject AND body; subject counts double). ------
    for phrase, sev, msg, _swap, rx in _PHRASE_RES:
        n = len(rx.findall(subj)) * 2 + len(rx.findall(txt))
        if n:
            findings.append(_finding(f"phrase:{phrase}", sev, msg, n))

    # -- Subject shape -----------------------------------------------------
    letters = [c for c in subj if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        findings.append(_finding(
            "subject:allcaps", "high",
            "The subject is in ALL CAPS — the single loudest spam look", 1))
    if len(_EXCLAM_RUN.findall(subj)):
        findings.append(_finding(
            "subject:exclamations", "high",
            "Multiple '!!' in the subject line", len(_EXCLAM_RUN.findall(subj))))
    if len(subject) > 78:
        findings.append(_finding(
            "subject:too-long", "medium",
            f"Subject is {len(subject)} characters — over ~78 gets cut off "
            "and reads as a blast", 1))
    elif len(subject) > 60:
        findings.append(_finding(
            "subject:long", "low",
            f"Subject is {len(subject)} characters — under ~60 reads cleaner",
            1))
    if _DECEPTIVE_PREFIX.search(subject or ""):
        findings.append(_finding(
            "subject:fake-reply", "high",
            "Subject starts with 'Re:'/'Fwd:' but this is not a reply — "
            "mailbox providers flag the trick", 1))

    # -- Body shape ---------------------------------------------------------
    caps_words = [w for w in _CAPS_WORD.findall(txt) if w not in _ACRONYM_OK]
    if caps_words:
        findings.append(_finding(
            "body:allcaps-words", "medium" if len(caps_words) < 5 else "high",
            f"{len(caps_words)} ALL-CAPS word(s) read as shouting "
            "(industry acronyms like GC/HVAC are fine)", len(caps_words)))
    runs = len(_EXCLAM_RUN.findall(txt))
    total_bangs = (txt or "").count("!")
    if runs:
        findings.append(_finding(
            "body:exclamation-runs", "medium",
            "Exclamation runs ('!!') in the body", runs))
    if total_bangs >= 3:
        findings.append(_finding(
            "body:too-many-bangs", "low",
            f"{total_bangs} exclamation marks total — one per email is plenty",
            total_bangs))
    urls = _URL.findall(txt)
    if len(urls) >= 2:
        findings.append(_finding(
            "body:link-farm", "medium",
            f"{len(urls)} links in the body — cold email with 2+ links is "
            "weighed as spam; one link (or none) is safest", len(urls)))
    if _SHORTENER.search(txt):
        findings.append(_finding(
            "body:url-shortener", "high",
            "Shortened URLs (bit.ly etc.) are a top spam signal — use the "
            "real address", len(_SHORTENER.findall(txt))))
    if (txt or "").count("$") >= 3:
        findings.append(_finding(
            "body:dollars", "low",
            "Several '$' amounts — money-dense cold email is weighed as spam",
            txt.count("$")))

    # -- Score: severity-weighted, counts capped so one word repeated 20
    #    times can't alone max the meter. -----------------------------------
    score = min(100, sum(_SEVERITY_WEIGHT[f["severity"]] * min(f["count"], 3)
                         for f in findings))
    level = "high" if score >= 50 else ("medium" if score >= 25 else "low")
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order[f["severity"]], -f["count"]))
    return {"score": score, "level": level, "findings": findings}


# ---------------------------------------------------------------------------
# The one-click fix
# ---------------------------------------------------------------------------

def _fix_case(match: re.Match[str]) -> str:
    word = match.group(0)
    if word in _ACRONYM_OK or _VAR.search(word):
        return word
    lowered = word.lower()
    return lowered


def _mask_vars(s: str) -> tuple[str, dict[str, str]]:
    """Replace {{variables}} with placeholder tokens nothing else touches
    (caps-lowering could break an uppercase variable), and the map back."""
    back: dict[str, str] = {}

    def _sub(m: re.Match[str]) -> str:
        tok = f"\x00{len(back)}\x00"
        back[tok] = m.group(0)
        return tok

    return _VAR.sub(_sub, s), back


def _unmask(s: str, back: dict[str, str]) -> str:
    for tok, var in back.items():
        s = s.replace(tok, var)
    return s


def _rules_fix(subject: str, body: str) -> tuple[str, str, list[str]]:
    """The deterministic rewrite: trigger swaps, punctuation, caps, fake
    reply prefix. Guaranteed usable (no AI involved) — also the fallback
    when the LLM is down or mangles the variables."""
    notes: list[str] = []
    subj, back_subj = _mask_vars(subject)
    txt, back_txt = _mask_vars(body)

    swapped = 0
    for phrase, _sev, _msg, swap, rx in _PHRASE_RES:
        if not rx.search(subj) and not rx.search(txt):
            continue
        replacement = swap
        # Sentence-start swap keeps its capital.
        def _sub(m: re.Match[str]) -> str:
            found = m.group(0)
            if replacement and found[:1].isupper() and m.start() == 0:
                return replacement[0].upper() + replacement[1:]
            return replacement
        subj = rx.sub(_sub, subj)
        txt = rx.sub(_sub, txt)
        swapped += 1
    if swapped:
        notes.append(f"Replaced {swapped} spam phrase(s) with neutral wording")

    if _DECEPTIVE_PREFIX.search(subj):
        subj = _DECEPTIVE_PREFIX.sub("", subj).lstrip()
        notes.append("Removed the fake 'Re:'/'Fwd:' subject prefix")

    caps_fixed = len([w for w in _CAPS_WORD.findall(txt)
                      if w not in _ACRONYM_OK])
    if caps_fixed:
        txt = _CAPS_WORD.sub(_fix_case, txt)
        notes.append(f"Lowered {caps_fixed} ALL-CAPS word(s)")
    subj_caps = bool(letters := [c for c in subj if c.isalpha()]) and \
        sum(c.isupper() for c in letters) / len(letters) > 0.7
    if subj_caps:
        subj = subj.capitalize()
        notes.append("Rewrote the ALL-CAPS subject in normal case")

    if _EXCLAM_RUN.search(subj) or _EXCLAM_RUN.search(txt) or \
            _QUESTION_RUN.search(txt):
        subj = _EXCLAM_RUN.sub("!", subj)
        txt = _EXCLAM_RUN.sub("!", txt)
        txt = _QUESTION_RUN.sub("?", txt)
        notes.append("Collapsed '!!'/'??' runs to single marks")

    if len(subj) > 78:
        cut = subj[:78]
        if " " in cut:
            cut = cut[:cut.rfind(" ")]
        subj = cut.rstrip(",;:-")
        notes.append("Trimmed the subject under 78 characters")

    # Word-swap debris: double spaces / space before punctuation.
    txt = re.sub(r"[ \t]{2,}", " ", txt)
    txt = re.sub(r" ,", ",", txt)
    txt = re.sub(r" \.", ".", txt)
    subj = re.sub(r"[ \t]{2,}", " ", subj).strip()
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    return _unmask(subj, back_subj), _unmask(txt, back_txt), notes


def build_improve_prompt(subject: str, body: str, findings: list[dict]) -> str:
    lines = [
        "You rewrite a cold-outreach email so mailbox providers do not read "
        "it as spam, keeping the writer's meaning and tone.",
        "",
        "Rules:",
        "1. Keep the SAME meaning, offer, and professional tone — this is a "
        "rewrite for deliverability, not new marketing copy.",
        "2. Remove every spam trigger listed below: no hype words, no fake "
        "urgency, no ALL CAPS, no '!!', no exaggerated claims.",
        "3. Keep it roughly the same length (short is good).",
        "4. Copy every {{variable}} (like {{first_name}}) EXACTLY as written "
        "— they are placeholders filled by software later.",
        "5. Plain text, no markdown, no quotes around the text.",
        "6. NEVER use em-dashes (—) or en-dashes (–) — they read as "
        "machine-written. Use commas instead.",
        "7. The subject stays under 60 characters if you can.",
        "",
        f"Current subject: {subject}",
        "",
        "Current body:",
        body,
        "",
        "Spam problems to fix:",
    ]
    lines += [f"- {f['message']}" for f in findings] or ["- (none)"]
    lines += [
        "",
        "Reply in EXACTLY this format (two lines, nothing else):",
        "SUBJECT: <new subject>",
        "BODY: <new body>",
    ]
    return "\n".join(lines)


def _parse_ai_reply(reply: str, subject: str, body: str) -> tuple[str, str] | None:
    """The model's 'SUBJECT:/BODY:' answer, or None when it is unusable
    (wrong shape, empty, or it lost a {{variable}})."""
    if not reply:
        return None
    text = reply.strip().strip('"').strip("'")
    m = re.search(
        r"SUBJECT:\s*(.+?)\s*\n+BODY:\s*(.+)\s*$", text, re.DOTALL)
    if not m:
        return None
    new_subject, new_body = m.group(1).strip(), m.group(2).strip()
    if not new_subject or not new_body:
        return None
    # Every original variable must survive — a dropped {{first_name}} would
    # break the send. Mangled variables -> rules fallback instead.
    for var in set(_VAR.findall(subject) + _VAR.findall(body)):
        if var not in new_subject and var not in new_body:
            return None
    # The format headers must not leak into the script itself.
    if "SUBJECT:" in new_body or "BODY:" in new_body:
        return None
    # Same dash ban as the opening lines (an "AI wrote this" tell).
    dash = re.compile(r"\s*[—–]\s*")
    new_subject = re.sub(r"(?:,\s*){2,}", ", ", dash.sub(", ", new_subject))
    new_body = re.sub(r"(?:,\s*){2,}", ", ", dash.sub(", ", new_body))
    return new_subject, new_body


def improve_script(ask: Ask, subject: str, body: str) -> dict:
    """The one-click fix: AI rewrite (best-effort), deterministic rules
    rewrite as the guaranteed fallback. Returns the new subject/body plus
    how it was produced and what was changed."""
    findings = analyze_script(subject, body)["findings"]
    try:
        parsed = _parse_ai_reply(ask(build_improve_prompt(subject, body, findings)),
                                 subject, body)
    except Exception:  # noqa: BLE001 — best-effort by design
        parsed = None
    if parsed is not None:
        return {"subject": parsed[0], "body": parsed[1], "method": "ai",
                "notes": ["Rewritten by AI with spam triggers removed — "
                          "review before sending"]}
    subj, txt, notes = _rules_fix(subject, body)
    return {"subject": subj, "body": txt, "method": "rules", "notes": notes}


_module_ask: Ask | None = None


def improve_endpoint(subject: str, body: str) -> dict:
    """The API-path wrapper: lazily builds the real AI callable once per
    process (tests monkeypatch ``_module_ask`` or the gateway)."""
    global _module_ask
    if _module_ask is None:
        _module_ask = default_ask()
    return improve_script(_module_ask, subject, body)

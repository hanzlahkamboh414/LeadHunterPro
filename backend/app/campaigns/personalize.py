"""AI opening lines for campaigns (Phase E5) — verified evidence ONLY.

The hook is the first 1-2 sentences of a campaign's first email, written
by the configured LLM from the lead's dossier. The hard rule (CLAUDE.md
+ the self-learning constraints): the AI may ONLY reference facts that
exist as VERIFIED evidence in the dossier (confidence "verified", backed
by a source_url). Unverified claims are never shown to it, and the prompt
forbids inventing. When the verified facts give nothing worth opening
with, the model answers ``NONE`` and the email goes out WITHOUT a hook —
an honest gap, never an invented compliment.

The full personalized opening (user request, E5 polish) is:

    Hi <first name / person name / company name>,

    <AI opening line>

    <the user's script — its own leading greeting is removed so the
    email never says "Hi" twice>

Em/en-dashes ("—", "–") are banned everywhere in the AI-written text:
they are the loudest "an AI wrote this" tell, so the prompt forbids them
AND ``clean_hook`` strips any that slip through (comma substitution) —
belt and braces, because the model will not always obey.

The generated hook is cached per (campaign, lead) by the store, so a
retry or a scheduler restart never re-calls the AI for the same lead.

All AI I/O goes through the injected ``ask`` callable (the AIGateway
pattern from lead_research) — tests inject a fake, production lazily
builds the real gateway.
"""

from __future__ import annotations

import re
from typing import Callable

from app.lead_research.models import LeadDossier

#: ask(prompt) -> str — the AIGateway.ask signature.
Ask = Callable[[str], str]

MAX_HOOK_CHARS = 400

_WHITESPACE = re.compile(r"\s+")
#: An em/en-dash with its surrounding spacing -> ", " (never "AI punctuation").
_DASH = re.compile(r"\s*[—–]\s*")
#: Comma runs left behind by dash substitution -> one comma.
_COMMA_RUN = re.compile(r"(?:,\s*){2,}")
#: A greeting the user's own template may start with — removed when the
#: personalized opening supplies its own, so the email never greets twice.
_LEADING_GREETING = re.compile(
    r"^\s*(?:hi|hello|hey|dear|greetings|assalam[ou]*\s*alaikum)\b"
    r"[^,\n]{0,80},\s*", re.IGNORECASE)


def verified_facts(dossier: LeadDossier) -> list[str]:
    """Every VERIFIED, source-backed claim in the dossier — the only raw
    material the AI is ever allowed to see for personalization."""
    facts: list[str] = []
    for group in (dossier.company.facts, dossier.person.evidence,
                  dossier.intent.evidence, dossier.timing.events):
        for e in group:
            if e.confidence == "verified" and e.source_url and e.claim:
                facts.append(e.claim.strip())
    # Stable order, no duplicates.
    return list(dict.fromkeys(facts))


def build_prompt(dossier: LeadDossier) -> str:
    person = dossier.person
    company = dossier.company
    facts = verified_facts(dossier)
    lines = [
        "You write the OPENING LINE of a short, professional cold email "
        "from an estimating-services company to a construction contractor.",
        "",
        f"Recipient: {person.name or 'unknown'}"
        f" ({person.role or 'role unknown'}) at "
        f"{company.name or dossier.domain}.",
        f"Company: {company.industry or 'industry unknown'}, "
        f"{company.location or 'location unknown'}.",
        "",
        "VERIFIED facts about them (the ONLY things you may reference):",
    ]
    lines += [f"- {f}" for f in facts] or ["- (none)"]
    lines += [
        "",
        "Rules:",
        "1. Write ONE opening sentence (max two) that references one or two "
        "of the verified facts above, so the email clearly isn't a blast.",
        "2. Reference ONLY the verified facts. NEVER invent, guess, or "
        "embellish anything — no projects, dates, numbers, or praise that "
        "is not literally in the list above.",
        "3. Plain text, no greeting (no 'Hi'), no sign-off, no quotes, "
        "no markdown.",
        "4. NEVER use em-dashes (—) or en-dashes (–) — they read as "
        "machine-written. Use commas instead.",
        "5. Write like a busy professional typing a short personal email: "
        "simple words, natural tone, no marketing language.",
        "6. If the verified facts give nothing specific worth mentioning, "
        "reply with exactly: NONE",
        "",
        "Reply with the opening line only (or NONE):",
    ]
    return "\n".join(lines)


def clean_hook(text: str) -> str:
    """Normalize the model's answer into a safe opening line. Empty string
    = no honest hook (send without one)."""
    if not text:
        return ""
    hook = _WHITESPACE.sub(" ", text).strip().strip('"').strip("'").strip()
    if not hook or hook.upper() == "NONE":
        return ""
    if len(hook) > MAX_HOOK_CHARS:
        return ""
    # A hook containing template tokens would survive render() as a literal
    # "{{first_name}}" in the sent email — reject it outright.
    if "{{" in hook:
        return ""
    # Em/en-dashes never reach the email — swap them for commas (the model
    # is told not to use them; this catches the times it does anyway).
    hook = _COMMA_RUN.sub(", ", _DASH.sub(", ", hook)).strip().rstrip(",").strip()
    if not hook:
        return ""
    return hook


def greeting_for(dossier: LeadDossier) -> str:
    """The opening greeting: the person's first name, else their full name,
    else the company name (a role-based inbox still reads naturally). Empty
    string = the dossier names nobody and nothing."""
    person = (dossier.person.name or "").strip()
    first = person.split()[0] if person else ""
    name = first or person or (dossier.company.name or "").strip()
    return f"Hi {name}," if name else ""


def strip_leading_greeting(body: str) -> str:
    """Drop the template's own 'Hi X,' line when the personalized opening
    supplies one — the email must greet exactly once. The remainder starts
    capitalized so it reads as a normal sentence."""
    stripped = _LEADING_GREETING.sub("", body, count=1).lstrip()
    if not stripped:
        return body  # the template was ONLY a greeting — leave it alone
    return stripped[0].upper() + stripped[1:]


def assemble_opening(dossier: LeadDossier, hook: str, body: str) -> str:
    """The full personalized first-email opening:

        Hi Jane,

        <AI opening line, when there is one>

        <the user's script, minus its own greeting>

    Without a name to greet, the pieces just stack in order. Deterministic
    (no AI involved) — used for every ai_personalize send, hook or not, so
    the email format is uniform even when the AI fails or answers NONE."""
    greeting = greeting_for(dossier)
    if greeting:
        body = strip_leading_greeting(body)
    parts = [p for p in (greeting, hook, body) if p and p.strip()]
    return "\n\n".join(parts)


def generate_hook(ask: Ask, dossier: LeadDossier) -> str:
    """One AI call -> the cleaned opening line ('' = none). Exceptions
    PROPAGATE: the scheduler decides (best-effort — a failing AI must not
    block the send, but the failure is logged, never swallowed here)."""
    intelligence = dossier.signal_intelligence or {}
    trigger = intelligence.get("outreach_trigger", {})
    if isinstance(trigger, dict) and trigger.get("strength") in {
        "confirmed", "strong_inference", "weak_inference", "none",
    }:
        wording = trigger.get("wording", "")
        if isinstance(wording, str):
            approved = clean_hook(wording)
            if approved:
                return approved
    return clean_hook(ask(build_prompt(dossier)))


def default_ask() -> Ask:
    """The production AI callable, built lazily so importing this module
    never constructs a provider (tests and offline tools stay cheap)."""
    from app.ai.gateway import AIGateway
    return AIGateway().ask

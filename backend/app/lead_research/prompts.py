"""AI Lead Research — prompt templates (V1).

Every prompt enforces JSON output with source_url for facts.
"""

from __future__ import annotations


def company_research_prompt(
    email: str,
    refined_domain: str,
    search_results: str,
    site_content: str,
) -> str:
    """Stage 1 — refine company facts. Every fact MUST cite a source_url."""
    return f"""\
You are a company research analyst for a construction preconstruction services company.

TASK: Research the company that owns this email and return structured JSON.

INPUT:
- Email: {email}
- Domain: {refined_domain}
- Search results:
{search_results}
- Website content (truncated):
{site_content}

RULES:
1. Every fact you state MUST have a source_url. If you cannot cite a URL, mark it "unverified".
2. Do NOT invent company names, person names, or roles. Only report what you can verify.
3. If search results and site content provide conflicting information, report both with separate citations.
4. Return ONLY valid JSON matching this exact schema — no markdown, no commentary:

{{
  "company_name": "string — official company name",
  "industry": "string — e.g. General Contractor, Specialty Subcontractor, Developer",
  "location": "string — city, state",
  "website": "string — best verified URL",
  "facts": [
    {{
      "claim": "string — one factual statement",
      "source_url": "string — URL that backs this claim",
      "source_type": "string — website | search_result | directory | other",
      "confidence": "verified or unverified"
    }}
  ]
}}

If you genuinely cannot determine a field, use "" for strings and [] for facts.
If the company cannot be identified at all, set company_name to "" and explain in the first fact claim.
"""


def person_research_prompt(
    email: str,
    refined_domain: str,
    company_name: str,
    search_results: str,
    site_content: str,
) -> str:
    """Stage 2 — person attribution via AI. Every binding MUST cite source_url."""
    return f"""\
You are a person-attribution researcher for a construction industry company.

TASK: Determine who owns or is associated with this email address. Return structured JSON.

INPUT:
- Email: {email}
- Domain: {refined_domain}
- Company name: {company_name}
- Search results:
{search_results}
- Website content (truncated):
{site_content}

RULES:
1. Every person name you state MUST have a source_url proving the association.
2. Do NOT derive names from email local-parts (e.g. "jsmith@acme.com" does NOT prove "John Smith").
3. Do NOT invent or guess names. If you cannot verify, say so.
4. Return ONLY valid JSON matching this exact schema — no markdown, no commentary:

{{
  "person_name": "string — full name if verified, else empty",
  "person_role": "string — job title / role if known",
  "role_relevance": true or false,
  "bound": true or false,
  "evidence": [
    {{
      "claim": "string — one factual statement about this person",
      "source_url": "string — URL backing this claim",
      "source_type": "string — website | search_result | directory | other",
      "confidence": "verified or unverified"
    }}
  ]
}}

CRITICAL: "bound" is true ONLY when you have a source_url that explicitly links this person to this email or this company in a verifiable way. The email local-part alone is NEVER sufficient for "bound".
"""


def intent_timing_prompt(
    email: str,
    company_name: str,
    industry: str,
    facts_summary: str,
) -> str:
    """Stage 3 — buying intent + timing analysis (reasoned judgment)."""
    return f"""\
You are a preconstruction sales analyst for The Best Estimator LLC, a construction estimation company in Texas.

TASK: Assess whether this company likely needs preconstruction/estimation services, and when. Return structured JSON.

INPUT:
- Email: {email}
- Company: {company_name}
- Industry: {industry}
- Known facts:
{facts_summary}

CONTEXT about The Best Estimator LLC:
- Sells preconstruction/estimation services to construction companies
- Helps general contractors, subcontractors, developers with bid preparation, cost estimation, quantity takeoffs
- Ideal client: active construction company that bids on projects but may lack in-house estimation capacity

RULES:
1. Reasoning must be based on the facts provided — do not assume facts not in evidence.
2. If information is insufficient, say "unknown" rather than guessing.
3. LOOK SPECIFICALLY for these growth / need signals and weigh them in your assessment:
   - Hiring posts: is the company hiring estimators, project managers, or construction staff? (indicates growth -> estimation load)
   - New office / expansion: any mention of new locations, larger facilities, or expansion? (more workload -> more bids)
   - Recent bid wins: any evidence of a contract award or low-bidder win, and if so how recent (days vs months)? (recency raises urgency -> "now" or "soon")
   - Active bidding: plan-deposit lists, open RFPs, bid announcements.
   - Staffing gaps: any signal the company lacks in-house estimation capacity.
   A "yes" on needs_estimation is much stronger when a growth/hiring or bid-win signal is present.
4. Return ONLY valid JSON matching this exact schema — no markdown, no commentary:

{{
  "needs_estimation": "yes | no | unknown",
  "signal": "string — key signal driving your assessment",
  "reason": "string — 1-2 sentence reasoning",
  "evidence": [
    {{
      "claim": "string — one supporting observation",
      "source_url": "string — URL if available, else empty",
      "source_type": "string — website | search_result | inferred | other",
      "confidence": "verified or unverified"
    }}
  ],
  "timing_window": "now | soon | later | unknown",
  "timing_reason": "string — why this timing estimate",
  "timing_events": [
    {{
      "claim": "string — one event or signal about timing",
      "source_url": "string — URL if available, else empty",
      "source_type": "string — website | search_result | inferred | other",
      "confidence": "verified or unverified"
    }}
  ]
}}
"""


def fit_scoring_prompt(
    email: str,
    company_name: str,
    industry: str,
    location: str,
    person_name: str,
    person_role: str,
    intent_needs_estimation: str,
    intent_signal: str,
    timing_window: str,
) -> str:
    """Stage 4 — fit assessment + potential lead score."""
    return f"""\
You are a lead qualification analyst for The Best Estimator LLC (Texas construction estimation services).

TASK: Assess fit and assign a potential-lead score (0.0-10.0). Return structured JSON.

INPUT:
- Email: {email}
- Company: {company_name}
- Industry: {industry}
- Location: {location}
- Contact: {person_name} ({person_role})
- Needs estimation: {intent_needs_estimation} — {intent_signal}
- Timing: {timing_window}

SCORING CRITERIA:
- 8.0-10.0: Strong fit — active construction company, clear estimation need, reachable decision-maker, good timing
- 5.0-7.9: Moderate fit — construction-related, some signals, but uncertainty in need or timing
- 2.0-4.9: Weak fit — tangentially related, no clear need signal, or bad timing
- 0.0-1.9: Poor fit — not construction, wrong role, or no actionable information

RULES:
1. Score must reflect actual evidence gathered — do not inflate.
2. "contact_now" requires score >= 6.0 AND a reachable person. "nurture" requires score >= 3.0. Below 3.0 = "skip".
3. Return ONLY valid JSON matching this exact schema — no markdown, no commentary:

{{
  "fit": "string — brief fit assessment (1 sentence)",
  "potential_score": 0.0,
  "recommendation": "contact_now | nurture | skip",
  "reasoning": "string — 1-2 sentences justifying the score"
}}
"""

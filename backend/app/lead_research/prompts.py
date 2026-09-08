"""AI Lead Research — prompt templates (V1).

Every prompt enforces JSON output with source_url for facts.
"""

from __future__ import annotations

from app.company_profile import get_profile


def company_research_prompt(
    email: str,
    refined_domain: str,
    search_results: str,
    site_content: str,
    *,
    trade: str = "",
    location: str = "",
) -> str:
    """Stage 1 — refine company facts. Every fact MUST cite a source_url.

    ``trade``/``location`` are the known construction context from the
    discovery layer (e.g. the lead was found on a plan-holder/bid list for
    trade "Electrical" in "Houston TX"). When present, they anchor the AI so
    it verifies the company as a construction contractor instead of drifting
    to whatever generic web pages the search returns.
    """
    if trade or location:
        context = (
            "DISCOVERY CONTEXT (treat as a strong prior, still VERIFY it):\n"
            f"- This email was found on a construction plan/bid-holder list"
            f"{f' for the trade {trade!r}' if trade else ''}"
            f"{f' in {location!r}' if location else ''}.\n"
            "- Construction bid lists contain ONLY construction contractors / "
            "subcontractors. If the company is genuinely NOT construction "
            "(IT, software, healthcare, retail...), label it with its TRUE "
            "industry — it will be filtered out, which is correct.\n"
        )
    else:
        context = ""
    return f"""\
You are a company research analyst for a construction preconstruction services company.

WHO WE ARE (our ideal client + who is NOT our client):
{get_profile().prompt_context()}

TASK: Research the company that owns this email and return structured JSON.

INPUT:
- Email: {email}
- Domain: {refined_domain}
{context}- Search results:
{search_results}
- Website content (truncated):
{site_content}

RULES:
1. Every fact you state MUST have a source_url. If you cannot cite a URL, mark it "unverified".
2. EXACT-PAGE RULE (CRITICAL): source_url MUST point to the EXACT page where the fact was
   observed — e.g. "https://acme.com/about", "https://acme.com/contact", a LinkedIn company page.
   NEVER cite the bare domain root (e.g. "https://acme.com") when the fact appears on a specific page.
   The page blocks are LABELED like "[PAGE: About us — URL]" — cite the URL from that label.
   If the fact came from search result snippets, cite that result URL and note it in source_type.
   A bare-root citation with no location reported gets demoted to "unverified" downstream.
3. source_note MUST be a short human-readable WHERE: the exact page name or source description
   (e.g. "About us", "Contact page", "Homepage", "Google search result", "LinkedIn company page").
4. Do NOT invent company names, person names, or roles. Only report what you can verify.
5. If search results and site content provide conflicting information, report both with separate citations.
6. industry MUST be the company's TRUE business — e.g. "General Contractor", "Specialty Subcontractor",
   "Developer", "Construction Materials", "IT Services", "Software". We sell estimation services to
   CONSTRUCTION companies. A company that is NOT construction (IT/software/healthcare/retail/etc.) is
   NEVER labelled as construction — set the industry to its true (non-construction) type. Do NOT stretch
   a label to fit construction.
7. CLIENT-FIT VERDICT (decide from WHO WE ARE above, then be honest): set "is_our_client" to
   "yes" only when this company is an ACTIVE building-trades bidder that could buy estimation
   services (a general contractor, subcontractor, or developer that bids on building projects).
   Set "no" when it is clearly NOT a buyer even if construction-adjacent — an A/E/C consultant or
   engineering/architecture/design firm, a trade association / builders' exchange / plan service,
   a software/IT/technology company, a materials-only supplier/distributor/manufacturer, or a
   fiber/telecom/utility/road/pipeline/transit contractor. Set "unsure" only when the evidence is
   genuinely insufficient. "client_reason" MUST be one honest sentence naming WHY, grounded in what
   you found (e.g. "Engineering consultancy, not a bidding contractor — does not buy estimation.").
   This verdict costs no extra research; it is your judgement over the facts you already gathered.
8. Return ONLY valid JSON matching this exact schema — no markdown, no commentary:

{{
  "company_name": "string — official company name",
  "industry": "string — e.g. General Contractor, Specialty Subcontractor, Developer",
  "location": "string — city, state",
  "website": "string — best verified URL",
  "is_our_client": "yes | no | unsure — is this an active building-trades bidder who could buy estimation services?",
  "client_reason": "string — one honest sentence: why they are (or are not) our client",
  "facts": [
    {{
      "claim": "string — one factual statement",
      "source_url": "string — EXACT page URL that backs this claim (never a bare root)",
      "source_type": "string — homepage | about_page | contact_page | team_page | services_page | projects_page | careers_page | news | search_result | directory | linkedin | website_other | other",
      "confidence": "verified or unverified",
      "source_note": "string — exact page name, e.g. About us / Contact page / Homepage / LinkedIn company page"
    }}
  ]
}}

If you genuinely cannot determine a field, use "" for strings and [] for facts.
If the company cannot be identified at all, set company_name to "" and explain in the first fact claim.
"""


def deep_research_prompt(
    domain: str,
    company_name: str,
    search_results: str,
) -> str:
    """Stage 1b — deep-dive growth/need signals (only for qualifying leads).

    Extracts hiring, expansion, and recent bid-win signals that indicate
    estimation demand and urgency.
    """
    return f"""\
You are a preconstruction sales analyst for {get_profile().company_name} ({get_profile().location}).

WHO WE ARE (our ideal client + who is NOT our client):
{get_profile().prompt_context()}

TASK: Deep-dive the growth and need signals for this company to judge whether
they likely need estimation services, and how urgent. Return structured JSON facts.

INPUT:
- Domain: {domain}
- Company name: {company_name}
- Search results (from growth/bid/hiring/news queries):
{search_results}

LOOK FOR AND REPORT:
1. HIRING — is the company hiring estimators, project managers, or construction staff?
   (indicates growth -> estimation load). Report the role + where (LinkedIn jobs, Indeed, etc.).
2. EXPANSION — any new office, larger facility, or location opening? (more workload -> more bids)
3. RECENT BID WIN — any contract award or low-bidder win, and HOW RECENT (days vs months)?
   Recency raises urgency.
4. ACTIVE BIDDING — plan-deposit lists, open RFPs, bid announcements.
5. NEWS — recent news indicating growth or new projects.

RULES:
1. Every fact MUST cite source_url. If you cannot cite, mark it "unverified".
2. EXACT-PAGE RULE (CRITICAL): source_url MUST point to the EXACT page the signal was observed
   on (a news article, a jobs page, LinkedIn company page, a bid board listing) — NEVER a bare
   domain root. cite the result URL when the signal came from a search snippet.
3. source_note MUST be a short human-readable WHERE (e.g. "LinkedIn company page", "company careers page",
   "Houston Business Journal article", "plan-deposit list"). A bare-root citation gets demoted
   to "unverified" downstream.
4. Do NOT invent facts. If a signal was not found, DO NOT claim it — just omit it.
5. Return ONLY valid JSON matching this exact schema — no markdown, no commentary:

{{
  "facts": [
    {{
      "claim": "string — one growth/need signal fact (include recency if known)",
      "source_url": "string — EXACT page URL that backs this claim (never a bare root)",
      "source_type": "string — homepage | about_page | contact_page | team_page | services_page | projects_page | careers_page | news | search_result | directory | linkedin | website_other | other",
      "confidence": "verified or unverified",
      "source_note": "string — exact page name or source description"
    }}
  ]
}}

If no growth/need signals are found, return {{"facts": []}}.
"""


def person_research_prompt(
    email: str,
    refined_domain: str,
    company_name: str,
    search_results: str,
    site_content: str,
    company_facts: str = "",
) -> str:
    """Stage 2 — person attribution via AI. Every binding MUST cite source_url.

    ``company_facts`` is a pre-formatted list of already-verified company facts
    (from Stage 1), e.g. "Mike Dretzka is the Vice President (source: ...)".
    Giving these to the person researcher lets it bind an email to a person who
    appears in verified company facts, instead of rediscovering the team page.
    """
    facts_block = company_facts if company_facts else "(none provided)"
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
- ALREADY-VERIFIED COMPANY FACTS (from company research):
{facts_block}

RULES:
1. Every person name you state MUST have a source_url proving the association.
2. Use the ALREADY-VERIFIED COMPANY FACTS: if a fact names a person + role at this
   company (with its source_url), you may bind to that person when the email
   plausibly belongs to them AND the facts support it. Cite the fact's source_url.
3. Do NOT derive names from email local-parts alone (e.g. "jsmith@acme.com" does
   NOT prove "John Smith"). But a verified company fact naming that family/person
   MAY combine with a matching local-part as supporting evidence.
4. Do NOT invent or guess names. If you cannot verify, say so.
5. SEARCH FOR LINKEDIN: Look through the search results for this person's LinkedIn
   profile URL (linkedin.com/in/<handle>). If found, include it in the "linkedin" field.
   If not found, leave it empty — never fabricate a LinkedIn URL.
6. EXACT-PAGE RULE (CRITICAL): source_url MUST point to the EXACT page where the association
   was observed (a team page, whois/registry page, a news article, a LinkedIn profile).
   NEVER a bare domain root. The page blocks are LABELED like "[PAGE: About us — URL]".
   A bare-root citation with no location reported gets demoted to "unverified".
7. source_note MUST be a short human-readable WHERE (e.g. "Team page", "About us",
   "LinkedIn profile", "State contractor registry").
8. Return ONLY valid JSON matching this exact schema — no markdown, no commentary:

{{
  "person_name": "string — full name if verified, else empty",
  "person_role": "string — job title / role if known",
  "role_relevance": true or false,
  "bound": true or false,
  "linkedin": "string — full linkedin.com/in/<handle> URL if found in search results, else empty",
  "evidence": [
    {{
      "claim": "string — one factual statement about this person",
      "source_url": "string — EXACT page URL backing this claim (never a bare root)",
      "source_type": "string — homepage | about_page | contact_page | team_page | services_page | projects_page | careers_page | news | search_result | directory | linkedin | website_other | other",
      "confidence": "verified or unverified",
      "source_note": "string — exact page name or source description"
    }}
  ]
}}

CRITICAL: "bound" is true ONLY when you have a source_url that explicitly links this person to this email or this company in a verifiable way. The email local-part alone is NEVER sufficient for "bound".
CRITICAL: "linkedin" MUST be a real URL from the search results — never invent a handle.
"""


def linkedin_profile_prompt(name: str, linkedin: str, linkedin_content: str) -> str:
    """Stage 2b — enrich a person with facts from their OWN public LinkedIn
    profile.

    Runs AFTER the main person pass already identified ``name`` AND the R1
    name-match guard verified the profile belongs to that person. Best-effort
    by design: only a read, public profile yields facts; a login-walled one
    contributes nothing (the caller returns the original evidence unchanged).
    """
    return f"""\
You are a person-attribution researcher. A lead's identity has been verified as "{name}".
Extract verifiable facts from THIS person's own public LinkedIn profile only.

- Person name (verified): {name}
- LinkedIn profile URL (the exact page every fact comes from): {linkedin}
- Profile content (truncated):
{linkedin_content}

RULES:
1. Every fact MUST cite {linkedin} as source_url — it is the exact page where the
   fact was observed. source_type MUST be "linkedin". source_note MUST be "LinkedIn profile".
2. Report ONLY business-relevant, verifiable facts about THIS person's current role:
   job title, employer, industry, location, and any company-level signals visible on the
   profile (website, phone, office address, services, specialization). FROM THIS PROFILE ONLY.
3. Do NOT report personal-profile filler. NEVER report: connection or follower counts,
   education history (schools, degrees, alumni), certifications, honour-society or
   association memberships, or languages spoken.
4. Do NOT invent or infer. If nothing readable or nothing business-relevant is present,
   return {{"facts": []}}.
5. Never report facts about a DIFFERENT person who happens to appear in the content.
6. Return ONLY valid JSON matching this exact schema — no markdown, no commentary:

{{
  "facts": [
    {{
      "claim": "string — one factual statement observed on the profile",
      "source_url": "string — {linkedin}",
      "source_type": "string — linkedin",
      "confidence": "verified or unverified",
      "source_note": "string — LinkedIn profile"
    }}
  ]
}}

If the profile shows nothing readable, return {{"facts": []}}.
"""


def intent_timing_prompt(
    email: str,
    company_name: str,
    industry: str,
    facts_summary: str,
) -> str:
    """Stage 3 — buying intent + timing analysis (reasoned judgment)."""
    return f"""\
You are a preconstruction sales analyst for {get_profile().company_name}, a construction estimation company in {get_profile().location}.

WHO WE ARE (our ideal client + who is NOT our client):
{get_profile().prompt_context()}

TASK: Assess whether this company likely needs preconstruction/estimation services, and when. Return structured JSON.

INPUT:
- Email: {email}
- Company: {company_name}
- Industry: {industry}
- Known facts:
{facts_summary}

OUR BUSINESS:
- Sells {get_profile().what_we_sell}
- Ideal client: {get_profile().ideal_client}
- NOT our clients: companies in the excluded vertical (fiber/telecom/utility/
  road/pipeline/materials…) — never invent estimation need for them. If the
  company is one of those, say needs_estimation = "no".

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
4. EXACT-PAGE RULE (CRITICAL): when you cite a source_url it MUST point to the EXACT page the
   observation came from — NEVER a bare domain root. source_note = short human-readable WHERE
   (e.g. "LinkedIn company page", "news article", "bid board"). A bare-root citation is demoted
   to "unverified" downstream; statements that are your own reasoned inference keep source_url
   empty, source_type "inferred", confidence "unverified".
5. Return ONLY valid JSON matching this exact schema — no markdown, no commentary:

{{
  "needs_estimation": "yes | no | unknown",
  "signal": "string — key signal driving your assessment",
  "reason": "string — 1-2 sentence reasoning",
  "evidence": [
    {{
      "claim": "string — one supporting observation",
      "source_url": "string — EXACT page URL if available, else empty",
      "source_type": "string — homepage | about_page | contact_page | team_page | services_page | projects_page | careers_page | news | search_result | directory | linkedin | website_other | inferred | other",
      "confidence": "verified or unverified",
      "source_note": "string — exact page name or source description, empty for inferred"
    }}
  ],
  "timing_window": "now | soon | later | unknown",
  "timing_reason": "string — why this timing estimate",
  "timing_events": [
    {{
      "claim": "string — one event or signal about timing",
      "source_url": "string — EXACT page URL if available, else empty",
      "source_type": "string — homepage | about_page | contact_page | team_page | services_page | projects_page | careers_page | news | search_result | directory | linkedin | website_other | inferred | other",
      "confidence": "verified or unverified",
      "source_note": "string — exact page name or source description, empty for inferred"
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
You are a lead qualification analyst for {get_profile().company_name} ({get_profile().location} construction estimation services).

WHO WE ARE (our ideal client + who is NOT our client):
{get_profile().prompt_context()}

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

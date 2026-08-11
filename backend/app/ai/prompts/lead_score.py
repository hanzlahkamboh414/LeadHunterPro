"""AI prompt template for lead qualification."""

from __future__ import annotations

from typing import Any


def _render_value(value: Any) -> str:
    """Render a supplied evidence value for the prompt."""
    if isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
        return ", ".join(items)
    return str(value).strip()


def _format_evidence(company: dict[str, Any]) -> str:
    """Render only the supplied company evidence, labeled, skipping blanks."""
    fields: list[tuple[str, str]] = [
        ("Company name", "title"),
        ("Company name", "company_name"),
        ("Website", "website"),
        ("City", "city"),
        ("State", "state"),
        ("Trade category", "trade_category"),
        ("Industry focus", "industry_focus"),
        ("Description", "description"),
        ("Source", "source"),
        ("Source URL", "source_url"),
        ("Emails", "emails"),
        ("Phones", "phones"),
        ("LinkedIn", "linkedin"),
        ("Contact page", "contact_page"),
        ("About page", "about_page"),
    ]
    lines: list[str] = []
    seen_labels: set[str] = set()
    for label, key in fields:
        if label in seen_labels:
            continue
        value = company.get(key)
        if value is None or value == "" or value == []:
            continue
        rendered = _render_value(value)
        if rendered:
            lines.append(f"- {label}: {rendered}")
            seen_labels.add(label)
    return "\n".join(lines) if lines else "(no company evidence supplied)"


def _format_intent_evidence(company: dict[str, Any]) -> str:
    """Render traceable buying-intent evidence items for the prompt.

    Only items with a non-empty ``source_url`` are evidence (lead schema hard
    rule #5 — an untraceable signal is NOT evidence). Each line keeps its
    public URL so the model can cite it in the justification.
    """
    items = company.get("intent_evidence") or []
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("source_url") or "").strip()
        if not url:
            continue  # untraceable signal — never presented as evidence
        ev_type = str(item.get("type") or "signal").strip()
        snippet = " ".join(str(item.get("snippet") or "").split())
        meta = ", ".join(
            filter(
                None,
                [
                    f"date: {item['date']}" if item.get("date") else "",
                    f"source: {item['source']}" if item.get("source") else "",
                ],
            )
        )
        line = f"- [{ev_type}] {url}"
        if snippet:
            line += f" - {snippet}"
        if meta:
            line += f" ({meta})"
        lines.append(line)
    return "\n".join(lines) if lines else "(no buying-intent evidence supplied)"


def build_lead_qualification_prompt(
    company: dict[str, Any],
    query: dict[str, Any] | None = None,
) -> str:
    """Build the prompt that asks the AI to qualify a B2B lead.

    The model evaluates lead QUALITY (fit, relevance, credibility, sales
    opportunity) from the SUPPLIED evidence only, and must return JSON only.
    """
    query = query or {}
    industry = str(query.get("industry") or "").strip()
    location = str(query.get("location") or "").strip()

    return f"""
You are an expert B2B lead-qualification analyst for The Best Estimator LLC,
a construction estimating and preconstruction services company.

Your task is to evaluate the QUALITY of the following sales lead — NOT merely
how much information is available about it.

TARGET QUERY (the context for this lead):
- Industry: {industry or "(none supplied)"}
- Location: {location or "(none supplied)"}

SUPPLIED COMPANY EVIDENCE (evaluate ONLY this — nothing else):
{_format_evidence(company)}

BUYING-INTENT EVIDENCE (evaluate ONLY this — never invent evidence or URLs):
{_format_intent_evidence(company)}

EVALUATE THE LEAD ON:
- Company relevance to the requested industry
- Location relevance to the requested location
- Company type / line of business
- Likely fit for The Best Estimator LLC (construction estimating / preconstruction)
- Construction / roofing relevance where applicable
- Decision-maker / contact evidence if present
- Estimating / preconstruction relevance
- Buying intent: how strongly the BUYING-INTENT EVIDENCE above shows an ACTIVE
  buying window (won a bid, current project, active hiring, expansion, news)
- Company credibility
- Available public contact information
- Overall sales opportunity

RULES:
- Evaluate ONLY the supplied company and buying-intent evidence above.
- NEVER invent people, emails, phone numbers, URLs, companies, projects,
  buying-intent signals, or any other facts. If evidence is absent, state that
  it is absent.
- "confidence" must reflect ONLY the supplied evidence: with NO traceable
  buying-intent evidence, confidence stays LOW (well below 90).
- "justification" MUST cite the specific evidence and source URLs actually used.
- Return ONLY valid JSON. No markdown, no code fences, no prose, no explanation.

Return exactly this JSON shape:
{{
  "score": 0,
  "qualified": false,
  "qualification": "one-sentence summary of fit for The Best Estimator LLC",
  "confidence": 0,
  "justification": "written reasoning citing the specific evidence source URLs",
  "reasons": ["short reason for a dimension"],
  "strengths": ["short evidence-backed strength"],
  "concerns": ["short gap or risk"]
}}

- "score": integer from 0 to 100 reflecting overall lead quality and fit.
- "qualified": boolean; whether this is a strong sales lead for The Best Estimator LLC.
- "confidence": integer from 0 to 100 — how confidently the supplied evidence
  shows the company is ACTIVELY buying. Evidence-backed, never invented.
- "justification": written reasoning that cites the SPECIFIC evidence source
  URLs (from BUYING-INTENT EVIDENCE) behind the confidence verdict.
- "reasons": the top evaluation dimensions and the evidence behind each.
- "strengths": evidence-backed strengths.
- "concerns": gaps, missing evidence, or risks.
"""


def build_lead_score_prompt(data: dict[str, Any]) -> str:
    """Backward-compatible wrapper for the legacy heuristic-AI score path.

    Delegates to :func:`build_lead_qualification_prompt`; the returned JSON
    still carries a ``score`` key that the legacy parser in
    ``CompanyScorer.calculate`` reads.
    """
    return build_lead_qualification_prompt(data)


def build_cold_email_prompt(context: dict) -> str:
    """Build a prompt for generating a personalised cold-email draft.

    Args:
        context: Company and contact context dictionary.

    Returns:
        Formatted prompt string.
    """
    return f"""
Write a concise B2B cold email to {{context.get("contact_name", "the hiring manager")}}
at {{context.get("company_name", "Company")}}.

Context: {context}

Keep it under 150 words. Professional tone.
"""

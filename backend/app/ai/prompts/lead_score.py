"""AI prompt template for lead scoring explanation."""


def build_lead_score_prompt(data: dict) -> str:
    """Build a prompt asking the AI to justify a company lead score.

    Args:
        data: Crawled company data.

    Returns:
        Formatted prompt string.
    """
    return f"""
Rate this company as a B2B sales lead on a scale of 0-100.

Data: {data}

Return a JSON object with keys: score (int), reason (str).
"""


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

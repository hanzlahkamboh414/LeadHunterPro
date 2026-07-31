"""AI prompt template for generating outreach cold emails."""


def build_cold_email_prompt(contact_name: str, company_name: str, context: dict) -> str:
    """Build a prompt for composing a personalised cold email.

    Args:
        contact_name: Name of the person to address.
        company_name: Name of the recipient's company.
        context: Additional context (industry, pain points, etc.).

    Returns:
        Formatted prompt string.
    """
    return f"""
Write a concise B2B cold email to {contact_name} at {company_name}.

Context: {context}

Keep it under 150 words. Professional, persuasive tone.
"""

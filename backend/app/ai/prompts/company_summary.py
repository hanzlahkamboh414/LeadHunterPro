"""AI prompt templates for company summarisation."""

from typing import Any


def build_company_summary_prompt(data: dict[str, Any]) -> str:
    """Build a system prompt for summarising a company from crawled data.

    Args:
        data: Crawled website data dictionary.

    Returns:
        A formatted prompt string.
    """
    return f"""
You are an expert B2B company research analyst.

Analyze the following company information.

Title:
{data.get("title", "")}

Description:
{data.get("description", "")}

Emails:
{data.get("emails", [])}

Phones:
{data.get("phones", [])}

LinkedIn:
{data.get("linkedin", [])}

Contact Page:
{data.get("contact_page", "")}

About Page:
{data.get("about_page", "")}

Return ONLY valid JSON in this format:

{{
    "company_name": "",
    "industry": "",
    "services": [],
    "target_market": [],
    "summary": "",
    "keywords": []
}}

Do not return markdown.
Do not explain anything.
Return JSON only.
"""

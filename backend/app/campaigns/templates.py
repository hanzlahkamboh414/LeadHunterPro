"""Campaign templates — {{placeholders}} rendered from dossier evidence ONLY.

The variables are plain ``{{name}}`` tokens. Known tokens fill from the
lead's dossier; a token with no value (or an unknown token) renders as ""
— an email must never go out with a literal "{{first_name}}" in it, and
(same principle as the AI personalization rule) nothing is ever invented to
fill a gap.
"""

from __future__ import annotations

import re

from app.lead_research.models import LeadDossier

_TOKEN = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


def context_for(dossier: LeadDossier) -> dict[str, str]:
    """The template variables for one lead, from verified dossier fields."""
    person = dossier.person.name or ""
    first = person.split()[0] if person else ""
    last = " ".join(person.split()[1:]) if person else ""
    return {
        "first_name": first,
        "last_name": last,
        "person_name": person,
        "role": dossier.person.role or "",
        "company_name": dossier.company.name or "",
        "company": dossier.company.name or "",
        "location": dossier.company.location or "",
        "domain": dossier.domain or "",
        "email": dossier.email,
    }


def render(template: str, context: dict[str, str]) -> str:
    """Fill every {{token}}. Unknown tokens and empty values both render as
    "" — honest gaps, never placeholders or invented facts."""
    def sub(m: re.Match) -> str:
        return context.get(m.group(1), "")
    return _TOKEN.sub(sub, template or "")

"""Region-based people extraction with role-anchored names and email binding.

Increment 2 rewrite of the old :class:`PeopleParser`. The old parser scanned
a whole page region for the FIRST capitalized phrase whenever it spotted a
title keyword — which routinely grabbed the company name ("Texas Skyline
Roofing") instead of the person standing next to it. This version:

1. anchors the person's name to the ROLE keyword in the region — the
   capitalized words immediately before the role (skipping role words,
   lead-ins, and connectors), else the first capitalized words after it — so
   a company name sitting earlier in the region no longer wins;
2. rejects noise/company-name phrases — the company name is auto-detected
   from the page (``og:site_name`` / ``<title>``) and overridable via
   ``company_name``;
3. binds every email found in the SAME region to the person, tiering each as
   ``person_bound`` (personal local-part) or ``format`` (generic mailbox —
   info@/contact@ can never be person_bound per the V1 schema);
4. emits :class:`PersonRecord` objects whose ``person`` is a ``LeadPerson``
   with ``role_relevance`` derived via ``role_is_plausibly_relevant`` and an
   honest ``unverified`` identity tier (rule #14: reuse the Lead schema).

Deterministic and fully offline — nothing here touches the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from app.email.email_cleaner import EMAIL_CLEAN_PATTERN, clean_emails
from app.engines.lead.lead_models import (
    EmailVerificationTier,
    LeadEmail,
    LeadPerson,
    PersonVerificationTier,
    is_generic_email_local_part,
    role_is_plausibly_relevant,
)

#: Region containers scanned for person cards. ``ul``/``main`` are excluded so
#: a whole team grid is NOT treated as one region — its ``li`` cards are.
REGION_TAGS = ("li", "div", "article", "section")

#: Regions shorter than this are micro-divs (icons, links); longer than this
#: are whole-page wrappers. Team cards sit comfortably in between.
MIN_REGION_LEN = 12
MAX_REGION_LEN = 700

#: Role/title vocabulary used for DETECTION (the schema's keyword list is for
#: RELEVANCE scoring — a different job, kept separate per rule #14).
TITLE_KEYWORDS = (
    "Chief Executive Officer",
    "Chief Operating Officer",
    "Chief Financial Officer",
    "Chief Estimator",
    "Vice President",
    "General Manager",
    "Operations Manager",
    "Project Manager",
    "Project Lead",
    "Managing Director",
    "Director",
    "Principal",
    "Partner",
    "President",
    "Owner",
    "Founder",
    "Co-Founder",
    "Estimator",
    "Superintendent",
    "Procurement",
    "Purchasing",
    "CEO",
    "COO",
    "CTO",
    "CFO",
    "VP",
    "GM",
    "PM",
)

#: Headings/phrases that look like a capitalized name but never are one.
NAME_NOISE = frozenset(
    {
        "our team",
        "meet our team",
        "meet the team",
        "the team",
        "leadership team",
        "our leadership",
        "management team",
        "our management",
        "our people",
        "our staff",
        "the staff",
        "about us",
        "about the company",
        "our company",
        "the company",
        "our story",
    }
)

#: Common connectors a real name never contains (exact whole-token match).
NAME_STOPWORDS = frozenset(
    {
        "the", "and", "our", "for", "with", "from", "by", "team",
        "of", "at", "to", "in", "we", "are", "a", "an",
    }
)

#: A person name: 2-4 capitalized words (e.g. "Maria Gomez").
NAME_PATTERN = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b")

#: A single capitalized word — the basic token of a person-name scan.
NAME_TOKEN_RE = re.compile(r"[A-Z][a-z]+")

#: An all-caps acronym/suffix (LLC, INC, TX) — a hard name boundary, since a
#: person's name never contains one mid-phrase.
ALL_CAPS_RE = re.compile(r"[A-Z]{2,}")

#: Capitalized words that lead into a sentence and are never part of a name
#: ("Contact Maria Gomez", "Reach Maria at ...").
NAME_LEADINS = frozenset(
    {
        "contact", "reach", "call", "email", "visit", "meet", "read",
        "join", "follow", "see", "view", "click",
    }
)

#: Adjective words that modify a role noun ("Marketing Director",
#: "Senior Estimator") — never part of the person's name.
ROLE_MODIFIERS = frozenset(
    {
        "marketing", "sales", "finance", "operations", "estimating",
        "procurement", "purchasing", "technical", "senior", "assistant",
        "executive", "general", "human", "accounting", "engineering",
        "quality", "safety", "commercial", "industrial", "residential",
    }
)

#: Whole words that mark a role word (title keyword or modifier) in a scan.
_ROLE_TOKEN_LOWER = frozenset(
    role.lower() for role in TITLE_KEYWORDS
) | ROLE_MODIFIERS

#: Precompiled whole-word role matchers (avoids "Owner" inside "Homeowner").
_ROLE_PATTERNS = tuple(
    (role, re.compile(r"\b" + re.escape(role) + r"\b", re.IGNORECASE))
    for role in TITLE_KEYWORDS
)


@dataclass
class PersonRecord:
    """One extracted person plus the emails bound to them from the same region."""

    person: LeadPerson
    emails: list[LeadEmail] = field(default_factory=list)
    region_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe shape (``person`` + bound ``emails``) for API exports."""
        return {
            "person": self.person.to_dict(),
            "emails": [e.to_dict() for e in self.emails],
        }


class PeopleParser:
    """Extract decision-makers from a page, binding region emails to each."""

    def extract_candidates(
        self,
        soup: BeautifulSoup,
        *,
        page_url: str = "",
        company_name: str = "",
    ) -> list[PersonRecord]:
        """Return every person found in ``soup`` as a :class:`PersonRecord`.

        Smaller regions are processed first so a team-card's tight email
        binding wins over any whole-page wrapper that also matched; the same
        (name, role) pair is emitted only once.
        """
        company = (company_name or "").strip() or self._site_name(soup)

        regions: list[str] = []
        for tag in soup.find_all(REGION_TAGS):
            text = tag.get_text(" ", strip=True)
            if not (MIN_REGION_LEN <= len(text) <= MAX_REGION_LEN):
                continue
            if not self._detect_role(text):
                continue
            if not NAME_PATTERN.search(text):
                continue
            regions.append(text)
        regions.sort(key=len)

        records: list[PersonRecord] = []
        seen: set[tuple[str, str]] = set()
        for text in regions:
            record = self._extract_from_text(text, page_url, company)
            if record is None:
                continue
            key = (record.person.name.lower(), record.person.role.lower())
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
        return records

    # -- region parsing ---------------------------------------------------

    def _extract_from_text(
        self,
        text: str,
        page_url: str,
        company_name: str,
    ) -> PersonRecord | None:
        role = self._detect_role(text)
        if not role:
            return None
        name = self._find_name_near_role(text, role.lower(), company_name)
        if not name:
            return None
        person = LeadPerson(
            name=name,
            role=role,
            role_relevance=role_is_plausibly_relevant(role),
            tier=PersonVerificationTier.unverified,
            source_url=page_url,
        )
        return PersonRecord(
            person=person,
            emails=self._bind_emails(text, page_url),
            region_text=text,
        )

    def _detect_role(self, text: str) -> str:
        """Longest whole-word title keyword present in the region (or "")."""
        best = ""
        for role, pattern in _ROLE_PATTERNS:
            if pattern.search(text) and len(role) > len(best):
                best = role
        return best

    def _find_name_near_role(
        self,
        text: str,
        role_lower: str,
        company_name: str,
    ) -> str:
        """The name closest to the role: the LAST capitalized phrase before
        it, else the FIRST after it — never a noise/company-name phrase."""
        role_idx = text.lower().find(role_lower)
        if role_idx == -1:
            return ""
        name = self._find_name_before(text, role_idx, company_name)
        if name:
            return name
        return self._find_name_after(text, role_idx + len(role_lower), company_name)

    def _find_name_before(self, text: str, end: int, company_name: str) -> str:
        """The capitalized words immediately before the role — skipping role
        words, lead-ins, and connectors; stopping at a stopword or acronym
        boundary. A greedy regex would swallow the role word ("Maria Gomez
        Owner"), so the name is assembled token-by-token instead."""
        words = re.findall(r"[A-Za-z]+", text[:end])
        collected: list[str] = []
        for tok in reversed(words):
            if NAME_TOKEN_RE.fullmatch(tok):
                low = tok.lower()
                if low in NAME_STOPWORDS:
                    break
                if low in NAME_LEADINS or low in _ROLE_TOKEN_LOWER:
                    continue
                collected.append(tok)
                if len(collected) == 4:
                    break
            elif ALL_CAPS_RE.fullmatch(tok):
                break  # LLC / INC / TX — a name never continues past one
        name = " ".join(reversed(collected))
        if self._is_plausible_name(name, company_name):
            return name
        return ""

    def _find_name_after(self, text: str, start: int, company_name: str) -> str:
        """The first capitalized words after the role — the name phrase ends
        at the first connector (lowercase word or acronym) once a name is
        underway."""
        words = re.findall(r"[A-Za-z]+", text[start:])
        collected: list[str] = []
        for tok in words:
            if NAME_TOKEN_RE.fullmatch(tok):
                low = tok.lower()
                if low in NAME_STOPWORDS:
                    break
                if low in NAME_LEADINS or low in _ROLE_TOKEN_LOWER:
                    continue
                collected.append(tok)
                if len(collected) == 4:
                    break
            elif collected:
                break
        name = " ".join(collected)
        if self._is_plausible_name(name, company_name):
            return name
        return ""

    def _is_plausible_name(self, name: str, company_name: str = "") -> bool:
        """A capitalized phrase that reads like a person's name, not a heading,
        role phrase, or the company name."""
        tokens = name.strip().split()
        if not 2 <= len(tokens) <= 4:
            return False
        lower = name.strip().lower()
        if lower in NAME_NOISE:
            return False
        if set(lower.split()) & NAME_STOPWORDS:
            return False
        if set(lower.split()) & _ROLE_TOKEN_LOWER:
            return False
        if company_name:
            company_lower = company_name.strip().lower()
            if company_lower in lower or lower in company_lower:
                return False
        return True

    # -- email binding ----------------------------------------------------

    def _bind_emails(self, text: str, page_url: str) -> list[LeadEmail]:
        """All emails in the region, tiered honestly by local-part.

        A personal-looking local-part (``m.gomez``) bound by region proximity
        is ``person_bound``; a generic mailbox (``info``/``contact``) is
        ``format`` and can never qualify on its own (V1 hard rule #3).
        """
        bound: list[LeadEmail] = []
        for email in clean_emails(EMAIL_CLEAN_PATTERN.findall(text)):
            local = email.split("@", 1)[0]
            tier = EmailVerificationTier.person_bound
            if is_generic_email_local_part(local):
                tier = EmailVerificationTier.format
            bound.append(LeadEmail(email=email, tier=tier, source_url=page_url))
        return bound

    # -- site-name guard --------------------------------------------------

    def _site_name(self, soup: BeautifulSoup) -> str:
        """Best-effort company name from the page itself (``og:site_name``,
        then the first capitalized phrase of ``<title>``). Used to keep the
        company name from being captured as a person."""
        for attr in ("property", "name"):
            meta = soup.find("meta", attrs={attr: "og:site_name"})
            if meta and meta.get("content", "").strip():
                return meta["content"].strip()
        if soup.title:
            match = NAME_PATTERN.search(soup.title.get_text(strip=True))
            if match:
                return match.group(1).strip()
        return ""

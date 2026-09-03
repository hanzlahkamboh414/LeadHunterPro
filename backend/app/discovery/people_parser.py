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

#: Marketing/heading/industry words a real person's name never contains.
#: These are the live-site (Inc11 Step A) phrases that were captured as
#: decision-makers — "Owned Dallas Since Honest", "Schedule No Obligation
#: Inspection", "You Back Same Day", "First Name Last Name" — plus the
#: headings they come from. A name walk STOPS at one (so a real name sitting
#: behind it — "...Inspection John Smith President" — is rescued instead of
#: swallowed), and :meth:`_is_plausible_name` rejects any phrase containing
#: one. Deliberately conservative: for Texas contractors, fabricating a fake
#: decision-maker is worse than occasionally missing a real "Austin Smith".
NAME_PROSE_WORDS = frozenset(
    {
        # Inc11 Step A evidence — captured verbatim as fake "people"
        "schedule", "obligation", "inspection", "honest", "since",
        "first", "last", "name", "you", "back", "same", "day",
        "request", "free", "tell", "start", "finish",
        # Number words never appear in a personal name
        "one", "two", "three", "four", "five",
        "six", "seven", "eight", "nine", "ten",
        # Marketing/heading vocabulary
        "call", "today", "learn", "more", "view", "read", "join",
        "become", "contact", "reach", "visit", "meet", "check", "see",
        "get", "book", "click", "talk", "speak", "save", "money",
        "fast", "quick", "trust", "guarantee", "guaranteed", "warranty",
        "proudly", "owned", "serve", "serving", "service", "services",
        "quality", "professional", "affordable", "reliable", "expert",
        "experience", "estimate", "estimates", "best", "top", "full",
        "new", "old", "your", "their", "no", "local", "family",
        "sons", "associates", "group",
        # Industry/company context — never part of a person's name
        "roofing", "construction", "company", "contractor", "contractors",
        "builder", "builders",
        # Texas geography — a heading ("Best Roofing In Dallas Texas") is
        # location prose, not a name (see the conservative note above)
        "texas", "dallas", "houston", "austin", "fort", "worth",
    }
)

#: Common given names, used as a POSITIVE name-likeness test: a decision-maker
#: must contain at least one of these. This ends the blacklist whack-a-mole —
#: "Roofer Whether", "Bathroom Remodel Cost" (Inc11 Step B-3 live evidence)
#: were prose that no word-list could fully predict. Requiring a known given
#: name rejects ALL such phrases at once while keeping every real person found
#: ("Brandon Barnett", "Chris Arrington", ...). Deliberate trade: a rare given
#: name may be missed — never fabricating a decision-maker wins over that.
COMMON_GIVEN_NAMES = frozenset(
    {
        "abigail", "abraham", "adam", "adrian", "adriana", "agnes", "aisha",
        "alan", "albert", "alejandro", "alex", "alexander", "alexandra",
        "alexis", "alfred", "alfredo", "alice", "alicia", "allan", "allen",
        "amanda", "amber", "amelia", "amy", "ana", "andrea", "andrew", "andy",
        "angela", "angelo", "anita", "ann", "anna", "annette", "anthony",
        "antonio", "april", "arlene", "arthur", "ashley", "audrey", "barbara",
        "barry", "beatrice", "becky", "belinda", "ben", "benjamin", "bernard",
        "bernice", "bert", "beth", "betty", "bill", "billy", "blake", "bob",
        "bobby", "brad", "bradley", "brandon", "brenda", "brent", "brett",
        "brian", "bridget", "brittany", "bruce", "bryan", "caitlin", "caleb",
        "calvin", "cameron", "candace", "carl", "carla", "carlos", "carol",
        "caroline", "carrie", "casey", "catherine", "cathy", "cedric", "cesar",
        "charles", "charlie", "cheryl", "chris", "christina", "christopher",
        "chuck", "cindy", "claire", "clara", "clarence", "clark", "claudia",
        "clayton", "clifford", "clint", "clinton", "cody", "colin", "collin",
        "colton", "connie", "conor", "corey", "corinne", "craig", "cristina",
        "crystal", "curtis", "cynthia", "dale", "damon", "dan", "dana",
        "daniel", "danielle", "danny", "darlene", "darrell", "darren",
        "daryl", "dave", "david", "dawn", "dean", "deborah", "debra",
        "delia", "denise", "dennis", "derek", "derrick", "devin", "diana",
        "diane", "dominick", "dominic", "don", "donald", "donna", "dora",
        "doreen", "doris", "dorothy", "doug", "douglas", "dulce", "dylan",
        "earl", "eddie", "edgar", "edith", "eduardo", "edward", "edwin",
        "eileen", "elaine", "elena", "elias", "elijah", "elizabeth", "ella",
        "ellen", "elmer", "elsa", "emily", "emma", "enrique", "eric", "erica",
        "erik", "erin", "ernest", "esteban", "esther", "ethan", "eugene",
        "eva", "evan", "evelyn", "faith", "felicia", "felipe", "felix",
        "fernando", "floyd", "frances", "francesco", "francis", "francisco",
        "frank", "franklin", "fred", "frederick", "gabriel", "gary", "gene",
        "george", "georgia", "gerald", "gerardo", "gina", "gilbert",
        "gilberto", "glenn", "gloria", "gordon", "grace", "grant", "greg",
        "gregory", "guadalupe", "gustavo", "guy", "gwen", "hannah", "harold",
        "harriet", "harry", "heather", "hector", "heidi", "helen", "henry",
        "herbert", "herman", "holly", "homer", "howard", "hugh", "hugo",
        "ian", "ingrid", "irene", "irma", "irving", "isabel", "isaac",
        "jack", "jackie", "jacob", "jacqueline", "jake", "james", "jamie",
        "jan", "jane", "janet", "janice", "jared", "jasmine", "jason",
        "javier", "jay", "jean", "jeff", "jeffrey", "jenna", "jennifer",
        "jeremy", "jermaine", "jerome", "jerry", "jess", "jesse", "jessica",
        "jesus", "jill", "jim", "jimmy", "joan", "joann", "joanna",
        "joanne", "joe", "joel", "joey", "john", "johnny", "jon",
        "jonathan", "jordan", "jorge", "jose", "joseph", "josh", "joshua",
        "josie", "juan", "judith", "judy", "julia", "julian", "julie",
        "julio", "justin", "karen", "karl", "kate", "katherine", "kathleen",
        "kathryn", "kathy", "katie", "kay", "keith", "kelly", "kelsey",
        "kendra", "kenneth", "kent", "kevin", "kim", "kimberly", "kirsten",
        "krista", "kristen", "kristin", "kristina", "kurt", "kyle", "lamar",
        "lance", "larry", "laura", "lauren", "lawrence", "leah", "lee",
        "leland", "leo", "leon", "leonard", "leslie", "lester", "lewis",
        "linda", "lindsay", "lionel", "lisa", "lloyd", "logan", "loren",
        "lorenzo", "lori", "lou", "louis", "louise", "lucas", "lucy",
        "luis", "luke", "lydia", "lyle", "lynn", "madison", "manuel",
        "marc", "marcus", "margaret", "maria", "marian", "marie", "marilyn",
        "marion", "mark", "marlene", "marlon", "martha", "martin", "marty",
        "marvin", "mary", "mason", "matthew", "maurice", "mauricio", "max",
        "megan", "melanie", "melinda", "melissa", "melvin", "meredith",
        "mia", "micah", "michael", "michele", "michelle", "miguel", "mike",
        "mildred", "miles", "miranda", "miriam", "mitchell", "molly",
        "monica", "morgan", "mya", "nadine", "nancy", "naomi", "nathan",
        "nathaniel", "neal", "nell", "neil", "nelly", "nelson", "nicholas",
        "nick", "nicole", "nina", "noah", "nolan", "norma", "norman",
        "omar", "orlando", "oscar", "owen", "pablo", "pamela", "patrick",
        "patricia", "patsy", "paul", "paula", "pedro", "peggy", "penny",
        "perry", "pete", "peter", "philip", "phillip", "phyllis", "pierre",
        "pilar", "quentin", "quinn", "rachel", "rafael", "ramon", "randall",
        "randolph", "randy", "raul", "raymond", "rebecca", "regina", "rene",
        "renata", "renee", "ricardo", "rich", "richard", "rick", "ricky",
        "rita", "rob", "robbie", "robert", "roberto", "robin", "rocco",
        "rodney", "roger", "roland", "roman", "ronald", "ronnie", "rosa",
        "rosemary", "ross", "roy", "ruben", "ruby", "russell", "ruth",
        "ryan", "sabrina", "sadie", "sally", "sam", "samantha", "samuel",
        "sandra", "sara", "sarah", "scott", "sean", "selena", "sergio",
        "seth", "shane", "shannon", "sharon", "shaun", "shawn", "sheila",
        "shelly", "sherri", "sherry", "shirley", "sidney", "silvia", "simon",
        "skylar", "sofia", "sonia", "sonya", "sophia", "spencer", "stacy",
        "stan", "stacey", "stanley", "stella", "stephanie", "stephen",
        "steve", "steven", "stewart", "stuart", "sue", "susan", "suzanne",
        "sydney", "sylvia", "tabitha", "tamara", "tammy", "tara", "taylor",
        "ted", "terence", "teresa", "terri", "terry", "theodore", "thomas",
        "tiara", "tim", "timothy", "tina", "tito", "tobias", "todd", "tom",
        "tomas", "tommy", "tony", "tonya", "tracey", "tracy", "travis",
        "trent", "trevor", "tricia", "trina", "trisha", "troy", "trudy",
        "tyler", "ulysses", "ursula", "valerie", "vanessa", "vaughn", "verna",
        "veronica", "victor", "victoria", "vincent", "virgil", "virginia",
        "vivian", "wade", "waldo", "wallace", "walter", "wanda", "warren",
        "wayne", "wendy", "wesley", "wilbert", "wilbur", "will", "william",
        "willie", "wilma", "wilson", "winifred", "wyatt", "yvonne", "zach",
        "zachary", "zoe",
    }
)

#: A person name: 2-4 capitalized words (e.g. "Maria Gomez").
NAME_PATTERN = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b")

#: A single capitalized word — the basic token of a person-name scan.
NAME_TOKEN_RE = re.compile(r"[A-Z][a-z]+")

#: An all-caps acronym/suffix (LLC, INC, TX) — a hard name boundary, since a
#: person's name never contains one mid-phrase.
ALL_CAPS_RE = re.compile(r"[A-Z]{2,}")

#: Hard sentence boundaries. A name walk STOPS at any of these, so prose in a
#: different sentence than the role can never leak into the name — the live-site
#: bug where "Owned Dallas Since Honest" and "Schedule No Obligation Inspection"
#: became decision-makers (the period was dropped and the tokens ran together).
#: Commas/colons/parens stay invisible so "John Smith, President" and
#: "Owner: John Smith" keep working.
_NAME_BOUNDARY_CHARS = ".!?|—–"

#: Tokenizer for a name walk: hyphenated/apostrophe words OR one hard boundary
#: char. Anything else (commas, colons, spaces) is dropped exactly as the old
#: ``re.findall(r"[A-Za-z]+", ...)`` dropped it — a name never ends mid-word.
_NAME_WALK_TOKEN = re.compile(
    r"[A-Za-z]+(?:['-][A-Za-z]+)*|["
    + "".join(re.escape(c) for c in _NAME_BOUNDARY_CHARS)
    + r"]"
)

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
        mailto_by_region = self._collect_mailto_by_region(soup)

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
            record = self._extract_from_text(
                text, page_url, company, mailto_by_region.get(text, ())
            )
            if record is None:
                continue
            key = (record.person.name.lower(), record.person.role.lower())
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
        return records

    def _collect_mailto_by_region(self, soup: BeautifulSoup) -> dict[str, list[str]]:
        """Map region text -> the ``mailto:`` emails inside it.

        Region-bound, so a mailbox in a NO-person region (a footer ``office@``)
        stays unbound — the existing contract. The anchor's TEXT may not contain
        the address ("Email Chris"), so the ``href`` itself is scanned: a mailto
        link is otherwise invisible to the parser and no email is ever extracted.
        """
        by_region: dict[str, list[str]] = {}
        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href", "").strip()
            if not href.lower().startswith("mailto:"):
                continue
            emails = clean_emails(EMAIL_CLEAN_PATTERN.findall(href))
            if not emails:
                continue
            parent = anchor.find_parent(REGION_TAGS)
            region_text = parent.get_text(" ", strip=True) if parent is not None else ""
            by_region.setdefault(region_text, []).extend(emails)
        return by_region

    # -- region parsing ---------------------------------------------------

    def _extract_from_text(
        self,
        text: str,
        page_url: str,
        company_name: str,
        region_mailto: tuple[str, ...] = (),
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
            emails=self._bind_emails(text, page_url, region_mailto),
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
        words, lead-ins, and connectors; stopping at a stopword, prose word,
        sentence boundary, or acronym. A greedy regex would swallow the role
        word ("Maria Gomez Owner"), so the name is assembled token-by-token."""
        words = _NAME_WALK_TOKEN.findall(text[:end])
        collected: list[str] = []
        for tok in reversed(words):
            if tok in _NAME_BOUNDARY_CHARS:
                break  # a different sentence/heading — not this person
            if NAME_TOKEN_RE.fullmatch(tok):
                low = tok.lower()
                if low in NAME_STOPWORDS or low in NAME_PROSE_WORDS:
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
        at a sentence boundary, connector, or prose word once a name is
        underway."""
        words = _NAME_WALK_TOKEN.findall(text[start:])
        collected: list[str] = []
        for tok in words:
            if tok in _NAME_BOUNDARY_CHARS:
                break  # a different sentence/heading — not this person
            if NAME_TOKEN_RE.fullmatch(tok):
                low = tok.lower()
                if low in NAME_STOPWORDS or low in NAME_PROSE_WORDS:
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
        if set(lower.split()) & (NAME_STOPWORDS | NAME_PROSE_WORDS):
            return False
        if set(lower.split()) & _ROLE_TOKEN_LOWER:
            return False
        # Inc11 Step B-2a — the name must actually LOOK like a person: at
        # least one token must be a known given name. Shape + blacklist alone
        # let "Roofer Whether" and "Bathroom Remodel Cost" through (live-run
        # evidence); a positive test rejects every prose variant at once.
        # Hyphenated names ("Juan-Carlos") are handled token-by-token.
        if not (set(re.findall(r"[a-z]+", lower)) & COMMON_GIVEN_NAMES):
            return False
        if company_name:
            company_lower = company_name.strip().lower()
            if company_lower in lower or lower in company_lower:
                return False
        return True

    # -- email binding ----------------------------------------------------

    def _bind_emails(
        self,
        text: str,
        page_url: str,
        extra_emails: tuple[str, ...] = (),
    ) -> list[LeadEmail]:
        """All emails in the region (plus its page-local ``mailto:`` hrefs,
        deduplicated), tiered honestly by local-part.

        A personal-looking local-part (``m.gomez``) bound by region proximity
        is ``person_bound``; a generic mailbox (``info``/``contact``) is
        ``format`` and can never qualify on its own (V1 hard rule #3).
        """
        bound: list[LeadEmail] = []
        for email in clean_emails(
            EMAIL_CLEAN_PATTERN.findall(text) + list(extra_emails)
        ):
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

"""Plan-holder / bid-holder PDF table extraction (lead-pipeline increment 1).

WHAT THIS SOLVES
----------------
Engineering firms publish "planholder lists" as PDFs: the contractors who
pulled plans for a specific project, with a named contact, an email and a
phone for each. That is a dense, pre-qualified feed of companies that are
provably bidding work right now — exactly the buying window the lead schema
is built around.

WHY TABLES AND NOT FLAT TEXT
----------------------------
``page.extract_text()`` on these documents interleaves the columns, so the
company name lands in the middle of somebody else's contact block::

    Adam Pfab / Office: 319-555-0100
    Rathje Construction Co. 01.03.2025      <- company between name and email
    apfab@rathjeconstruction.com Cell: ...

A line-oriented regex over that text pairs the wrong company with the wrong
person — silently, and in a way that looks like real data. So this parser is
TABLE-FIRST: ``page.extract_tables()`` returns properly separated cells, and
flat text is used only to count how many emails the document contains, as the
coverage check below.

WHY COLUMN ROLES ARE INFERRED FROM CONTENT, NOT FROM THE HEADER
---------------------------------------------------------------
The observed header is NOT column-aligned with its own data::

    row 0:  Contractor |  |               |  | Phone/Fax | Date Emailed or P/U
    row 1:             |  | Email/Contact |  |           |
                             ^ column 2        ...but the emails are in column 1

Mapping "the email column is the one headed Email/Contact" therefore resolves
to a permanently empty column and yields ZERO emails while reporting success —
the exact silent-failure shape CLAUDE.md §5/§6 exist to prevent. Roles are
instead assigned by what the cells actually CONTAIN (an ``@``, a 10-digit run,
a date), and the header is used only to recognise rows worth skipping. That
also means a different firm's template, with different labels or a different
column order, parses without a code change (§4: no per-provider hardcoding).

HONEST LIMITS, STATED UP FRONT
------------------------------
- A plan-holder list carries no job TITLE, so every ``LeadPerson`` here has an
  empty ``role`` and ``role_relevance=False``. These rows therefore CANNOT
  clear the V1 qualification gate on their own (hard rule #2) — they are a
  high-quality sourcing feed that still needs role enrichment downstream.
- Phone extensions (``ext.7``) are not captured; the base number is.
- A company or person whose value is not structurally a name/company (a fax
  number, a URL/domain, an email, a structured-row label) is BLANKED, never
  guessed at and never inferred from an email username (Inc 8). A row whose
  company is blanked is still emitted when it carries a contact, honestly,
  with ``company_name=""``.
- A company value that dominates its column (≥ 80 % of data rows) while
  other columns show row-level variation is treated as a document header,
  project name or template label and BLANKED (Inc 8 Phase C, F1 fix).
  Legitimate repeats (2-3 rows) never reach the threshold.
- A single-cell ``Name:``/``Email:`` field record (Inc 8 Phase D1) is the
  one layout where the REAL person and REAL company are explicitly present:
  ``Name: Eric Mills, iSqFt`` yields person "Eric Mills" and company "iSqFt",
  and the same-cell ``Email:`` is a person-bound binding. A company-only
  ``Name:`` ("Wisconsin Bid Network") yields NO person — the email stays
  unbound. A person is never inferred from an email username or domain.
- Scanned/image PDFs are reported as ``needs_ocr``, never as zero rows.

Deterministic and fully offline: this module takes BYTES. Fetching is a
separate concern, which keeps the parser unit-testable without the network.
``pdfplumber`` is imported at module level on purpose — it is a declared
dependency (``requirements.txt``), so its absence is a broken environment that
should fail loudly, not a data condition to be papered over with a status code.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import pdfplumber

from app.email.email_cleaner import (
    EMAIL_CLEAN_PATTERN,
    clean_emails,
    is_free_mail_domain,
)
from app.engines.lead.lead_models import (
    EmailVerificationTier,
    LeadEmail,
    LeadPerson,
    LeadPhone,
    PersonVerificationTier,
    PhoneVerificationTier,
    is_generic_email_local_part,
)
from app.engines.phone_engine import PHONE_PATTERN
from app.engines.verification.location_verifier import _extract_mentions

logger = logging.getLogger(__name__)

#: PERMISSIVE phone detector, used ONLY to count how many phone-like runs the
#: document contains — never to extract.
#:
#: WHY A SECOND PATTERN EXISTS HERE (rule #14 requires justifying duplication):
#: an audit that reuses the extractor's own regex cannot detect the extractor
#: being blind, because numerator and denominator fail together and the ratio
#: stays a reassuring 1.0. That is not a hypothetical — it is the defect this
#: constant was added to catch: ``phone_engine.PHONE_PATTERN`` accepts only
#: ``[\s-]`` between groups and cannot match a parenthesized number at all, so
#: a real document yielded 2 phones out of 32 while reporting a clean parse.
#: The denominator therefore has to be measured INDEPENDENTLY, with a pattern
#: deliberately looser than the one under audit.
#:
#: ``phone_engine.PHONE_PATTERN`` is intentionally left alone: four other call
#: sites share it, so changing it is its own increment with its own regression
#: proof, not a change smuggled in beside a new parser.
PHONE_AUDIT_PATTERN = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")

#: How many distinct unmatched phone SHAPES to report. Shapes are digit-masked
#: (``(NNN) NNN-NNNN``), so the report explains why extraction failed without
#: ever printing somebody's phone number.
MAX_REPORTED_PHONE_SHAPES = 5

#: The "Date Emailed or P/U" column: ``01.02.2025``, ``1/2/25``, ``01-02-2025``.
#: Deliberately cannot match a dashed phone number — ``\b`` plus the 1-2 digit
#: leading group means ``319-555-1234`` never parses as a date, which is what
#: keeps the date and phone column roles from stealing each other.
DATE_PATTERN = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b")

#: Labels that mark a row as table furniture rather than a plan holder. Checked
#: ONLY after a row is found to carry no email/phone/date, so a real company
#: called "… Contractors Inc." is never mistaken for the "Contractor" header.
HEADER_LABEL_TOKENS = (
    "contractor",
    "email/contact",
    "email / contact",
    "phone/fax",
    "phone / fax",
    "emailed or p/u",
    "date emailed",
    "plan holder",
    "planholder",
    "plan deposit",
    "bidder",
    "company name",
    "prequalification",
)

#: Phone-type labels seen in the Phone/Fax cell, e.g. ``Office: …``, ``Cell:…``.
PHONE_LABEL_PATTERN = re.compile(
    r"\b(office|cell|mobile|direct|fax|phone|tel|main|home)\b",
    re.IGNORECASE,
)

#: Words that are a column label, never a person. Guards the name residue left
#: after the email is subtracted from a contact cell.
NAME_STOP_WORDS = frozenset({"contact", "email", "name", "phone", "fax", "date"})

#: A structured-row label ("Name: John Address: …") marks a cell as a field
#: list — never a company name and never a person. Requires the colon so a
#: company called "Phone Home Contracting" is never rejected.
_FIELD_LABEL_PATTERN = re.compile(
    r"\b(?:name|address|phone|email|contact|fax|company|firm|city|state|zip|"
    r"website|url|project|bid)\s*:",
    re.IGNORECASE,
)

#: A URL or bare website domain embedded in a cell. Never a company name and
#: never a person. The TLD list is deliberately short (the observed
#: corruptions are .com / .net / .org); a name like "J.B. Hunt" has no
#: ``word.tld`` shape and a real company like "County Materials" has no dot.
_URL_OR_DOMAIN_PATTERN = re.compile(
    r"(?:https?://|www\.|\b[\w-]+\.(?:com|net|org|us|io|biz|info|co)\b)",
    re.IGNORECASE,
)

#: A 10-digit US phone (parentheses optional). A company column holding a phone
#: ("f: (432) 385-7280") is a mis-split, never a company.
_PHONE_IN_CELL_PATTERN = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")

#: Whole-value tokens that are a column label, never a company. Checked ONLY
#: against the entire value, so "Contractors Inc." is never touched.
_NON_COMPANY_TOKENS = frozenset(
    {
        "contractor", "contractors", "bidder", "bidders", "plan holder",
        "planholder", "plan holders", "email", "email/contact", "phone",
        "phone/fax", "fax", "name", "address", "contact", "date", "company",
        "company name", "firm", "prequalification", "pre-qualification",
    }
)

#: A single-cell structured field record label (Inc 8 Phase D1). Anchored to
#: the line start so the cudahywi-style ``Name: X`` / ``Email: y`` lines parse
#: deterministically. The trailing ``(.*)`` captures the field value.
_FIELD_RECORD_LABEL = re.compile(
    r"^\s*(name|address|phone|email|fax)\s*:\s*(.*)$", re.IGNORECASE
)

#: Tokens that mark a phrase as a COMPANY, not a person. Used only to validate
#: the person half of a ``Name: <person>, <company>`` comma split (Inc 8 D1).
#: A candidate containing any of these is treated as company-only, so the
#: comma is not a false person split ("Wisconsin Bid Network" is never a
#: person; "County Materials" is never a person).
_COMPANY_INDICATOR_TOKENS = frozenset(
    {
        "construction", "contractors", "contracting", "builders", "building",
        "excavating", "excavation", "materials", "network", "county", "city",
        "association", "asphalt", "pavement", "paving", "company", "companies",
        "incorporated", "corp", "corporation", "llc", "ltd", "group",
        "industries", "services", "supply", "supplies", "concrete", "roofing",
        "masonry", "engineering", "architects", "developers", "realty",
        "properties", "exchange", "authority",
    }
)

#: Below this many characters per page the document has no usable text layer,
#: i.e. it is a scan. Reported as ``needs_ocr`` — OCR is a later increment.
MIN_TEXT_CHARS_PER_PAGE = 25

#: Longest plausible person name; anything longer is a mis-split cell.
MAX_NAME_CHARS = 60

#: Below this captured/present email ratio the template has probably drifted.
#: NOT 1.0 on purpose: the page-1 letterhead block (the issuing firm's own
#: contact) contributes emails that are correctly NOT plan-holder rows, so a
#: healthy parse of a real document sits comfortably under 100%.
COVERAGE_WARN_RATIO = 0.70

# Parse outcomes. Strings rather than an Enum so they survive JSON export and
# log greps unchanged, matching the ``str, Enum`` values used elsewhere.
STATUS_OK = "ok"
STATUS_NEEDS_OCR = "needs_ocr"
STATUS_UNREADABLE = "unreadable_pdf"
STATUS_NO_TABLE = "no_table_found"
STATUS_COVERAGE_LOW = "coverage_low"


@dataclass
class _ColumnRoles:
    """Which column index carries which field, inferred from cell content."""

    company: int | None = None
    contact: int | None = None
    phone: int | None = None
    date: int | None = None

    @property
    def usable(self) -> bool:
        """True when the table has both a company and a contactable column.

        A table with no contact-bearing column is furniture — the wrapped
        header that pdfplumber reports as its own 1-column table.
        """
        return self.company is not None and (
            self.contact is not None or self.phone is not None
        )


@dataclass
class PlanHolderRow:
    """One plan holder: the company plus whoever was listed against it.

    ``person`` is None when the cell held an email but no readable name — the
    row is still useful (the company is real) and is kept, honestly, without a
    fabricated contact.
    """

    company: str
    person: LeadPerson | None = None
    emails: list[LeadEmail] = field(default_factory=list)
    phones: list[LeadPhone] = field(default_factory=list)
    #: Phone type per entry in ``phones``, same index ("office", "cell", "").
    phone_labels: list[str] = field(default_factory=list)
    date_contacted: str = ""
    #: Every address is consumer/ISP mail, so this row yields NO company
    #: domain. Downstream must not treat ``aol.com`` as the company website.
    free_mail_only: bool = False
    #: The email local-part echoes the contact's name (``arnold`` <- Arnold).
    #: False is common and NOT a rejection: shared estimating inboxes are
    #: legitimately listed under a named person.
    local_part_matches_name: bool = False
    page: int = 0
    source_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe shape for API export and diagnostics."""
        return {
            "company": self.company,
            "person": self.person.to_dict() if self.person else None,
            "emails": [e.to_dict() for e in self.emails],
            "phones": [
                {**phone.to_dict(), "label": label}
                for phone, label in zip(self.phones, self.phone_labels, strict=True)
            ],
            "date_contacted": self.date_contacted,
            "free_mail_only": self.free_mail_only,
            "local_part_matches_name": self.local_part_matches_name,
            "page": self.page,
            "source_url": self.source_url,
        }


@dataclass
class PlanHolderParseReport:
    """Why the parse produced what it produced (CLAUDE.md §6 logging standard).

    Every field here exists so that a bad run is diagnosable from the report
    alone. ``coverage_ratio`` is the template-drift alarm: if a firm changes
    its layout, emails stay present in the text while rows stop being built,
    so coverage collapses and the status turns ``coverage_low`` instead of
    quietly returning fewer leads.
    """

    parse_status: str = STATUS_OK
    pages: int = 0
    text_chars: int = 0
    tables_seen: int = 0
    tables_used: int = 0
    tables_discarded: int = 0
    rows_extracted: int = 0
    rows_rejected: int = 0
    emails_in_document: int = 0
    emails_captured: int = 0
    #: Distinct US state CODES (2-letter) the document text explicitly names —
    #: computed once here so the plan-holder SOURCE can gate parsed documents by
    #: query region (a document that names a non-target state is evidence the
    #: list is out-of-region). Empty when the document names no state at all.
    state_codes: list[str] = field(default_factory=list)
    coverage_ratio: float = 0.0
    #: Phone equivalents of the email coverage fields. Measured with
    #: ``PHONE_AUDIT_PATTERN``, NOT with the extractor's own regex, so that an
    #: extractor that cannot see a format still shows up as a shortfall here.
    phones_in_document: int = 0
    phones_captured: int = 0
    phone_coverage_ratio: float = 0.0
    #: Digit-masked shapes (``(NNN) NNN-NNNN``) of phone-like runs present in
    #: the text that extraction did NOT capture. This is the actionable half of
    #: the alarm: it names the format the extractor is blind to.
    unmatched_phone_shapes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True only for a clean parse that produced rows."""
        return self.parse_status == STATUS_OK

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe shape for API export and diagnostics."""
        return {
            "parse_status": self.parse_status,
            "pages": self.pages,
            "text_chars": self.text_chars,
            "tables_seen": self.tables_seen,
            "tables_used": self.tables_used,
            "tables_discarded": self.tables_discarded,
            "rows_extracted": self.rows_extracted,
            "rows_rejected": self.rows_rejected,
            "emails_in_document": self.emails_in_document,
            "emails_captured": self.emails_captured,
            "state_codes": list(self.state_codes),
            "coverage_ratio": round(self.coverage_ratio, 4),
            "phones_in_document": self.phones_in_document,
            "phones_captured": self.phones_captured,
            "phone_coverage_ratio": round(self.phone_coverage_ratio, 4),
            "unmatched_phone_shapes": list(self.unmatched_phone_shapes),
            "reasons": list(self.reasons),
        }


@dataclass
class PlanHolderParseResult:
    """Extracted rows plus the report explaining the run."""

    rows: list[PlanHolderRow] = field(default_factory=list)
    report: PlanHolderParseReport = field(default_factory=PlanHolderParseReport)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe shape for API export and diagnostics."""
        return {
            "rows": [r.to_dict() for r in self.rows],
            "report": self.report.to_dict(),
        }


class PdfPlanHolderParser:
    """Extract plan-holder rows from a plan-holder / bid-holder list PDF."""

    def parse(
        self, pdf_bytes: bytes, *, source_url: str = ""
    ) -> PlanHolderParseResult:
        """Parse ``pdf_bytes`` into plan-holder rows plus a report.

        Never raises on bad input and never returns an unexplained empty list:
        a scan, a malformed file or a drifted template each produce a distinct
        ``parse_status`` with a human-readable reason attached.

        Args:
            pdf_bytes: The raw PDF payload.
            source_url: Where the PDF came from, recorded on every emitted
                record so each email/phone stays traceable (hard rule #5).

        Returns:
            A :class:`PlanHolderParseResult`.
        """
        report = PlanHolderParseReport()

        if not pdf_bytes:
            report.parse_status = STATUS_UNREADABLE
            report.reasons.append("empty byte payload - nothing to parse")
            self._log(report, source_url)
            return PlanHolderParseResult(report=report)

        try:
            page_texts, tables_by_page = self._read_document(pdf_bytes)
        except Exception as exc:  # noqa: BLE001 - a bad PDF reports, never crashes
            report.parse_status = STATUS_UNREADABLE
            report.reasons.append(f"pdfplumber could not read the document: {exc}")
            self._log(report, source_url)
            return PlanHolderParseResult(report=report)

        full_text = "\n".join(page_texts)
        report.pages = len(page_texts)
        report.text_chars = len(full_text)
        report.emails_in_document = len(
            set(clean_emails(EMAIL_CLEAN_PATTERN.findall(full_text)))
        )
        document_phones = self._document_phones(full_text)
        report.phones_in_document = len(document_phones)
        # Expose the US states this document names (if any) so the source can
        # gate parsed rows by query region. Reuses the shared mention extractor,
        # so the source and this report always agree on what counts (§14).
        report.state_codes = sorted({
            code for _, code in _extract_mentions(full_text)[0] if code
        })

        if report.pages and report.text_chars < MIN_TEXT_CHARS_PER_PAGE * report.pages:
            report.parse_status = STATUS_NEEDS_OCR
            report.reasons.append(
                f"only {report.text_chars} chars of text across {report.pages} "
                "page(s) - almost certainly a scanned/image PDF, which needs OCR"
            )
            self._log(report, source_url)
            return PlanHolderParseResult(report=report)

        rows: list[PlanHolderRow] = []
        for page_number, tables in tables_by_page:
            for table in tables:
                report.tables_seen += 1
                table_rows = self._rows_from_table(
                    table, page_number, source_url, report
                )
                if table_rows:
                    report.tables_used += 1
                    rows.extend(table_rows)
                else:
                    report.tables_discarded += 1

        self._finalize(rows, report, source_url, document_phones)
        return PlanHolderParseResult(rows=rows, report=report)

    # -- document reading --------------------------------------------------

    def _read_document(
        self, pdf_bytes: bytes
    ) -> tuple[list[str], list[tuple[int, list[list[list[str | None]]]]]]:
        """Pull the text and the raw tables out of every page, in order."""
        page_texts: list[str] = []
        tables_by_page: list[tuple[int, list[list[list[str | None]]]]] = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                page_texts.append(page.extract_text() or "")
                tables_by_page.append((page_number, page.extract_tables() or []))
        return page_texts, tables_by_page

    def _document_phones(self, text: str) -> dict[str, str]:
        """Every phone-like run in the text, as ``{digits: digit-masked shape}``.

        Keyed on digits so one number written two ways counts once; valued with
        its shape (``(NNN) NNN-NNNN``) so an unmatched format can be named in
        the report without ever printing somebody's number.
        """
        found: dict[str, str] = {}
        for match in PHONE_AUDIT_PATTERN.findall(text):
            digits = re.sub(r"\D", "", match)
            if len(digits) < 10:
                continue
            found.setdefault(digits, re.sub(r"\d", "N", match))
        return found

    def _finalize(
        self,
        rows: list[PlanHolderRow],
        report: PlanHolderParseReport,
        source_url: str,
        document_phones: dict[str, str],
    ) -> None:
        """Fill in the counts, decide the status, and log the whole picture."""
        report.rows_extracted = len(rows)

        captured_emails = {e.email for row in rows for e in row.emails}
        report.emails_captured = len(captured_emails)
        if report.emails_in_document:
            report.coverage_ratio = report.emails_captured / report.emails_in_document

        captured_digits = {
            re.sub(r"\D", "", phone.phone) for row in rows for phone in row.phones
        }
        report.phones_captured = len(captured_digits)
        if report.phones_in_document:
            report.phone_coverage_ratio = (
                report.phones_captured / report.phones_in_document
            )
        report.unmatched_phone_shapes = sorted(
            {
                shape
                for digits, shape in document_phones.items()
                if digits not in captured_digits
            }
        )[:MAX_REPORTED_PHONE_SHAPES]

        if not rows:
            report.parse_status = STATUS_NO_TABLE
            report.reasons.append(
                f"{report.tables_seen} table(s) found, none produced a usable row - "
                "the document has a text layer, so this is a layout/template "
                "mismatch rather than a scan"
            )
            self._log(report, source_url)
            return

        # Email and phone shortfalls are checked INDEPENDENTLY: either one on its
        # own is enough to make the parse untrustworthy, and reporting only the
        # first would hide the second.
        if report.emails_in_document and report.coverage_ratio < COVERAGE_WARN_RATIO:
            report.parse_status = STATUS_COVERAGE_LOW
            report.reasons.append(
                f"captured {report.emails_captured} of "
                f"{report.emails_in_document} emails present in the text "
                f"({report.coverage_ratio:.0%}) - likely template drift, "
                "inspect the document before trusting these rows"
            )

        if (
            report.phones_in_document
            and report.phone_coverage_ratio < COVERAGE_WARN_RATIO
        ):
            report.parse_status = STATUS_COVERAGE_LOW
            shapes = ", ".join(report.unmatched_phone_shapes) or "(none recorded)"
            report.reasons.append(
                f"captured {report.phones_captured} of "
                f"{report.phones_in_document} phone numbers present in the text "
                f"({report.phone_coverage_ratio:.0%}) - extraction is blind to at "
                f"least one format. Unmatched shapes: {shapes}"
            )

        self._log(report, source_url)

    # -- table -> rows -----------------------------------------------------

    def _rows_from_table(
        self,
        table: list[list[str | None]],
        page_number: int,
        source_url: str,
        report: PlanHolderParseReport,
    ) -> list[PlanHolderRow]:
        """Turn one pdfplumber table into rows, or return [] if it is furniture."""
        grid = self._drop_empty_columns(self._normalize_grid(table))
        if not grid:
            return []

        roles = self._infer_column_roles(grid)
        if not roles.usable:
            return []

        # Detect a company column dominated by a single repeated value (e.g.
        # a document header or project name stamped on every row).  When
        # detected, blank the company cell for affected rows so the garbage
        # value does not become company_name.  The row is still emitted when
        # it carries a contact — honestly, with company_name="".
        repeated_indices = self._detect_repeated_company_values(grid, roles)
        if repeated_indices and roles.company is not None:
            for idx in repeated_indices:
                grid[idx][roles.company] = ""

        rows: list[PlanHolderRow] = []
        for raw_row in grid:
            if not self._row_has_signal(raw_row) or self._is_header_row(raw_row):
                continue
            row = self._build_row(raw_row, roles, page_number, source_url)
            if row is None:
                report.rows_rejected += 1
                continue
            rows.append(row)
        return rows

    def _normalize_grid(self, table: list[list[str | None]]) -> list[list[str]]:
        """None -> "", normalize hard spaces, trim edges. Newlines are KEPT:
        they carry the ``Office:``/``Cell:`` split inside a phone cell and the
        ``Name /`` / email split inside a contact cell."""
        grid: list[list[str]] = []
        for raw_row in table or []:
            grid.append(
                [(cell or "").replace("\xa0", " ").strip() for cell in raw_row or []]
            )
        return grid

    def _drop_empty_columns(self, grid: list[list[str]]) -> list[list[str]]:
        """Pad rows to equal width and drop columns that are empty everywhere.

        The observed tables carry two permanently blank columns; removing them
        before role inference keeps "leftmost column with text" meaningful.
        """
        if not grid:
            return []
        width = max((len(row) for row in grid), default=0)
        if not width:
            return []
        padded = [[*row, *[""] * (width - len(row))] for row in grid]
        keep = [i for i in range(width) if any(row[i] for row in padded)]
        if not keep:
            return []
        return [[row[i] for i in keep] for row in padded]

    def _infer_column_roles(self, grid: list[list[str]]) -> _ColumnRoles:
        """Assign column roles by what the DATA rows contain, not by header text.

        Order matters: ``@`` is unambiguous so the contact column is claimed
        first, then a 10-digit run, then a date; the company is whatever
        leftmost column still holds text. Each role excludes the ones already
        taken, so no column is used twice.
        """
        width = len(grid[0]) if grid else 0
        email_hits = [0] * width
        phone_hits = [0] * width
        date_hits = [0] * width
        text_hits = [0] * width

        for row in grid:
            if not self._row_has_signal(row) or self._is_header_row(row):
                continue
            for index, cell in enumerate(row):
                if not cell:
                    continue
                text_hits[index] += 1
                if EMAIL_CLEAN_PATTERN.search(cell):
                    email_hits[index] += 1
                if self._has_phone(cell):
                    phone_hits[index] += 1
                if DATE_PATTERN.search(cell):
                    date_hits[index] += 1

        taken: set[int] = set()
        contact = self._best_column(email_hits, taken)
        phone = self._best_column(phone_hits, taken)
        date = self._best_column(date_hits, taken)
        company = next(
            (i for i in range(width) if text_hits[i] and i not in taken),
            None,
        )
        return _ColumnRoles(
            company=company, contact=contact, phone=phone, date=date
        )

    def _best_column(self, hits: list[int], taken: set[int]) -> int | None:
        """Index of the highest-scoring unclaimed column, leftmost on a tie.

        Mutates ``taken`` so the caller's later roles cannot reuse the column.
        """
        best: int | None = None
        best_score = 0
        for index, score in enumerate(hits):
            if index in taken or score <= 0:
                continue
            if score > best_score:
                best, best_score = index, score
        if best is not None:
            taken.add(best)
        return best

    # -- table-level repeated-value detection -------------------------------

    def _detect_repeated_company_values(
        self, grid: list[list[str]], roles: _ColumnRoles
    ) -> set[int]:
        """Identify rows whose company cell is a repeated document-level value.

        A single value that dominates the company column (≥ 80 % of data rows)
        while other columns show row-level variation is strong structural
        evidence that the value is a document header, project name or template
        label — not a per-row company name.

        The threshold is deliberately strict: it only fires when the same
        *non-empty* value appears in nearly every row.  Legitimate repeats
        (the same company bidding on multiple projects, maybe 2-3 rows out of
        9) never reach 80 %, so they are preserved.

        Returns the set of **row indices** whose company value should be
        blanked.  An empty set means nothing was detected.
        """
        if roles.company is None:
            return set()

        # Collect the raw (pre-sanitized) company cell for each data row.
        company_cells: list[tuple[int, str]] = []
        for idx, row in enumerate(grid):
            if not self._row_has_signal(row) or self._is_header_row(row):
                continue
            cell = self._flatten(self._cell(row, roles.company)).strip()
            if cell:
                company_cells.append((idx, cell))

        if len(company_cells) < 3:
            # Fewer than 3 data rows: not enough signal for a reliable call.
            return set()

        # Frequency of the most common non-empty company value.
        from collections import Counter

        value_counts = Counter(cell for _, cell in company_cells)
        most_common_value, most_common_count = value_counts.most_common(1)[0]
        data_row_count = len(company_cells)

        if most_common_count / data_row_count < 0.80:
            return set()

        # Structural confirmation: other columns must show row-level variation.
        # If every column is the same repeated value the table is a single-cell
        # furniture block (already caught by ``_is_header_row`` in most cases),
        # not a real plan-holder table.  We only blank the company column when
        # the *other* columns vary — confirming the repeat is a document-level
        # artefact sitting alongside real per-row data.
        other_columns = [
            col
            for col in range(len(grid[0]) if grid else 0)
            if col != roles.company
        ]
        if not other_columns:
            return set()

        for col in other_columns:
            col_values = [
                self._flatten(self._cell(row, col)).strip()
                for row_idx, _ in company_cells
                for row in [grid[row_idx]]
            ]
            unique_in_col = len(set(v for v in col_values if v))
            if unique_in_col > 1:
                # At least one other column varies → structural evidence is met.
                return {
                    row_idx
                    for row_idx, cell in company_cells
                    if cell == most_common_value
                }

        return set()

    # -- row classification ------------------------------------------------

    def _row_has_signal(self, row: list[str]) -> bool:
        """True when the row carries an email, a phone or a date."""
        return any(
            "@" in cell or self._has_phone(cell) or DATE_PATTERN.search(cell)
            for cell in row
            if cell
        )

    def _is_header_row(self, row: list[str]) -> bool:
        """True for a blank row or one whose text is a column label.

        Only consulted for rows with no email/phone/date, so a contractor
        named "… Contractors" can never be swallowed as the "Contractor"
        header.
        """
        joined = " ".join(cell for cell in row if cell).lower()
        if not joined:
            return True
        return any(token in joined for token in HEADER_LABEL_TOKENS)

    def _has_phone(self, text: str) -> bool:
        """True when ``text`` holds a number with at least 10 digits.

        Reuses ``phone_engine.PHONE_PATTERN`` and its 10-digit rule rather than
        adding a sixth phone regex to this repo (rule #14). The wrapper
        ``extract_phones`` is not reused because it sorts and de-dupes, which
        would destroy the positions needed to attach ``Office:``/``Cell:``.
        Parenthesized area codes and dots between digit groups are normalized
        to hyphens first (see :meth:`_normalize_phone_separators`) so formats
        like ``(319) 555-0101`` and ``319.842.2130`` score as phones.
        """
        return any(
            self._digit_count(m) >= 10
            for m in PHONE_PATTERN.findall(self._normalize_phone_separators(text))
        )

    @staticmethod
    def _digit_count(text: str) -> int:
        """How many digits are in ``text``."""
        return sum(1 for char in text if char.isdigit())

    def _sanitize_company(self, company: str) -> str:
        """Return a usable company name, or "" for a cell that is not one.

        Rejects the failure shapes the live PDFs produced — a fax number
        ("f: (432) 385-7280"), a URL/domain ("https: www.fcgcorpo.com"), an
        email fragment, a structured-row label ("Name: … Address: …"), a bare
        column label and a too-short residue. Rejection is by pattern, never
        by inference: a bad value is BLANKED, never guessed at (Inc 8 rule:
        never derive a company from an email domain), and a real company that
        merely looks unusual ("AB Inc") is preserved.
        """
        # Whitespace-only trim: trailing periods are legitimate in real company
        # names ("Co.", "Inc.", "Corp."), so they are preserved, never stripped.
        value = re.sub(r"\s+", " ", company).strip()
        if not value:
            return ""
        if "@" in value:
            return ""  # an email address, not a company
        if _URL_OR_DOMAIN_PATTERN.search(value):
            return ""  # a website/domain, not a company
        if _PHONE_IN_CELL_PATTERN.search(value):
            return ""  # a phone/fax, not a company
        if _FIELD_LABEL_PATTERN.search(value):
            return ""  # a structured-row label, not a company
        if len(re.sub(r"[^A-Za-z0-9]", "", value)) < 2:
            return ""  # nothing name-like survives
        if value.lower() in _NON_COMPANY_TOKENS:
            return ""  # a bare column label, not a company
        return value

    # -- structured field-record parsing (Inc 8 Phase D1) -------------------

    def _parse_structured_field_record(
        self, cell: str
    ) -> dict[str, str] | None:
        """Parse a ``Name:``/``Email:`` single-cell field record, or None.

        The cudahywi plan-holder PDF packs one holder into ONE multi-line cell::

            Name: Eric Mills, iSqFt
            Address:
            Phone: (P)
            Email: emills@isqft.com

        Returns ``{"person": ..., "company": ...}`` when the cell carries BOTH
        a ``Name:`` and an ``Email:`` label (the two labels together confirm the
        cell is a field record, not incidental text). Person/company come from
        the ``Name:`` value; emails are still harvested from the whole cell by
        the caller. Returns None for any other layout (the HR Green
        ``"Name /"``/email cells, pipe cells, etc.).

        The person is only ever what the ``Name:`` field literally says —
        never the email username. ``Name: Wisconsin Bid Network`` yields
        ``person=""`` (a company, no person), not "Sam" from ``sam@napc.me``.
        """
        name_value: str | None = None
        email_seen = False
        for line in cell.splitlines():
            m = _FIELD_RECORD_LABEL.match(line)
            if not m:
                continue
            label = m.group(1).lower()
            value = m.group(2).strip()
            if label == "name" and name_value is None:
                name_value = value
            elif label == "email":
                email_seen = True
        if name_value is None or not email_seen:
            return None
        person, company = self._split_name_field(name_value)
        return {"person": person, "company": company}

    def _split_name_field(self, value: str) -> tuple[str, str]:
        """Split a ``Name:`` value into (person, company), deterministically.

        ``"Eric Mills, iSqFt"`` -> ``("Eric Mills", "iSqFt")``. ``"Jim,
        Reesman's Excavating"`` -> ``("Jim", "Reesman's Excavating")``. A value
        with no comma is company-only: ``"Wisconsin Bid Network"`` ->
        ``("", "Wisconsin Bid Network")``.

        The comma is only honoured when BOTH halves validate: the left is a
        person-shaped name and the right is a valid company. Otherwise the
        comma is ambiguous and we refuse to guess a person (blank over
        incorrect) — the whole value is kept as the company.

        Firm-name pattern: a right half containing "&" ("Smith, Jones &
        Associates") reads as a surname + firm, not a person — that split is
        refused so no person is guessed.
        """
        value = value.strip()
        if "," not in value:
            return "", value
        left, right = value.split(",", 1)
        person_candidate = left.strip()
        company_candidate = right.strip()
        if (
            "&" in company_candidate  # firm-name pattern -> ambiguous
            or not self._looks_like_person_name(person_candidate)
            or not self._sanitize_company(company_candidate)
        ):
            return "", value
        return person_candidate, company_candidate

    def _looks_like_person_name(self, candidate: str) -> bool:
        """True when ``candidate`` is a person-shaped name, not a company.

        A person candidate is 1-3 alphabetic tokens (apostrophes/hyphens
        allowed), passes the Phase A name validation, and contains NO company
        indicator token ("Construction", "Network", "County", …). This keeps
        "Wisconsin Bid Network" and "County Materials" out of the person slot
        without ever consulting an email username or domain.
        """
        if not self._validate_person_name(candidate):
            return False
        tokens = candidate.split()
        if not (1 <= len(tokens) <= 3):
            return False
        for token in tokens:
            if token.lower() in _COMPANY_INDICATOR_TOKENS:
                return False
            if not re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", token):
                return False
        return True

    # -- row construction --------------------------------------------------

    def _build_row(
        self,
        raw_row: list[str],
        roles: _ColumnRoles,
        page_number: int,
        source_url: str,
    ) -> PlanHolderRow | None:
        """Build one row, or None when it is not an actionable plan holder."""
        company = self._sanitize_company(
            self._flatten(self._cell(raw_row, roles.company))
        )

        contact_cell = self._cell(raw_row, roles.contact)
        # Primary source is the contact column; the whole row is the fallback so
        # a stray address in another column still counts toward coverage.
        addresses = clean_emails(EMAIL_CLEAN_PATTERN.findall(contact_cell))
        if not addresses:
            addresses = clean_emails(EMAIL_CLEAN_PATTERN.findall(" ".join(raw_row)))

        # Inc 8 D1: a structured ``Name:``/``Email:`` field record carries the
        # person AND the real company inside the contact cell (cudahywi layout).
        # The person comes from the Name: field; the company is used only when
        # the company column was blanked (e.g. the F1 "Dropbox" column Phase C
        # rejected) — it is explicit PDF evidence, never a domain inference.
        structured = self._parse_structured_field_record(contact_cell)
        if structured is not None:
            name = self._validate_person_name(structured["person"])
            if not company and structured["company"]:
                company = self._sanitize_company(structured["company"])
        else:
            name = self._validate_person_name(self._name_from_contact(contact_cell))
        person = (
            LeadPerson(
                name=name,
                role="",  # a plan-holder list carries no title - see module docstring
                role_relevance=False,
                tier=PersonVerificationTier.unverified,
                source_url=source_url,
            )
            if name
            else None
        )

        emails = [
            LeadEmail(
                email=address,
                tier=self._email_tier(address, person),
                source_url=source_url,
            )
            for address in addresses
        ]
        phones, phone_labels = self._phones_from(
            self._cell(raw_row, roles.phone), source_url
        )
        # A row whose only defect is an unreadable company (garbage blanked to
        # "") is still a real plan holder when it carries a contact — emit it
        # honestly with company_name="" (Inc 8 precision-over-recall) rather
        # than discarding its email/phone.
        if not company and not emails and not phones:
            return None

        return PlanHolderRow(
            company=company,
            person=person,
            emails=emails,
            phones=phones,
            phone_labels=phone_labels,
            date_contacted=self._first_date(self._cell(raw_row, roles.date)),
            free_mail_only=bool(addresses)
            and all(is_free_mail_domain(a) for a in addresses),
            local_part_matches_name=any(
                self._local_part_matches(a.split("@", 1)[0], name) for a in addresses
            ),
            page=page_number,
            source_url=source_url,
        )

    def _email_tier(
        self, address: str, person: LeadPerson | None
    ) -> EmailVerificationTier:
        """Tier an address from a plan-holder table row.

        A table row is an EXPLICIT person-to-email binding — stronger evidence
        than the page-proximity binding ``PeopleParser`` works from — so a
        personal local-part listed against a named contact is ``person_bound``.
        A generic mailbox can never be ``person_bound`` (hard rule #3), and
        neither can an address with no name beside it.
        """
        local = address.split("@", 1)[0]
        if person is None or is_generic_email_local_part(local):
            return EmailVerificationTier.format
        return EmailVerificationTier.person_bound

    def _name_from_contact(self, cell: str) -> str:
        """The contact's name, recovered by SUBTRACTING the email from the cell.

        Subtraction rather than a name regex: the observed cell is
        ``"Tara O’Donnell /\\nt…@countymaterials.com"``, and a
        ``[A-Z][a-z]+`` style pattern truncates at the curly apostrophe.
        Removing the known-shaped part and keeping the remainder preserves
        apostrophes, hyphens and accents without enumerating them.
        """
        residue = EMAIL_CLEAN_PATTERN.sub(" ", cell)
        residue = re.sub(r"[/|;,]+", " ", residue)
        residue = re.sub(r"\s+", " ", residue).strip(" -:.–—")
        if not residue or len(residue) > MAX_NAME_CHARS:
            return ""
        if not re.search(r"[A-Za-z]{2,}", residue):
            return ""
        if residue.lower() in NAME_STOP_WORDS:
            return ""
        return residue

    def _validate_person_name(self, name: str) -> str:
        """Return ``name`` if it is a person's name, else "".

        Refines the residue :meth:`_name_from_contact` already produced by
        refusing the corruption shapes seen in live PDFs: a residue that is
        really a structured-row label ("Name: … Address: Phone: …"), a
        URL/domain ("www.westfloridaasphalt.com"), or a phone/email. A
        person's name is never invented and never inferred from an email
        username (Inc 8 hard rule) — if the residue is not clearly a name, it
        is blanked and the row's person becomes None.
        """
        if not name or len(name) > MAX_NAME_CHARS:
            return ""
        if "@" in name:
            return ""  # an email address, not a name
        if _URL_OR_DOMAIN_PATTERN.search(name):
            return ""  # a website/domain, not a name
        if _PHONE_IN_CELL_PATTERN.search(name):
            return ""  # a phone number, not a name
        if _FIELD_LABEL_PATTERN.search(name):
            return ""  # a structured-row label, not a name
        if not re.search(r"[A-Za-z]{2,}", name):
            return ""  # no name-like characters
        if name.lower() in NAME_STOP_WORDS:
            return ""
        return name

    def _phones_from(
        self, cell: str, source_url: str
    ) -> tuple[list[LeadPhone], list[str]]:
        """Every phone in the cell, each with its ``Office``/``Cell`` label.

        Scanned line by line so a label only ever attaches to a number on its
        own line. De-duplicated on digits, so ``(319) 555-0100`` and
        ``319-555-0100`` are one number; the first label seen wins. Dots and
        parenthesized area codes are normalized so the shared pattern can match
        formats like ``319.842.2130`` and ``(319) 555-0100``; the stored phone
        is the normalized form, which is what the shared regex matched.

        The *normalized* line is used for both matching and label extraction so
        that removed characters (e.g. parentheses) do not shift the prefix
        boundary and cause the label to include stale characters.
        """
        phones: list[LeadPhone] = []
        labels: list[str] = []
        seen: set[str] = set()
        for line in cell.splitlines():
            norm_line = self._normalize_phone_separators(line)
            for match in PHONE_PATTERN.finditer(norm_line):
                raw = match.group(0).strip()
                if self._digit_count(raw) < 10:
                    continue
                digits = re.sub(r"\D", "", raw)
                if digits in seen:
                    continue
                seen.add(digits)
                phones.append(
                    LeadPhone(
                        phone=raw,
                        tier=PhoneVerificationTier.format,
                        source_url=source_url,
                    )
                )
                labels.append(self._phone_label(norm_line[: match.start()]))
        return phones, labels

    def _phone_label(self, prefix: str) -> str:
        """The phone type immediately preceding a number (``office``, ``cell``)."""
        found = PHONE_LABEL_PATTERN.findall(prefix)
        return found[-1].lower() if found else ""

    @staticmethod
    def _normalize_phone_separators(text: str) -> str:
        """Rewrite non-standard separators so the shared pattern matches.

        Two rewrites, applied in order:

        1. ``(NNN) NNN-NNNN`` → ``NNN-NNN-NNNN``: strip the parentheses around
           the area code and join with a hyphen.  ``PHONE_PATTERN``'s optional
           area-code group *can* consume ``(NNN)`` but the following required
           3-3-4 body then fails because only 7 digits remain (the common
           ``NNN-NNNN`` tail).  Normalizing first lets the body match cleanly.

        2. Dots between digits → hyphens (``319.842.2130`` → ``319-842-2130``).
           ``PHONE_PATTERN`` only accepts ``[\\s-]`` between groups, so a dot
           would break the match.  Rewritten ONLY when the dot sits between two
           digits, so ``ext.7`` and ``01.10.2025`` dates are untouched.

        Per rule #14 the shared pattern is left unchanged; the parser adapts.
        """
        text = re.sub(r"\((\d{3})\)\s*", r"\1-", text)
        return re.sub(r"(?<=\d)\.(?=\d)", "-", text)

    def _local_part_matches(self, local: str, name: str) -> bool:
        """True when the local-part echoes a token of the contact's name.

        ``carnold`` <- "Charlie Arnold" matches; ``billing`` <- "Jodi Ohrt" does
        not. Reported, never enforced: a mismatch means the listed address is a
        shared inbox, which is worth knowing but is not grounds for discarding
        a real row.
        """
        local_alpha = re.sub(r"[^a-z]", "", local.lower())
        if not local_alpha or not name:
            return False
        tokens = [
            re.sub(r"[^a-z]", "", token.lower()) for token in name.split()
        ]
        return any(
            len(token) >= 2 and (token in local_alpha or local_alpha in token)
            for token in tokens
        )

    # -- small helpers -----------------------------------------------------

    def _cell(self, row: list[str], index: int | None) -> str:
        """The cell at ``index``, or "" when the role was never assigned."""
        if index is None or index >= len(row):
            return ""
        return row[index]

    def _flatten(self, text: str) -> str:
        """Collapse newlines/runs of whitespace, e.g. a wrapped company name."""
        return re.sub(r"\s+", " ", text).strip()

    def _first_date(self, text: str) -> str:
        """The first date in ``text``, or "" if there is none."""
        match = DATE_PATTERN.search(text)
        return match.group(0) if match else ""

    # -- reporting ---------------------------------------------------------

    def _log(self, report: PlanHolderParseReport, source_url: str) -> None:
        """Log the full execution picture, per the CLAUDE.md §6 standard."""
        logger.info(
            "plan-holder PDF parse | url=%s status=%s pages=%s text_chars=%s "
            "tables_seen=%s used=%s discarded=%s rows=%s rejected=%s "
            "emails_in_doc=%s emails_captured=%s email_coverage=%.0f%% "
            "phones_in_doc=%s phones_captured=%s phone_coverage=%.0f%%",
            source_url or "(raw bytes)",
            report.parse_status,
            report.pages,
            report.text_chars,
            report.tables_seen,
            report.tables_used,
            report.tables_discarded,
            report.rows_extracted,
            report.rows_rejected,
            report.emails_in_document,
            report.emails_captured,
            report.coverage_ratio * 100,
            report.phones_in_document,
            report.phones_captured,
            report.phone_coverage_ratio * 100,
        )
        for reason in report.reasons:
            logger.warning("plan-holder PDF parse reason: %s", reason)

"""Tests for :mod:`app.discovery.pdf_plan_holder_parser`.

Two layers, deliberately:

1. GRID tests run against ``OBSERVED_GRID`` — the cell layout transcribed from
   a real planholder PDF, including its misaligned header. These pin the
   behaviour that a screenshot of the document cannot: that column roles are
   inferred from content, so the header trap (the ``Email/Contact`` label
   sitting one column away from the emails) cannot silently zero the output.
2. DOCUMENT tests run the parser end-to-end over the committed fixture PDF.
   They are skipped, loudly, when the fixture is absent.

Local-parts and phone digits in ``OBSERVED_GRID`` are SYNTHETIC (555-01xx, the
reserved fictional range) — the real ones are personal contact details. The
structure, company names and email DOMAINS are transcribed from the real
document, because those are what the parser has to cope with.
"""

from __future__ import annotations

import pathlib

import pytest

from app.discovery.pdf_plan_holder_parser import (
    MAX_PAGES,
    STATUS_NEEDS_OCR,
    STATUS_NO_TABLE,
    STATUS_OK,
    STATUS_TOO_LARGE,
    STATUS_UNREADABLE,
    PdfPlanHolderParser,
    PlanHolderParseReport,
    PlanHolderParseResult,
    _ColumnRoles,
)
from app.email.email_cleaner import is_free_mail_domain
from app.engines.lead.lead_models import EmailVerificationTier

#: The real fixture: HR Green planholder list, 4 pages, text-layer PDF.
FIXTURE_PDF = (
    pathlib.Path(__file__).resolve().parents[1]
    / "fixtures"
    / "pdf"
    / "hrgreen_plan_holder_list.pdf"
)

#: Transcribed cell layout from page 1 of the real document. Note rows 0-2:
#: the header spans three rows and puts "Email/Contact" in column 2 while the
#: emails themselves live in column 1, and columns 2/3 are blank on every data
#: row. Both facts are what the parser has to survive.
OBSERVED_GRID = [
    ["Contractor", "", "", "", "Phone/Fax", "Date\nEmailed or P/U"],
    ["", "", "Email/Contact", "", "", ""],
    ["", "", "", "", "", ""],
    [
        "Pirc-Tobin",
        "Charlie Arnold /\ncarnold@pirctobin.com",
        "",
        "",
        "319-555-0101",
        "01.02.2025",
    ],
    [
        "Rathje Construction Co.",
        "Adam Pfab /\napfab@rathjeconstruction.com",
        "",
        "",
        "Office: 319-555-0102\nCell: 319-555-0103",
        "01.03.2025",
    ],
    [
        "County Materials",
        "Tara O’Donnell /\ntodonnell@countymaterials.com",
        "",
        "",
        "Office: 319-555-0104",
        "01.03.2025",
    ],
    [
        "Grimes Asphalt and Paving\nCorporation",
        "Rob Lehman /\nrlehman@grimesasphalt.com",
        "",
        "",
        "515-555-0105",
        "01.14.2025",
    ],
    [
        "City Wide Construction Corp.",
        "Wendy Bertelli /\ncitywide@aol.com",
        "",
        "",
        "319-555-0106",
        "01.10.2025",
    ],
    [
        "Cornerstone Excavating",
        "Karissa Gilchirst /\noffice@cstoneinc.com",
        "",
        "",
        "319-555-0107",
        "01.06.2025",
    ],
    [
        "Novick Land Surveying",
        "Tom Novick /\ntnovick@novicklandsurveying.com",
        "",
        "",
        "",
        "01.20.2025",
    ],
]

#: The phantom table pdfplumber reports on every page: the wrapped header text
#: detected as a table of its own. Must never produce a row.
PHANTOM_GRID = [["Date"], ["Emailed or P/U"]]


def _rows(grid: list[list[str]]) -> list:
    """Run one grid through the table stage and return the rows it produced."""
    parser = PdfPlanHolderParser()
    return parser._rows_from_table(  # noqa: SLF001 - the unit under test
        grid, page_number=1, source_url="https://example.invalid/list.pdf",
        report=PlanHolderParseReport(),
    )


def _by_company(grid: list[list[str]]) -> dict[str, object]:
    """Rows from ``grid`` keyed by company name, for targeted assertions."""
    return {row.company: row for row in _rows(grid)}


def _parse_grid(grid: list[list[str]]) -> PlanHolderParseResult:
    """Run one grid through the full table stage and return the parse result.

    Unlike ``_rows`` this returns the full ``PlanHolderParseResult`` so tests
    can inspect the report (rows_rejected, etc.).
    """
    parser = PdfPlanHolderParser()
    report = PlanHolderParseReport()
    rows = parser._rows_from_table(  # noqa: SLF001
        grid, page_number=1, source_url="https://example.invalid/list.pdf",
        report=report,
    )
    return PlanHolderParseResult(rows=rows, report=report)


# ---------------------------------------------------------------------------
# Column-role inference: the header trap
# ---------------------------------------------------------------------------


def test_roles_are_inferred_from_content_not_from_header_labels() -> None:
    """The email column is found at index 1 despite the label sitting at 2.

    This is the regression that matters most: mapping by header text resolves
    the email column to the permanently blank column 2, which yields zero
    emails while reporting a successful parse.
    """
    parser = PdfPlanHolderParser()
    grid = parser._drop_empty_columns(  # noqa: SLF001
        parser._normalize_grid(OBSERVED_GRID)  # noqa: SLF001
    )
    roles = parser._infer_column_roles(grid)  # noqa: SLF001

    company_col = grid[3].index("Pirc-Tobin")
    assert roles.company == company_col
    assert roles.contact is not None
    assert "@" in grid[3][roles.contact]
    assert roles.phone is not None
    assert roles.date is not None
    assert roles.usable


def test_blank_columns_are_dropped_before_role_inference() -> None:
    """Column 3 is dropped; column 2 SURVIVES, and that is the point.

    Column 2 is blank on every data row but holds the ``Email/Contact`` header
    label, so a blank-column sweep cannot remove it. That is precisely why role
    inference scores DATA rows only: column 2 ends with a text score of zero
    and can never be mistaken for the company column, even though it is not
    empty.
    """
    parser = PdfPlanHolderParser()
    grid = parser._drop_empty_columns(  # noqa: SLF001
        parser._normalize_grid(OBSERVED_GRID)  # noqa: SLF001
    )
    assert len(grid[0]) == 5, "6 raw columns minus the 1 that is blank everywhere"
    header_only_col = 2
    assert all(not row[header_only_col] for row in grid[3:])


def test_phantom_header_table_produces_no_rows() -> None:
    """The wrapped-header pseudo-table is discarded, not parsed."""
    assert _rows(PHANTOM_GRID) == []


def test_header_rows_never_become_companies() -> None:
    """No row is emitted for the three header/spacer rows."""
    companies = set(_by_company(OBSERVED_GRID))
    assert "Contractor" not in companies
    assert not any("Email/Contact" in name for name in companies)
    assert not any("Phone/Fax" in name for name in companies)


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------


def test_every_data_row_is_extracted() -> None:
    """All seven data rows survive; the three furniture rows do not."""
    assert len(_rows(OBSERVED_GRID)) == 7


def test_company_is_paired_with_its_own_contact() -> None:
    """The pairing flat text destroys is preserved by table parsing."""
    row = _by_company(OBSERVED_GRID)["Rathje Construction Co."]
    assert row.person is not None
    assert row.person.name == "Adam Pfab"
    assert [e.email for e in row.emails] == ["apfab@rathjeconstruction.com"]
    assert row.date_contacted == "01.03.2025"


def test_wrapped_company_name_is_flattened() -> None:
    """A company name split across two lines becomes one clean string."""
    assert "Grimes Asphalt and Paving Corporation" in _by_company(OBSERVED_GRID)


def test_curly_apostrophe_in_name_survives() -> None:
    """``O’Donnell`` is kept whole - a ``[A-Z][a-z]+`` regex would truncate it."""
    row = _by_company(OBSERVED_GRID)["County Materials"]
    assert row.person is not None
    assert row.person.name == "Tara O’Donnell"


def test_row_with_no_phone_is_still_extracted() -> None:
    """A missing phone never drops a row - phone is optional (hard rule #4)."""
    row = _by_company(OBSERVED_GRID)["Novick Land Surveying"]
    assert row.phones == []
    assert [e.email for e in row.emails] == ["tnovick@novicklandsurveying.com"]


def test_multiple_phones_keep_their_office_cell_labels() -> None:
    """Both numbers are captured, each with the label printed beside it."""
    row = _by_company(OBSERVED_GRID)["Rathje Construction Co."]
    assert [p.phone for p in row.phones] == ["319-555-0102", "319-555-0103"]
    assert row.phone_labels == ["office", "cell"]


def test_phone_labels_appear_in_the_exported_dict() -> None:
    """``to_dict`` zips each phone with its label for API export."""
    exported = _by_company(OBSERVED_GRID)["Rathje Construction Co."].to_dict()
    assert exported["phones"][1]["label"] == "cell"
    assert exported["phones"][1]["phone"] == "319-555-0103"


def test_date_column_is_not_mistaken_for_a_phone() -> None:
    """``01.02.2025`` must not be captured as a phone number."""
    row = _by_company(OBSERVED_GRID)["Pirc-Tobin"]
    assert [p.phone for p in row.phones] == ["319-555-0101"]


# ---------------------------------------------------------------------------
# Honest tiering
# ---------------------------------------------------------------------------


def test_named_contact_with_personal_local_part_is_person_bound() -> None:
    """A table row is an explicit person-to-email binding (hard rule #3)."""
    row = _by_company(OBSERVED_GRID)["Pirc-Tobin"]
    assert row.emails[0].tier == EmailVerificationTier.person_bound
    assert row.local_part_matches_name is True


def test_generic_local_part_is_never_person_bound() -> None:
    """``office@`` stays ``format`` even though a name sits beside it."""
    row = _by_company(OBSERVED_GRID)["Cornerstone Excavating"]
    assert row.emails[0].tier == EmailVerificationTier.format
    assert row.local_part_matches_name is False


def test_role_relevance_is_reported_false_not_guessed() -> None:
    """A planholder list carries no title, so no role is invented."""
    row = _by_company(OBSERVED_GRID)["Pirc-Tobin"]
    assert row.person is not None
    assert row.person.role == ""
    assert row.person.role_relevance is False


def test_free_mail_row_is_flagged() -> None:
    """``aol.com`` must not be handed downstream as a company domain."""
    row = _by_company(OBSERVED_GRID)["City Wide Construction Corp."]
    assert row.free_mail_only is True


def test_corporate_domain_row_is_not_flagged_free_mail() -> None:
    """A real company domain is left alone."""
    assert _by_company(OBSERVED_GRID)["Pirc-Tobin"].free_mail_only is False


def test_every_record_carries_the_source_url() -> None:
    """No email or phone is emitted without provenance (hard rule #5)."""
    for row in _rows(OBSERVED_GRID):
        assert row.source_url
        for email in row.emails:
            assert email.source_url == row.source_url
        for phone in row.phones:
            assert phone.source_url == row.source_url


# ---------------------------------------------------------------------------
# Free-mail helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["bob@aol.com", "BOB@AOL.COM", "someone@gmail.com", "gmail.com", "x@mchsi.com"],
)
def test_free_mail_domain_detected(value: str) -> None:
    assert is_free_mail_domain(value) is True


@pytest.mark.parametrize(
    "value",
    ["a@pirctobin.com", "pirctobin.com", "", "notaol.com", "a@aol.com.co", "garbage"],
)
def test_free_mail_domain_not_over_matched(value: str) -> None:
    """Exact registered-domain match only - no suffix sweeping, no empty=True."""
    assert is_free_mail_domain(value) is False


# ---------------------------------------------------------------------------
# Failure reporting: never a silent empty list
# ---------------------------------------------------------------------------


def test_empty_payload_reports_unreadable_with_a_reason() -> None:
    result = PdfPlanHolderParser().parse(b"")
    assert result.rows == []
    assert result.report.parse_status == STATUS_UNREADABLE
    assert result.report.reasons


def test_garbage_payload_reports_unreadable_instead_of_raising() -> None:
    result = PdfPlanHolderParser().parse(b"this is not a pdf at all")
    assert result.rows == []
    assert result.report.parse_status == STATUS_UNREADABLE
    assert result.report.reasons


def test_report_is_json_safe() -> None:
    exported = PdfPlanHolderParser().parse(b"").report.to_dict()
    assert exported["parse_status"] == STATUS_UNREADABLE
    assert isinstance(exported["reasons"], list)


# ---------------------------------------------------------------------------
# Company / person integrity (Inc 8 Phase A)
# ---------------------------------------------------------------------------


@pytest.fixture()
def parser() -> PdfPlanHolderParser:
    return PdfPlanHolderParser()


# -- _sanitize_company ------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "f: (432) 385-7280",           # fax fragment
        "(432) 385-7280",              # bare phone
        "https: www.fcgcorpo.com",     # URL in the company cell
        "www.westfloridaasphalt.com",  # bare domain
        "countymaterials.com",         # bare domain
        "john@abcconstruction.com",    # email fragment
        "Name: Wisconsin Bid Network", # structured-row label
        "Address: 123 Main St",        # structured-row label
        "Phone: (555) 123-4567",       # structured-row label
        "Contractor",                  # bare column label
        "Bidder",                      # bare column label
        "X",                           # too short
    ],
)
def test_sanitize_company_rejects_non_companies(parser, value: str) -> None:
    assert parser._sanitize_company(value) == ""  # noqa: SLF001


@pytest.mark.parametrize(
    "value",
    [
        "Pirc-Tobin",
        "Rathje Construction Co.",
        "County Materials",
        "Grimes Asphalt and Paving Corporation",
        "City Wide Construction Corp.",
        "AB Inc",
        "3M",
        "Smith & Sons Construction",
    ],
)
def test_sanitize_company_preserves_real_companies(parser, value: str) -> None:
    assert parser._sanitize_company(value) == value  # noqa: SLF001


def test_sanitize_company_flattens_whitespace(parser) -> None:
    assert parser._sanitize_company("  County   Materials  ") == "County Materials"  # noqa: SLF001


def test_sanitize_company_never_infers_from_email_domain(parser) -> None:
    # "abcconstruction.com" must NOT become "ABC Construction" — it is blanked.
    assert parser._sanitize_company("abcconstruction.com") == ""  # noqa: SLF001


# -- _validate_person_name --------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "Name: Wisconsin Bid Network Address: Phone: Email",
        "Name: Eric Mills iSqFt Address: Phone: 800-364-2059 Email",
        "www.westfloridaasphalt.com",
        "https: www.fcgcorpo.com",
        "john@abcconstruction.com",
        "(432) 385-7280",
        "Phone: (555) 123-4567",
        "Address: 123 Main St",
        "Name:",  # label with empty value
    ],
)
def test_validate_person_name_rejects_non_names(parser, value: str) -> None:
    assert parser._validate_person_name(value) == ""  # noqa: SLF001


@pytest.mark.parametrize(
    "value",
    [
        "Charlie Arnold",
        "Adam Pfab",
        "Tara O’Donnell",
        "Rob Lehman",
        "Jean-Luc Picard",
        "Mary Jane Doe",
        "Kelli Jo Ernst",
    ],
)
def test_validate_person_name_preserves_real_names(parser, value: str) -> None:
    assert parser._validate_person_name(value) == value  # noqa: SLF001


def test_validate_person_name_empty(parser) -> None:
    assert parser._validate_person_name("") == ""  # noqa: SLF001


# -- _build_row integration -------------------------------------------------


def test_build_row_blanks_garbage_company_keeps_email(parser) -> None:
    """A fax-fragment company is blanked, but the row's email survives."""
    roles = _ColumnRoles(company=0, contact=1, phone=None, date=None)
    row = parser._build_row(  # noqa: SLF001
        ["f: (432) 385-7280", "Tara O’Donnell /\ntodonnell@countymaterials.com"],
        roles,
        page_number=1,
        source_url="https://example.invalid/list.pdf",
    )
    assert row is not None
    assert row.company == ""
    assert [e.email for e in row.emails] == ["todonnell@countymaterials.com"]
    assert row.person is not None
    assert row.person.name == "Tara O’Donnell"


def test_build_row_blanks_corrupted_person(parser) -> None:
    """A structured-row label residue is not a person — it becomes None."""
    roles = _ColumnRoles(company=0, contact=1, phone=None, date=None)
    row = parser._build_row(  # noqa: SLF001
        [
            "Reesmans",
            "Name: Wisconsin Bid Network Address: Phone: Email /\nsam@napc.me",
        ],
        roles,
        page_number=1,
        source_url="https://example.invalid/list.pdf",
    )
    assert row is not None
    assert row.company == "Reesmans"
    assert row.person is None
    assert [e.email for e in row.emails] == ["sam@napc.me"]
    # No person -> the email cannot be person_bound (Inc 8 binding rule).
    assert row.emails[0].tier == EmailVerificationTier.format


def test_build_row_preserves_clean_person_bound(parser) -> None:
    """A clean name+email pair stays person_bound (Inc 3 unchanged)."""
    roles = _ColumnRoles(company=0, contact=1, phone=None, date=None)
    row = parser._build_row(  # noqa: SLF001
        ["Pirc-Tobin", "Charlie Arnold /\ncarnold@pirctobin.com"],
        roles,
        page_number=1,
        source_url="https://example.invalid/list.pdf",
    )
    assert row.person is not None
    assert row.person.name == "Charlie Arnold"
    assert row.emails[0].tier == EmailVerificationTier.person_bound


def test_build_row_drops_row_with_no_company_and_no_contact(parser) -> None:
    roles = _ColumnRoles(company=0, contact=1, phone=None, date=None)
    row = parser._build_row(  # noqa: SLF001
        ["", ""],
        roles,
        page_number=1,
        source_url="https://example.invalid/list.pdf",
    )
    assert row is None


# ---------------------------------------------------------------------------
# Phone format coverage (Inc 8 Phase B)
# ---------------------------------------------------------------------------


class TestPhoneFormatCoverage:
    """Every genuinely-observed phone format is captured; labels preserved."""

    def test_parens_area_code_captured(self, parser) -> None:
        """(319) 555-0101 — the one format Phase A missed."""
        phones, labels = parser._phones_from(  # noqa: SLF001
            "(319) 555-0101", "https://example.invalid/test.pdf"
        )
        assert len(phones) == 1
        assert phones[0].phone == "319-555-0101"
        assert labels == [""]

    def test_office_label_with_parens_area_code(self, parser) -> None:
        """Office: (319) 555-0102 — label + parens format."""
        phones, labels = parser._phones_from(  # noqa: SLF001
            "Office: (319) 555-0102", "https://example.invalid/test.pdf"
        )
        assert len(phones) == 1
        assert phones[0].phone == "319-555-0102"
        assert labels == ["office"]

    def test_cell_label_with_parens_area_code(self, parser) -> None:
        """Cell: (319) 555-0103 — label + parens format."""
        phones, labels = parser._phones_from(  # noqa: SLF001
            "Cell: (319) 555-0103", "https://example.invalid/test.pdf"
        )
        assert len(phones) == 1
        assert phones[0].phone == "319-555-0103"
        assert labels == ["cell"]

    def test_hyphenated_format_still_works(self, parser) -> None:
        """319-555-0101 — existing hyphenated format, no regression."""
        phones, labels = parser._phones_from(  # noqa: SLF001
            "319-555-0101", "https://example.invalid/test.pdf"
        )
        assert len(phones) == 1
        assert phones[0].phone == "319-555-0101"
        assert labels == [""]

    def test_space_separated_format(self, parser) -> None:
        """319 555 0101 — space-separated, already worked."""
        phones, labels = parser._phones_from(  # noqa: SLF001
            "319 555 0101", "https://example.invalid/test.pdf"
        )
        assert len(phones) == 1
        assert phones[0].phone == "319 555 0101"
        assert labels == [""]

    def test_dot_separated_format(self, parser) -> None:
        """319.842.2130 — dot-separated, normalized to hyphens."""
        phones, labels = parser._phones_from(  # noqa: SLF001
            "319.842.2130", "https://example.invalid/test.pdf"
        )
        assert len(phones) == 1
        assert phones[0].phone == "319-842-2130"
        assert labels == [""]

    def test_no_separator_format(self, parser) -> None:
        """3195550101 — 10 digits, no separators."""
        phones, labels = parser._phones_from(  # noqa: SLF001
            "3195550101", "https://example.invalid/test.pdf"
        )
        assert len(phones) == 1
        assert phones[0].phone == "3195550101"
        assert labels == [""]

    def test_office_cell_multiline_with_parens(self, parser) -> None:
        """Office: (319) 555-0102 / Cell: (319) 555-0103 — multi-line parens."""
        phones, labels = parser._phones_from(  # noqa: SLF001
            "Office: (319) 555-0102\nCell: (319) 555-0103",
            "https://example.invalid/test.pdf",
        )
        assert [p.phone for p in phones] == ["319-555-0102", "319-555-0103"]
        assert labels == ["office", "cell"]

    def test_dedup_across_formats(self, parser) -> None:
        """Same number in parens and hyphenated → one entry, first label wins."""
        phones, labels = parser._phones_from(  # noqa: SLF001
            "Office: (319) 555-0101\n319-555-0101",
            "https://example.invalid/test.pdf",
        )
        assert len(phones) == 1
        assert phones[0].phone == "319-555-0101"
        assert labels == ["office"]

    def test_fax_with_parens_has_fax_label(self, parser) -> None:
        """Fax: (432) 385-7280 — captured with 'fax' label (filtered at build_row)."""
        phones, labels = parser._phones_from(  # noqa: SLF001
            "Fax: (432) 385-7280", "https://example.invalid/test.pdf"
        )
        assert len(phones) == 1
        assert phones[0].phone == "432-385-7280"
        assert labels == ["fax"]

    def test_phone_with_plus_prefix(self, parser) -> None:
        """+1 319-555-0101 — international prefix preserved."""
        phones, labels = parser._phones_from(  # noqa: SLF001
            "+1 319-555-0101", "https://example.invalid/test.pdf"
        )
        assert len(phones) == 1
        assert phones[0].phone == "+1 319-555-0101"
        assert labels == [""]

    def test_six_four_dash_format(self, parser) -> None:
        """123456-7890 — 6-4 dash format, already worked."""
        phones, labels = parser._phones_from(  # noqa: SLF001
            "123456-7890", "https://example.invalid/test.pdf"
        )
        assert len(phones) == 1
        assert phones[0].phone == "123456-7890"
        assert labels == [""]

    def test_has_phone_recognizes_parens_format(self, parser) -> None:
        """_has_phone must return True for (NNN) NNN-NNNN."""
        assert parser._has_phone("(319) 555-0101")  # noqa: SLF001

    def test_has_phone_recognizes_existing_formats(self, parser) -> None:
        """_has_phone still works for all previously-supported formats."""
        assert parser._has_phone("319-555-0101")  # noqa: SLF001
        assert parser._has_phone("319 555 0101")  # noqa: SLF001
        assert parser._has_phone("319.555.0101")  # noqa: SLF001
        assert parser._has_phone("3195550101")  # noqa: SLF001


# ---------------------------------------------------------------------------
# Repeated-value / header detection (Inc 8 Phase C — F1 fix)
# ---------------------------------------------------------------------------


class TestRepeatedValueDetection:
    """Document-level repeated company values are detected and blanked."""

    REPEATED_GRID = [
        ["Contractor", "", "", "", "Phone/Fax", "Date\nEmailed or P/U"],
        ["Dropbox", "info@reesmans.com", "", "", "", ""],
        ["Dropbox", "joe@sirrahconstruction.com", "", "", "", ""],
        ["Dropbox", "office@starkcorp.com", "", "", "", ""],
        ["Dropbox", "bid@midweststeel.com", "", "", "", ""],
        ["Dropbox", "plans@citywide.com", "", "", "", ""],
        ["Dropbox", "estimating@grimes.com", "", "", "", ""],
        ["Dropbox", "sarah@pirctobin.com", "", "", "", ""],
        ["Dropbox", "charlie@arnold.com", "", "", "", ""],
        ["Dropbox", "mike@cornerstone.com", "", "", "", ""],
    ]

    def test_repeated_value_blanked(self) -> None:
        """A value repeated across all rows is blanked (F1 pattern)."""
        result = _parse_grid(TestRepeatedValueDetection.REPEATED_GRID)
        companies = [r.company for r in result.rows]
        # Every row had "Dropbox" → all blanked.
        assert all(c == "" for c in companies)
        # But rows survived because they carry emails.
        assert len(result.rows) > 0

    def test_repeated_value_preserves_emails(self) -> None:
        """Blanking the company does not destroy the row's contact data."""
        result = _parse_grid(TestRepeatedValueDetection.REPEATED_GRID)
        emails = [e.email for r in result.rows for e in r.emails]
        assert "info@reesmans.com" in emails
        assert "sarah@pirctobin.com" in emails

    def test_valid_repeated_company_not_blanked(self) -> None:
        """A legitimate company appearing in 2 of 4 rows is NOT blanked."""
        grid = [
            ["Contractor", "", "", "", "Phone/Fax", "Date\nEmailed or P/U"],
            ["ABC Corp", "a@abc.com", "", "", "", ""],
            ["ABC Corp", "b@abc.com", "", "", "", ""],
            ["XYZ Inc", "c@xyz.com", "", "", "", ""],
            ["123 LLC", "d@123.com", "", "", "", ""],
        ]
        result = _parse_grid(grid)
        companies = [r.company for r in result.rows]
        # ABC Corp appears in 2/4 data rows (50%) — below 80% threshold.
        assert "ABC Corp" in companies
        assert "XYZ Inc" in companies
        assert "123 LLC" in companies

    def test_two_of_three_not_blanked(self) -> None:
        """2 of 3 rows = 67% — still below 80%, company preserved."""
        grid = [
            ["Company", "Email", "Phone", ""],
            ["Acme Co", "a@acme.com", "555-0101", ""],
            ["Acme Co", "b@acme.com", "555-0102", ""],
            ["Beta Inc", "c@beta.com", "555-0103", ""],
        ]
        result = _parse_grid(grid)
        companies = [r.company for r in result.rows]
        assert companies.count("Acme Co") == 2

    def test_fax_company_value_still_rejected(self, parser) -> None:
        """Phase A fax-as-company rejection is not weakened by Phase C."""
        roles = _ColumnRoles(company=0, contact=1, phone=None, date=None)
        row = parser._build_row(  # noqa: SLF001
            ["f: (432) 385-7280", "Tara O'Donnell /\ntodonnell@countymaterials.com"],
            roles,
            page_number=1,
            source_url="https://example.invalid/test.pdf",
        )
        assert row is not None
        assert row.company == ""

    def test_url_company_value_still_rejected(self, parser) -> None:
        """Phase A URL-as-company rejection is not weakened by Phase C."""
        roles = _ColumnRoles(company=0, contact=1, phone=None, date=None)
        row = parser._build_row(  # noqa: SLF001
            ["https: www.fcgcorpo.com", "contact@fcgcorpo.com"],
            roles,
            page_number=1,
            source_url="https://example.invalid/test.pdf",
        )
        assert row is not None
        assert row.company == ""

    def test_field_label_company_still_rejected(self, parser) -> None:
        """Phase A field-label rejection is not weakened by Phase C."""
        roles = _ColumnRoles(company=0, contact=1, phone=None, date=None)
        row = parser._build_row(  # noqa: SLF001
            ["Name: Wisconsin Bid Network", "sam@napc.me"],
            roles,
            page_number=1,
            source_url="https://example.invalid/test.pdf",
        )
        assert row is not None
        assert row.company == ""

    def test_valid_companies_in_mixed_table(self) -> None:
        """Valid company names in a diverse table are all preserved."""
        result = _parse_grid(OBSERVED_GRID)
        companies = [r.company for r in result.rows]
        assert "Pirc-Tobin" in companies
        assert "Rathje Construction Co." in companies
        assert "City Wide Construction Corp." in companies

    def test_hr_green_fixture_unchanged(self) -> None:
        """HR Green end-to-end parse is identical after Phase C."""
        if not FIXTURE_PDF.exists():
            pytest.skip(f"fixture not present at {FIXTURE_PDF}")
        parser = PdfPlanHolderParser()
        result = parser.parse(
            FIXTURE_PDF.read_bytes(),
            source_url="https://www.hrgreen.com/test.pdf",
        )
        assert result.report.rows_extracted == 28
        companies = [r.company for r in result.rows]
        assert "Pirc-Tobin" in companies
        assert "Rathje Construction Co." in companies

    def test_report_exposes_document_state_codes(self) -> None:
        """The additive ``state_codes`` surface the SOURCE content-gate reads:
        the committed fixture is an IOWA list, so its parsed text names IA and
        nothing else. Guards the parser->source contract (§14)."""
        if not FIXTURE_PDF.exists():
            pytest.skip(f"fixture not present at {FIXTURE_PDF}")
        result = PdfPlanHolderParser().parse(
            FIXTURE_PDF.read_bytes(),
            source_url="https://www.hrgreen.com/test.pdf",
        )
        assert result.report.state_codes == ["IA"]

    def test_person_email_binding_unchanged(self, parser) -> None:
        """Phase C does not alter person or email extraction."""
        roles = _ColumnRoles(company=0, contact=1, phone=None, date=None)
        row = parser._build_row(  # noqa: SLF001
            ["Pirc-Tobin", "Charlie Arnold /\ncarnold@pirctobin.com"],
            roles,
            page_number=1,
            source_url="https://example.invalid/test.pdf",
        )
        assert row is not None
        assert row.person is not None
        assert row.person.name == "Charlie Arnold"
        assert row.emails[0].tier == EmailVerificationTier.person_bound

    def test_no_email_domain_company_inference(self) -> None:
        """Phase C never derives a company from an email domain."""
        grid = [
            ["Company", "Email", "Phone", ""],
            ["Dropbox", "a@reesmans.com", "555-0101", ""],
            ["Dropbox", "b@sirrah.com", "555-0102", ""],
            ["Dropbox", "c@starkcorp.com", "555-0103", ""],
            ["Dropbox", "d@midwest.com", "555-0104", ""],
            ["Dropbox", "e@citywide.com", "555-0105", ""],
        ]
        result = _parse_grid(grid)
        companies = [r.company for r in result.rows]
        # "Dropbox" blanked; NO row gets "reesmans.com" or "sirrah" as company.
        assert all(c == "" for c in companies)


# ---------------------------------------------------------------------------
# Structured Name:/Email: field records (Inc 8 Phase D1)
# ---------------------------------------------------------------------------


class TestStructuredFieldRecord:
    """The cudahywi single-cell ``Name:``/``Email:`` field-record layout.

    These call ``_build_row`` directly with explicit roles because the D1
    logic lives in row construction, and the company cell here stands in for
    the Phase-C-blanked repeated column (empty => structured Name: field wins).
    """

    def _build(self, contact: str, company: str = ""):  # noqa: ANN202
        parser = PdfPlanHolderParser()
        roles = _ColumnRoles(company=0, contact=1, phone=None, date=None)
        return parser._build_row(  # noqa: SLF001
            [company, contact],
            roles,
            page_number=1,
            source_url="https://example.invalid/test.pdf",
        )

    def test_name_company_email_structured_record(self) -> None:
        """Name: Eric Mills, iSqFt + Email: emills@isqft.com → person_bound."""
        row = self._build(
            "Name: Eric Mills, iSqFt\nAddress:\nPhone:\nEmail: emills@isqft.com"
        )
        assert row is not None
        assert row.person is not None
        assert row.person.name == "Eric Mills"
        assert row.company == "iSqFt"
        assert [e.email for e in row.emails] == ["emills@isqft.com"]
        assert row.emails[0].tier == EmailVerificationTier.person_bound

    def test_company_only_name_field(self) -> None:
        """Name: Wisconsin Bid Network (no comma) → person None, company kept."""
        row = self._build(
            "Name: Wisconsin Bid Network\nAddress:\nPhone:\nEmail: sam@napc.me"
        )
        assert row is not None
        assert row.person is None
        assert row.company == "Wisconsin Bid Network"
        assert [e.email for e in row.emails] == ["sam@napc.me"]

    def test_sam_napc_must_not_become_sam(self) -> None:
        """sam@napc.me is a username; 'Sam' must never be inferred."""
        row = self._build(
            "Name: Wisconsin Bid Network\nAddress:\nPhone:\nEmail: sam@napc.me"
        )
        assert row is not None
        assert row.person is None
        assert row.emails[0].tier == EmailVerificationTier.format  # NOT person_bound

    def test_eric_mills_isqft_becomes_person_bound(self) -> None:
        """Person + email explicitly in the same record → person_bound."""
        row = self._build(
            "Name: Eric Mills, iSqFt\nAddress:\nPhone:\nEmail: emills@isqft.com"
        )
        assert row is not None
        assert row.person is not None
        assert row.person.name == "Eric Mills"
        assert row.emails[0].tier == EmailVerificationTier.person_bound

    def test_company_extraction_from_name_field(self) -> None:
        """Real company recovered from Name: field (F1 requirement)."""
        row = self._build(
            "Name: Jim, Reesman's Excavating\nAddress:\nPhone:\nEmail: jr@reesmans.com"
        )
        assert row is not None
        assert row.person is not None
        assert row.person.name == "Jim"
        assert row.company == "Reesman's Excavating"

    def test_ambiguous_comma_no_false_person(self) -> None:
        """An ambiguous comma does not invent a person."""
        row = self._build(
            "Name: Smith, Jones & Associates\nAddress:\nPhone:\n"
            "Email: info@smithjones.com"
        )
        assert row is not None
        # "Jones & Associates" is not a clean company (has '&'); the split is
        # rejected, so no person is guessed — the whole value stays company-only.
        assert row.person is None

    def test_url_remains_invalid_person(self) -> None:
        """A URL in the contact cell is never a person (Phase A guard holds).

        With no valid person and no email/phone the whole row has no usable
        signal, so ``_build_row`` may drop it entirely (``None``); either way
        the URL must never surface as a person.
        """
        row = self._build("www.westfloridaasphalt.com")
        assert row is None or row.person is None

    def test_field_label_residue_remains_invalid_person(self) -> None:
        """Structured-label residue is never a person."""
        row = self._build(
            "Name: Wisconsin Bid Network Address: Phone: Email /\nsam@napc.me"
        )
        assert row is not None
        assert row.person is None
        assert row.emails[0].tier == EmailVerificationTier.format

    def test_pipe_separated_binding_unchanged(self) -> None:
        """The wtagc pipe cell (Name | email) stays person_bound."""
        row = self._build("Charity Roberts | abilene@wtagc.org")
        assert row is not None
        assert row.person is not None
        assert row.person.name == "Charity Roberts"
        assert row.emails[0].tier == EmailVerificationTier.person_bound

    def test_hr_green_fixture_unchanged(self) -> None:
        """HR Green end-to-end parse is identical after D1."""
        if not FIXTURE_PDF.exists():
            pytest.skip(f"fixture not present at {FIXTURE_PDF}")
        parser = PdfPlanHolderParser()
        result = parser.parse(
            FIXTURE_PDF.read_bytes(),
            source_url="https://www.hrgreen.com/test.pdf",
        )
        assert result.report.rows_extracted == 28
        companies = [r.company for r in result.rows]
        assert "Pirc-Tobin" in companies
        assert "Rathje Construction Co." in companies

    def test_no_email_domain_inference(self) -> None:
        """A company is never derived from an email domain."""
        # "reesmans.com" must NOT become the company "Reesmans".
        row = self._build(
            "Name: Wisconsin Bid Network\nAddress:\nPhone:\n"
            "Email: info@reesmans.com"
        )
        assert row is not None
        assert row.company == "Wisconsin Bid Network"  # from Name: field
        assert row.company != "Reesmans"  # not from domain

    def test_no_email_username_inference(self) -> None:
        """A person is never derived from an email username."""
        row = self._build(
            "Name: Wisconsin Bid Network\nAddress:\nPhone:\nEmail: sam@napc.me"
        )
        assert row is not None
        assert row.person is None  # never "Sam" from sam@napc.me


# ---------------------------------------------------------------------------
# End-to-end over the real document
# ---------------------------------------------------------------------------

pytestmark_fixture = pytest.mark.skipif(
    not FIXTURE_PDF.exists(),
    reason=f"planholder fixture PDF not present at {FIXTURE_PDF}",
)


@pytest.fixture(scope="module")
def parsed_fixture() -> object:
    """Parse the committed planholder PDF once for the whole module."""
    if not FIXTURE_PDF.exists():
        pytest.skip(f"planholder fixture PDF not present at {FIXTURE_PDF}")
    return PdfPlanHolderParser().parse(
        FIXTURE_PDF.read_bytes(),
        source_url=(
            "https://www.hrgreen.com/wp-content/uploads/2024/12/"
            "Plan-Holder-List_20250121.pdf"
        ),
    )


@pytestmark_fixture
def test_real_document_parses_cleanly(parsed_fixture) -> None:
    assert parsed_fixture.report.parse_status == STATUS_OK
    assert parsed_fixture.report.pages == 4
    assert parsed_fixture.report.reasons == []


@pytestmark_fixture
def test_real_document_row_count(parsed_fixture) -> None:
    """Pages 1-3 hold 8 + 10 + 9 = 27 data rows; page 4 holds the 28th.

    Exact, not ``>=``: the count is now pinned by the committed fixture, so a
    template drift that silently drops or merges rows fails here instead of
    quietly returning fewer leads.
    """
    assert len(parsed_fixture.rows) == 28


@pytestmark_fixture
def test_real_document_known_companies_present(parsed_fixture) -> None:
    companies = {row.company for row in parsed_fixture.rows}
    for expected in (
        "Pirc-Tobin",
        "Rathje Construction Co.",
        "Steele Excavating Inc.",
        "JB Holland Construction, Inc.",
        "Grimes Asphalt and Paving Corporation",
        "Novick Land Surveying",
    ):
        assert expected in companies


@pytestmark_fixture
def test_real_document_letterhead_contact_is_not_a_row(parsed_fixture) -> None:
    """The issuing firm's own contact sits above the table and must not leak."""
    names = {row.person.name for row in parsed_fixture.rows if row.person}
    assert "Kelli Jo Ernst" not in names
    assert "HR Green, Inc." not in {row.company for row in parsed_fixture.rows}


@pytestmark_fixture
def test_real_document_reports_email_coverage(parsed_fixture) -> None:
    """Coverage is measured and healthy - the template-drift alarm is armed."""
    report = parsed_fixture.report
    assert report.emails_in_document > 0
    assert report.emails_captured > 0
    assert report.coverage_ratio >= 0.70


@pytestmark_fixture
def test_real_document_flags_its_free_mail_rows(parsed_fixture) -> None:
    """The document contains an ``aol.com`` and a ``gmail.com`` contact."""
    flagged = [row.company for row in parsed_fixture.rows if row.free_mail_only]
    assert flagged, "expected the aol.com / gmail.com rows to be flagged"


@pytestmark_fixture
def test_real_document_phantom_tables_were_discarded(parsed_fixture) -> None:
    """Every page contributes one wrapped-header pseudo-table."""
    assert parsed_fixture.report.tables_discarded > 0
    assert parsed_fixture.report.tables_used > 0


@pytestmark_fixture
def test_real_document_needs_no_ocr(parsed_fixture) -> None:
    assert parsed_fixture.report.parse_status != STATUS_NEEDS_OCR
    assert parsed_fixture.report.text_chars > 1000


# ---------------------------------------------------------------------------
# The page bound (2026-09-18 production OOM)
#
# pdfplumber's cost tracks PAGE COUNT, not byte size: a real 352-page, 1.88 MB
# Alaska DOT spec peaked at 2,200 MB RSS to yield zero rows, and that is what
# crash-looped the production backend (4 GB cgroup, MAX_PDFS=10 per pass).
# These tests pin the refusal — and that the refusal happens BEFORE any page is
# read, which is the whole point (counting costs 48 MB, extracting costs 2,200).
# ---------------------------------------------------------------------------


def _n_page_pdf(pages: int) -> bytes:
    """A valid PDF with *pages* empty pages, built by hand.

    Deliberately not a committed fixture: the bound is about page COUNT, so the
    test needs to choose the count, and a 41-page binary in the repo would be
    the largest file here for no reason. ~100 bytes per page.
    """
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    bodies = ["<< /Type /Catalog /Pages 2 0 R >>",
              f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>"]
    bodies += ["<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"] * pages

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(bodies) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(bodies) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def test_oversize_document_is_refused_and_never_read() -> None:
    """Past MAX_PAGES the parser refuses — and reports that as its OWN status.

    ``no_table_found`` would be a lie here: refusing to look is a different
    fact from looking and finding nothing, and an operator reading the log has
    to be able to tell a dork that keeps surfacing spec books from ordinary
    template drift.
    """
    parser = PdfPlanHolderParser()
    result = parser.parse(_n_page_pdf(MAX_PAGES + 1))

    assert result.report.parse_status == STATUS_TOO_LARGE
    assert result.report.parse_status != STATUS_NO_TABLE
    assert result.report.pages == MAX_PAGES + 1
    assert result.rows == []
    assert any(str(MAX_PAGES) in r for r in result.report.reasons)


def test_the_refusal_costs_no_page_read() -> None:
    """The guard's entire value: text_chars / tables_seen stay 0.

    Those two counters are only ever filled by ``_read_document``. If they are
    non-zero the document WAS read, the bound bought nothing, and the 2,200 MB
    is back — so this is the assertion that actually protects the cgroup.
    """
    parser = PdfPlanHolderParser()
    report = parser.parse(_n_page_pdf(400)).report

    assert report.parse_status == STATUS_TOO_LARGE
    assert report.text_chars == 0
    assert report.tables_seen == 0
    assert report.tables_used == 0


def test_document_exactly_at_the_cap_is_still_read() -> None:
    """MAX_PAGES is inclusive: the bound separates document CLASSES (a roster
    from a spec book), so it must not trim a legitimately large roster."""
    parser = PdfPlanHolderParser()
    report = parser.parse(_n_page_pdf(MAX_PAGES)).report

    assert report.parse_status != STATUS_TOO_LARGE
    assert report.pages == MAX_PAGES

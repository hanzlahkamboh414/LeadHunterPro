# Inc 8 — PDF Parser Layout Coverage Expansion (PLANNING ONLY)

**Date:** 2026-09-02
**Status:** PLANNING — awaiting approval before coding
**Scope:** Expand `PdfPlanHolderParser` to handle real-world template/layout drift

---

## 1. Current Parser Architecture

### 1.1 Pipeline

```
PDF bytes
  → pdfplumber: extract_text() + extract_tables() per page
  → coverage audits (email/phone ratios)
  → per-table: _rows_from_table(grid)
    → _normalize_grid()
    → _drop_empty_columns()
    → _infer_column_roles()     ← content-based, NOT header-based
    → filter: _is_header_row() / _row_has_signal()
    → _build_row()
  → _finalize()
```

### 1.2 Column Role Inference (`_infer_column_roles`)

Assignment order (first claim wins):
1. **Email**: first cell with `@`
2. **Phone**: 10-digit number (`_has_phone`)
3. **Date**: matches `DATE_PATTERN`
4. **Company**: leftmost text column (2+ chars, not a stop word)

Critical design: roles are assigned from DATA cells, not headers. This survives the HR Green header trap where "Email/Contact" label sits one column away from actual emails.

### 1.3 Row Construction (`_build_row`)

- **Company**: from `roles.company` cell
- **Contact/Name**: from `_name_from_contact(contact_cell)` — subtracts email pattern, strips separators, validates 2+ alpha chars
- **Emails**: from contact cell, tiered via `_email_tier` (person_bound if person present + non-generic local-part)
- **Phones**: from phone cell via `_phones_from`, line-by-line with labels

### 1.4 What Works (HR Green Layout)

| Feature | Status |
|---|---|
| Table-based PDF with grid structure | Working |
| Misaligned header labels | Working |
| Wrapped company names | Working |
| Curly apostrophes in names | Working |
| Multiple phones with Office/Cell labels | Working |
| Optional phone (row without phone) | Working |
| Free-mail detection | Working |
| Source URL provenance | Working |
| Coverage-ratio audits | Working |

### 1.5 Test Coverage

- 43 tests in `test_pdf_plan_holder_parser.py`
- Grid tests against `OBSERVED_GRID` (transcribed from HR Green)
- End-to-end tests against committed fixture `hrgreen_plan_holder_list.pdf`
- Document tests skipped when fixture absent

---

## 2. Failure-Pattern Analysis (63 Live Records)

### 2.1 Data Summary

| Metric | Value |
|---|---|
| Total records emitted | 63 |
| Unique PDFs parsed | 6 (3 Construction/Texas, 3 Bid/Texas) |
| PDFs fetched | 6 |
| PDFs unreadable | 0 |
| Named persons extracted | 5 |
| Person-bound emails | 3 |
| Both (person + person_bound) | 3 |
| Complete plan_holder_confirmed | 3 |
| Records with `person=None` | 58 (92%) |
| Records with corrupted person name | 4 |

### 2.2 Failure Pattern F1: "Dropbox" Company Name (24 records)

**Source PDF:** `cms9files1.revize.com/.../2016-02 Plan Holders List.pdf`

All 24 records from this PDF have `company_name: "Dropbox"` — clearly wrong. The real company names (Reesmans, Sirrah Construction, Stark Corp, etc.) appear only in the `website` field (derived from email domain).

**Root cause:** The PDF layout is NOT a clean Company|Contact|Phone|Email grid. pdfplumber detects a table where:
- Column 0 contains a single repeated value (likely a project name or document header)
- The actual company name is in a separate text region or a different column position
- Contact+email is correctly extracted into another column

The parser assigns `roles.company = 0` (leftmost text column) but that column holds the repeated header/project name, not the real company.

**Impact:** 24 records have wrong company_name. The real company is recoverable from email domain but the parser doesn't emit it as company_name.

### 2.3 Failure Pattern F2: Corrupted Person Names (4 records)

| Record | Person Name | Should Be |
|---|---|---|
| napc.me | `"Name: Wisconsin Bid Network Address: Phone: Email"` | "Sam" (from sam@napc.me) |
| isqft.com | `"Name: Eric Mills iSqFt Address: Phone: 800-364-2059 Email"` | "Eric Mills" |
| fcgcorpo.com | `"https: www.fcgcorpo.com"` | (no valid person) |
| westfloridaasphalt | `"www.westfloridaasphalt.com"` | (no valid person) |

**Root cause:** `_name_from_contact` subtracts the email but keeps whatever residue remains. When the PDF has multi-field structured rows (like `Name: John Address: 123 St Phone: (555) 123-4567 Email: john@co.com`), the residue includes field labels + address fragments.

For URL-as-person: the contact cell contains a URL instead of a name+email pair, and after email subtraction, the URL residue passes the 2-alpha-char check.

**Impact:** 4 records have corrupted person names. `_is_plan_holder_confirmed` in AcceptanceGate would still return False for these (correctly), but the `plan_holder.person.name` field is garbage.

### 2.4 Failure Pattern F3: Fax Fragment as Company Name (5 records)

| Record | company_name |
|---|---|
| wtagc.org (row 1) | `"f: (432) 385-7280"` |
| wtagc.org (row 2) | `"f: (903) 732-4782"` |
| wtagc.org (row 3) | `"f: (806) 944-5271"` |
| wtagc.org (row 4) | `"f: (830) 634-7150"` |
| wtagc.org (row 5) | `"f: (817) 491-3831"` |

**Root cause:** The wtagc.org PDF has a layout where fax numbers appear in the leftmost column. The parser assigns `roles.company` to this column because it's the leftmost text. The `f:` prefix (fax label) is not filtered out.

**Impact:** 5 records have fax numbers as company_name instead of real company names.

### 2.5 Failure Pattern F4: Person Name as Company Name (1 record)

**Source:** `Donna Craib | wichitafalls@wtagc.org`

**Root cause:** The contact cell contains `Name | email@domain` (pipe-separated). The parser's `_name_from_contact` correctly extracts the name, but the COMPANY column for this row happens to contain the contact cell text. This suggests a column-alignment issue where the contact+email text spans into the company column.

### 2.6 Failure Pattern F5: Email-Only Rows (58 of 63 records have person=None)

58 records (92%) have no person name extracted. Only 5 have any name at all (4 of which are corrupted).

**Root cause:** Most real-world plan-holder PDFs do NOT put name+email in the same cell. Common layouts:
- **Separate columns:** Company | Name | Email | Phone (3-4 separate columns)
- **Multi-line contact:** Name on one line, email on next line (within same cell)
- **Email only:** Only email is listed, no person name visible

The parser's current logic ONLY extracts a name from the contact cell via `_name_from_contact` (subtract email from cell residue). If name and email are in separate columns or separate lines that pdfplumber splits into different cells, the name is lost.

### 2.7 Failure Pattern F6: Zero Phone Numbers (construction PDF) / Near-Zero (bid PDF)

- Construction/Texas: 0 phones captured out of 26 in document text
- Bid/Texas: 1 phone captured out of 175 in document text

**Root cause:** The phone column is either not detected (different column position than expected) or the phone format in the PDF doesn't match `PHONE_PATTERN`. The unmatched shapes reported: `(NNN) NNN-NNNN`, `NNN NNN NNNN`, `NNN.NNN.NNNN`, `NNNNNN-NNNN`, `NNNNNNNNNN`.

The `_normalize_phone_separators` converts dots to hyphens, but the `PHONE_PATTERN` from `phone_engine` may not match all formats. Specifically, `(432) 385-7280` format with parentheses may not match.

---

## 3. HR Green Assumptions (What the Parser Expects)

The parser was built and tested against ONE layout: the HR Green plan-holder list. Its assumptions:

| Assumption | HR Green | Real World |
|---|---|---|
| PDF has extractable table grid | Yes | Not always |
| Company name is in leftmost column | Yes | Varies |
| Contact = name + "/" + email in same cell | Yes | Often separate |
| Phone in its own column | Yes | Sometimes embedded |
| Header rows are distinguishable | Yes | Not always |
| Table has >= 3 columns | Yes (6) | 2-4 common |
| Email is always in the same column | Yes | Varies by PDF |

---

## 4. Proposed Architecture Changes

### 4.1 Strategy: Detect, Don't Assume

The fix is NOT to add more hardcoded layout patterns. It is to add DETECTION LAYERS that identify common failure modes and handle them gracefully.

### 4.2 Change 1: Company Name Sanitization (`_sanitize_company`)

Add a post-extraction validation step for company names. Reject names that match:
- Phone/fax patterns: `f:`, `ph:`, `(NNN)`, `NNN-NNN-NNNN`
- URLs: starts with `http`, `www.`, contains `.com`/`.net`/`.org`
- Email fragments: contains `@`
- Field labels: `Name:`, `Address:`, `Phone:`, `Email:`
- Too short (< 2 meaningful chars after stripping)
- Single word that is a known non-company token

When rejected: emit `company_name = ""` (honest blank) rather than garbage.

### 4.3 Change 2: Person Name Validation (`_validate_person_name`)

Add validation to `_name_from_contact` and the person-name extraction path:
- Reject names that contain field labels (`Name:`, `Address:`, `Phone:`, `Email:`)
- Reject names that are URLs (`http`, `www.`)
- Reject names that are phone numbers
- Reject names that are email addresses
- Reject names > MAX_NAME_CHARS (already exists, 60)
- Reject names without 2+ consecutive alpha chars (already exists)

These filters catch F2 corruption patterns.

### 4.4 Change 3: Multi-Line Contact Cell Handling

When the contact cell contains multiple lines and the email is on a different line than the name:
```
Charlie Arnold
carnold@pirctobin.com
```
Currently `_name_from_contact` subtracts the email pattern and keeps the residue. This already works for multi-line cells. The issue is when pdfplumber splits them into separate cells.

**Proposed:** Before `_build_row`, if the contact cell has no email but an adjacent cell does, and the contact cell looks like a name (2+ alpha words, no special chars), treat it as the person name and the adjacent cell's email as the binding.

### 4.5 Change 4: Header/Row Rejection Improvements

Current `_is_header_row` checks for specific tokens. Add:
- Reject rows where company_name matches `_sanitize_company` failure patterns
- Reject rows where ALL cells are field labels (Name/Address/Phone/Email pattern)
- Reject rows where the company cell is a phone number

### 4.6 Change 5: Phone Format Coverage

Investigate and extend `_normalize_phone_separators` or add a pre-processing step to handle:
- `(NNN) NNN-NNNN` format (parentheses around area code)
- `NNN.NNN.NNNN` format (already handled by dot→hyphen normalization)
- `NNNNNNNNNN` (10 digits no separator — add formatting)

Check if `PHONE_PATTERN` from `phone_engine` already handles parentheses. If not, extend the normalization.

### 4.7 What NOT to Change

Per the user's constraints:
- AcceptanceGate: NO changes
- qualification_gate(): NO changes
- lead_models.py: NO changes
- Person-bound email semantics (Inc 3): NO changes
- Role enrichment (Inc 4): NO changes
- Verification semantics (Inc 5): NO changes
- No LinkedIn/search-engine/external enrichment
- No fabrication of missing data

---

## 5. Exact Files to Modify

| File | Change |
|---|---|
| `backend/app/discovery/pdf_plan_holder_parser.py` | Add `_sanitize_company()`, `_validate_person_name()`, improve `_is_header_row()`, improve phone normalization, add multi-line contact handling |
| `backend/tests/discovery/test_pdf_plan_holder_parser.py` | Add tests for all new validation/sanitization logic |
| `backend/tests/discovery/test_people_parser.py` | Verify no regression |
| `backend/tests/verification/test_acceptance_gate.py` | Verify no regression |

**No new files created.** All changes are additive within existing modules.

---

## 6. Data-Integrity Rules (Hard Constraints)

These are NON-NEGOTIABLE. Violating any one = implementation failure.

1. **Never invent a person's name.** If the PDF doesn't provide a clear name, `person.name = ""`.
2. **Never infer a person from an email username alone.** `sam@napc.me` does NOT mean the person's name is "Sam". Leave `person.name = ""`.
3. **Never bind an email to a person unless the PDF structure provides sufficient evidence that they belong together.** Same-cell, same-row, or explicit separator (/, |) between name and email.
4. **Never convert a generic/company email into person_bound.** `info@`, `office@`, `plans@` stay `format` tier.
5. **Never copy a person/email association from another row.** Each row is independent.
6. **If association is ambiguous, leave it unbound.** `person_bound` only when the binding is structurally certain.
7. **Parser recall must NEVER come at the expense of precision/honesty.** It is better to emit `person=None` than a wrong name.
8. **Existing Inc 3 person-bound email semantics must remain unchanged.**
9. **Existing Inc 4 role enrichment must remain unchanged.**
10. **Existing Inc 5 verification semantics must remain unchanged.**
11. **Do not fabricate missing data.** If a field is absent, leave it empty.

---

## 7. Testing Plan (12 Areas)

### T1: Company Name Sanitization
- Phone/fax as company → rejected (blank)
- URL as company → rejected (blank)
- Email fragment as company → rejected (blank)
- Field label as company → rejected (blank)
- Valid company name → preserved
- Short but valid company name ("AB Inc") → preserved

### T2: Person Name Validation
- Field-label residue → rejected (blank)
- URL residue → rejected (blank)
- Phone number as name → rejected (blank)
- Valid name with apostrophe → preserved
- Valid name with hyphen → preserved
- Empty residue after email subtraction → empty (correct)

### T3: Multi-Line Contact Handling
- Name on line 1, email on line 2 (same cell) → name extracted
- Name and email in separate cells (adjacent) → name extracted if structure supports it
- Email only (no name) → person=None (correct)

### T4: Header/Row Rejection
- Row with all field labels → rejected
- Row with fax number in company column → rejected
- Valid data row → preserved
- Row with "Dropbox"-style repeated header → rejected if it matches header patterns

### T5: Phone Format Coverage
- `(NNN) NNN-NNNN` → captured
- `NNN-NNN-NNNN` → captured (existing)
- `NNN.NNN.NNNN` → captured (existing via normalization)
- `NNN NNN NNNN` → captured if PHONE_PATTERN supports it
- 10-digit no separator → captured if possible
- Label detection (Office/Cell/Fax) → preserved

### T6: Existing HR Green Regression
- All 43 existing tests continue to pass
- Grid tests: column role inference unchanged
- Document tests: 28 rows, known companies, letterhead not leaked
- Coverage ratio >= 0.70

### T7: F1 Pattern (Dropbox PDF)
- Company name is NOT "Dropbox" for all rows
- Real company names are extracted (or blank if parser can't determine)
- Person + email extraction remains correct

### T8: F2 Pattern (Corrupted Names)
- "Name: Wisconsin Bid Network Address: Phone: Email" → person.name = ""
- "Name: Eric Mills iSqFt Address: Phone: 800-364-2059 Email" → person.name = "" (or "Eric Mills" if extraction is clean)
- URL-as-person → person.name = ""

### T9: F3 Pattern (Fax as Company)
- "f: (432) 385-7280" → company_name = "" (not a company)
- Real company name extracted from correct column

### T10: F4 Pattern (Pipe-Separated Contact)
- "Donna Craib | wichitafalls@wtagc.org" → person.name = "Donna Craib", email = wichitafalls@wtagc.org

### T11: Existing Test Fixture Stability
- `hrgreen_plan_holder_list.pdf` parse result unchanged
- Row count, company names, person names, emails, phones all identical

### T12: Ruff Clean
- No new ruff violations
- No existing violations reintroduced

---

## 8. Baseline and Success Metrics

### 8.1 Baseline (Current Parser, 63 Records)

| Metric | Baseline |
|---|---|
| Total rows emitted | 63 |
| Named persons | 5 (4 corrupted) |
| Person-bound emails | 3 |
| Complete person + person_bound | 3 |
| plan_holder_confirmed | 3 |
| Corrupted person names | 4 |
| Garbage company names (phone/URL/fragment) | 30+ |
| Email coverage (construction PDF) | 31% |
| Email coverage (bid PDF) | ~5% |
| Phone coverage (construction PDF) | 0% |
| Phone coverage (bid PDF) | ~1% |

### 8.2 Success Criteria

The implementation is successful when:

1. **Zero corrupted person names.** All 4 F2-pattern corruptions are eliminated (names become blank or clean).
2. **Zero garbage company names.** All F3-pattern fax-as-company and F1-pattern "Dropbox" errors are eliminated.
3. **All existing tests pass.** 43 parser tests + 1758 full suite tests + ruff clean.
4. **No regression on HR Green fixture.** 28 rows, identical extraction.
5. **Person-bound email count >= 3.** (No decrease from baseline.)
6. **plan_holder_confirmed count >= 3.** (No decrease from baseline.)

### 8.3 What Success Does NOT Mean

- NOT: more qualified leads (the goal is correct extraction, not quantity)
- NOT: 100% email/phone coverage (template drift is real; honest reporting is correct)
- NOT: every PDF perfectly parsed (some layouts are genuinely unreadable)
- NOT: fabricated data to hit targets

---

## 9. Expected Risks

| Risk | Mitigation |
|---|---|
| Company sanitization over-rejects valid names | Whitelist approach: only reject known-bad patterns, not "anything unusual" |
| Multi-line contact extraction creates false person bindings | Strict structural evidence requirement: same-cell or explicit separator only |
| Phone normalization breaks existing formats | Test against ALL existing phone test cases before and after |
| New validation logic adds complexity | Keep it simple: regex filters, not ML. Each filter is independently testable |
| Some real company names look like garbage | Better to emit blank than garbage; downstream AcceptanceGate handles missing company_name |

---

## 10. Implementation Phases (Proposed)

After approval, implement in this order:

**Phase A: Sanitization + Validation (Low Risk)**
- Add `_sanitize_company()` and `_validate_person_name()`
- Add 20+ unit tests for these functions
- Verify all existing tests pass
- Ruff clean

**Phase B: Phone Coverage (Low Risk)**
- Extend phone normalization for parentheses format
- Add tests for new phone formats
- Verify all existing tests pass
- Ruff clean

**Phase C: Row Rejection (Medium Risk)**
- Improve `_is_header_row()` with new rejection patterns
- Add tests for header rejection
- Verify all existing tests pass
- Ruff clean

**Phase D: Multi-Line Contact (Higher Risk)**
- Add adjacent-cell contact extraction
- Add strict structural binding tests
- Verify all existing tests pass
- Ruff clean
- **This phase requires the most careful testing due to person-binding implications**

Each phase is independently shippable. If any phase causes regression, it can be reverted without affecting others.

---

## 11. Files Changed Summary

| File | Action | Risk |
|---|---|---|
| `backend/app/discovery/pdf_plan_holder_parser.py` | MODIFY (add sanitization, validation, phone normalization, row rejection) | Medium |
| `backend/tests/discovery/test_pdf_plan_holder_parser.py` | MODIFY (add 40+ new tests) | Low |

No new files. No changes to AcceptanceGate, qualification_gate, lead_models, or any other production module.

---

*Awaiting approval before coding. Each phase will be implemented, tested, and verified independently per CLAUDE.md rules.*

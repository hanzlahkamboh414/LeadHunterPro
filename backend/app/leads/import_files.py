"""Extract contact email strings from user supplied research files."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from app.email.email_cleaner import is_acceptable_email

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_EMAILS = 5000
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
    r"(?![A-Za-z0-9.-])", re.I,
)


class ImportFileError(ValueError):
    pass


def _safe_zip(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        if sum(item.file_size for item in archive.infolist()) > MAX_UNPACKED_BYTES:
            archive.close()
            raise ImportFileError("Spreadsheet or document expands beyond 100 MB")
        return archive
    except zipfile.BadZipFile as exc:
        raise ImportFileError("Invalid spreadsheet or document file") from exc


def _plain_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ImportFileError("Could not read this file as text")


def _xml_text(data: bytes, member: str, *, ods: bool = False) -> str:
    with _safe_zip(data) as archive:
        if member not in archive.namelist():
            raise ImportFileError("Document has no readable text")
        try:
            root = ET.fromstring(archive.read(member))
        except ET.ParseError as exc:
            raise ImportFileError("Document text is malformed") from exc
    if ods:
        return "\n".join("".join(paragraph.itertext()) for paragraph in root.iter()
                         if paragraph.tag.endswith("}p"))
    return "\n".join(
        "".join(child.text or "" for child in paragraph.iter()
                if child.tag.endswith("}t"))
        for paragraph in root.iter() if paragraph.tag.endswith("}p")
    )


def _extract_texts(data: bytes, suffix: str) -> list[str]:
    if suffix in {".csv", ".tsv", ".txt", ".html", ".htm", ".rtf"}:
        return [_plain_text(data)]
    if suffix == ".xlsx":
        with _safe_zip(data):
            pass
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise ImportFileError("Excel reader is unavailable") from exc
        try:
            book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            out: list[str] = []
            cell_count = 0
            for sheet in book.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    cell_count += len(row)
                    if cell_count > 200_000:
                        raise ImportFileError("Workbook exceeds the 200,000-cell upload limit")
                    out.extend(str(value) for value in row if value is not None)
            book.close()
            return out
        except ImportFileError:
            raise
        except Exception as exc:
            raise ImportFileError("Could not read XLSX workbook") from exc
    if suffix == ".xls":
        try:
            import xlrd
        except ImportError as exc:
            raise ImportFileError("Legacy Excel reader is unavailable") from exc
        try:
            book = xlrd.open_workbook(file_contents=data, on_demand=True)
            out = []
            cells = 0
            for sheet in book.sheets():
                for row in range(sheet.nrows):
                    values = sheet.row_values(row)
                    cells += len(values)
                    if cells > 200_000:
                        raise ImportFileError("Workbook exceeds the 200,000-cell upload limit")
                    out.extend(str(value) for value in values if value)
            book.release_resources()
            return out
        except ImportFileError:
            raise
        except Exception as exc:
            raise ImportFileError("Could not read XLS workbook") from exc
    if suffix == ".pdf":
        import pdfplumber

        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                if len(pdf.pages) > 200:
                    raise ImportFileError("PDF exceeds the 200-page upload limit")
                return [page.extract_text() or "" for page in pdf.pages]
        except ImportFileError:
            raise
        except Exception as exc:
            raise ImportFileError("Could not read PDF text; scanned PDFs need OCR") from exc
    if suffix == ".docx":
        return [_xml_text(data, "word/document.xml")]
    if suffix == ".ods":
        return [_xml_text(data, "content.xml", ods=True)]
    # A file with an unfamiliar extension can still be a plain-text export.
    text = _plain_text(data)
    if not text or sum(ch.isprintable() or ch.isspace() for ch in text) / len(text) < .95:
        raise ImportFileError("Unsupported binary file; use CSV, Excel, PDF, DOCX, ODS or text")
    return [text]


def extract_emails(data: bytes, filename: str) -> tuple[list[str], int]:
    """Return unique acceptable emails and count of rejected/duplicate hits."""
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ImportFileError("File must be between 1 byte and 10 MB")
    texts = _extract_texts(data, Path(filename or "upload.txt").suffix.lower())
    seen: set[str] = set()
    emails: list[str] = []
    rejected = 0
    for text in texts:
        for match in _EMAIL.finditer(text):
            email = match.group(1).lower()
            if email in seen or not is_acceptable_email(email):
                rejected += 1
                continue
            seen.add(email)
            emails.append(email)
            if len(seen) > MAX_EMAILS:
                raise ImportFileError("File contains over 5,000 unique emails")
    if not seen:
        raise ImportFileError("No usable email addresses found in this file")
    return emails, rejected

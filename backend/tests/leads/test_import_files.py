"""Uploaded list formats and the removed fixed-size limits."""

import io
import zipfile

from openpyxl import Workbook

from app.leads.import_files import extract_emails
from app.campaigns.store import CampaignStore
from app.schemas.campaigns import CampaignCreateIn


def test_large_text_file_has_no_ten_mb_cap():
    source = io.BytesIO(b"note\n" * 2_100_000 + b"owner@acme-electric.com\n")
    assert extract_emails(source, "leads.csv")[0] == ["owner@acme-electric.com"]


def test_xlsx_and_docx_extract_addresses():
    book = Workbook()
    book.active.append(["Contact", "owner@acme-electric.com"])
    xlsx = io.BytesIO()
    book.save(xlsx)
    assert extract_emails(xlsx, "list.xlsx")[0] == ["owner@acme-electric.com"]

    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="urn:word"><w:body><w:p><w:r>'
            '<w:t>owner@acme-electric.com</w:t></w:r></w:p></w:body></w:document>',
        )
    assert extract_emails(docx, "list.docx")[0] == ["owner@acme-electric.com"]


def test_campaign_schema_accepts_more_than_500_addresses():
    payload = {
        "name": "Large list", "account_id": 1, "subject": "Subject",
        "body": "Message", "start_at": "2030-01-01T00:00:00Z",
        "emails": [f"person{i}@example.com" for i in range(501)],
        "audience_source": "own_list",
    }
    assert len(CampaignCreateIn.model_validate(payload).emails) == 501


def test_campaign_store_queues_more_than_500_addresses(tmp_path):
    store = CampaignStore(db_path=str(tmp_path / "campaigns.db"))
    emails = [f"person{i}@acme-electric.com" for i in range(501)]
    result = store.create(
        "owner", account_id=1, name="Large list", subject="Subject",
        body="Message", emails=emails, start_at="2030-01-01T00:00:00Z",
    )
    assert result["pending"] == 501


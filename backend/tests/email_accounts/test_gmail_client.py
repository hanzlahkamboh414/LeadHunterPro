"""Phase E7 — the Gmail client extensions (hermetic: every Google call
faked at the requests seam; no real network, no real tokens)."""

from __future__ import annotations

import base64
import email
import json
import types

import app.email_accounts.google as google


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _install(monkeypatch, *, get=None, post=None):
    """Replace the module's `requests` with a stub (the monkeypatch-undo
    keeps the real one for every other test)."""
    monkeypatch.setattr(google, "requests", types.SimpleNamespace(
        get=get or (lambda *a, **k: _Resp({})),
        post=post or (lambda *a, **k: _Resp({})),
    ))


# ---------------------------------------------------------------------------
# list_message_ids — one page of ids
# ---------------------------------------------------------------------------

def test_list_message_ids_passes_label_query_and_token(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen.update(url=url, headers=headers, params=params)
        return _Resp({"messages": [{"id": "m1"}, {"id": "m2"}],
                      "nextPageToken": "ptoken",
                      "resultSizeEstimate": 123})

    _install(monkeypatch, get=fake_get)
    page = google.list_message_ids(
        "tok", label="INBOX", q="from:jane after:2024/01/01",
        page_token="pt", limit=50,
    )
    assert page == {"ids": ["m1", "m2"], "next_page_token": "ptoken",
                    "total_estimate": 123}
    assert seen["params"] == {"maxResults": 50, "labelIds": "INBOX",
                              "q": "from:jane after:2024/01/01",
                              "pageToken": "pt"}
    assert seen["headers"] == {"Authorization": "Bearer tok"}


def test_list_message_ids_empty_page(monkeypatch):
    _install(monkeypatch, get=lambda *a, **k: _Resp({}))
    page = google.list_message_ids("tok")
    assert page == {"ids": [], "next_page_token": "", "total_estimate": 0}


# ---------------------------------------------------------------------------
# get_message — the multipart walk (text + html + attachments)
# ---------------------------------------------------------------------------

def _full_payload() -> dict:
    return {
        "headers": [
            {"name": "From", "value": "Jane <jane@acme.com>"},
            {"name": "To", "value": "me@gmail.com"},
            {"name": "Subject", "value": "Quote attached"},
            {"name": "Date", "value": "Mon, 1 Jan 2024 10:00:00 +0000"},
            {"name": "Message-ID", "value": "<m1@acme.com>"},
            {"name": "In-Reply-To", "value": "<old@x>"},
            {"name": "References", "value": "<old@x>"},
        ],
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("Hello there")}},
                {"mimeType": "text/html",
                 "body": {"data": _b64("<p>Hello there</p>")}},
            ]},
            {"mimeType": "application/pdf", "filename": "quote.pdf",
             "body": {"attachmentId": "att1", "size": 1234}},
        ],
    }


def test_get_message_parses_body_and_attachments(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        assert params == {"format": "full"}
        return _Resp({"id": "m1", "threadId": "t1", "snippet": "Hello…",
                      "labelIds": ["INBOX", "UNREAD"],
                      "payload": _full_payload()})

    _install(monkeypatch, get=fake_get)
    m = google.get_message("tok", "m1")
    assert m["id"] == "m1" and m["thread_id"] == "t1"
    assert m["text"] == "Hello there"
    assert m["html"] == "<p>Hello there</p>"
    assert m["attachments"] == [{"attachment_id": "att1",
                                 "filename": "quote.pdf",
                                 "mime_type": "application/pdf",
                                 "size": 1234}]
    h = m["headers"]
    assert h["from"] == "Jane <jane@acme.com>"
    assert h["in-reply-to"] == "<old@x>"
    assert m["labels"] == ["INBOX", "UNREAD"]


def test_get_message_metadata_only_skips_body(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen["params"] = params
        return _Resp({"id": "m1", "threadId": "t1", "snippet": "s",
                      "labelIds": [],
                      "payload": _full_payload()})

    _install(monkeypatch, get=fake_get)
    m = google.get_message("tok", "m1", metadata_only=True)
    assert m["text"] == "" and m["attachments"] == []
    assert "format" in seen["params"] and seen["params"]["format"] == "metadata"


# ---------------------------------------------------------------------------
# get_attachment + modify_message
# ---------------------------------------------------------------------------

def test_get_attachment_decodes_bytes(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        assert url.endswith("/messages/m1/attachments/att1")
        return _Resp({"data": _b64("PDFBYTES"), "size": 9})

    _install(monkeypatch, get=fake_get)
    att = google.get_attachment("tok", "m1", "att1")
    assert att["data"] == b"PDFBYTES"
    assert att["size"] == 9


def test_modify_message_body_shape(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, body=json)
        return _Resp({"id": "m1", "labelIds": ["INBOX"]})

    _install(monkeypatch, post=fake_post)
    google.modify_message("tok", "m1", add_labels=("STARRED",),
                          remove_labels=("UNREAD",))
    assert seen["body"] == {"addLabelIds": ["STARRED"],
                            "removeLabelIds": ["UNREAD"]}
    assert seen["url"].endswith("/messages/m1/modify")


# ---------------------------------------------------------------------------
# send_gmail — cc/bcc + reply threading headers
# ---------------------------------------------------------------------------

def test_send_gmail_carries_cc_bcc_and_reply_headers(monkeypatch):
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, raw=json["raw"])
        return _Resp({"id": "sent1", "threadId": "t9"})

    _install(monkeypatch, post=fake_post)
    out = google.send_gmail(
        "tok", to="jane@acme.com", subject="Re: Quote",
        body="Here you go", from_email="me@gmail.com",
        cc="bob@acme.com", bcc="audit@x.com",
        in_reply_to="<orig@acme.com>", references="<orig@acme.com>",
    )
    assert out["id"] == "sent1"
    raw = sent["raw"] + "=" * (-len(sent["raw"]) % 4)
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert msg["To"] == "jane@acme.com"
    assert msg["Cc"] == "bob@acme.com"
    assert msg["Bcc"] == "audit@x.com"
    assert msg["In-Reply-To"] == "<orig@acme.com>"
    assert msg["References"] == "<orig@acme.com>"
    assert msg["Subject"] == "Re: Quote"
    assert msg.get_payload().strip() == "Here you go"


def test_send_gmail_without_optional_headers_is_unchanged(monkeypatch):
    """The campaign/test-send callers pass no new kwargs — their wire shape
    must be exactly what it always was."""
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["raw"] = json["raw"]
        return _Resp({})

    _install(monkeypatch, post=fake_post)
    google.send_gmail("tok", to="a@b.com", subject="s", body="b",
                      from_email="me@gmail.com")
    raw = sent["raw"] + "=" * (-len(sent["raw"]) % 4)
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert msg["Cc"] is None and msg["Bcc"] is None
    assert msg["In-Reply-To"] is None


# ---------------------------------------------------------------------------
# The xlsx writer (same seam: bytes out, no deps)
# ---------------------------------------------------------------------------

def test_build_xlsx_is_a_real_zip_with_rows():
    import io
    import zipfile
    from xml.etree import ElementTree as ET

    from app.email.xlsx_export import build_xlsx

    data = build_xlsx([["b@x.com"], ["a <a@x.com>"]], header=["email"],
                      sheet="Emails")
    zf = zipfile.ZipFile(io.BytesIO(data))
    sheet = zf.read("xl/worksheets/sheet1.xml").decode()
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(sheet)
    rows = root.findall(".//m:row", ns)
    texts = [t.text for t in root.findall(".//m:is/m:t", ns)]
    assert len(rows) == 3
    assert texts == ["email", "b@x.com", "a <a@x.com>"]  # < escaped
    # Every part a reader needs is present.
    assert set(zf.namelist()) == {
        "[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels", "xl/worksheets/sheet1.xml"}
    json.dumps({"ok": len(data) > 0})  # bytes are just bytes

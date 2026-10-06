"""SMTP account storage and TLS send path, without network traffic."""

import sqlite3

from tests.email_accounts.test_google_oauth import _setup as account_setup

from app.email_accounts import smtp
from app.email_accounts.store import EmailAccountStore
from tests.campaigns.test_campaigns import _make_campaign, _setup as campaign_setup


def test_smtp_account_is_encrypted_and_owner_scoped(tmp_path):
    path = tmp_path / "users.db"
    store = EmailAccountStore(db_path=str(path))
    store.connect("u1", "sender@example.com", access_token="oauth")
    account = store.connect_smtp("u1", "sender@example.com", host="smtp.example.com",
                                 port=587, security="starttls", username="sender",
                                 password="APP-SECRET")
    rows = store.list_for_user("u1")
    assert len(rows) == 2
    assert account["provider"] == "smtp"
    assert "APP-SECRET" not in str(rows)
    assert "sender" not in str(account.get("smtp_username", ""))
    assert store.get_credentials(account["id"], "u2") is None
    assert store.get_credentials(account["id"], "u1")["smtp_password"] == "APP-SECRET"
    with sqlite3.connect(path) as conn:
        raw = conn.execute("SELECT smtp_password FROM email_accounts WHERE id=?", (account["id"],)).fetchone()[0]
    assert "APP-SECRET" not in raw
    again = store.connect_smtp("u1", "sender@example.com", host="smtp.example.com",
                               port=587, security="starttls", username="sender",
                               password="NEW-SECRET")
    assert again["id"] == account["id"]
    assert store.get_credentials(account["id"], "u1")["smtp_password"] == "NEW-SECRET"


def test_smtp_connect_and_test_send_http(tmp_path, monkeypatch):
    client, user, store = account_setup(tmp_path, monkeypatch)
    verified = []
    delivered = []
    monkeypatch.setattr(smtp, "verify_connection", lambda **kwargs: verified.append(kwargs))
    monkeypatch.setattr(smtp, "send_smtp", lambda **kwargs: delivered.append(kwargs))
    response = client.post("/api/v1/email-accounts/smtp", json={
        "email": "Sender@Example.com", "host": "smtp.example.com", "port": 587,
        "security": "starttls", "username": "sender", "password": "APP-SECRET",
    })
    assert response.status_code == 201, response.text
    account = response.json()
    assert account["email"] == "sender@example.com"
    assert "APP-SECRET" not in response.text
    assert verified[0]["port"] == 587
    assert store.get_credentials(account["id"], user.id)["smtp_password"] == "APP-SECRET"
    tested = client.post(f"/api/v1/email-accounts/{account['id']}/send-test")
    assert tested.status_code == 200, tested.text
    assert delivered[0]["to"] == "sender@example.com"


def test_smtp_send_uses_starttls_before_login(monkeypatch):
    events = []
    class Client:
        def __init__(self, *args, **kwargs):
            events.append("connect")
        def ehlo(self):
            events.append("ehlo")
        def starttls(self, **kwargs):
            events.append("starttls")
        def login(self, username, password):
            events.append("login")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def send_message(self, message):
            assert message["To"] == "lead@example.com"
            assert message["From"] == "sender@example.com"
            assert message.get_body(preferencelist=("html",)) is not None
            events.append("send")
    monkeypatch.setattr(smtp, "validate_host", lambda host: host)
    monkeypatch.setattr(smtp.smtplib, "SMTP", Client)
    smtp.send_smtp(host="smtp.example.com", port=587, security="starttls",
                   username="sender", password="secret", from_email="sender@example.com",
                   to="lead@example.com", subject="Hello", body="Hi", tracking_url="https://example.com/open")
    assert events == ["connect", "ehlo", "starttls", "ehlo", "login", "send"]


def test_campaign_uses_smtp_and_keeps_pacing(tmp_path, monkeypatch):
    ctx = campaign_setup(tmp_path, monkeypatch)
    account = ctx["email_store"].connect_smtp(ctx["user"].id, "smtp@example.com",
                                               host="smtp.example.com", port=587,
                                               security="starttls", username="sender",
                                               password="secret")
    ctx["account_id"] = account["id"]
    sent = []
    monkeypatch.setattr(smtp, "send_smtp", lambda **kwargs: sent.append(kwargs))
    _make_campaign(ctx)
    assert ctx["sched"].run_once()["sent"] == 1
    assert sent[0]["to"] == "jane@acme.com"
    assert ctx["sched"].run_once()["sent"] == 0
    ctx["sched"]._clock.advance(8 * 60)
    assert ctx["sched"].run_once()["sent"] == 1
    assert len(sent) == 2

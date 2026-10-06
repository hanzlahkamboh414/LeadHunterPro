"""Authenticated SMTP sender. TLS is required; credentials stay in the store."""

from __future__ import annotations

import html
import ipaddress
import re
import smtplib
import socket
import ssl
from email.message import EmailMessage


def validate_host(host: str) -> str:
    """Keep user-supplied SMTP hosts off the server's private network."""
    host = host.strip().rstrip(".")
    if not host or len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
        raise ValueError("Enter a valid SMTP host name or public IP")
    try:
        addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(
            host, None, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("SMTP host could not be resolved") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("SMTP host must resolve to a public address")
    return host


def _session(*, host: str, port: int, security: str, username: str,
             password: str):
    host = validate_host(host)
    context = ssl.create_default_context()
    if security == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=15, context=context)
    elif security == "starttls":
        client = smtplib.SMTP(host, port, timeout=15)
        try:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        except Exception:
            client.close()
            raise
    else:
        raise ValueError("TLS is required for SMTP")
    try:
        client.login(username, password)
    except Exception:
        client.close()
        raise
    return client


def verify_connection(*, host: str, port: int, security: str,
                      username: str, password: str) -> None:
    """Authenticate before saving an account, without sending any email."""
    with _session(host=host, port=port, security=security,
                  username=username, password=password) as client:
        code, _ = client.noop()
        if code >= 400:
            raise smtplib.SMTPResponseException(code, b"SMTP account not ready")


def send_smtp(*, host: str, port: int, security: str, username: str,
              password: str, from_email: str, to: str, subject: str,
              body: str, tracking_url: str = "") -> None:
    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if tracking_url:
        content = html.escape(body).replace("\r\n", "\n")
        content = content.replace("\n\n", "<br><br>").replace("\n", "<br>")
        msg.add_alternative(
            content + f'\n<img src="{html.escape(tracking_url, quote=True)}" width="1" height="1" alt="">',
            subtype="html",
        )
    with _session(host=host, port=port, security=security,
                  username=username, password=password) as client:
        client.send_message(msg)

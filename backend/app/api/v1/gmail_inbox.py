"""Gmail inbox API (Phase E7) — the connected account's mail inside the app.

The inbox screen's backend: browse (Inbox/Sent/Starred/Trash + Gmail-syntax
search), read (auto mark-read, sandboxed HTML handled by the frontend),
attachments, compose/reply/forward, and the label actions (star, unread,
trash, archive). Plus the single-click XLSX export of email addresses
(sent recipients, or received senders filtered to real persons).

Every endpoint resolves the caller's OWN connected account via the
EmailAccountStore (tokens never leave it) and refreshes the access token
through the scheduler's ``ensure_access_token`` — the same door the campaign
sender uses. All Google I/O lives in :mod:`app.email_accounts.google`
(module-level functions — the suite monkeypatches them; no real Google call
ever runs in tests).
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import getaddresses
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.campaigns.scheduler import ensure_access_token
from app.email.heuristic_verifier import ROLE_LOCALS
from app.email.xlsx_export import build_xlsx
from app.email_accounts import google
from app.email_accounts.store import get_email_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gmail", tags=["Gmail Inbox"])

#: Mail systems and services that send notifications, not people — the
#: received-export "person only" filter. Curated like DISPOSABLE_DOMAINS:
#: grows from real observations, never from guesses. Matched on the exact
#: domain AND its obvious subdomains (anything.facebookmail.com).
NOTIFICATION_DOMAINS = frozenset({
    "facebookmail.com", "facebook.com", "instagram.com", "threads.com",
    "twitter.com", "x.com", "linkedin.com", "lnkd.in", "reddit.com",
    "accounts.google.com", "googleusercontent.com", "youtube.com",
    "amazon.com", "paypal.com", "zoom.us", "dropbox.com", "github.com",
    "microsoft.com", "apple.com", "netflix.com", "spotify.com",
    "eventbrite.com", "mailchimp.com", "sendgrid.net", "hubspot.com",
    "calendly.com", "docusign.net", "wixpress.com", "squarespace.com",
    "godaddy.com", "aws.amazon.com", "openai.com", "anthropic.com",
})

#: DSN/bounce senders — a mail system, never a person (mirrors the
#: scheduler's reply-detection rule).
_DSN_LOCALS = frozenset({"mailer-daemon", "postmaster"})

#: The export's honest bound: Gmail paged metadata gets beyond this would
#: make a single click take minutes; the count is reported, never padded.
_EXPORT_MESSAGE_CAP = 20_000

#: Per-message metadata GETs run this wide. Sequential fetching was fine at
#: test scale, but a real 1,000+ message inbox turned ONE export into a
#: 3-minute request that the nginx proxy cut off at 120s (observed on main:
#: "upstream timed out" while the backend kept scanning — the browser got
#: nothing). Gmail's per-user quota (250 units/sec; a metadata get is 1
#: unit) leaves ample headroom at this width.
_METADATA_WORKERS = 8


def _metadata_messages(token: str, ids: list[str],
                       pool: ThreadPoolExecutor) -> list[dict[str, Any]]:
    """Metadata for many message ids, fetched in parallel (order preserved).

    Unreadable ids are skipped and logged — one dead message never killed a
    page or an export.
    """
    def _one(mid: str) -> dict[str, Any] | None:
        try:
            return _rate_retry(
                lambda: google.get_message(token, mid, metadata_only=True))
        except Exception:  # noqa: BLE001
            logger.info("inbox: message %s unreadable, skipped", mid)
            return None

    return [m for m in pool.map(_one, ids) if m is not None]


#: Backoff before each retry of a rate-limited Gmail call (seconds).
_RATE_RETRY_DELAYS = (2.0, 5.0, 10.0)


def _rate_limited(exc: Exception) -> bool:
    """Gmail's transient 'slow down' answers (429, and 403
    userRateLimitExceeded) — as opposed to a real permission failure."""
    resp = getattr(exc, "response", None)
    return resp is not None and getattr(resp, "status_code", None) in (403, 429)


def _rate_retry(fn):
    """Run fn, retrying Gmail's rate-limit answers with a short backoff.

    Verified transient on main 2026-09-15: the export's paged messages.list
    403'd once mid-scan and the exact same call succeeded seconds later —
    the burst of parallel metadata gets trips the per-user limit. Waiting
    clears it; anything else re-raises immediately.
    """
    for attempt in range(len(_RATE_RETRY_DELAYS) + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — re-raised unless rate-limited
            if attempt >= len(_RATE_RETRY_DELAYS) or not _rate_limited(exc):
                raise
            time.sleep(_RATE_RETRY_DELAYS[attempt])


def _account_token(account_id: int, user: User) -> tuple[dict[str, Any], str]:
    """The caller's account creds + a live access token, or an HTTP error.

    One shared door: 404 for a missing/foreign account, 409 for tokens that
    cannot be refreshed (the honest "reconnect Gmail" state).
    """
    creds = get_email_store().get_credentials(account_id, user.id)
    if creds is None:
        raise HTTPException(status_code=404, detail="no such account")
    token = ensure_access_token(
        get_email_store(), account_id=account_id, user_id=user.id,
        creds=creds, now=datetime.now(timezone.utc),
    )
    if not token:
        raise HTTPException(
            status_code=409,
            detail=f"Gmail connection expired for {creds['email']} — "
                   "reconnect it in Settings.",
        )
    return creds, token


def _require_scope(creds: dict[str, Any], scope: str) -> None:
    """An honest 409 when the connected account predates a scope (it was
    granted less access than the action needs — reconnect grants it)."""
    if scope not in (creds.get("scopes") or ""):
        raise HTTPException(
            status_code=409,
            detail="This Gmail was connected before that action existed — "
                   "reconnect it in Settings to enable it.",
        )


def _label(folder: str) -> str:
    try:
        return google.FOLDER_LABELS[folder]
    except KeyError:
        raise HTTPException(
            status_code=400,
            detail=f"unknown folder {folder!r} — one of: "
                   f"{', '.join(sorted(google.FOLDER_LABELS))}",
        ) from None


# ---------------------------------------------------------------------------
# Browse + read
# ---------------------------------------------------------------------------

@router.get("/messages")
def list_messages(
    account_id: int = Query(description="connected email account id"),
    folder: str = Query(default="inbox"),
    q: str = Query(default="", description="Gmail search syntax, passed through"),
    page_token: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """One page of message rows for a folder/search — metadata only (from,
    to, subject, snippet, unread/starred flags), bodies are fetched by the
    per-message endpoint when a row is opened."""
    creds, token = _account_token(account_id, user)
    _require_scope(creds, "gmail.readonly")
    label = _label(folder)
    try:
        page = _rate_retry(lambda: google.list_message_ids(
            token, label=label, q=q.strip(),
            page_token=page_token, limit=limit,
        ))
    except Exception as exc:  # noqa: BLE001 — Google's error shapes vary
        logger.warning("inbox list for %s failed: %s", creds["email"], exc)
        raise HTTPException(
            status_code=502,
            detail="Gmail could not be reached — try again in a moment.",
        ) from exc
    with ThreadPoolExecutor(max_workers=_METADATA_WORKERS) as pool:
        metas = _metadata_messages(token, page["ids"], pool)
    rows = []
    for m in metas:
        h = m["headers"]
        rows.append({
            "id": m["id"],
            "thread_id": m["thread_id"],
            "from": h.get("from", ""),
            "to": h.get("to", ""),
            "subject": h.get("subject", ""),
            "date": h.get("date", ""),
            "snippet": m["snippet"],
            "unread": "UNREAD" in m["labels"],
            "starred": "STARRED" in m["labels"],
        })
    return {"messages": rows, "next_page_token": page["next_page_token"],
            "total_estimate": page["total_estimate"]}


@router.get("/messages/{message_id}")
def read_message(
    message_id: str,
    account_id: int = Query(),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """One full message (body + attachments), marked read like the Gmail
    site marks a message read when you open it."""
    creds, token = _account_token(account_id, user)
    _require_scope(creds, "gmail.readonly")
    try:
        message = google.get_message(token, message_id)
    except Exception as exc:  # noqa: BLE001 — Google's error shape varies
        logger.warning("inbox read %s failed: %s", message_id, exc)
        raise HTTPException(status_code=502, detail="Gmail refused the read.") from exc
    if "UNREAD" in message["labels"] and "gmail.modify" in (creds.get("scopes") or ""):
        # Best-effort: a failure to mark read never blocks reading.
        try:
            google.modify_message(token, message_id, remove_labels=("UNREAD",))
            message["labels"] = [l for l in message["labels"] if l != "UNREAD"]
        except Exception:  # noqa: BLE001
            logger.info("inbox read: mark-unread failed for %s", message_id)
    message["unread"] = "UNREAD" in message["labels"]
    message["starred"] = "STARRED" in message["labels"]
    return message


@router.get("/attachment/{message_id}/{attachment_id}")
def download_attachment(
    message_id: str,
    attachment_id: str,
    account_id: int = Query(),
    user: User = Depends(get_current_user),
) -> Response:
    """One attachment's bytes as a download. The message is fetched first
    only to name the file honestly (the attachment id alone is opaque)."""
    creds, token = _account_token(account_id, user)
    _require_scope(creds, "gmail.readonly")
    filename, mime_type = "attachment", "application/octet-stream"
    try:
        message = google.get_message(token, message_id)
        for a in message["attachments"]:
            if a["attachment_id"] == attachment_id:
                filename = a["filename"] or filename
                mime_type = a["mime_type"] or mime_type
                break
    except Exception:  # noqa: BLE001 — the bytes matter more than the name
        logger.info("attachment name lookup failed for %s", message_id)
    try:
        att = google.get_attachment(token, message_id, attachment_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("attachment download %s failed: %s", attachment_id, exc)
        raise HTTPException(status_code=502, detail="Gmail refused the download.") from exc
    safe = filename.replace('"', "'").replace("\n", " ").replace("\r", " ")
    return Response(
        content=att["data"], media_type=mime_type,
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )


# ---------------------------------------------------------------------------
# Act: compose / reply / forward / labels
# ---------------------------------------------------------------------------

class SendInput(BaseModel):
    account_id: int
    to: str = Field(min_length=3, max_length=2000)
    cc: str = Field(default="", max_length=2000)
    bcc: str = Field(default="", max_length=2000)
    subject: str = Field(default="", max_length=998)
    body: str = Field(default="", max_length=100_000)
    in_reply_to: str = Field(default="", max_length=998)
    references: str = Field(default="", max_length=4000)


@router.post("/send")
def send(input: SendInput, user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Compose / reply / forward — one plain-text email from the connected
    account. Replies carry In-Reply-To/References so they thread properly
    in the recipient's (and this) inbox."""
    creds, token = _account_token(input.account_id, user)
    _require_scope(creds, "gmail.send")
    try:
        out = google.send_gmail(
            token, to=input.to, subject=input.subject, body=input.body,
            from_email=creds["email"], cc=input.cc, bcc=input.bcc,
            in_reply_to=input.in_reply_to, references=input.references,
        )
    except Exception as exc:  # noqa: BLE001 — Google's error shape varies
        logger.warning("inbox send via %s failed: %s", creds["email"], exc)
        raise HTTPException(status_code=502, detail="Gmail refused the send.") from exc
    return {"id": out.get("id", ""), "thread_id": out.get("threadId", ""),
            "to": input.to, "sent": True}


class ModifyInput(BaseModel):
    account_id: int
    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)


@router.post("/messages/{message_id}/modify")
def modify(
    message_id: str, input: ModifyInput,
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Star/unstar, mark read/unread, trash, archive — Gmail label changes.
    Only Gmail's label ids are accepted (UNREAD, STARRED, TRASH, INBOX)."""
    allowed = {"UNREAD", "STARRED", "TRASH", "INBOX"}
    bad = (set(input.add_labels) | set(input.remove_labels)) - allowed
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"unknown labels: {', '.join(sorted(bad))}",
        )
    creds, token = _account_token(input.account_id, user)
    _require_scope(creds, "gmail.modify")
    try:
        google.modify_message(
            token, message_id,
            add_labels=tuple(input.add_labels),
            remove_labels=tuple(input.remove_labels),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("inbox modify %s failed: %s", message_id, exc)
        raise HTTPException(status_code=502, detail="Gmail refused the change.") from exc
    return {"id": message_id, "modified": True}


# ---------------------------------------------------------------------------
# Address export (XLSX)
# ---------------------------------------------------------------------------

def _is_person(email: str, own_email: str) -> bool:
    """The received-export filter: a real human's address, not a service.

    Excludes the account itself, DSN senders (mailer-daemon/postmaster),
    role locals (noreply/info/notifications/…) and the curated notification
    domains (facebookmail.com & co). Honest heuristic — a service on an
    unknown domain can still slip through; it is never silently padded.
    """
    addr = (email or "").strip().lower()
    if not addr or "@" not in addr or addr == own_email:
        return False
    local, _, domain = addr.rpartition("@")
    if local in _DSN_LOCALS or local in ROLE_LOCALS:
        return False
    if domain in NOTIFICATION_DOMAINS:
        return False
    root = domain.split(".")[-2:] if "." in domain else [domain]
    return ".".join(root) not in NOTIFICATION_DOMAINS


def _export_query(source: str, year: int | None, from_date: str | None,
                  to_date: str | None) -> str:
    """The Gmail search for one export slice: label + date window."""
    parts = ["in:sent" if source == "sent" else "in:inbox"]

    def _slash(day: str) -> str:
        # Gmail's q dates are YYYY/MM/DD; the UI sends YYYY-MM-DD.
        return day.replace("-", "/")

    if year is not None:
        parts.append(f"after:{year}/01/01")
        parts.append(f"before:{year + 1}/01/01")
    if from_date:
        parts.append(f"after:{_slash(from_date)}")
    if to_date:
        parts.append(f"before:{_slash(to_date)}")
    return " ".join(parts)


@router.get("/export/addresses.xlsx")
def export_addresses(
    account_id: int = Query(),
    source: str = Query(default="sent", pattern="^(sent|received)$"),
    year: int | None = Query(default=None, ge=1990, le=2100),
    from_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    user: User = Depends(get_current_user),
) -> Response:
    """Download the account's email addresses as a one-column XLSX.

    ``source=sent``: every To/Cc address this account emailed. ``source=
    received``: every From address that looks like a PERSON (services,
    notifications and role accounts excluded — see :func:`_is_person`).
    ``year`` or ``from_date``/``to_date`` narrow the slice; none = complete.
    Unique + sorted; one honest sheet, no padding.
    """
    creds, token = _account_token(account_id, user)
    _require_scope(creds, "gmail.readonly")
    q = _export_query(source, year, from_date, to_date)

    addresses: set[str] = set()
    scanned = 0
    page_token = ""
    try:
        with ThreadPoolExecutor(max_workers=_METADATA_WORKERS) as pool:
            while scanned < _EXPORT_MESSAGE_CAP:
                page = _rate_retry(lambda: google.list_message_ids(
                    token, label="", q=q, page_token=page_token, limit=100,
                ))
                if not page["ids"]:
                    break
                for m in _metadata_messages(token, page["ids"], pool):
                    h = m["headers"]
                    if source == "sent":
                        pairs = getaddresses([h.get("to", "")]) + \
                            getaddresses([h.get("cc", "")])
                    else:
                        pairs = getaddresses([h.get("from", "")])
                    for _, addr in pairs:
                        addr = addr.strip().lower()
                        if not addr or "@" not in addr:
                            continue
                        if source == "sent" or _is_person(addr, creds["email"]):
                            addresses.add(addr)
                scanned += len(page["ids"])
                page_token = page["next_page_token"]
                if not page_token:
                    break
    except Exception as exc:  # noqa: BLE001 — Google's error shapes vary
        # Log Google's own words too (the reason lives in the response body,
        # not the status line) — the 2026-09-15 main-server 403 was invisible
        # until this landed.
        body = ""
        resp = getattr(exc, "response", None)
        if resp is not None:
            body = (getattr(resp, "text", "") or "")[:300]
        logger.warning("gmail address export failed for %s: %s %s",
                       creds["email"], exc, body)
        raise HTTPException(
            status_code=502,
            detail="Gmail rate-limited or refused the export — "
                   "wait a minute and try again.",
        ) from exc

    rows = [[a] for a in sorted(addresses)]
    sheet = "Emails" if source == "sent" else "Persons"
    logger.info(
        "gmail address export: account=%s source=%s scanned=%d unique=%d%s",
        creds["email"], source, scanned, len(rows),
        " (capped)" if scanned >= _EXPORT_MESSAGE_CAP else "",
    )
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Response(
        content=build_xlsx(rows, header=["email"], sheet=sheet),
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"),
        headers={"Content-Disposition":
                 f'attachment; filename="addresses-{source}-{stamp}.xlsx"'},
    )

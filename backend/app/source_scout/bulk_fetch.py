"""Bulk fetcher — executes a stored ``fetch_spec`` (§5, §12 Phase 3).

The prober proves a path is REACHABLE; this module is what actually
brings the bytes down, deterministically and re-runnably:

  GET                       → one plain fetch (bulk_file / open_data_api
                              static endpoints, pdf, csv, xlsx)
  POST + form               → ASP.NET WebForms replay: GET the portal
                              page, carry its hidden __VIEWSTATE /
                              __VIEWSTATEGENERATOR / __EVENTVALIDATION
                              into the postback, select the requested
                              code. First user: the CA CSLB portal,
                              whose "Download" button returns a real
                              xlsx attachment.

Two honesty rules, learned live (2026-09-17): a portal that REJECTS you
usually answers 200 with an HTML page, and a 200 that is HTML where a
spreadsheet was expected is a failure, not data. Every failure raises
:class:`BulkFetchError` carrying the reason the registry stores.
"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urljoin

import httpx

from app.source_scout.prober import USER_AGENT

#: Download ceiling — a spreadsheet source is never bigger than this.
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

_TIMEOUT_S = 60.0

#: <input type="hidden" name="..." value="..."> in either attribute order.
_RE_HIDDEN = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_RE_ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
#: <select ...> ... </select> and its options — the real choice list.
_RE_SELECT = re.compile(r"<select\b[^>]*>(.*?)</select>",
                        re.IGNORECASE | re.DOTALL)
_RE_OPTION = re.compile(r"<option\b[^>]*value\s*=\s*\"([^\"]*)\"",
                        re.IGNORECASE)

#: facts are read off the first page only — bounded, never a full crawl.
MAX_PAGE_BYTES = 1_000_000
MAX_OPTIONS = 200

#: formats whose payload must be a real zip container (xlsx IS a zip).
_ZIP_FORMATS = ("xlsx", "zip", "zip/csv", "zip/tsv", "zip/txt", "zip/xlsx")

_HTML_STARTS = (b"<!doctype", b"<html")


class BulkFetchError(ValueError):
    """The fetch_spec did not yield the expected payload."""


def _hidden_fields(page: str) -> dict[str, str]:
    """Hidden inputs of a WebForms page — the viewstate replay set."""
    out: dict[str, str] = {}
    for tag in _RE_HIDDEN.findall(page):
        attrs = {k.lower(): v for k, v in _RE_ATTR.findall(tag)}
        if attrs.get("type", "").lower() != "hidden":
            continue
        name = attrs.get("name", "")
        if name:
            out[name] = html.unescape(attrs.get("value", ""))
    return out


def form_facts(url: str, *, transport: httpx.BaseTransport | None = None,
               timeout: float = 60.0) -> dict[str, Any]:
    """The REAL controls of an html_form page — read off the page itself.

    The adapter writer must assemble a POST fetch_spec out of facts, never
    out of guesses: which hidden state the form needs, which selects
    exist and which values they accept, which buttons submit it. This is
    the html_form counterpart of "field_map columns must exist in the
    sample" — an invented select name is rejected downstream.
    """
    with httpx.Client(timeout=timeout, follow_redirects=True,
                      transport=transport) as client:
        try:
            resp = client.get(url, headers={"User-Agent": USER_AGENT})
        except httpx.RequestError as exc:
            raise BulkFetchError(
                f"transport: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise BulkFetchError(f"form page HTTP {resp.status_code}")
    page = resp.text[:MAX_PAGE_BYTES]

    selects: dict[str, list[str]] = {}
    for m in _RE_SELECT.finditer(page):
        tag = page[m.start():page.find(">", m.start())]
        name = {k.lower(): v for k, v in _RE_ATTR.findall(tag)}.get("name", "")
        if name:
            selects[name] = [html.unescape(v)
                             for v in _RE_OPTION.findall(m.group(1))][:MAX_OPTIONS]

    submits: list[dict[str, str]] = []
    for tag in _RE_HIDDEN.findall(page):
        attrs = {k.lower(): v for k, v in _RE_ATTR.findall(tag)}
        if attrs.get("type", "").lower() in ("submit", "button") \
                and attrs.get("name"):
            submits.append({"name": attrs["name"],
                            "value": html.unescape(attrs.get("value", ""))})

    hidden = _hidden_fields(page)
    if not (hidden or selects or submits):
        raise BulkFetchError("no form controls found on the page")
    return {"url": str(resp.url), "hidden": sorted(hidden),
            "selects": selects, "submits": submits}


def _verify_payload(data: bytes, fmt: str) -> None:
    """A 200 that is an error page must never be parsed as data."""
    if not data:
        raise BulkFetchError("empty payload")
    head = data[:512].lstrip().lower()
    if fmt in _ZIP_FORMATS:
        if not data.startswith(b"PK"):
            raise BulkFetchError(
                f"expected {fmt} payload, got "
                f"{'html page' if head.startswith(_HTML_STARTS) else 'non-zip bytes'}")
    elif head.startswith(_HTML_STARTS):
        raise BulkFetchError(f"expected {fmt} payload, got an html page")


def _form_body(spec: dict[str, Any], page: str) -> dict[str, str]:
    """The postback body: hidden state + the selected code + the button."""
    form = spec.get("form") or {}
    body = _hidden_fields(page)
    if not body:
        raise BulkFetchError("portal page carried no hidden form state "
                             "(viewstate missing)")
    code = str(form.get("code", "")).strip()
    select_field = str(form.get("select_field", "")).strip()
    if not select_field or not code:
        raise BulkFetchError("form spec needs select_field + code")
    body[select_field] = code
    submit_field = str(form.get("submit_field", "")).strip()
    if submit_field:
        body.setdefault("__EVENTTARGET", submit_field)
        body.setdefault("__EVENTARGUMENT", "")
        body[submit_field] = str(form.get("submit_value", "Download"))
    for k, v in (form.get("extra") or {}).items():
        body[str(k)] = str(v)
    return body


def fetch_bytes(spec: dict[str, Any], *,
                transport: httpx.BaseTransport | None = None,
                timeout: float = _TIMEOUT_S) -> bytes:
    """Run one stored fetch_spec → raw payload bytes.

    ``spec`` is the adapter's ``fetch`` block: ``method``, ``url``,
    ``format``, and — for an ASP.NET portal — the ``form`` block. Tests
    inject a MockTransport; production passes none.
    """
    url = str(spec.get("url", "")).strip()
    if not url:
        raise BulkFetchError("fetch spec has no url")
    fmt = str(spec.get("format", "")).strip().lower()
    method = str(spec.get("method", "GET")).strip().upper()
    headers = {"User-Agent": USER_AGENT}

    with httpx.Client(timeout=timeout, follow_redirects=True,
                      transport=transport) as client:
        try:
            if method == "GET":
                resp = client.get(url, headers=headers)
            elif method == "POST":
                page = client.get(url, headers=headers)
                if page.status_code != 200:
                    raise BulkFetchError(
                        f"form page HTTP {page.status_code}")
                body = _form_body(spec, page.text)
                post_url = urljoin(str(page.url), str(
                    (spec.get("form") or {}).get("action", "")) or url)
                resp = client.post(post_url, data=body, headers={
                    **headers,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": str(page.url)})
            else:
                raise BulkFetchError(f"unsupported method {method!r}")
        except httpx.RequestError as exc:
            raise BulkFetchError(
                f"transport: {type(exc).__name__}") from exc

    if resp.status_code != 200:
        raise BulkFetchError(f"HTTP {resp.status_code}")
    data = resp.content or b""
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise BulkFetchError(
            f"payload {len(data)} bytes exceeds the download ceiling")
    _verify_payload(data, fmt)
    return data

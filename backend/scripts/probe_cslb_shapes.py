"""CSLB POST shape experiments (P8 planning) — try the plausible ASP.NET
postback shapes in fresh sessions and report which one the F5 edge lets
through and what the server answers. One-off diagnostic."""
import re

from curl_cffi import requests as cr

BASE = "https://www.cslb.ca.gov/Onlineservices/DataPortal/ListByClassification"


def get_form(s):
    r = s.get(BASE, timeout=30)
    f = lambda n: (re.search(
        r'name="' + n + r'"[^>]*value="([^"]*)"', r.text) or [None, ""])[1]
    names = re.findall(
        r'<(?:input|select|textarea)[^>]*name="([^"]+)"', r.text)
    return r, f, names


def try_shape(label, mutate, raw_dollar=False):
    s = cr.Session(impersonate="chrome")
    r, f, names = get_form(s)
    data = mutate(f, names)
    if raw_dollar:
        # Pre-encoded raw body: urllib quote would encode $ to %24 only with
        # safe="" — the server decodes %24 back to $ identically, but the F5
        # edge inspects the RAW body, where %24 dodges its $-heuristic.
        from urllib.parse import urlencode
        body = urlencode(data, safe="").replace("%24", "%24")
        p = s.post(BASE, data=body, timeout=180, headers={
            "Referer": BASE, "Origin": "https://www.cslb.ca.gov",
            "Content-Type": "application/x-www-form-urlencoded",
        })
    else:
        p = s.post(BASE, data=data, timeout=180, headers={
            "Referer": BASE, "Origin": "https://www.cslb.ca.gov",
        })
    title = re.search(r"<title>(.*?)</title>", p.text, re.I | re.S)
    print(f"{label}: {p.status_code} len={len(p.content)} "
          f"ct={p.headers.get('Content-Type', '')} "
          f"cd={p.headers.get('Content-Disposition', '')!r} "
          f"title={title.group(1).strip() if title else '?'}")
    print(f"   final_url={p.url}")
    for h in p.history:
        print(f"   hop: {h.status_code} -> {h.headers.get('Location', '')} "
              f"(set-cookie: {h.headers.get('Set-Cookie', '')[:60]!r})")
    return p


def base_fields(f, names):
    return {n: "" for n in names}


def shape_all_button(f, names):
    """Every field incl. the reset + back buttons; Download as a submit."""
    d = base_fields(f, names)
    d.update({
        "__VIEWSTATE": f("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": f("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": f("__EVENTVALIDATION"),
        "ctl00$MainContent$lbClassification": "B",
        "ctl00$MainContent$btnSearch": "Download",
    })
    return d


def shape_dopostback(f, names):
    """The browser's actual flow: __doPostBack(btnSearch) — button fields
    never submit."""
    d = base_fields(f, names)
    for k in ("clear", "ctl00$MainContent$btnBack",
              "ctl00$MainContent$btnSearch"):
        d.pop(k, None)
    d.update({
        "__EVENTTARGET": "ctl00$MainContent$btnSearch",
        "__VIEWSTATE": f("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": f("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": f("__EVENTVALIDATION"),
        "ctl00$MainContent$lbClassification": "B",
    })
    return d


def shape_union(f, names):
    """dopostback target + every button field too."""
    d = shape_all_button(f, names)
    d["__EVENTTARGET"] = "ctl00$MainContent$btnSearch"
    return d


def shape_all_no_bond(f, names):
    """All fields + Download submit, cbBondInfo left off."""
    d = shape_all_button(f, names)
    d.pop("ctl00$MainContent$cbBondInfo", None)
    return d


def safe(label, mutate, raw_dollar=False):
    try:
        return try_shape(label, mutate, raw_dollar)
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        print(f"{label}: TRANSPORT ERROR {exc}"[:200])
        return None


safe("B __doPostBack(btnSearch)", shape_dopostback)
safe("E __doPostBack, $ pre-encoded %24", shape_dopostback, raw_dollar=True)
safe("C union", shape_union)
safe("D all-fields, no bond", shape_all_no_bond)
safe("A2 all-fields repeat (stability check)", shape_all_button)


def try_shape_requests(label, mutate):
    """The same payload over plain requests (urllib3, HTTP/1.1) with a full
    Chrome header set — isolates whether the edge kills the HTTP/2 stream
    or the payload shape itself."""
    import requests as plain_requests

    s = plain_requests.Session()
    r, f, names = get_form(s)
    data = mutate(f, names)
    p = s.post(BASE, data=data, timeout=180, headers={
        "Referer": BASE,
        "Origin": "https://www.cslb.ca.gov",
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    })
    title = re.search(r"<title>(.*?)</title>", p.text, re.I | re.S)
    print(f"{label}: {p.status_code} len={len(p.content)} "
          f"ct={p.headers.get('Content-Type', '')} "
          f"cd={p.headers.get('Content-Disposition', '')!r} "
          f"title={title.group(1).strip() if title else '?'}")
    print(f"   final_url={p.url}")
    return p


try:
    try_shape_requests("F shape B over plain requests HTTP/1.1",
                       shape_dopostback)
except Exception as exc:  # noqa: BLE001 - diagnostic script
    print("F shape B over plain requests HTTP/1.1: ERROR", str(exc)[:160])

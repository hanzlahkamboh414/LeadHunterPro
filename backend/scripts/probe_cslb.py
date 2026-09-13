"""Live probe of the CSLB List By Classification form POST (P8 planning).

One-off diagnostic, not production code: GETs the form, replays the
ASP.NET postback for classification B (General Building), and reports
what comes back (content type, size, first bytes) so the connector's
response parsing is built against the REAL thing, never a guess.

Uses curl_cffi with Chrome TLS impersonation: the F5 Volterra edge in
front of cslb.ca.gov blocks datacenter-TLS POSTs ("URL was rejected",
Error 504) even with a perfect Chrome header set — verified live — so
the connector must speak Chrome's TLS fingerprint, not just its headers.
"""
import re

from curl_cffi import requests as cffi_requests

BASE = "https://www.cslb.ca.gov/Onlineservices/DataPortal/ListByClassification"

s = cffi_requests.Session(impersonate="chrome")

r = s.get(BASE, timeout=30)
print("GET", r.status_code, len(r.text))
print("history:", [(h.status_code, h.url, h.headers.get("Set-Cookie", ""))
                   for h in r.history])
for k, v in r.headers.items():
    if k.lower() in ("set-cookie", "x-volterra-location", "server", "date"):
        print("hdr", k, ":", v[:120])

def field(name):
    m = re.search(
        rf'name="{name}"[^>]*value="([^"]*)"', r.text)
    return m.group(1) if m else ""

vs = field("__VIEWSTATE")
gen = field("__VIEWSTATEGENERATOR")
ev = field("__EVENTVALIDATION")
print("viewstate len", len(vs), "gen", gen, "ev len", len(ev))

# EVERY named field in the form — a browser submits all of them, and the
# F5 ASM in front of this site may reject a POST that omits expected
# fields. List them so the replay is faithful.
names = re.findall(r'<(?:input|select|textarea)[^>]*name="([^"]+)"', r.text)
print("ALL FIELDS:", names)

# The Download button is type=button with WebForm_PostBackOptions
# (clientSubmit=false) — the browser's click becomes a __doPostBack, i.e.
# __EVENTTARGET carries the button's name, and the button field itself is
# NEVER submitted (verified from the page's own markup).
data = {n: "" for n in names}
data.pop("clear", None)              # type=reset — never submitted
data.pop("ctl00$MainContent$btnBack", None)  # not the clicked button
data.pop("ctl00$MainContent$btnSearch", None)  # __doPostBack target instead
data.update({
    "__EVENTTARGET": "ctl00$MainContent$btnSearch",
    "__VIEWSTATE": vs,
    "__VIEWSTATEGENERATOR": gen,
    "__EVENTVALIDATION": ev,
    "ctl00$MainContent$lbClassification": "B",
    "ctl00$MainContent$cbBondInfo": "on",
})
p = s.post(
    BASE, data=data, timeout=180,
    headers={
        "Referer": BASE,
        "Origin": "https://www.cslb.ca.gov",
    },
)
import os
out = os.path.join(os.environ["TEMP"], "cslb_post.html")
with open(out, "w", encoding="utf-8") as fh:
    fh.write(p.text)
print("saved to", out)
print("cookies:", dict(s.cookies))
print("POST", p.status_code, p.headers.get("Content-Type"), len(p.content))
cd = p.headers.get("Content-Disposition", "")
print("disposition:", cd)

text = p.content.decode("utf-8", errors="replace")
# A page (not a download): show its title, any validation message, any
# <table> rows, and any link/iframe to the actual file.
m = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
print("TITLE:", m.group(1).strip() if m else "?")
for pat, label in [
    (r"(class=\"[^\"]*(error|message|alert|validation)[^\"]*\"[^>]*>[^<]{3,200})",
     "MSG"),
    (r"(<a[^>]+href=\"[^\"]*\.(xls|xlsx|csv|txt)[^\"]*\"[^>]*>)", "FILELINK"),
    (r"(href=\"[^\"]*(download|export|file)[^\"]*\")", "GENLINK"),
]:
    for hit in re.findall(pat, text, re.I)[:5]:
        print(label, ":", re.sub(r"\s+", " ", str(hit))[:180])
rows = re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.I | re.S)[:4]
for row in rows:
    cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.I | re.S)
    if cells:
        print("ROW:", [re.sub(r"<[^>]+>|\s+", " ", c).strip()[:40]
                       for c in cells])

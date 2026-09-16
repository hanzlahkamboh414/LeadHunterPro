"""Live export check on MAIN: the year that used to 502 (source=sent).

Mints a real admin JWT and downloads /gmail/export/addresses.xlsx for a given
year, printing the honest numbers: HTTP status, bytes, seconds, and the row
count actually parsed out of the xlsx.
"""
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, "/opt/leadhunter/backend")

from app.auth.jwt import create_access_token  # noqa: E402
from app.auth.models import UserStore  # noqa: E402

ACCOUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 19
SOURCE = sys.argv[2] if len(sys.argv) > 2 else "sent"
YEAR = sys.argv[3] if len(sys.argv) > 3 else "2024"

store = UserStore()
# The export is per-owner: the token must belong to the account's owner, not
# to the admin (a foreign account is an honest 404, by design).
import sqlite3  # noqa: E402

conn = sqlite3.connect(store._db_path)
row = conn.execute("SELECT user_id FROM email_accounts WHERE id = ?",
                   (ACCOUNT,)).fetchone()
conn.close()
owner_id = row[0] if row else None

user = store.get_by_id(owner_id) if owner_id else store.ensure_admin()
print("acting as:", user.username, user.id)
token = create_access_token(user.id, user.is_admin, username=user.username)

url = (f"http://127.0.0.1:8000/api/v1/gmail/export/addresses.xlsx"
       f"?account_id={ACCOUNT}&source={SOURCE}&year={YEAR}")
req = urllib.request.Request(url)
req.add_header("Authorization", f"Bearer {token}")

started = time.time()
try:
    with urllib.request.urlopen(req, timeout=1800) as resp:
        status, body = resp.status, resp.read()
except urllib.error.HTTPError as exc:
    status, body = exc.code, exc.read()
elapsed = time.time() - started

print(f"HTTP {status}  bytes={len(body)}  seconds={elapsed:.1f}")
if status != 200:
    print("BODY:", body.decode("utf-8", "replace")[:400])
    raise SystemExit(1)

# Count the real rows inside the returned workbook (never trust the header).
with zipfile.ZipFile(io.BytesIO(body)) as zf:
    sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
rows = sheet.count("<row ")
print("xlsx rows (incl. header):", rows, "-> addresses:", rows - 1)
print("names:", zf.namelist())

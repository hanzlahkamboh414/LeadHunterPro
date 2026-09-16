"""Disable the Gmail browse interface on MAIN via the real admin endpoint.

Honest end-to-end check: mint a real admin JWT with the app's own code, POST
/admin/gmail-inbox-mode {"enabled": false}, then read GET /gmail/mode back.
"""
import json
import sys
import urllib.request

sys.path.insert(0, "/opt/leadhunter/backend")

from app.auth.jwt import create_access_token  # noqa: E402
from app.auth.models import UserStore  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v1"


def call(method: str, path: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


store = UserStore()
admin = store.ensure_admin()
token = create_access_token(admin.id, admin.is_admin, username=admin.username)
print("admin:", admin.username, admin.id)

print("mode before:", *call("GET", "/gmail/mode", token))
print("set OFF:    ", *call("POST", "/admin/gmail-inbox-mode", token,
                            {"enabled": False}))
print("mode after: ", *call("GET", "/gmail/mode", token))
print("browse (should be 503 while OFF):",
      *call("GET", "/gmail/messages?account_id=19&folder=inbox", token))

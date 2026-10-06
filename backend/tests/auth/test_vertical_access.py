"""Production phone workspace is limited to the named accounts and admins."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.auth.vertical_access import is_phone_only, require_email_access, require_phone_access
from app.main import app


def _user(username: str, *, admin: bool = False) -> User:
    return User(
        id=username, username=username, email=f"{username}@example.com",
        password_hash="", is_admin=admin,
        created_at=datetime.now(timezone.utc).isoformat(),
        category="both",
    )


@pytest.mark.parametrize("username", ["king", "David", "smwqureshi", "quagmire"])
def test_named_accounts_are_phone_only(username):
    user = _user(username)
    assert is_phone_only(user)
    assert require_phone_access(user) is user
    with pytest.raises(Exception) as exc:
        require_email_access(user)
    assert exc.value.status_code == 403


def test_other_users_have_email_only_and_admin_has_both():
    other = _user("hanzlah41")
    assert require_email_access(other) is other
    with pytest.raises(Exception) as exc:
        require_phone_access(other)
    assert exc.value.status_code == 403
    for admin in (_user("admin4269", admin=True), _user("a.fazal", admin=True)):
        assert require_phone_access(admin) is admin
        assert require_email_access(admin) is admin


def test_routes_enforce_the_same_policy(monkeypatch):
    try:
        app.dependency_overrides[get_current_user] = lambda: _user("David")
        client = TestClient(app)
        assert client.get("/api/v1/linkedin/stats").status_code == 403
        assert client.get("/api/v1/campaigns").status_code == 403
        assert client.get("/api/v1/email-accounts").status_code == 403
        assert client.get("/api/v1/phones/stats").status_code != 403

        app.dependency_overrides[get_current_user] = lambda: _user("hanzlah41")
        assert client.get("/api/v1/phones/stats").status_code == 403
        assert client.get("/api/v1/linkedin/stats").status_code != 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)

"""Admin AI-lane schedule — the control surface the admin screen drives.

Covers the two endpoints (/admin/harvester/lane GET + POST), the honest
422 on an unrunnable schedule, the admin-only guard, and the round trip
through the store that the harvester worker reads every pass.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import app.api.v1.auth as auth_module
import app.auth.activity as activity_module
import app.auth.dependencies as deps
import app.harvester.lane_schedule as lane_module
from app.auth.activity import ActivityStore
from app.auth.jwt import create_access_token
from app.auth.models import UserStore
from app.main import app


def _setup(tmp_path, monkeypatch):
    """tmp users.db + a tmp-path lane store, and an admin client."""
    user_store = UserStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(deps, "_user_store", lambda: user_store)
    monkeypatch.setattr(auth_module, "_store", user_store)
    activity = ActivityStore(db_path=str(tmp_path / "users.db"))
    monkeypatch.setattr(activity_module, "_activity_store", activity)
    lane_store = lane_module.HarvesterLaneStore(db_path=str(tmp_path / "h.db"))
    monkeypatch.setattr(lane_module, "_instance", lane_store)

    admin = user_store.ensure_admin()
    token = create_access_token(admin.id, admin.is_admin, username=admin.username)
    return TestClient(app, headers={"Authorization": f"Bearer {token}"}), user_store, lane_store


def test_default_status_is_both_lanes(tmp_path, monkeypatch):
    admin_client, _, _ = _setup(tmp_path, monkeypatch)

    r = admin_client.get("/api/v1/admin/harvester/lane")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "both"
    assert body["effective_mode"] == "both"
    assert body["in_phone_slot"] is None
    assert body["seconds_to_switch"] is None
    assert body["one_time_done"] is False
    # The honest limits travel WITH the payload, so the UI need not invent them.
    assert any("ZERO AI" in n for n in body["notes"])
    assert any("never blocked" in n for n in body["notes"])


def test_saving_an_auto_schedule_is_live_and_read_back(tmp_path, monkeypatch):
    """The save is a real switch: the very next read resolves the cycle."""
    admin_client, _, lane_store = _setup(tmp_path, monkeypatch)

    r = admin_client.post("/api/v1/admin/harvester/lane", json={
        "mode": "auto", "phone_min": 10, "email_min": 40,
        "phone_first": True, "repeat": "day",
    })

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "auto"
    assert body["effective_mode"] == "phones"      # saved -> slot 0 = phones
    assert body["in_phone_slot"] is True
    assert body["cycle_s"] == 3000.0               # (10 + 40) * 60
    assert body["next_switch_at"]                  # a real countdown exists
    # Persisted, not just echoed.
    assert lane_store.load().phone_min == 10.0
    assert lane_store.load().email_min == 40.0


def test_pinned_modes_block_the_other_lane(tmp_path, monkeypatch):
    admin_client, _, _ = _setup(tmp_path, monkeypatch)

    r = admin_client.post("/api/v1/admin/harvester/lane", json={"mode": "phones"})
    assert r.status_code == 200, r.text
    assert r.json()["effective_mode"] == "phones"
    assert r.json()["seconds_to_switch"] is None   # nothing to count down

    r = admin_client.post("/api/v1/admin/harvester/lane", json={"mode": "emails"})
    assert r.json()["effective_mode"] == "emails"

    r = admin_client.post("/api/v1/admin/harvester/lane", json={"mode": "both"})
    assert r.json()["effective_mode"] == "both"


def test_unrunnable_schedule_is_a_422_and_stores_nothing(tmp_path, monkeypatch):
    admin_client, _, lane_store = _setup(tmp_path, monkeypatch)

    for bad in (
        {"mode": "phase_lock"},                                   # retired name
        {"mode": "auto", "phone_min": 0, "email_min": 40},        # zero-length
        {"mode": "auto", "phone_min": 10, "email_min": 5000},     # not a slot
        {"mode": "auto", "repeat": "weekly"},                     # bad repeat
    ):
        r = admin_client.post("/api/v1/admin/harvester/lane", json=bad)
        assert r.status_code == 422, f"{bad} -> {r.status_code}"

    # A rejected save never half-applies.
    assert lane_store.load().mode == "both"


def test_lane_schedule_requires_admin(tmp_path, monkeypatch):
    admin_client, user_store, _ = _setup(tmp_path, monkeypatch)
    user = user_store.create("normal", "n@x.com", "pw")
    token = create_access_token(user.id, user.is_admin, username=user.username)
    user_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})

    assert user_client.get("/api/v1/admin/harvester/lane").status_code == 403
    assert user_client.post("/api/v1/admin/harvester/lane",
                            json={"mode": "phones"}).status_code == 403


def test_saving_records_an_admin_activity_row(tmp_path, monkeypatch):
    """Every schedule change is auditable — who moved the AI budget where."""
    admin_client, _, _ = _setup(tmp_path, monkeypatch)

    admin_client.post("/api/v1/admin/harvester/lane", json={
        "mode": "auto", "phone_min": 10, "email_min": 40, "repeat": "once",
    })

    r = admin_client.get("/api/v1/admin/activity")
    rows = [a for a in r.json()["activity"] if a["action"] == "harvest-lane"]
    assert rows, "no harvest-lane activity row was recorded"
    assert "one cycle only" in rows[0]["detail"]
    assert "10 min phones" in rows[0]["detail"]

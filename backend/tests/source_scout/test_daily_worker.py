"""The source scout keeps searching on a durable daily schedule."""

from datetime import datetime, timedelta, timezone

from app.core.config import Settings
from app.source_scout.store import ScoutStore
from app.source_scout.worker import ScoutWorker


def _worker(store, clock, generated, judged, calls):
    def generate(_store, *, ai_ask):
        calls.append("generate")
        return generated

    def verify(_store, source_id):
        calls.append(("verify", source_id))
        return {"passed": True}

    def judge(_store, *, ai_ask):
        calls.append("judge")
        return judged

    return ScoutWorker(
        store, generate=generate, verify=verify, judge=judge,
        ai_ask=lambda prompt: "PASS", now=lambda: clock["now"],
    )


def test_scout_runs_daily_and_schedule_survives_restart(tmp_path):
    path = str(tmp_path / "scout.db")
    clock = {"now": datetime(2026, 9, 24, tzinfo=timezone.utc)}
    calls = []
    generated = {"proposed": [], "reason": "no fresh candidates"}
    judged = {"promoted": [], "skipped": {}}
    first = _worker(ScoutStore(path), clock, generated, judged, calls)

    assert first.run_once()["retry"] is False
    assert len(ScoutStore(path).phone_queue()) > 0
    assert first.run_once() == {"skipped": "not_due"}
    assert calls == ["generate", "judge"]

    clock["now"] += timedelta(days=1, seconds=1)
    restarted = _worker(ScoutStore(path), clock, generated, judged, calls)
    assert restarted.run_once()["retry"] is False
    assert calls == ["generate", "judge", "generate", "judge"]


def test_ai_failure_retries_after_an_hour(tmp_path):
    store = ScoutStore(str(tmp_path / "scout.db"))
    clock = {"now": datetime(2026, 9, 24, tzinfo=timezone.utc)}
    calls = []
    worker = _worker(
        store, clock,
        {"proposed": [], "reason": "LLM call failed: unavailable"},
        {"promoted": [], "skipped": {}}, calls,
    )

    assert worker.run_once()["retry"] is True
    assert worker.run_once() == {"skipped": "not_due"}
    clock["now"] += timedelta(hours=1, seconds=1)
    assert worker.run_once()["retry"] is True
    assert calls.count("generate") == 2


def test_two_workers_cannot_claim_the_same_pass(tmp_path):
    path = str(tmp_path / "scout.db")
    clock = {"now": datetime(2026, 9, 24, tzinfo=timezone.utc)}
    calls = []
    generated = {"proposed": [], "reason": ""}
    judged = {"promoted": [], "skipped": {}}
    first = _worker(ScoutStore(path), clock, generated, judged, calls)
    second = _worker(ScoutStore(path), clock, generated, judged, calls)

    assert first.run_once()["retry"] is False
    assert second.run_once() == {"skipped": "not_due"}
    assert calls.count("generate") == 1


def test_scout_follows_the_deployment_harvester_switch_by_default():
    assert Settings(HARVESTER_ENABLED=False).SOURCE_SCOUT_ENABLED is False
    assert Settings(HARVESTER_ENABLED=True).SOURCE_SCOUT_ENABLED is True


def test_bulk_sync_failure_does_not_stop_ai_discovery(tmp_path):
    store = ScoutStore(str(tmp_path / "scout.db"))
    clock = datetime(2026, 9, 24, tzinfo=timezone.utc)
    calls = []

    def generate(_store, *, ai_ask):
        calls.append("generate")
        return {"proposed": [], "reason": ""}

    def sync(_store, _phone_store):
        calls.append("sync")
        return {
            "checked": ["state_or_board"], "unchanged": [],
            "inserted": 0, "errors": {"state_or_board": "board offline"},
        }

    worker = ScoutWorker(
        store, generate=generate, sync=sync,
        sync_state_only=lambda *_: {"inserted": 0, "errors": {}},
        sync_mn_trades=lambda *_: {"verified": 0, "errors": {}},
        sync_nyc_state_only=lambda *_: {"inserted": 0, "errors": {}},
        phone_store=object(),
        judge=lambda _store, *, ai_ask: {"promoted": [], "skipped": {}},
        ai_ask=lambda prompt: "PASS", now=lambda: clock,
    )
    result = worker.run_once()
    assert result["retry"] is False
    assert result["synced"]["errors"] == {
        "state_or_board": "board offline"}
    assert calls == ["sync", "generate"]


def test_state_only_sync_runs_before_ai_and_isolates_board_failure(tmp_path):
    store = ScoutStore(str(tmp_path / "scout.db"))
    clock = datetime(2026, 9, 24, tzinfo=timezone.utc)
    calls = []

    def state_only(_store, _phone_store):
        calls.append("state_only")
        return {"inserted": 0, "errors": {"mn_dli_registration": "offline"}}

    def generate(_store, *, ai_ask):
        calls.append("generate")
        return {"proposed": [], "reason": ""}

    worker = ScoutWorker(
        store, generate=generate, sync=lambda *_: {"inserted": 0},
        sync_state_only=state_only, phone_store=object(),
        sync_mn_trades=lambda *_: calls.append("mn_trades") or
        {"verified": 0, "errors": {}},
        sync_nyc_state_only=lambda *_: {"inserted": 0, "errors": {}},
        judge=lambda _store, *, ai_ask: {"promoted": [], "skipped": {}},
        ai_ask=lambda prompt: "PASS", now=lambda: clock,
    )
    result = worker.run_once()
    assert result["retry"] is False
    assert result["state_only"]["errors"] == {
        "mn_dli_registration": "offline"}
    assert calls == ["state_only", "mn_trades", "generate"]


def test_nyc_sync_runs_before_ai_and_isolates_board_failure(tmp_path):
    store = ScoutStore(str(tmp_path / "scout.db"))
    calls = []

    def nyc(_store, _phone_store):
        calls.append("nyc")
        return {"inserted": 0, "errors": {"nyc_dcwp_hic": "offline"}}

    worker = ScoutWorker(
        store, generate=lambda *_args, **_kw: {"proposed": [], "reason": ""},
        sync=lambda *_: {"inserted": 0},
        sync_state_only=lambda *_: {"inserted": 0},
        sync_mn_trades=lambda *_: {"verified": 0, "errors": {}},
        sync_nyc_state_only=nyc, phone_store=object(),
        judge=lambda _store, *, ai_ask: {"promoted": [], "skipped": {}},
        ai_ask=lambda prompt: "PASS",
        now=lambda: datetime(2026, 9, 24, tzinfo=timezone.utc),
    )
    result = worker.run_once()
    assert result["retry"] is False
    assert result["nyc_state_only"]["errors"] == {"nyc_dcwp_hic": "offline"}
    assert calls == ["nyc"]

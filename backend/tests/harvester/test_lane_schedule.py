"""Lane schedule — the admin screen's control over WHICH harvester lane runs.

Three layers are covered, smallest first:

* ``resolve``  — the pure clock arithmetic (auto cycle, one-time window).
* ``HarvesterLaneStore`` — persistence + validation in harvester.db.
* ``HarvesterWorker.run_once`` — the dispatch that really blocks a lane.

Hermetic throughout: fake fetch/research lanes, a tmp SQLite store and an
injected clock, so no test touches the network, the model, or a real DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.harvester.lane_schedule import (
    HarvesterLaneStore,
    LaneSpec,
    MODE_AUTO,
    MODE_BOTH,
    MODE_EMAILS,
    MODE_PHONES,
    REPEAT_DAY,
    REPEAT_ONCE,
    resolve,
)
from app.harvester.store import HarvesterStore
from app.harvester.worker import HarvesterWorker
from app.phones.soda import SourceStatus
from app.phones.store import PhoneLeadsStore

BASE = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


def _auto(phone_min: float, email_min: float, *, first="phones",
          repeat=REPEAT_DAY, started_at: datetime | None = None) -> LaneSpec:
    return LaneSpec(
        mode=MODE_AUTO,
        phone_min=phone_min,
        email_min=email_min,
        phone_first=(first == "phones"),
        repeat=repeat,
        started_at=(started_at or BASE).isoformat(timespec="seconds"),
    )


# ---------------------------------------------------------------------------
# resolve — the clock arithmetic
# ---------------------------------------------------------------------------

def test_pinned_modes_pass_through():
    """both/phones/emails need no clock: the decision IS the stored mode."""
    for mode in (MODE_BOTH, MODE_PHONES, MODE_EMAILS):
        d = resolve(LaneSpec(mode=mode), BASE)
        assert d.mode == mode
        assert d.in_phone_slot is None
        assert d.seconds_to_switch is None


def test_auto_cycle_starts_at_save_time_not_midnight():
    """10 min phones / 40 min emails saved at 12:00 -> phones NOW, not
    wherever a midnight-anchored cycle would happen to be."""
    spec = _auto(10.0, 40.0, first="phones")
    assert resolve(spec, BASE).mode == MODE_PHONES          # t+0
    assert resolve(spec, BASE + timedelta(minutes=9)).mode == MODE_PHONES
    assert resolve(spec, BASE + timedelta(minutes=11)).mode == MODE_EMAILS
    assert resolve(spec, BASE + timedelta(minutes=49)).mode == MODE_EMAILS
    # 50 min = one full cycle -> phones again.
    assert resolve(spec, BASE + timedelta(minutes=50)).mode == MODE_PHONES


def test_auto_cycle_email_first_inverts_the_slots():
    """The operator can open with the email half of the cycle."""
    spec = _auto(10.0, 40.0, first="emails")
    assert resolve(spec, BASE).mode == MODE_EMAILS
    assert resolve(spec, BASE + timedelta(minutes=39)).mode == MODE_EMAILS
    assert resolve(spec, BASE + timedelta(minutes=41)).mode == MODE_PHONES


def test_seconds_to_switch_counts_down_to_the_next_flip():
    spec = _auto(10.0, 40.0, first="phones")
    d = resolve(spec, BASE + timedelta(minutes=4))
    assert d.mode == MODE_PHONES
    assert d.seconds_to_switch == pytest.approx(6 * 60)
    assert d.cycle_s == pytest.approx(50 * 60)

    d = resolve(spec, BASE + timedelta(minutes=30))
    assert d.mode == MODE_EMAILS
    assert d.seconds_to_switch == pytest.approx(20 * 60)   # 50 - 30


def test_repeat_day_wraps_forever_and_once_stops_after_one_cycle():
    spec_day = _auto(10.0, 40.0, repeat=REPEAT_DAY)
    assert resolve(spec_day, BASE + timedelta(hours=5)).mode != MODE_BOTH
    assert resolve(spec_day, BASE + timedelta(hours=50)).mode != MODE_BOTH

    spec_once = _auto(10.0, 40.0, repeat=REPEAT_ONCE)
    assert resolve(spec_once, BASE + timedelta(minutes=49)).mode == MODE_EMAILS
    done = resolve(spec_once, BASE + timedelta(minutes=50))
    assert done.mode == MODE_BOTH            # the window is over
    assert done.one_time_done is True
    # ...and it stays over, hours later.
    later = resolve(spec_once, BASE + timedelta(hours=8))
    assert later.mode == MODE_BOTH and later.one_time_done is True


def test_auto_without_an_anchor_starts_now():
    """A stored spec that somehow lost its timestamp must not crash: it
    anchors at ``now``, i.e. slot 0."""
    spec = LaneSpec(mode=MODE_AUTO, phone_min=10.0, email_min=40.0,
                    phone_first=True, started_at="")
    assert resolve(spec, BASE).mode == MODE_PHONES


def test_future_anchor_clamps_to_slot_zero():
    """Clock skew (a saved timestamp slightly in the future) clamps to 0
    rather than going negative and landing in the wrong slot."""
    spec = _auto(10.0, 40.0, started_at=BASE + timedelta(minutes=30))
    assert resolve(spec, BASE).mode == MODE_PHONES


# ---------------------------------------------------------------------------
# LaneSpec.validate
# ---------------------------------------------------------------------------

def test_validate_rejects_unknown_mode_and_repeat():
    with pytest.raises(ValueError):
        LaneSpec(mode="phase_lock").validate()
    with pytest.raises(ValueError):
        LaneSpec(mode=MODE_AUTO, repeat="weekly").validate()


def test_validate_bounds_only_apply_to_auto():
    with pytest.raises(ValueError):
        LaneSpec(mode=MODE_AUTO, phone_min=0.0).validate()
    with pytest.raises(ValueError):
        LaneSpec(mode=MODE_AUTO, email_min=10_000.0).validate()
    # In a pinned mode the slot numbers are unused — saving must not fail
    # over a field the worker will never read.
    assert LaneSpec(mode=MODE_PHONES, phone_min=0.0).validate().mode == MODE_PHONES


# ---------------------------------------------------------------------------
# HarvesterLaneStore
# ---------------------------------------------------------------------------

def test_store_defaults_to_both_with_no_row(tmp_path):
    store = HarvesterLaneStore(db_path=str(tmp_path / "h.db"))
    spec = store.load()
    assert spec.mode == MODE_BOTH
    assert store.resolve(BASE).mode == MODE_BOTH


def test_store_roundtrips_and_stamps_the_anchor(tmp_path):
    store = HarvesterLaneStore(db_path=str(tmp_path / "h.db"))
    saved = store.save(_auto(10.0, 40.0, first="emails"), BASE)
    assert saved.started_at.startswith("2026-09-23T12:00")

    loaded = HarvesterLaneStore(db_path=str(tmp_path / "h.db")).load()
    assert loaded.mode == MODE_AUTO
    assert loaded.phone_min == 10.0 and loaded.email_min == 40.0
    assert loaded.phone_first is False
    assert store.resolve(BASE).mode == MODE_EMAILS


def test_save_stamps_now_over_any_supplied_anchor(tmp_path):
    """Saving always restarts the cycle: the operator pressing Save expects
    the first slot to begin then, not at whatever anchor was in the box."""
    store = HarvesterLaneStore(db_path=str(tmp_path / "h.db"))
    stale = _auto(10.0, 40.0, started_at=BASE - timedelta(hours=3))
    saved = store.save(stale, BASE)
    assert saved.started_at != stale.started_at
    assert store.resolve(BASE).mode == MODE_PHONES


def test_save_rejects_an_unrunnable_schedule(tmp_path):
    store = HarvesterLaneStore(db_path=str(tmp_path / "h.db"))
    with pytest.raises(ValueError):
        store.save(LaneSpec(mode="nonsense"), BASE)
    # ...and stores nothing: the old row is untouched.
    assert store.load().mode == MODE_BOTH


def test_corrupt_row_degrades_to_both(tmp_path):
    """A hand-edited/garbage row must never stop the harvester."""
    store = HarvesterLaneStore(db_path=str(tmp_path / "h.db"))
    conn = store._conn()
    conn.execute(
        "INSERT INTO lane_schedule "
        "(id, mode, phone_min, email_min, phone_first, repeat, started_at) "
        "VALUES (1, 'garbage', 10, 40, 1, 'day', '')"
    )
    conn.commit()
    conn.close()
    assert store.load().mode == MODE_BOTH


# ---------------------------------------------------------------------------
# worker dispatch
# ---------------------------------------------------------------------------

class _FakeFetch:
    def __init__(self):
        self.calls: list = []

    def __call__(self, source_id, slug, city, limit, offset=0):
        self.calls.append((source_id, slug, city, limit, offset))
        return SourceStatus.SUCCESS, [], {}


class _FakeResearch:
    def __init__(self):
        self.calls: list = []

    def __call__(self, query):
        self.calls.append(query)
        return {"leads_found": 0, "working_leads": 0, "shortfall": 0}


def _worker(tmp_path, *, lane_source=None, **overrides):
    defaults = dict(
        fetch=_FakeFetch(),
        research=_FakeResearch(),
        enabled=True,
        active_jobs=lambda: 0,
        now=lambda: BASE,
        max_active=4,
    )
    defaults.update(overrides)
    if lane_source is not None:
        defaults["lane_source"] = lane_source
    return HarvesterWorker(
        HarvesterStore(db_path=str(tmp_path / "harvester.db")),
        PhoneLeadsStore(db_path=str(tmp_path / "phones.db")),
        lead_store=object(),
        pending_store=object(),
        **defaults,
    )


def test_only_lane_runs_when_a_schedule_pins_it(tmp_path):
    """PHONES captures the AI lane: the emails lane must not be entered at
    all (that is what makes it a zero-AI pass), and vice versa."""
    research = _FakeResearch()
    w = _worker(tmp_path, research=research,
                lane_source=lambda: resolve(LaneSpec(mode=MODE_PHONES), BASE))
    stats = w.run_once()
    assert stats["emails"]["skipped"] == "lane_schedule_phones"
    assert stats["emails"]["stocked"] == 0
    assert research.calls == []                       # no AI spent

    fetch = _FakeFetch()
    w = _worker(tmp_path, fetch=fetch,
                lane_source=lambda: resolve(LaneSpec(mode=MODE_EMAILS), BASE))
    stats = w.run_once()
    assert stats["phones"]["skipped"] == "lane_schedule_emails"
    assert fetch.calls == []                          # no fetch spent


def test_auto_schedule_switches_the_lane_with_the_clock(tmp_path):
    """The whole point: the same worker runs phones inside the phone slot
    and emails inside the email slot, with no restart."""
    spec = _auto(10.0, 40.0)
    clock = [BASE]
    research = _FakeResearch()
    w = _worker(tmp_path, research=research,
                now=lambda: clock[0],
                lane_source=lambda: resolve(spec, clock[0]))

    first = w.run_once()
    assert first["emails"]["skipped"] == "lane_schedule_phones"
    assert research.calls == []

    clock[0] = BASE + timedelta(minutes=20)          # inside the email slot
    second = w.run_once()
    assert second["phones"]["skipped"] == "lane_schedule_emails"


def test_one_time_window_falls_back_to_both_lanes(tmp_path, caplog):
    """repeat='once': after the single cycle the worker goes back to running
    both lanes (running them together is the default behaviour) and says so
    exactly once in the log — never silently."""
    import logging

    caplog.set_level(logging.INFO)
    spec = _auto(10.0, 40.0, repeat=REPEAT_ONCE, started_at=BASE - timedelta(minutes=51))
    w = _worker(tmp_path, lane_source=lambda: resolve(spec, BASE))

    stats = w.run_once()
    assert stats["phones"]["skipped"] != "lane_schedule_emails"
    assert stats["emails"]["skipped"] != "lane_schedule_phones"
    assert sum("one-time cycle finished" in r.message for r in caplog.records) == 1

    w.run_once()
    assert sum("one-time cycle finished" in r.message for r in caplog.records) == 1


def test_default_lane_mode_is_both_and_unchanged(tmp_path):
    """With no injected source the constructor's .env default decides —
    the pre-existing behaviour must survive untouched."""
    w = _worker(tmp_path)
    stats = w.run_once()
    assert stats["phones"] is not None and stats["emails"] is not None
    assert stats["emails"]["skipped"] == "no_demand"      # ran, found no demand


def test_boot_default_auto_uses_the_midnight_anchor(tmp_path):
    """The .env fallback keeps the original phase_lock semantics: slot
    boundaries come from midnight UTC, not from a save time."""
    midnight = BASE.replace(hour=0, minute=0, second=0, microsecond=0)
    clock = [midnight + timedelta(minutes=5)]             # inside phones
    w = _worker(tmp_path, now=lambda: clock[0], lane_mode=MODE_AUTO,
                phone_slot_s=1800.0, email_slot_s=5400.0, phone_first=True)
    assert w.run_once()["emails"]["skipped"] == "lane_schedule_phones"

    clock[0] = midnight + timedelta(minutes=45)           # inside emails
    assert w.run_once()["phones"]["skipped"] == "lane_schedule_emails"


def test_broken_lane_source_degrades_to_both_lanes(tmp_path):
    """A reading failure must never stop the stocker — it runs both lanes
    and logs the reason rather than silently doing nothing."""
    def boom():
        raise RuntimeError("harvester.db is gone")

    w = _worker(tmp_path, lane_source=boom)
    stats = w.run_once()
    assert stats["phones"] is not None and stats["emails"] is not None
    assert stats["skipped"] == ""


def test_phase_log_reports_real_transitions_only(tmp_path, caplog):
    """The cycle turn is visible in the log; switching OUT of auto clears
    the memory so no phantom transition is reported later."""
    import logging

    caplog.set_level(logging.INFO)
    spec = _auto(10.0, 40.0)
    clock = [BASE]
    w = _worker(tmp_path, now=lambda: clock[0],
                lane_source=lambda: resolve(spec, clock[0]))

    w.run_once()                                          # phones, no prior state
    assert not [r for r in caplog.records if "phase:" in r.message]

    clock[0] = BASE + timedelta(minutes=20)               # -> emails
    w.run_once()
    assert any("phase: phones → emails" in r.message for r in caplog.records)

    clock[0] = BASE + timedelta(minutes=55)               # wraps -> phones
    w.run_once()
    assert any("phase: emails → phones" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# config boot default
# ---------------------------------------------------------------------------

def test_config_accepts_the_lane_vocabulary_and_rejects_anything_else():
    from pydantic import ValidationError

    from app.core.config import Settings

    for mode in (MODE_BOTH, MODE_PHONES, MODE_EMAILS, MODE_AUTO):
        assert Settings(HARVEST_LANE_MODE=mode).HARVEST_LANE_MODE == mode
    with pytest.raises(ValidationError):
        Settings(HARVEST_LANE_MODE="phase_lock")   # retired spelling
    with pytest.raises(ValidationError):
        Settings(HARVEST_PHONE_SLOT_S=0.0, HARVEST_EMAIL_SLOT_S=-1.0)


def test_worker_module_has_no_leftover_slot_calculator():
    """The worker must own ONE lane calculator (lane_schedule.resolve).
    A second, stale one is exactly how the two drift apart."""
    assert not hasattr(HarvesterWorker, "_in_phone_slot")
    assert not hasattr(HarvesterWorker, "_lane_mode")

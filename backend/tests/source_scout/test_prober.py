"""Access Prober tests — the fake-HTTP matrix (coverage_engine_v2.md §12).

403/429/captcha/5xx/transport → blocked with an honest reason; 404/410 →
dead (terminal, walk stops); 200 → access_path recorded, gate_fail_reason
cleared. Everything against httpx MockTransport — the classification is
pinned with NO network (the catalog.py pattern). Walk semantics: next
path after blocked, 30-day re-arm when every path is blocked.
"""

from __future__ import annotations

import datetime

import httpx
import pytest

from app.source_scout.prober import (
    KIND_BLOCKED,
    KIND_DEAD,
    KIND_OK,
    classify,
    fetch_probe,
    probe_queue,
    walk_paths,
)
from app.source_scout.store import (
    STATUS_BLOCKED,
    STATUS_DEAD,
    STATUS_PROBING,
    STATUS_UNTRIED,
    ScoutStore,
)


def _store(tmp_path) -> ScoutStore:
    return ScoutStore(db_path=str(tmp_path / "scout.db"))


def _seed(store: ScoutStore, source_id="state_CA_board", base_url="",
          rank=1):
    store.seed_upsert(
        source_id, seed_domain="phones", state="CA",
        name="CSLB", endpoint=base_url or "https://www.cslb.ca.gov",
        base_url=base_url,
        seed_meta={"trade_scope": "all_trades", "priority_rank": rank,
                   "estab": 86763, "known_access": "bulk_file"})


# ---------------------------------------------------------------------------
# classification — the matrix itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code,kind", [
    (200, KIND_OK),
    (403, KIND_BLOCKED), (429, KIND_BLOCKED),
    (401, KIND_BLOCKED), (405, KIND_BLOCKED), (500, KIND_BLOCKED),
    (404, KIND_DEAD), (410, KIND_DEAD),  # dead is 404/410 ONLY
])
def test_classify_status_matrix(code, kind):
    assert classify(code)[0] == kind


def test_classify_200_with_captcha_markers_is_blocked():
    assert classify(200, b"<html>Cloudflare challenge</html>")[0] == \
        KIND_BLOCKED
    assert classify(200, b"please type the captcha below")[0] == KIND_BLOCKED
    assert classify(200, b"<html>a normal portal page</html>")[0] == KIND_OK


def test_fetch_probe_transport_error_is_blocked_never_dead():
    def handler(request):  # noqa: ARG001 — the fake matrix raises anyway
        raise httpx.ConnectError("boom")

    probe = fetch_probe("https://portal/bulk.zip",
                        transport=httpx.MockTransport(handler))
    assert probe.kind == KIND_BLOCKED
    assert "ConnectError" in probe.reason


# ---------------------------------------------------------------------------
# walk semantics on the store
# ---------------------------------------------------------------------------

def test_403_then_200_walks_to_the_next_path(tmp_path):
    store = _store(tmp_path)
    _seed(store, base_url="https://portal/cslb")
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(403)  # hint path WAF-blocked ...
        return httpx.Response(200, content=b"name,phone\n",
                              headers={"content-type": "text/csv"})

    # path 1 (bulk_file hint) 403s → the walk tries path 2 (open_data_api)
    row, probe = walk_paths(store, "state_CA_board",
                            transport=httpx.MockTransport(handler))
    assert probe.kind == KIND_OK
    assert row["status"] == STATUS_PROBING
    assert row["access_path"] == "open_data_api"
    assert row["gate_fail_reason"] == ""  # stale reason cleared on success

    store2 = _store(tmp_path)
    _seed(store2, source_id="state_CA_board_2", base_url="https://portal/x")
    row2, _ = walk_paths(store2, "state_CA_board_2",
                         transport=httpx.MockTransport(handler))
    assert row2["access_path"] == "bulk_file"  # hint path wins on first 200
    assert len(calls) == 3  # 1 blocked attempt + 2 successful walks


def test_404_is_dead_terminal_and_stops_the_walk(tmp_path):
    store = _store(tmp_path)
    _seed(store, base_url="https://portal/gone")
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(404)

    row, probe = walk_paths(store, "state_CA_board",
                            transport=httpx.MockTransport(handler))
    assert probe.kind == KIND_DEAD
    assert row["status"] == STATUS_DEAD
    assert "404" in row["gate_fail_reason"]
    assert len(calls) == 1  # dead stops the walk — no next path
    with pytest.raises(ValueError, match="blocked/exhausted"):
        store.re_arm("state_CA_board")  # dead never re-arms


def test_all_paths_blocked_stamps_30d_rearm(tmp_path):
    store = _store(tmp_path)
    _seed(store, base_url="https://portal/walled")
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(403)

    row, probe = walk_paths(store, "state_CA_board",
                            transport=httpx.MockTransport(handler))
    assert probe.kind == KIND_BLOCKED
    assert row["status"] == STATUS_BLOCKED
    assert "all 5 paths blocked" in row["gate_fail_reason"]
    assert len(calls) == 5  # every admitted path was tried
    far = (datetime.datetime.now(datetime.timezone.utc)
           + datetime.timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%S")
    assert row["next_retry_at"] > far


def test_no_base_url_row_keeps_untried_with_honest_reason(tmp_path):
    store = _store(tmp_path)
    _seed(store, base_url="")  # NC-class seed gap
    row, probe = walk_paths(store, "state_CA_board")
    assert probe.kind == KIND_BLOCKED
    assert row["status"] == STATUS_UNTRIED  # nothing was attempted
    assert "no base_url" in row["gate_fail_reason"]


def test_probe_queue_orders_by_rank_and_skips_url_less(tmp_path):
    store = _store(tmp_path)
    _seed(store, source_id="state_NC_board", base_url="", rank=28)
    _seed(store, source_id="state_CA_board", base_url="https://portal/cslb",
          rank=1)

    def handler(request):
        return httpx.Response(200, content=b"name,phone\n")

    out = probe_queue(store, limit=5, transport=httpx.MockTransport(handler))
    assert [o["source_id"] for o in out] == ["state_ca_board"]
    assert out[0]["status"] == STATUS_PROBING
    assert out[0]["probe"] == KIND_OK
    # the URL-less row stayed untried, visible with the reason
    assert store.get("state_nc_board")["status"] == STATUS_UNTRIED
    assert "no base_url" in store.get("state_nc_board")["gate_fail_reason"]


def test_record_probe_success_rejects_legacy_path_vocabulary(tmp_path):
    store = _store(tmp_path)
    _seed(store, base_url="https://portal/cslb")
    with pytest.raises(ValueError, match="access_path"):
        store.record_probe_success("state_CA_board", "soda")


def test_rework_adapter_bounces_to_probing_with_reason(tmp_path):
    store = _store(tmp_path)
    _seed(store, base_url="https://portal/cslb")
    store.start_probing("state_CA_board")
    store.mark_adapter_draft("state_CA_board")
    row = store.rework_adapter("state_CA_board", "gate fail: schema_mismatch")
    assert row["status"] == STATUS_PROBING
    assert "schema_mismatch" in row["gate_fail_reason"]
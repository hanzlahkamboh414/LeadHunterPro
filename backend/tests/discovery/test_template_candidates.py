"""Phase H — LLM-generated dork candidate lifecycle store.

The ADD side of the self-learning loop: an LLM proposes NEW plan-holder dork
phrasings, they are staged here, and the discovery source dispatches the
survivors through the SAME Phase G yield loop. This file pins the dispatch rule
(single source of truth = the yield store): a candidate dispatches unless
proven dead, is lazy-promoted ``active`` at working >= PROVEN_GOOD, and is
lazy-demoted ``dropped`` once the yield says it is dead. ``status`` is lifecycle
visibility, never the decision.
"""

from __future__ import annotations

from app.discovery.template_candidates import PROVEN_GOOD, TemplateCandidateStore
from app.discovery.yield_learning import MIN_TRIALS, DiscoveryYieldStore, segment_key


# ---------------------------------------------------------------------------
# propose — staging + PK dedupe
# ---------------------------------------------------------------------------

def test_propose_stages_new_candidate(tmp_path):
    store = TemplateCandidateStore(str(tmp_path / "cand.db"))
    assert store.propose('"plan holder list" {industry} {location} filetype:pdf') is True
    rows = store.all()
    assert len(rows) == 1
    assert rows[0]["label"].startswith('"plan holder list"')
    assert rows[0]["status"] == "candidate"
    assert rows[0]["source"] == "llm"


def test_propose_dedupes_on_label(tmp_path):
    store = TemplateCandidateStore(str(tmp_path / "cand.db"))
    label = '"bid roster" {industry} {location} filetype:pdf'
    assert store.propose(label) is True
    assert store.propose(label) is False  # same pattern never staged twice
    assert len(store.all()) == 1


def test_propose_blank_is_false(tmp_path):
    store = TemplateCandidateStore(str(tmp_path / "cand.db"))
    assert store.propose("") is False
    assert store.propose(None) is False  # type: ignore[arg-type]
    assert store.all() == []


# ---------------------------------------------------------------------------
# effective_dorks — the dispatch rule (single source of truth = yield store)
# ---------------------------------------------------------------------------

def test_fresh_candidate_dispatches(tmp_path):
    cand = TemplateCandidateStore(str(tmp_path / "cand.db"))
    cand.propose('"plan holder" {industry} {location} filetype:pdf')
    yield_store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    assert cand.effective_dorks(yield_store) == [
        '"plan holder" {industry} {location} filetype:pdf',
    ]


def test_proven_good_promotes_to_active(tmp_path):
    cand = TemplateCandidateStore(str(tmp_path / "cand.db"))
    cand.propose('"bid list" {industry} {location} filetype:pdf')
    yield_store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    for _ in range(PROVEN_GOOD):
        yield_store.record_working('"bid list" {industry} {location} filetype:pdf')
    dorks = cand.effective_dorks(yield_store)
    assert len(dorks) == 1
    assert cand._status(dorks[0]) == "active"  # promoted on evidence


def test_proven_dead_drops_and_stops_dispatching(tmp_path):
    cand = TemplateCandidateStore(str(tmp_path / "cand.db"))
    cand.propose('"dead angle" {industry} {location} filetype:pdf')
    yield_store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    for _ in range(MIN_TRIALS):
        yield_store.record_dispatch('"dead angle" {industry} {location} filetype:pdf')
    # MIN_TRIALS dispatches, zero working -> should_skip True -> dropped.
    assert cand.effective_dorks(yield_store) == []
    assert cand._status('"dead angle" {industry} {location} filetype:pdf') == "dropped"


def test_segment_dead_candidate_drops_there_keeps_elsewhere(tmp_path):
    """Phase I — the dispatch gate is segment-aware: a candidate proven dead
    FOR ONE segment is lazy-demoted and excluded there, but still dispatches
    for another segment where its row is quiet (global keep)."""
    label = '"seg angle" {industry} {location} filetype:pdf'
    cand = TemplateCandidateStore(str(tmp_path / "cand.db"))
    cand.propose(label)
    yield_store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    seg_dead = segment_key("roofing", "dallas tx")
    seg_live = segment_key("roofing", "houston tx")
    for _ in range(MIN_TRIALS):
        yield_store.record_dispatch(label, seg_dead)  # dead only for dallas

    dorks_dead = cand.effective_dorks(yield_store, segment=seg_dead)
    assert dorks_dead == []                                   # dropped for dallas
    assert cand._status(label) == "dropped"
    assert cand.effective_dorks(yield_store, segment=seg_live) == [label]
    # The global dispatch rule (no segment) is untouched — the global row kept it.
    assert cand.effective_dorks(yield_store) == [label]


def test_segment_working_promotes_for_that_segment_only(tmp_path):
    """A candidate's PROVEN_GOOD evidence is segment-scoped: working leads for
    one segment promote it only when queried with that segment."""
    label = '"seg good" {industry} {location} filetype:pdf'
    cand = TemplateCandidateStore(str(tmp_path / "cand.db"))
    cand.propose(label)
    yield_store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    seg = segment_key("gc", "houston tx")
    for _ in range(PROVEN_GOOD):
        yield_store.record_working(label, seg)

    assert cand.effective_dorks(yield_store, segment=seg) == [label]
    assert cand._status(label) == "active"
    # Global evidence is empty, so the global pass keeps it as candidate.
    cand2 = TemplateCandidateStore(str(tmp_path / "cand2.db"))
    cand2.propose(label)
    assert cand2.effective_dorks(yield_store) == [label]
    assert cand2._status(label) != "active"


def test_not_yet_dead_candidate_still_dispatches(tmp_path):
    cand = TemplateCandidateStore(str(tmp_path / "cand.db"))
    cand.propose('"plan room" {industry} {location} filetype:pdf')
    yield_store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    for _ in range(MIN_TRIALS - 1):
        yield_store.record_dispatch('"plan room" {industry} {location} filetype:pdf')
    # Not yet proven dead (below MIN_TRIALS) -> default-keep.
    assert cand.effective_dorks(yield_store) == [
        '"plan room" {industry} {location} filetype:pdf',
    ]


def test_human_resurrection_via_delete_template_redispatches(tmp_path):
    cand = TemplateCandidateStore(str(tmp_path / "cand.db"))
    cand.propose('"dead angle" {industry} {location} filetype:pdf')
    yield_store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    for _ in range(MIN_TRIALS):
        yield_store.record_dispatch('"dead angle" {industry} {location} filetype:pdf')
    assert cand.effective_dorks(yield_store) == []  # dropped
    # Human-only resurrection (P-G): clearing the yield record re-enables it.
    yield_store.delete_template('"dead angle" {industry} {location} filetype:pdf')
    assert cand.effective_dorks(yield_store) == [
        '"dead angle" {industry} {location} filetype:pdf',
    ]


# ---------------------------------------------------------------------------
# all / reset
# ---------------------------------------------------------------------------

def test_all_and_reset(tmp_path):
    store = TemplateCandidateStore(str(tmp_path / "cand.db"))
    store.propose('"a" {industry} {location} filetype:pdf')
    store.propose('"b" {industry} {location} filetype:pdf')
    assert len(store.all()) == 2
    store.reset()
    assert store.all() == []


# ---------------------------------------------------------------------------
# Inc 2 — layer='web' (LLM-invented web-search angles ride the SAME lifecycle)
# ---------------------------------------------------------------------------

_DORK = '"plan holder list" {industry} {location} filetype:pdf'
_WEB = 'chamber of commerce member directory {industry} {location}'


def test_propose_stores_web_layer(tmp_path):
    store = TemplateCandidateStore(str(tmp_path / "cand.db"))
    assert store.propose(_WEB, layer="web") is True
    rows = store.all()
    assert len(rows) == 1
    assert rows[0]["layer"] == "web"
    assert rows[0]["status"] == "candidate"


def test_layers_dispatch_separately(tmp_path):
    """Layer separation (Inc 2): a 'web' angle NEVER dispatches on the dork
    lane and a 'dork' candidate never runs as a web angle — the two discovery
    lanes share the yield loop, not the dispatch set."""
    cand = TemplateCandidateStore(str(tmp_path / "cand.db"))
    cand.propose(_DORK)
    cand.propose(_WEB, layer="web")
    yield_store = DiscoveryYieldStore(str(tmp_path / "yield.db"))

    assert cand.effective_dorks(yield_store, layer="dork") == [_DORK]
    assert cand.effective_dorks(yield_store, layer="web") == [_WEB]


def test_web_layer_lifecycle_earn_or_die(tmp_path):
    """A web angle dies on its OWN evidence (trials without working) and is
    demoted exactly like a dork — proposal never counts as proof (§12)."""
    cand = TemplateCandidateStore(str(tmp_path / "cand.db"))
    cand.propose(_WEB, layer="web")
    yield_store = DiscoveryYieldStore(str(tmp_path / "yield.db"))

    # Not yet proven dead -> default-keep (dispatches).
    for _ in range(MIN_TRIALS - 1):
        yield_store.record_dispatch(_WEB)
    assert cand.effective_dorks(yield_store, layer="web") == [_WEB]

    # MIN_TRIALS with zero working -> dropped, stops dispatching.
    yield_store.record_dispatch(_WEB)
    assert cand.effective_dorks(yield_store, layer="web") == []
    assert cand._status(_WEB) == "dropped"

    # Dropped web layer leaves the DORK lane untouched.
    cand2 = TemplateCandidateStore(str(tmp_path / "cand2.db"))
    cand2.propose(_DORK)
    assert cand2.effective_dorks(yield_store, layer="dork") == [_DORK]


def test_web_layer_proven_good_promotes(tmp_path):
    cand = TemplateCandidateStore(str(tmp_path / "cand.db"))
    cand.propose(_WEB, layer="web")
    yield_store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    for _ in range(PROVEN_GOOD):
        yield_store.record_working(_WEB)
    assert cand.effective_dorks(yield_store, layer="web") == [_WEB]
    assert cand._status(_WEB) == "active"  # earned through the loop


def test_web_label_dedupe_across_layers(tmp_path):
    """The label is the PK, so the same phrasing is staged once whatever the
    layer — a web angle can never collide with a dork in practice (dorks carry
    filetype:, web angles forbid it), but the dedupe is total."""
    store = TemplateCandidateStore(str(tmp_path / "cand.db"))
    assert store.propose(_WEB, layer="web") is True
    assert store.propose(_WEB, layer="dork") is False
    assert len(store.all()) == 1

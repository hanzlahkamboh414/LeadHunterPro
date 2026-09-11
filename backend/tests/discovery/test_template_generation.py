"""Phase H — LLM dork-template generation (the guarded ADD side).

The generator must be HONEST and INERT: it proposes new phrasings only when the
yield loop has evidence to improve on (cold-start guard — never spend a credit
to invent angles from nothing), every proposal is sanitized against a hard shape
({industry}/{location} + filetype:pdf, no dupes, length-bounded) so garbage never
enters the pipeline (§12), and a generated dork is only ever STAGED — it earns
every dispatch through the Phase G yield loop. LLM error / empty reply is a loud,
honest 0, never a silent skip (§6). All tests inject ``ai_ask`` — nothing touches
the network.
"""

from __future__ import annotations

from app.discovery.template_generation import (
    GENERATION_MIN_TRIALS,
    _evidence_lines,
    _sanitize_dork,
    cold_start_guard,
    generate_dork_candidates,
)
from app.discovery.yield_learning import MIN_TRIALS, DiscoveryYieldStore, segment_key

_VALID = '"plan holder list" {industry} {location} filetype:pdf'
_STATICS = (_VALID,)
_SEG = segment_key("roofing", "dallas tx")


# ---------------------------------------------------------------------------
# cold_start_guard — no LLM call without evidence
# ---------------------------------------------------------------------------

def _store_with(tmp_path, min_trials_reached: bool):
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    if min_trials_reached:
        for _ in range(MIN_TRIALS):
            store.record_dispatch("static:dork")
    return store


def test_cold_start_guard_holds_without_evidence(tmp_path):
    store = _store_with(tmp_path, min_trials_reached=False)
    reason = cold_start_guard(store)
    assert reason is not None
    assert "trials" in reason


def test_cold_start_guard_passes_with_evidence(tmp_path):
    store = _store_with(tmp_path, min_trials_reached=True)
    assert cold_start_guard(store) is None


def test_cold_start_guard_holds_empty_store(tmp_path):
    assert cold_start_guard(DiscoveryYieldStore(str(tmp_path / "empty.db"))) is not None


def test_cold_start_guard_passes_at_lowered_generation_threshold(tmp_path):
    """The generation gate now clears at GENERATION_MIN_TRIALS (decoupled from
    the 12-trial dead-dork drop gate): the live store already has ~9 real trials
    on the static dorks, so the add-side AI can propose new angles now."""
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    for _ in range(GENERATION_MIN_TRIALS):
        store.record_dispatch("static:dork")
    assert cold_start_guard(store) is None


def test_cold_start_guard_still_holds_one_below_threshold(tmp_path):
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    for _ in range(GENERATION_MIN_TRIALS - 1):
        store.record_dispatch("static:dork")
    assert cold_start_guard(store) is not None


def test_cold_start_guard_segment_holds_without_segment_evidence(tmp_path):
    """A segment with NO own trials holds even when the GLOBAL row has evidence
    (segment hierarchy: the targeted pass has nothing about ITS segment yet)."""
    store = _store_with(tmp_path, min_trials_reached=True)  # global has trials
    assert cold_start_guard(store, _SEG) is not None
    assert "segment" in cold_start_guard(store, _SEG)


def test_cold_start_guard_segment_passes_with_segment_evidence(tmp_path):
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    for _ in range(MIN_TRIALS):
        store.record_dispatch("static:dork", _SEG)
    assert cold_start_guard(store, _SEG) is None


# ---------------------------------------------------------------------------
# _sanitize_dork — hard shape filters
# ---------------------------------------------------------------------------

def test_sanitize_accepts_valid():
    assert _sanitize_dork(_VALID, already=set()) == _VALID


def test_sanitize_rejects_missing_filetype():
    assert _sanitize_dork('"x" {industry} {location}', already=set()) is None


def test_sanitize_rejects_missing_placeholders():
    assert _sanitize_dork('"x" {industry} filetype:pdf', already=set()) is None
    assert _sanitize_dork('"x" {location} filetype:pdf', already=set()) is None
    assert _sanitize_dork('"x" filetype:pdf', already=set()) is None


def test_sanitize_rejects_duplicate():
    assert _sanitize_dork(_VALID, already={_VALID}) is None


def test_sanitize_rejects_empty_and_oversized():
    assert _sanitize_dork("", already=set()) is None
    assert _sanitize_dork("  ", already=set()) is None
    long = _VALID + " " + "x" * 500
    assert _sanitize_dork(long, already=set()) is None


# ---------------------------------------------------------------------------
# generate_dork_candidates — with a fake ai_ask
# ---------------------------------------------------------------------------

def test_generate_stages_valid_and_rejects_garbage(tmp_path):
    cand_store = __import__(
        "app.discovery.template_candidates", fromlist=["TemplateCandidateStore"]
    ).TemplateCandidateStore(str(tmp_path / "cand.db"))
    yield_store = _store_with(tmp_path, min_trials_reached=True)

    def fake_ai(prompt):
        # One valid new angle, one malformed (has filetype but drops the
        # {industry} placeholder — survives _parse_dorks, fails sanitize),
        # and one dupe of the static set.
        return (
            '"plan holder roster" {industry} {location} filetype:pdf\n'
            '"contractor lookup" {location} filetype:pdf\n'
            f"{_VALID}\n"
        )

    result = generate_dork_candidates(
        yield_store, fake_ai, static_templates=_STATICS, candidate_store=cand_store,
    )
    assert result["reason"] == ""
    assert result["generated"] == ['"plan holder roster" {industry} {location} filetype:pdf']
    # The malformed dork (missing {industry}) and the static dupe are rejected.
    assert len(result["rejected"]) == 2
    assert len(cand_store.all()) == 1


def test_generate_dedupes_against_existing_candidates(tmp_path):
    cand_store = __import__(
        "app.discovery.template_candidates", fromlist=["TemplateCandidateStore"]
    ).TemplateCandidateStore(str(tmp_path / "cand.db"))
    yield_store = _store_with(tmp_path, min_trials_reached=True)
    existing = '"plan holder roster" {industry} {location} filetype:pdf'
    cand_store.propose(existing)

    def fake_ai(prompt):
        return f"{existing}\n"

    result = generate_dork_candidates(
        yield_store, fake_ai, static_templates=_STATICS, candidate_store=cand_store,
    )
    assert result["generated"] == []  # already staged in a prior run
    assert result["rejected"] == [existing]


def test_evidence_lines_are_segment_scoped(tmp_path):
    """The LLM evidence table for a segment shows THAT segment's yield (the
    global path shows the global rows, unchanged)."""
    store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    store.record_dispatch("static:dork")             # GLOBAL: trials=1 working=0
    store.record_working("static:dork", _SEG)        # SEGMENT: trials=0 working=1
    global_view = _evidence_lines(store, _STATICS)
    seg_view = _evidence_lines(store, _STATICS, _SEG)
    # The global view shows the global row (trials=1), never the segment's.
    assert "working=1" not in global_view      # segment working stays out
    assert "trials=1" in global_view
    # The segment view shows the segment row (working=1), whose proven working
    # is the evidence the LLM fills the gap against.
    assert "working=1" in seg_view
    assert "trials=0" in seg_view


def test_generate_segment_targeted_honest_zero_without_segment_evidence(tmp_path):
    """A --segment pass with NO segment evidence holds — no LLM call, honest
    reason naming the segment (never pollutes global behavior)."""
    cand_store = __import__(
        "app.discovery.template_candidates", fromlist=["TemplateCandidateStore"]
    ).TemplateCandidateStore(str(tmp_path / "cand.db"))
    yield_store = _store_with(tmp_path, min_trials_reached=True)  # global ok, segment empty

    def fake_ai(prompt):
        raise AssertionError("LLM must not be called for a segment without evidence")

    result = generate_dork_candidates(
        yield_store, fake_ai, static_templates=_STATICS,
        candidate_store=cand_store, segment=_SEG,
    )
    assert result["generated"] == []
    assert "segment" in result["reason"]
    assert cand_store.all() == []


def test_generate_segment_targeted_calls_llm_with_segment_evidence(tmp_path):
    """With THE SEGMENT's evidence present, the targeted pass asks the LLM and
    stages a valid proposal (the segment-scoped path of the same generator)."""
    cand_store = __import__(
        "app.discovery.template_candidates", fromlist=["TemplateCandidateStore"]
    ).TemplateCandidateStore(str(tmp_path / "cand.db"))
    yield_store = DiscoveryYieldStore(str(tmp_path / "yield.db"))
    for _ in range(MIN_TRIALS):
        yield_store.record_dispatch("static:dork", _SEG)

    seen_prompt: list[str] = []

    def fake_ai(prompt):
        seen_prompt.append(prompt)
        return '"bid roster" {industry} {location} filetype:pdf\n'

    result = generate_dork_candidates(
        yield_store, fake_ai, static_templates=_STATICS,
        candidate_store=cand_store, segment=_SEG,
    )
    assert seen_prompt  # the LLM WAS asked (evidence exists for this segment)
    assert _SEG in seen_prompt[0]  # prompt names the targeted segment
    assert result["generated"] == ['"bid roster" {industry} {location} filetype:pdf']
    assert cand_store.all()


def test_generate_holds_on_cold_start_no_llm_call(tmp_path):
    """Cold-start guard -> reason returned, fake ai_ask raises if ever touched."""
    cand_store = __import__(
        "app.discovery.template_candidates", fromlist=["TemplateCandidateStore"]
    ).TemplateCandidateStore(str(tmp_path / "cand.db"))
    yield_store = _store_with(tmp_path, min_trials_reached=False)

    def fake_ai(prompt):
        raise AssertionError("LLM must not be called without evidence")

    result = generate_dork_candidates(
        yield_store, fake_ai, static_templates=_STATICS, candidate_store=cand_store,
    )
    assert result["generated"] == []
    assert result["rejected"] == []
    assert "cold-start" in result["reason"]
    assert cand_store.all() == []  # nothing staged


def test_generate_llm_error_is_honest_zero(tmp_path):
    cand_store = __import__(
        "app.discovery.template_candidates", fromlist=["TemplateCandidateStore"]
    ).TemplateCandidateStore(str(tmp_path / "cand.db"))
    yield_store = _store_with(tmp_path, min_trials_reached=True)

    def fake_ai(prompt):
        raise RuntimeError("provider down")

    result = generate_dork_candidates(
        yield_store, fake_ai, static_templates=_STATICS, candidate_store=cand_store,
    )
    assert result["generated"] == []
    assert result["reason"].startswith("LLM call failed:")
    assert cand_store.all() == []


def test_generate_empty_reply_is_honest_zero(tmp_path):
    cand_store = __import__(
        "app.discovery.template_candidates", fromlist=["TemplateCandidateStore"]
    ).TemplateCandidateStore(str(tmp_path / "cand.db"))
    yield_store = _store_with(tmp_path, min_trials_reached=True)

    def fake_ai(prompt):
        return ""

    result = generate_dork_candidates(
        yield_store, fake_ai, static_templates=_STATICS, candidate_store=cand_store,
    )
    assert result["generated"] == []
    assert result["reason"]  # honest: nothing usable came back


# ---------------------------------------------------------------------------
# Inc 2 — Layer-2 WEB-SEARCH ANGLE generation (AI invents new methods)
# ---------------------------------------------------------------------------

from app.discovery.template_generation import (  # noqa: E402
    _sanitize_web_angle,
    _parse_web_angles,
    generate_web_angle_candidates,
)

_WEB_VALID = 'chamber of commerce member directory {industry} {location}'


def _web_cand_store(tmp_path):
    from app.discovery.template_candidates import TemplateCandidateStore
    return TemplateCandidateStore(str(tmp_path / "cand.db"))


# -- _sanitize_web_angle — the web-lane hard shape --------------------------

def test_sanitize_web_angle_accepts_valid():
    assert _sanitize_web_angle(_WEB_VALID, already=set()) == _WEB_VALID


def test_sanitize_web_angle_accepts_quoted():
    quoted = f'"{_WEB_VALID}"'
    assert _sanitize_web_angle(quoted, already=set()) == _WEB_VALID


def test_sanitize_web_angle_rejects_missing_placeholder():
    assert _sanitize_web_angle(
        "license roster {location}", already=set()) is None
    assert _sanitize_web_angle(
        "license roster {industry}", already=set()) is None


def test_sanitize_web_angle_rejects_dork_lane_crossover():
    """A filetype: proposal belongs to the Layer-1 dork lane — it must NOT run
    as a web angle (the plan-holder lane already covers pdfs; a crossover would
    flood the search lane with plan-room pages)."""
    assert _sanitize_web_angle(
        '"bid roster" {industry} {location} filetype:pdf', already=set()) is None


def test_sanitize_web_angle_rejects_dupe_empty_oversized():
    assert _sanitize_web_angle(_WEB_VALID, already={_WEB_VALID}) is None
    assert _sanitize_web_angle("", already=set()) is None
    assert _sanitize_web_angle("   ", already=set()) is None
    assert _sanitize_web_angle(
        _WEB_VALID + " " + "x" * 300, already=set()) is None


# -- _parse_web_angles — robust reply parsing --------------------------------

def test_parse_web_angles_quoted_and_bare():
    reply = (
        f'"{_WEB_VALID}"\n'
        '- state license board roster {industry} {location}\n'
        '"garbage line without placeholders"\n'
        'intro prose the LLM may add\n'
    )
    assert _parse_web_angles(reply) == [
        _WEB_VALID,
        "state license board roster {industry} {location}",
    ]


def test_parse_web_angles_empty():
    assert _parse_web_angles("") == []


# -- generate_web_angle_candidates — guarded add side ------------------------

def test_web_generate_holds_on_cold_start(tmp_path):
    """Same cold-start contract as dorks: no yield evidence -> no LLM credit
    spent inventing angles from nothing."""
    cand_store = _web_cand_store(tmp_path)
    yield_store = _store_with(tmp_path, min_trials_reached=False)

    def fake_ai(prompt):
        raise AssertionError("LLM must not be called without evidence")

    result = generate_web_angle_candidates(
        yield_store, fake_ai, candidate_store=cand_store,
    )
    assert result["generated"] == []
    assert "cold-start" in result["reason"]
    assert cand_store.all() == []


def test_web_generate_stages_valid_and_rejects_garbage(tmp_path):
    """One valid new angle staged on layer 'web'; a filetype: crossover and a
    missing-placeholder line are rejected; nothing is injected as proven."""
    cand_store = _web_cand_store(tmp_path)
    yield_store = _store_with(tmp_path, min_trials_reached=True)

    def fake_ai(prompt):
        return (
            f'"{_WEB_VALID}"\n'
            '"bid-notice board" {industry} {location} filetype:pdf\n'
            '"award list" {location}\n'
        )

    result = generate_web_angle_candidates(
        yield_store, fake_ai, candidate_store=cand_store,
    )
    assert result["reason"] == ""
    assert result["generated"] == [_WEB_VALID]
    # The filetype: crossover survives parse (both placeholders) and is
    # rejected at SANITIZE — it lands in `rejected`. The missing-placeholder
    # line ("award list" {location}) is filtered at PARSE level, so it never
    # even becomes a rejected candidate.
    assert result["rejected"] == [
        '"bid-notice board" {industry} {location} filetype:pdf',
    ]
    rows = cand_store.all()
    assert len(rows) == 1
    assert rows[0]["layer"] == "web"
    assert rows[0]["status"] == "candidate"  # staged, never proven (§12)


def test_web_generate_prompt_demands_new_methods_not_dorks(tmp_path):
    """The prompt itself must push the LLM AWAY from the pdf lane — 'NOT PDF'
    and the source-page kinds (directories/rosters/boards) are in-prompt."""
    cand_store = _web_cand_store(tmp_path)
    yield_store = _store_with(tmp_path, min_trials_reached=True)
    seen: list[str] = []

    def fake_ai(prompt):
        seen.append(prompt)
        return f'"{_WEB_VALID}"\n'

    generate_web_angle_candidates(yield_store, fake_ai,
                                  candidate_store=cand_store)
    assert seen
    assert "NOT PDF" in seen[0]
    assert "filetype:" in seen[0]  # forbids the operator explicitly


def test_web_generate_dedupes_against_existing(tmp_path):
    cand_store = _web_cand_store(tmp_path)
    yield_store = _store_with(tmp_path, min_trials_reached=True)
    cand_store.propose(_WEB_VALID, layer="web")

    def fake_ai(prompt):
        return f'"{_WEB_VALID}"\n'

    result = generate_web_angle_candidates(
        yield_store, fake_ai, candidate_store=cand_store,
    )
    assert result["generated"] == []
    assert result["rejected"] == [_WEB_VALID]


def test_web_generate_llm_error_is_honest(tmp_path):
    cand_store = _web_cand_store(tmp_path)
    yield_store = _store_with(tmp_path, min_trials_reached=True)

    def fake_ai(prompt):
        raise RuntimeError("provider down")

    result = generate_web_angle_candidates(
        yield_store, fake_ai, candidate_store=cand_store,
    )
    assert result["generated"] == []
    assert result["reason"].startswith("LLM call failed:")
    assert cand_store.all() == []


def test_web_generate_empty_reply_is_honest(tmp_path):
    cand_store = _web_cand_store(tmp_path)
    yield_store = _store_with(tmp_path, min_trials_reached=True)

    def fake_ai(prompt):
        return ""

    result = generate_web_angle_candidates(
        yield_store, fake_ai, candidate_store=cand_store,
    )
    assert result["generated"] == []
    assert result["reason"]

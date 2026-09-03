"""ResearchScorer — boolean verdict engine (Step A / B / C)."""

from __future__ import annotations

from app.person_research.models import (
    AttributionVerdict,
    PersonCandidate,
)
from app.person_research.scoring import ResearchScorer
from app.person_research.sources import WebsiteSource
from tests.person_research.conftest import make_fetch


def _score_via_site(pages, email, domain="acmebuild.com"):
    facts = WebsiteSource(fetch_page=make_fetch(pages)).discover(domain, email)
    return ResearchScorer().evaluate(email, domain, facts)


def test_attributed_when_exact_local_match(acme_pages):
    candidates, verdict = _score_via_site(acme_pages, "johnsmith@acmebuild.com")
    assert verdict is AttributionVerdict.attributed
    bound = [c for c in candidates if c.bound]
    assert len(bound) == 1
    assert bound[0].name == "John Smith"
    assert bound[0].local_part_match_level == "exact"


def test_initial_last_is_not_binding():
    from tests.person_research.conftest import homepage, team_page

    pages = {
        "https://acmebuild.com": homepage(("/team", "Team")),
        "https://acmebuild.com/team": team_page("John Smith", "Project Manager", "jsmith@acmebuild.com"),
    }
    candidates, verdict = _score_via_site(pages, "jsmith@acmebuild.com")
    assert verdict is AttributionVerdict.candidates_found
    assert all(not c.bound for c in candidates)


def test_no_candidates_is_unattributed(acme_pages):
    _, verdict = _score_via_site(acme_pages, "office@acmebuild.com")
    assert verdict is AttributionVerdict.unattributed


def test_score_ranks_exact_above_initial():
    scorer = ResearchScorer()
    a = PersonCandidate(name="John Smith", co_occurrence=True, local_part_match_level="exact")
    b = PersonCandidate(name="John Smith", co_occurrence=True, local_part_match_level="initial_last")
    assert scorer._score(a) > scorer._score(b)  # noqa: SLF001


def _candidate(name, level):
    return PersonCandidate(name=name, co_occurrence=True, local_part_match_level=level)


def test_contradiction_is_unresolved():
    scorer = ResearchScorer()
    candidates = [_candidate("John Smith", "exact"), _candidate("Jane Doe", "exact")]
    verdict = scorer._verdict(candidates, candidates)  # noqa: SLF001
    assert verdict is AttributionVerdict.unresolved
    assert all(c.contradictory for c in candidates)


def test_single_eligible_binds():
    scorer = ResearchScorer()
    candidates = [_candidate("John Smith", "exact")]
    verdict = scorer._verdict(candidates, candidates)  # noqa: SLF001
    assert verdict is AttributionVerdict.attributed
    assert candidates[0].bound


def test_no_eligible_is_candidates_found():
    scorer = ResearchScorer()
    candidates = [_candidate("John Smith", "reject")]
    verdict = scorer._verdict(candidates, [])  # noqa: SLF001
    assert verdict is AttributionVerdict.candidates_found

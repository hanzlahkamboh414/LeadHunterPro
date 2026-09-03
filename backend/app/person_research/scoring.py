"""Person Attribution Research — verdict engine (boolean rule-first).

Spec §7: binding is a *boolean* decision, not a scalar threshold. The
confidence score is used for RANKING only; it never decides who gets bound.

Pipeline:

- **Step A — blockers**: a candidate may only ever be bound if it passes the
  hard safety rules (tight website co-occurrence AND a bindable local-part
  match). Everything else is rejected from binding.
- **Step B — score**: a 0-100 float for ordering candidates.
- **Step C — verdict**: boolean binding + contradiction → ``unresolved``.

Conservative by design (spec §9): blank/``unattributed`` is always preferred
over an unsupported or ambiguous association.
"""

from __future__ import annotations

import re
from typing import Any

from app.engines.lead.lead_models import role_is_plausibly_relevant
from app.person_research.models import (
    AttributionVerdict,
    LocalPartMatchLevel,
    PersonCandidate,
    ResearchEvidence,
)
from app.person_research.sources import DomainFacts
from app.person_research.store import normalize_email

#: Local-part match levels strong enough to support binding (unique identity).
_BINDABLE_LEVELS = {LocalPartMatchLevel.exact, LocalPartMatchLevel.first_last}

_WORD_RE = re.compile(r"[A-Za-z]+")
_TOKEN_SPLIT_RE = re.compile(r"[._\-]+")


class LocalPartMatcher:
    """Deterministic level of email-local-part <-> candidate-name matching."""

    def level(self, local: str, name: str) -> LocalPartMatchLevel:
        local = (local or "").strip().lower()
        tokens = _name_tokens(name)
        if not tokens or not local:
            return LocalPartMatchLevel.reject
        first = tokens[0]
        last = tokens[-1]

        joined = _TOKEN_SPLIT_RE.sub("", local)
        local_tokens = [t for t in _TOKEN_SPLIT_RE.split(local) if t]

        if len(tokens) == 1:
            # A single-token name matches only when the local part is that name.
            return (
                LocalPartMatchLevel.exact
                if joined == first
                else LocalPartMatchLevel.reject
            )

        # exact: "johnsmith" / "john.smith" / "john-smith"
        if joined == first + last or (
            len(local_tokens) == 2
            and local_tokens[0] == first
            and local_tokens[1] == last
        ):
            return LocalPartMatchLevel.exact

        # first_last: "john.m.smith" (middle initial) — same identity, ordered.
        if (
            len(local_tokens) >= 3
            and local_tokens[0] == first
            and local_tokens[-1] == last
        ):
            return LocalPartMatchLevel.first_last

        # initial_last: "jsmith" / "j.smith" / "j-smith"
        if joined == first[0] + last or (
            len(local_tokens) == 2
            and len(local_tokens[0]) == 1
            and local_tokens[0] == first[0]
            and local_tokens[1] == last
        ):
            return LocalPartMatchLevel.initial_last

        # first: "john" — supports ranking, never binding.
        if joined == first:
            return LocalPartMatchLevel.first

        return LocalPartMatchLevel.reject


def _name_tokens(name: str) -> list[str]:
    return [t.lower() for t in _WORD_RE.findall(name or "")]


class ResearchScorer:
    """Builds candidates from site facts and returns the verdict (Steps A-C)."""

    def __init__(self) -> None:
        self._matcher = LocalPartMatcher()

    def evaluate(
        self,
        email: str,
        domain: str,
        facts: DomainFacts,
        corroborations: dict[str, list[ResearchEvidence]] | None = None,
    ) -> tuple[list[PersonCandidate], AttributionVerdict]:
        """Evaluate one email against site facts (+ optional indexed evidence)."""
        corroborations = corroborations or {}
        target = normalize_email(email)
        local = target.rsplit("@", 1)[0] if target else ""
        candidates = self._build_candidates(facts, target, local)

        # Attach indexed corroboration by candidate name (ranking/strength only).
        for c in candidates:
            for ev in corroborations.get(c.name.lower(), []):
                c.evidence.append(ev)
                c.corroboration_count += 1
            if c.corroboration_count:
                c.authority_evidence = True

        # Step A — blockers.
        eligible = [
            c for c in candidates if self._is_eligible(c)
        ]

        # Step B — score every candidate (ranking only).
        for c in candidates:
            c.confidence = self._score(c)

        # Step C — boolean verdict.
        verdict = self._verdict(candidates, eligible)
        return candidates, verdict

    def rescore(
        self, candidates: list[PersonCandidate]
    ) -> tuple[list[PersonCandidate], AttributionVerdict]:
        """Re-derive scores + verdict from stored candidates. NO external calls.

        This is the ``research_version`` counterpart (spec §10): re-running the
        scoring rules over already-collected evidence — e.g. after a rule change
        — without re-crawling the website or re-hitting a search API.
        """
        for c in candidates:
            c.confidence = self._score(c)
        eligible = [c for c in candidates if self._is_eligible(c)]
        return candidates, self._verdict(candidates, eligible)

    # -- Step A -------------------------------------------------------------

    def _is_eligible(self, c: PersonCandidate) -> bool:
        if not c.co_occurrence:
            return False
        try:
            lvl = LocalPartMatchLevel(c.local_part_match_level)
        except ValueError:  # pragma: no cover - defensive
            return False
        return lvl in _BINDABLE_LEVELS

    # -- Step B -------------------------------------------------------------

    def _score(self, c: PersonCandidate) -> float:
        score = 0.0
        lvl = c.local_part_match_level
        if lvl == LocalPartMatchLevel.exact.value:
            score += 50
        elif lvl == LocalPartMatchLevel.first_last.value:
            score += 40
        elif lvl == LocalPartMatchLevel.initial_last.value:
            score += 20
        elif lvl == LocalPartMatchLevel.first.value:
            score += 10
        if c.role_relevance:
            score += 15
        if c.authority_evidence:
            score += 10
        score += min(c.corroboration_count, 3) * 8
        return round(min(score, 100.0), 1)

    # -- Step C -------------------------------------------------------------

    def _verdict(
        self, candidates: list[PersonCandidate], eligible: list[PersonCandidate]
    ) -> AttributionVerdict:
        if not candidates:
            return AttributionVerdict.unattributed

        if len(eligible) > 1:
            distinct = {c.name for c in eligible}
            if len(distinct) > 1:
                # Two different people both claim this email -> do not decide.
                for c in candidates:
                    c.contradictory = True
                    c.evidence.append(
                        ResearchEvidence(
                            source_url=next(
                                (e.source_url for e in c.evidence if e.source_url), ""
                            ),
                            source_type="company_site",
                            authority="supporting",
                            evidence_kind="contradiction",
                            snippet="multiple distinct candidates claim this email",
                        )
                    )
                return AttributionVerdict.unresolved

        if len(eligible) == 1:
            eligible[0].bound = True
            return AttributionVerdict.attributed

        return AttributionVerdict.candidates_found

    # -- candidate construction ---------------------------------------------

    def _build_candidates(
        self, facts: DomainFacts, target: str, local: str
    ) -> list[PersonCandidate]:
        candidates: list[PersonCandidate] = []
        for ctx in facts.email_contexts.get(target, []):
            name = (ctx.get("name") or "").strip()
            if not name:
                continue
            page = (ctx.get("page") or "").strip()
            role = (ctx.get("role") or "").strip()

            cand = PersonCandidate(
                name=name,
                role=role,
                role_relevance=role_is_plausibly_relevant(role),
                co_occurrence=True,
                local_part_match_level=self._matcher.level(local, name).value,
            )
            if page:
                cand.evidence.append(
                    ResearchEvidence(
                        source_url=page,
                        source_type="company_site",
                        authority="authoritative",
                        evidence_kind="email_present",
                    )
                )
                cand.evidence.append(
                    ResearchEvidence(
                        source_url=page,
                        source_type="company_site",
                        authority="authoritative",
                        evidence_kind="name_co_occurrence",
                        snippet=name,
                    )
                )
                if cand.local_part_match_level in {
                    LocalPartMatchLevel.exact.value,
                    LocalPartMatchLevel.first_last.value,
                }:
                    cand.evidence.append(
                        ResearchEvidence(
                            source_url=page,
                            source_type="company_site",
                            authority="supporting",
                            evidence_kind="local_part_match",
                        )
                    )
            candidates.append(cand)
        return candidates

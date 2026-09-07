"""Offline tests for the rewritten PeopleParser (Increment 2).

Proves the two things the old parser got wrong:

1. a person's NAME is anchored to their ROLE in the region — the old parser
   grabbed the FIRST capitalized phrase in the region, which was usually the
   company name ("Texas Skyline Roofing" instead of "Maria Gomez");
2. each person is bound to the emails found in the SAME page region, with an
   honest tier: a personal local-part is ``person_bound``, a generic mailbox
   (info@/contact@) is ``format`` and can never qualify on its own.

Also pins the V1 record contract: ``person.role_relevance`` derives from
``role_is_plausibly_relevant`` and ``person.tier`` is ``unverified``.

Everything runs fully offline on the saved real-HTML fixtures in
``tests.fixtures.people_pages`` — no live network, no monkeypatching needed.
"""

from __future__ import annotations

import json

from bs4 import BeautifulSoup

from app.discovery.people_parser import PeopleParser, PersonRecord
from app.engines.lead.lead_models import (
    EmailVerificationTier,
    LeadEmail,
    LeadPerson,
    PersonVerificationTier,
)
from tests.fixtures.people_pages import (
    COMPANY_BEFORE_ROLE_HTML,
    MAILTO_ANCHOR_TEXT_HTML,
    MARKETING_PROSE_HTML,
    NO_PEOPLE_PAGE_HTML,
    PROSE_TEAM_PAGE_HTML,
    SENTENCE_BOUNDARY_HTML,
    TEAM_GRID_HTML,
    TEAM_PAGE_WITH_DUPLICATE_HTML,
)

TEAM_URL = "https://texasskylineco.com/team"


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


class TestTeamGrid:
    """The dominant real-world shape: per-member ``<li>`` team cards."""

    def _parse(self):
        return PeopleParser().extract_candidates(
            _soup(TEAM_GRID_HTML), page_url=TEAM_URL
        )

    def test_extracts_each_member(self):
        names = {r.person.name for r in self._parse()}
        assert names == {"Maria Gomez", "Jake Reed", "Sue Lee"}

    def test_person_records_carry_v1_fields(self):
        record = next(r for r in self._parse() if r.person.name == "Maria Gomez")
        assert record.person.role == "President"
        assert record.person.role_relevance is True
        assert record.person.tier is PersonVerificationTier.unverified
        assert record.person.source_url == TEAM_URL

    def test_relevant_roles_flag_true(self):
        jake = next(r for r in self._parse() if r.person.name == "Jake Reed")
        assert jake.person.role == "Project Manager"
        assert jake.person.role_relevance is True

    def test_irrelevant_role_flags_false(self):
        sue = next(r for r in self._parse() if r.person.name == "Sue Lee")
        assert sue.person.role_relevance is False

    def test_emails_bound_with_honest_tiers(self):
        maria = next(r for r in self._parse() if r.person.name == "Maria Gomez")
        tiers = {e.email: e.tier for e in maria.emails}
        assert tiers["m.gomez@texasskylineco.com"] is EmailVerificationTier.person_bound
        assert tiers["info@texasskylineco.com"] is EmailVerificationTier.format

    def test_personal_email_is_person_bound(self):
        """j.reed@ (non-generic local-part) bound to Jake is person_bound."""
        jake = next(r for r in self._parse() if r.person.name == "Jake Reed")
        assert [e.email for e in jake.emails] == ["j.reed@texasskylineco.com"]
        assert jake.emails[0].tier is EmailVerificationTier.person_bound

    def test_footer_generic_mailbox_not_bound_to_anyone(self):
        """The footer ``office@`` sits in a region with no person -> not seen."""
        records = self._parse()
        bound = {e.email for r in records for e in r.emails}
        assert "office@texasskylineco.com" not in bound

    def test_company_name_is_not_a_member(self):
        names = {r.person.name for r in self._parse()}
        assert "Texas Skyline Roofing" not in names


class TestProsePage:
    """The old bug: company name BEFORE the person in one region."""

    def _parse(self, html=PROSE_TEAM_PAGE_HTML, **kwargs):
        return PeopleParser().extract_candidates(
            _soup(html), page_url="https://texasskylineco.com/about", **kwargs
        )

    def test_company_name_is_not_captured_as_person(self):
        """Old parser grabbed 'Texas Skyline Roofing'; the new one anchors to
        the role and returns the person 'Maria Gomez'."""
        records = self._parse()
        assert len(records) == 1
        assert records[0].person.name == "Maria Gomez"
        assert records[0].person.role_relevance is True

    def test_person_gets_the_region_email(self):
        records = self._parse()
        assert [e.email for e in records[0].emails] == [
            "m.gomez@texasskylineco.com"
        ]
        assert records[0].emails[0].tier is EmailVerificationTier.person_bound


class TestCompanyBeforeRole:
    """Adversarial prose: only the site-name guard saves the day."""

    def _parse(self, **kwargs):
        return PeopleParser().extract_candidates(
            _soup(COMPANY_BEFORE_ROLE_HTML),
            page_url="https://texasskylineco.com/leadership",
            **kwargs,
        )

    def test_site_guard_rejects_company_name(self):
        """Company name is the LAST phrase before the role; role-anchoring
        alone is not enough — the auto-detected site name must reject it."""
        records = self._parse()
        assert len(records) == 1
        assert records[0].person.name == "Maria Gomez"

    def test_explicit_company_name_override(self):
        records = self._parse(company_name="Texas Skyline Roofing LLC")
        assert records[0].person.name == "Maria Gomez"

    def test_plausible_name_guard_units(self):
        parser = PeopleParser()
        assert parser._is_plausible_name(
            "Texas Skyline Roofing", "Texas Skyline Roofing"
        ) is False
        assert parser._is_plausible_name("Maria Gomez", "Texas Skyline Roofing") is True
        assert parser._is_plausible_name("Our Team", "") is False


class TestEdgeCases:

    def test_no_people_page_returns_nothing(self):
        assert PeopleParser().extract_candidates(_soup(NO_PEOPLE_PAGE_HTML)) == []

    def test_empty_html_returns_nothing(self):
        assert PeopleParser().extract_candidates(BeautifulSoup("", "html.parser")) == []

    def test_duplicate_person_emitted_once(self):
        records = PeopleParser().extract_candidates(_soup(TEAM_PAGE_WITH_DUPLICATE_HTML))
        assert len(records) == 1
        assert records[0].person.name == "Tom Alvarez"
        assert records[0].person.role == "Estimator"
        # Estimators DO estimation in-house (competitor, not buyer) — founder
        # rule: never a relevant decision-maker. Only the dedup is under test.
        assert records[0].person.role_relevance is False

    def test_person_without_email_is_still_emitted(self):
        """A named decision-maker is a record even with no bound email — the
        V1 gate later blocks on the missing person_bound email."""
        records = PeopleParser().extract_candidates(_soup(TEAM_PAGE_WITH_DUPLICATE_HTML))
        assert records[0].emails == []


class TestPersonRecordSerialization:

    def test_to_dict_is_json_safe_and_api_ready(self):
        record = PersonRecord(
            person=LeadPerson(name="Maria Gomez", role="Owner", role_relevance=True),
            emails=[
                LeadEmail(
                    email="m.gomez@texasskylineco.com",
                    tier=EmailVerificationTier.person_bound,
                )
            ],
        )
        payload = record.to_dict()
        json.dumps(payload)  # must not raise
        assert payload["person"]["name"] == "Maria Gomez"
        assert payload["person"]["tier"] == "unverified"
        assert payload["person"]["role_relevance"] is True
        assert payload["emails"][0]["tier"] == "person_bound"


class TestLiveSiteEvidenceGarbage:
    """Inc11 Step B — the Inc11 Step A live evidence, locked in regression.

    Real Texas roofing sites put marketing copy ("Schedule No Obligation
    Inspection", "Owned Dallas Since Honest", "You Back Same Day", the
    "First Name Last Name" placeholder) right next to an actual person. The
    old parser turned that copy into fake decision-makers. Now: prose never
    becomes a name, the sentence/product boundary can't leak into the name,
    and a mailto link whose anchor text hides the address still binds.
    """

    def test_marketing_prose_produces_no_fake_people(self):
        assert PeopleParser().extract_candidates(_soup(MARKETING_PROSE_HTML)) == []

    def test_real_president_survives_adjacent_prose(self):
        records = PeopleParser().extract_candidates(_soup(SENTENCE_BOUNDARY_HTML))
        assert [r.person.name for r in records] == ["Chris Arrington"]
        assert records[0].person.role == "President"
        assert records[0].person.role_relevance is True
        assert records[0].person.tier is PersonVerificationTier.unverified

    def test_mailto_anchor_text_still_binds_the_email(self):
        records = PeopleParser().extract_candidates(_soup(MAILTO_ANCHOR_TEXT_HTML))
        assert [r.person.name for r in records] == ["John Smith"]
        assert [e.email for e in records[0].emails] == ["john.smith@bertroofing.com"]
        assert records[0].emails[0].tier is EmailVerificationTier.person_bound

    def test_plausible_name_rejects_the_evidence_phrases(self):
        parser = PeopleParser()
        for phrase in (
            "Owned Dallas Since Honest",
            "Schedule No Obligation Inspection",
            "You Back Same Day",
            "First Name Last Name",
        ):
            assert parser._is_plausible_name(phrase, "") is False
        assert parser._is_plausible_name("Maria Gomez", "") is True

    def test_name_likeness_rejects_step_b3_prose(self):
        """Inc11 Step B-2a: positive given-name test ends the blacklist
        whack-a-mole — the NEW prose phrases from the Step B-3 live run."""
        parser = PeopleParser()
        for phrase in (
            "Roofer Whether",
            "Bathroom Remodel Cost",
            "Calculator Bathroom Remodel Cost",
        ):
            assert parser._is_plausible_name(phrase, "") is False

    def test_name_likeness_keeps_the_real_people_found(self):
        """Every real decision-maker the B-3 live run found still passes."""
        parser = PeopleParser()
        for real in (
            "Brandon Barnett",
            "Daniel Guzman",
            "Leslie Folsom",
            "Chris Arrington",
            "Randy Eubank",
        ):
            assert parser._is_plausible_name(real, "") is True

    def test_hyphenated_given_name_counts(self):
        """Juan-Carlos is a person even though no single token is in the set."""
        assert PeopleParser()._is_plausible_name("Juan-Carlos Cruz", "") is True

"""Relevance filter — deterministic social-noise rejection (Phase 2A).

The LinkedIn PERSON lane is where the noise lived (connection/follower counts,
education, certs, honour societies, languages). The filter is deliberate and
source-scoped: it is wired ONLY into that lane (see person_research_ai), never
into company facts — so business-relevant claims (licenses, HUBZone, offices)
are structurally immune. "licen*" is intentionally NOT matched per that rule.
"""

from __future__ import annotations

from app.lead_research.models import AIEvidence
from app.lead_research.relevance import filter_social_noise, is_social_noise


# ---------------------------------------------------------------------------
# is_social_noise — drop cases
# ---------------------------------------------------------------------------

def test_drops_connection_counts():
    assert is_social_noise("Miguel Galarza has 500+ connections on LinkedIn") is True
    assert is_social_noise("27 mutual connections with the founder") is True


def test_drops_follower_counts():
    assert is_social_noise("Miguel Galarza has 5,412 followers on LinkedIn") is True
    assert is_social_noise("2,000 followers of the company page") is True


def test_drops_education_history():
    assert is_social_noise("Attended Louisiana State University") is True
    assert is_social_noise("Studied at Stanford University") is True
    assert is_social_noise("Graduated with a degree in civil engineering") is True
    assert is_social_noise("Alumnus of University of Texas") is True


def test_drops_honour_societies():
    assert is_social_noise("Member of Omicron Delta Kappa honor society") is True
    assert is_social_noise("President of Delta Kappa") is True


def test_drops_certifications_but_keeps_licenses():
    assert is_social_noise("Completed CITI human subjects certification") is True
    assert is_social_noise("Holds several certifications in construction") is True
    # License is a BUSINESS signal — the "licen*" exclusion is the point.
    assert is_social_noise("Licensed General Contractor in Texas") is False
    assert is_social_noise("Holds a Texas contractor license") is False


def test_drops_languages():
    assert is_social_noise("Speaks Spanish and English") is True
    assert is_social_noise("Fluent in Spanish") is True
    assert is_social_noise("Languages: Spanish") is True


def test_drops_empty_claim():
    assert is_social_noise("") is True
    assert is_social_noise("   ") is True


# ---------------------------------------------------------------------------
# is_social_noise — keep cases (real dossiers' business facts)
# ---------------------------------------------------------------------------

def test_keeps_business_facts():
    assert is_social_noise("Miguel Galarza is the founder and president of Yerba Buena Engineering") is False
    assert is_social_noise("Miguel Galarza, President of Yerba Buena Engineering") is False
    assert is_social_noise("Yerba Buena Engineering is listed as a Prime contractor") is False
    assert is_social_noise("Located in San Francisco, California, United States") is False
    assert is_social_noise("The company was founded in 2002") is False
    assert is_social_noise("Company size is 51–200 employees") is False
    assert is_social_noise("Estimating services needed") is False


# ---------------------------------------------------------------------------
# filter_social_noise
# ---------------------------------------------------------------------------

def test_filter_preserves_order_and_drops_noise():
    facts = [
        AIEvidence(claim="Founder and president of Yerba Buena Engineering", source_url="https://li/in/x", source_type="linkedin", confidence="verified"),
        AIEvidence(claim="500+ connections on LinkedIn", source_url="https://li/in/x", source_type="linkedin", confidence="verified"),
        AIEvidence(claim="Located in San Francisco, California", source_url="https://li/in/x", source_type="linkedin", confidence="verified"),
        AIEvidence(claim="Attended LSU", source_url="https://li/in/x", source_type="linkedin", confidence="verified"),
    ]
    kept = filter_social_noise(facts)
    assert [f.claim for f in kept] == [
        "Founder and president of Yerba Buena Engineering",
        "Located in San Francisco, California",
    ]


def test_filter_keeps_all_when_clean():
    facts = [
        AIEvidence(claim="VP Estimating at Acme", source_url="https://li/in/x", source_type="linkedin", confidence="verified"),
        AIEvidence(claim="Licensed contractor", source_url="https://li/in/x", source_type="linkedin", confidence="verified"),
    ]
    assert filter_social_noise(facts) == facts
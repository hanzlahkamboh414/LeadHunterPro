"""P4 — the byproduct splitter: a researched dossier's person LinkedIn URL
stocks the LinkedIn vertical.

Hermetic: the singleton ``get_store`` is monkeypatched onto a tmp DB, so
no test ever touches output/linkedin_leads.db.
"""

from __future__ import annotations

import pytest

from app.lead_research.models import (
    CompanyProfile,
    LeadDossier,
    PersonFindings,
)
from app.lead_research.service import LeadResearchStore
from app.linkedin.store import LinkedInLeadsStore


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    research = LeadResearchStore(db_path=str(tmp_path / "research.db"))
    linkedin = LinkedInLeadsStore(db_path=str(tmp_path / "linkedin.db"))

    import app.linkedin.store as li_store_module
    monkeypatch.setattr(li_store_module, "get_store", lambda: linkedin)
    return research, linkedin


def _dossier(linkedin="", email="owner@acmegc.com"):
    return LeadDossier(
        email=email, domain="acmegc.com",
        company=CompanyProfile(
            name="Acme GC", industry="General Contractor",
            location="Vancouver, WA",
        ),
        person=PersonFindings(
            name="Jane Smith", role="Owner", bound=True, linkedin=linkedin,
        ),
    )


def test_save_splits_person_linkedin_into_the_pool(stores):
    research, linkedin = stores
    research.save(_dossier(linkedin="https://www.linkedin.com/in/jane-smith"))

    served = linkedin.serve("", "", "", 10, "any-user")
    assert len(served) == 1
    lead = served[0]
    assert lead["person_name"] == "Jane Smith"
    assert lead["role"] == "Owner"
    assert lead["company_name"] == "Acme GC"
    assert lead["trade"] == "gc"               # industry folded at stock
    assert lead["state"] == "WA" and lead["city"] == "Vancouver"
    assert lead["source"] == "email_research"
    assert lead["source_email"] == "owner@acmegc.com"


def test_save_without_linkedin_stocks_nothing(stores):
    research, linkedin = stores
    research.save(_dossier(linkedin=""))
    assert linkedin.pool_stats()["total"] == 0


def test_save_refuses_company_pages(stores):
    research, linkedin = stores
    research.save(_dossier(
        linkedin="https://www.linkedin.com/company/acme-gc",
        email="co@acmegc.com",
    ))
    assert linkedin.pool_stats()["total"] == 0


def test_re_research_restocks_as_duplicate_not_new(stores):
    research, linkedin = stores
    d = _dossier(linkedin="https://www.linkedin.com/in/jane-smith")
    research.save(d)
    research.save(d)  # re-research of the same person
    assert linkedin.pool_stats()["total"] == 1


def test_byproduct_failure_never_breaks_the_save(stores, monkeypatch):
    """Even if the LinkedIn store explodes, the DOSSIER must be saved — the
    byproduct rides on the save, it must never sink it."""
    research, linkedin = stores

    def exploding_add(records):
        raise RuntimeError("linkedin db on fire")

    monkeypatch.setattr(linkedin, "add", exploding_add)
    research.save(_dossier(linkedin="https://www.linkedin.com/in/jane-smith"))

    assert research.get("owner@acmegc.com") is not None  # dossier survived

"""An enabled unified database must never silently become an empty database."""

import sqlite3
import subprocess
import sys

import pytest

from app.core.db_paths import operational_db_path


def test_legacy_path_is_preserved_without_unified_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("LEADHUNTER_UNIFIED_DB_PATH", raising=False)
    legacy = str(tmp_path / "users.db")

    assert operational_db_path(legacy) == legacy


def test_missing_unified_file_fails_closed(tmp_path, monkeypatch):
    target = tmp_path / "missing.db"
    monkeypatch.setenv("LEADHUNTER_UNIFIED_DB_PATH", str(target))

    with pytest.raises(RuntimeError, match="does not exist"):
        operational_db_path(str(tmp_path / "users.db"))

    assert not target.exists()


def test_unmarked_file_fails_closed(tmp_path, monkeypatch):
    target = tmp_path / "unmarked.db"
    with sqlite3.connect(target) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
    monkeypatch.setenv("LEADHUNTER_UNIFIED_DB_PATH", str(target))

    with pytest.raises(RuntimeError, match="not a verified unified database"):
        operational_db_path(str(tmp_path / "users.db"))


def test_marked_file_is_selected(tmp_path, monkeypatch):
    target = tmp_path / "unified.db"
    with sqlite3.connect(target) as conn:
        conn.execute("CREATE TABLE unified_metadata (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO unified_metadata VALUES (1)")
    monkeypatch.setenv("LEADHUNTER_UNIFIED_DB_PATH", str(target))

    assert operational_db_path(str(tmp_path / "users.db")) == str(target)


def test_default_stores_open_one_verified_database(tmp_path, monkeypatch):
    target = tmp_path / "unified.db"
    with sqlite3.connect(target) as conn:
        conn.execute("CREATE TABLE unified_metadata (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO unified_metadata VALUES (1)")
    monkeypatch.setenv("LEADHUNTER_UNIFIED_DB_PATH", str(target))
    script = """
from app.auth.models import UserStore
from app.auth.settings import AuthSettings
from app.auth.activity import ActivityStore
from app.email_accounts.store import EmailAccountStore
from app.phones.store import PhoneLeadsStore
from app.campaigns.store import CampaignStore
from app.harvester.store import HarvesterStore
from app.harvester.lane_schedule import HarvesterLaneStore
from app.leads.jobs import JobStore
from app.lead_research.service import LeadResearchStore, PendingLeadsStore
from app.source_scout.store import ScoutStore
from app.research.store import ResearchEvidenceStore
from app.search_providers.cache import SearchCache
from app.email.bounce_learning import BounceStore
from app.linkedin.store import LinkedInLeadsStore
for cls in (UserStore, AuthSettings, ActivityStore, EmailAccountStore,
            PhoneLeadsStore, CampaignStore, HarvesterStore,
            HarvesterLaneStore, JobStore, LeadResearchStore,
            PendingLeadsStore, ScoutStore, ResearchEvidenceStore,
            BounceStore, LinkedInLeadsStore):
    cls()
cache = SearchCache()
assert cache.available
cache.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(target) as conn:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"users", "phone_leads", "dossiers", "campaigns", "harvest_runs",
            "sources", "evidence", "search_cache", "linkedin_leads"} <= tables
    assert not (tmp_path / "users.db").exists()


def test_configured_search_cache_cannot_escape_unified_database(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.search_providers.cache import get_search_cache, reset_search_cache

    target = tmp_path / "unified.db"
    with sqlite3.connect(target) as conn:
        conn.execute("CREATE TABLE unified_metadata (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO unified_metadata VALUES (1)")
    separate = tmp_path / "separate_cache.db"
    monkeypatch.setenv("LEADHUNTER_UNIFIED_DB_PATH", str(target))
    monkeypatch.setattr(settings, "SEARCH_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "SEARCH_CACHE_DB", str(separate))
    reset_search_cache()
    try:
        cache = get_search_cache()
        assert cache is not None
        assert cache.path == str(target)
        assert not separate.exists()
    finally:
        reset_search_cache()

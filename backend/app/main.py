from contextlib import asynccontextmanager
import os
from pathlib import Path
import sqlite3

from fastapi import FastAPI
import logging

from app.auth.dependencies import multi_tenant_enabled


def verify_tenant_startup() -> None:
    """Reject unisolated storage before importing routers or starting workers."""
    if not multi_tenant_enabled():
        return
    configured = os.environ.get("LEADHUNTER_UNIFIED_DB_PATH", "").strip()
    if not configured:
        raise RuntimeError("multi-tenant mode requires a unified database")
    target = Path(configured)
    if not target.is_absolute() or not target.is_file():
        raise RuntimeError("multi-tenant mode requires an existing unified database")
    try:
        with sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True) as conn:
            version = conn.execute("SELECT version FROM unified_metadata").fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError("tenant isolation schema not ready") from exc
    if version != (2,):
        raise RuntimeError("tenant isolation schema not ready")
    from app.auth.models import UserStore
    UserStore().require_secure_platform_admin()


verify_tenant_startup()

from app.api.v1.router import api_router  # noqa: E402 - tenant preflight runs first

from app.core.config import settings
from app.core.logging import setup_logging

setup_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    verify_tenant_startup()

    # Server boot: fail any job left in a live state by a process that died
    # without finishing it (jobs.py recover_orphans). Deliberately a lifespan
    # hook and NOT module scope — merely importing this module (pytest, other
    # scripts) must never sweep a job that is genuinely running in another
    # process.
    from app.api.v1 import leads as leads_module
    try:
        recovered = leads_module._manager.recover_orphans()
        if recovered:
            logger.warning("stale in-flight jobs marked failed at boot: %d", recovered)
    except Exception:  # noqa: BLE001 - boot must survive a failed sweep
        logger.exception("recover_orphans failed at boot — continuing")

    # Seed the admin user on first boot (idempotent).
    try:
        from app.auth.models import UserStore
        store = UserStore()
        admin = store.ensure_admin()
        logger.info("Admin user ready: %s (id=%s)", admin.username, admin.id)
    except Exception:  # noqa: BLE001 — auth failure must never block startup
        logger.exception("admin seed failed — continuing without auth")

    # Campaign scheduler (Phase E3): one daemon thread that slowly drains
    # running campaigns (pacing, daily caps, 429 backoff). Started here —
    # NOT at import time — so pytest never spawns a real sending loop.
    scheduler = None
    if multi_tenant_enabled():
        logger.warning("campaign scheduler disabled until tenant isolation is complete")
    else:
        try:
            from app.campaigns.scheduler import get_scheduler
            scheduler = get_scheduler()
            scheduler.start()
        except Exception:  # noqa: BLE001 — a scheduler failure must not kill the app
            logger.exception("campaign scheduler failed to start — continuing")

    # Phone-lead email enrichment (phones vertical): one daemon thread that
    # enriches claimed phone leads with emails from their own websites and
    # feeds the finds into the emails vertical's pending cache. Same rule —
    # lifespan, never import time, so pytest never spawns a real crawl loop.
    enricher = None
    try:
        from app.phones.enrich_worker import get_worker
        enricher = get_worker()
        enricher.start()
    except Exception:  # noqa: BLE001 — an enricher failure must not kill the app
        logger.exception("phone enrichment worker failed to start — continuing")

    # Background harvester (P6): one daemon thread that stocks the trade×state
    # phone pools (free license-board fetches) and runs demand-gated email
    # research with the spare admission-control budget (SHARED dossiers, so
    # any user's later search serves them instantly). Same rule — lifespan,
    # never import time, so pytest never spawns a real harvest loop.
    harvester = None
    try:
        from app.harvester.worker import get_worker as get_harvester
        harvester = get_harvester()
        harvester.start()
    except Exception:  # noqa: BLE001 — a harvester failure must not kill the app
        logger.exception("background harvester failed to start — continuing")

    # Discover and verify new public board datasets independently of phone
    # demand. Its own durable schedule prevents duplicate daily AI work on
    # restarts and retries failures without stopping the background loop.
    scout = None
    if settings.SOURCE_SCOUT_ENABLED:
        try:
            from app.source_scout.worker import get_worker as get_scout
            scout = get_scout()
            scout.start()
        except Exception:  # noqa: BLE001 — scout failure must not block startup
            logger.exception("source scout failed to start — continuing")

    # Intent-plugin registration (Phase 1, Company Signal Intelligence
    # engine). The three buying-intent plugins (usaspending / google_news /
    # company_site) were built, tested and then never wired to a caller;
    # registering them here is what makes them discoverable by name and
    # capability at runtime, and it gives the process an explicit record of
    # what evidence collection can actually reach. That record matters:
    # CLAUDE.md §5/§12 — an empty provider registry must never be silent.
    #
    # This cannot affect company discovery: the plugins declare
    # PROJECT_/NEWS_/BID_DISCOVERY and never COMPANY_DISCOVERY, and the
    # production discovery orchestrator registers its own sources rather
    # than reading this registry.
    try:
        from app.discovery.intent import register_intent_plugins, registered_intent_plugins
        from app.discovery.plugins.plugin_registry import get_registry

        added = register_intent_plugins()
        active = registered_intent_plugins()
        if active:
            logger.info(
                "intent plugins ready: %s (registered now: %s)",
                [p.name for p in active],
                added or "none — already registered",
            )
        else:
            logger.error(
                "NO INTENT PLUGINS REGISTERED — evidence collection will fall "
                "back to the built-in plugin list at call time. Registered "
                "now: %s, registry size: %d",
                added,
                len(get_registry()),
            )
    except Exception:  # noqa: BLE001 — plugin setup must not block startup
        logger.exception("intent plugin registration failed — continuing")

    # The sweep is the startup block; yield hands control to the app until it
    # shuts down.
    yield
    if scheduler is not None:
        scheduler.stop()
    if enricher is not None:
        enricher.stop()
    if harvester is not None:
        harvester.stop()
    if scout is not None:
        scout.stop()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AI-powered Lead Generation Platform",
    lifespan=lifespan,
)

app.include_router(api_router)

@app.get("/")
def root():
    logger.info("Root endpoint accessed")

    return {
        "message": "LeadHunter Pro API is running",
        "status": "success",
        "version": settings.APP_VERSION,
    }

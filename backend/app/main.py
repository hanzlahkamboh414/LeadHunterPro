from contextlib import asynccontextmanager

from app.api.v1.router import api_router
from fastapi import FastAPI
import logging


from app.core.config import settings
from app.core.logging import setup_logging

setup_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
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

    # The sweep is the startup block; yield hands control to the app until it
    # shuts down (no shutdown work needed — an orphaned job is exactly the
    # other process's business, the next boot sweeps it).
    yield


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
from app.api.health import router as health_router
from fastapi import FastAPI
import logging


from app.core.config import settings
from app.core.logging import setup_logging
from app.api.database import router as database_router
from app.api.company import router as company_router

setup_logging()

logger = logging.getLogger(__name__)


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AI-powered Lead Generation Platform"
)

app.include_router(health_router)
app.include_router(database_router)
app.include_router(company_router)


@app.get("/")
def root():
    logger.info("Root endpoint accessed")

    return {
        "message": "LeadHunter Pro API is running",
        "status": "success",
        "version": settings.APP_VERSION,
    }
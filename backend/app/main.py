from app.api.v1.router import api_router
from fastapi import FastAPI
import logging


from app.core.config import settings
from app.core.logging import setup_logging

setup_logging()

logger = logging.getLogger(__name__)


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AI-powered Lead Generation Platform"
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
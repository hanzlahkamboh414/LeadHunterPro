"""API v1 router — aggregates all sub-routers."""

import logging

from fastapi import APIRouter

from app.api.v1.company import router as company_router
from app.api.v1.connectors import router as connectors_router
from app.api.v1.contact import router as contact_router
from app.api.v1.database import router as database_router
from app.api.v1.discovery import router as discovery_router
from app.api.v1.email import router as email_router
from app.api.v1.email_accounts import router as email_accounts_router
from app.api.v1.gmail_inbox import router as gmail_inbox_router
from app.api.v1.health import router as health_router
from app.api.v1.auth import router as auth_router
from app.api.v1.admin import router as admin_router
from app.api.v1.campaigns import router as campaigns_router
from app.api.v1.leadership import router as leadership_router
from app.api.v1.leads import router as leads_router
from app.api.v1.linkedin import router as linkedin_router
from app.api.v1.phones import router as phones_router
from app.api.v1.research import router as research_router
from app.api.v1.source_intelligence import router as source_intelligence_router

logger = logging.getLogger(__name__)

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(admin_router)
api_router.include_router(database_router)
api_router.include_router(company_router)
api_router.include_router(contact_router)
api_router.include_router(research_router)
api_router.include_router(leadership_router)
api_router.include_router(email_router)
api_router.include_router(discovery_router)
api_router.include_router(source_intelligence_router)
api_router.include_router(connectors_router)
api_router.include_router(leads_router)
api_router.include_router(phones_router)
api_router.include_router(linkedin_router)
api_router.include_router(email_accounts_router)
api_router.include_router(gmail_inbox_router)
api_router.include_router(campaigns_router)

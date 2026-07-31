"""Company API routes."""

import logging
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.schemas.company import CompanyCreate, CompanyResponse
from app.services.company_service import CompanyService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/companies", tags=["Companies"])

service = CompanyService()


@router.post("/", response_model=CompanyResponse)
def create_company(
    company: CompanyCreate,
    db: Session = Depends(get_db),
) -> CompanyResponse:
    """Create a new company record."""
    return service.create_company(db, company)


@router.get("/", response_model=List[CompanyResponse])
def get_companies(
    db: Session = Depends(get_db),
) -> List[CompanyResponse]:
    """List all companies."""
    return service.get_companies(db)

"""Research API routes."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.schemas.research import ResearchCreate, ResearchResponse
from app.services.research_service import ResearchService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research", tags=["Research"])

service = ResearchService()


@router.post("/", response_model=ResearchResponse)
def create_research(
    research: ResearchCreate,
    db: Session = Depends(get_db),
) -> ResearchResponse:
    """Create a research record and kick off scoring."""
    result = service.create_research(
        db=db,
        company_id=research.company_id,
        raw_data=research.raw_data,
    )
    service.generate_score(
        db=db,
        company_id=research.company_id,
        raw_data=research.raw_data,
    )
    return result


@router.get("/{company_id}", response_model=ResearchResponse)
def get_research(
    company_id: int,
    db: Session = Depends(get_db),
) -> ResearchResponse:
    """Retrieve research for a given company."""
    return service.get_company_research(db=db, company_id=company_id)


@router.post("/{research_id}/summary")
def generate_summary(
    research_id: int,
    db: Session = Depends(get_db),
):
    """Generate an AI summary for a specific research record."""
    research = service.repository.get_by_id(db=db, research_id=research_id)
    if not research:
        return {"message": "Research not found"}
    return service.generate_summary(
        db=db,
        research_id=research_id,
        raw_data=research.raw_data,
    )

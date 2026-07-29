from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database.database import get_db
from app.schemas.company import CompanyCreate, CompanyResponse
from app.services.company_service import CompanyService

router = APIRouter(
    prefix="/companies",
    tags=["Companies"],
)

service = CompanyService()


@router.post("/", response_model=CompanyResponse)
def create_company(
    company: CompanyCreate,
    db: Session = Depends(get_db),
):
    return service.create_company(db, company)


@router.get("/", response_model=List[CompanyResponse])
def get_companies(
    db: Session = Depends(get_db),
):
    return service.get_companies(db)
from sqlalchemy.orm import Session

from app.repositories.company_repository import CompanyRepository
from app.schemas.company import CompanyCreate


class CompanyService:

    def __init__(self):
        self.repository = CompanyRepository()

    def create_company(self, db: Session, company: CompanyCreate):
        return self.repository.create(db, company)

    def get_companies(self, db: Session):
        return self.repository.get_all(db)
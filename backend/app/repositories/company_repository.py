from sqlalchemy.orm import Session

from app.models.company import Company
from app.schemas.company import CompanyCreate


class CompanyRepository:

    def create(self, db: Session, company: CompanyCreate) -> Company:
        db_company = Company(
            company_name=company.company_name,
            website=str(company.website),
            industry=company.industry,
            headquarters=company.headquarters,
            employee_count=company.employee_count,
        )

        db.add(db_company)
        db.commit()
        db.refresh(db_company)

        return db_company

    def get_all(self, db: Session):
        return db.query(Company).all()
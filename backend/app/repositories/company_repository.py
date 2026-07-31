"""Company repository – database access for Company model."""

import logging

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import CompanyAlreadyExistsException
from app.models.company import Company
from app.schemas.company import CompanyCreate

logger = logging.getLogger(__name__)


class CompanyRepository:
    """Data-access layer for companies."""

    def create(self, db: Session, company: CompanyCreate) -> Company:
        """Insert a new company into the database.

        Args:
            db: Database session.
            company: Pydantic schema with company data.

        Returns:
            The persisted Company ORM instance.

        Raises:
            CompanyAlreadyExistsException: If a company with the same
                website already exists.
        """
        db_company = Company(
            company_name=company.company_name,
            website=str(company.website),
            industry=company.industry,
            headquarters=company.headquarters,
            employee_count=company.employee_count,
        )

        try:
            db.add(db_company)
            db.commit()
            db.refresh(db_company)
            return db_company
        except IntegrityError:
            db.rollback()
            raise CompanyAlreadyExistsException()

    def get_all(self, db: Session) -> list[Company]:
        """Return all companies in the database.

        Args:
            db: Database session.

        Returns:
            List of all Company records.
        """
        return db.query(Company).all()

    def update_score(self, db: Session, company_id: int, score: int) -> Company | None:
        """Update the AI score for a company.

        Args:
            db: Database session.
            company_id: Primary key of the company to update.
            score: New AI lead score (0-100).

        Returns:
            The updated Company record, or None if not found.
        """
        company = db.query(Company).filter(Company.id == company_id).first()
        if company is None:
            logger.warning("Company ID %s not found for score update", company_id)
            return None
        company.ai_score = score
        db.add(company)
        db.commit()
        db.refresh(company)
        return company

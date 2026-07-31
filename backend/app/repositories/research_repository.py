"""Research repository – database access for Research model."""

import logging

from sqlalchemy.orm import Session

from app.models.research import Research

logger = logging.getLogger(__name__)


class ResearchRepository:
    """Data-access layer for research records."""

    def create(self, db: Session, company_id: int, raw_data: dict) -> Research:
        """Insert a new research record.

        Args:
            db: Database session.
            company_id: Foreign key to the Company table.
            raw_data: Raw crawled website data as a dictionary.

        Returns:
            The persisted Research ORM instance.
        """
        research = Research(company_id=company_id, raw_data=raw_data)
        db.add(research)
        db.commit()
        db.refresh(research)
        return research

    def get_by_company(self, db: Session, company_id: int) -> Research | None:
        """Return the latest research record for a company.

        Args:
            db: Database session.
            company_id: Company primary key.

        Returns:
            The most recent Research record, or None.
        """
        return db.query(Research).filter(Research.company_id == company_id).first()

    def get_by_id(self, db: Session, research_id: int) -> Research | None:
        """Return a single research record by its primary key.

        Args:
            db: Database session.
            research_id: Primary key of the research record.

        Returns:
            The Research record, or None if not found.
        """
        return db.query(Research).filter(Research.id == research_id).first()

    def update_summary(self, db: Session, research_id: int, summary: dict) -> Research | None:
        """Store an AI-generated summary for a research record.

        Args:
            db: Database session.
            research_id: Primary key of the research record.
            summary: Parsed JSON summary dictionary.

        Returns:
            The updated Research record, or None if not found.
        """
        research = db.query(Research).filter(Research.id == research_id).first()
        if not research:
            return None
        research.ai_summary = summary
        db.commit()
        db.refresh(research)
        return research

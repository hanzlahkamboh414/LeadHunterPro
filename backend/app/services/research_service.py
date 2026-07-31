"""Research service – business logic for company research."""

import logging

from sqlalchemy.orm import Session

from app.ai.summarizer import CompanySummarizer
from app.repositories.company_repository import CompanyRepository
from app.repositories.research_repository import ResearchRepository
from app.scoring.company_score import CompanyScorer

logger = logging.getLogger(__name__)


class ResearchService:
    """Orchestrate research creation, scoring, and summarisation."""

    def __init__(self) -> None:
        self.repository = ResearchRepository()
        self.company_repository = CompanyRepository()
        self.summarizer = CompanySummarizer()
        self.scorer = CompanyScorer()

    def create_research(self, db: Session, company_id: int, raw_data: dict) -> dict:
        """Persist raw crawled data as a research record.

        Args:
            db: Database session.
            company_id: Associated company ID.
            raw_data: Crawled website data.

        Returns:
            Dict representation of the created record.
        """
        research = self.repository.create(db=db, company_id=company_id, raw_data=raw_data)
        return {
            "id": research.id,
            "company_id": research.company_id,
            "raw_data": research.raw_data,
            "ai_summary": research.ai_summary,
            "created_at": str(research.created_at),
        }

    def get_company_research(self, db: Session, company_id: int) -> dict | None:
        """Retrieve the latest research for a company.

        Args:
            db: Database session.
            company_id: Company primary key.

        Returns:
            Research dict or None.
        """
        research = self.repository.get_by_company(db=db, company_id=company_id)
        if not research:
            return None
        return {
            "id": research.id,
            "company_id": research.company_id,
            "raw_data": research.raw_data,
            "ai_summary": research.ai_summary,
            "created_at": str(research.created_at),
        }

    def get_research_by_id(self, db: Session, research_id: int) -> dict | None:
        """Retrieve a single research record by its ID.

        Args:
            db: Database session.
            research_id: Research record primary key.

        Returns:
            Research dict or None if not found.
        """
        research = self.repository.get_by_id(db=db, research_id=research_id)
        if not research:
            return None
        return {
            "id": research.id,
            "company_id": research.company_id,
            "raw_data": research.raw_data,
            "ai_summary": research.ai_summary,
            "created_at": str(research.created_at),
        }

    def generate_score(self, db: Session, company_id: int, raw_data: dict) -> int:
        """Calculate and persist a lead score for a company.

        Args:
            db: Database session.
            company_id: Company primary key.
            raw_data: Crawled website data used for scoring.

        Returns:
            The computed score (0–100).
        """
        score = self.scorer.calculate(raw_data)
        self.company_repository.update_score(db=db, company_id=company_id, score=score)
        return score

    def generate_summary(self, db: Session, research_id: int, raw_data: dict) -> dict:
        """Generate an AI summary and persist it to the research record.

        Args:
            db: Database session.
            research_id: Research record primary key.
            raw_data: Crawled website data.

        Returns:
            The generated summary dictionary.
        """
        summary = self.summarizer.summarize(raw_data)
        # Attempt to parse JSON; fall back to raw string on failure
        import json
        try:
            parsed = json.loads(summary)
        except (TypeError, json.JSONDecodeError):
            parsed = {"summary": summary}
        self.repository.update_summary(db=db, research_id=research_id, summary=parsed)
        return parsed

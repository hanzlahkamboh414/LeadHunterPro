from datetime import datetime

from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import Integer
from sqlalchemy import String

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.database.base import Base


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    company_name: Mapped[str] = mapped_column(String(255), nullable=False)

    website: Mapped[str] = mapped_column(String(255), unique=True)

    industry: Mapped[str | None] = mapped_column(String(100))

    headquarters: Mapped[str | None] = mapped_column(String(255))

    employee_count: Mapped[int | None] = mapped_column(Integer)

    ai_score: Mapped[int] = mapped_column(Integer, default=0)

    research_completed: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )
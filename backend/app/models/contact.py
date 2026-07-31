from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.orm import relationship

from app.database.base import Base


class Contact(Base):
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True, index=True)

    company_id = Column(
        Integer,
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )

    full_name = Column(String(255), nullable=False)

    job_title = Column(String(255))

    email = Column(String(255), unique=True)

    linkedin_url = Column(String(500))

    phone = Column(String(100))

    verified = Column(Boolean, default=False)

    company = relationship("Company", back_populates="contacts")
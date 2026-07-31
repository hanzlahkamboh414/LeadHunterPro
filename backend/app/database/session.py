"""SQLAlchemy session management.

Provides a reusable ``SessionLocal`` factory and a FastAPI dependency
``get_db`` that yields a database session and guarantees closure.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    echo=False,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


def get_db() -> Session:
    """Yield a database session and ensure it is closed afterwards.

    Yields:
        Session: A SQLAlchemy database session.
    """
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()

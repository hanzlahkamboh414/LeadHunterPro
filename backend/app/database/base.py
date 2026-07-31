"""SQLAlchemy declarative base.

All ORM model classes inherit from :class:`Base`, which centralises
metadata and provides the ``declarative_base()`` functionality from
SQLAlchemy 2.x.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all ORM model classes."""

    pass

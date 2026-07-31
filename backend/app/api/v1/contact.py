"""Contact API routes."""

import logging
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.schemas.contact import ContactCreate, ContactResponse
from app.services.contact_service import ContactService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/contacts", tags=["Contacts"])

service = ContactService()


@router.post("/", response_model=ContactResponse)
def create_contact(
    contact: ContactCreate,
    db: Session = Depends(get_db),
) -> ContactResponse:
    """Create a new contact record."""
    return service.create_contact(db, contact)


@router.get("/", response_model=List[ContactResponse])
def get_contacts(
    db: Session = Depends(get_db),
) -> List[ContactResponse]:
    """List all contacts."""
    return service.get_contacts(db)

from sqlalchemy.orm import Session

from app.repositories.contact_repository import ContactRepository
from app.schemas.contact import ContactCreate


class ContactService:

    def __init__(self):
        self.repository = ContactRepository()

    def create_contact(self, db: Session, contact: ContactCreate):
        return self.repository.create(db, contact)

    def get_contacts(self, db: Session):
        return self.repository.get_all(db)
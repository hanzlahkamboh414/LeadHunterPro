from sqlalchemy.orm import Session

from app.models.contact import Contact
from app.schemas.contact import ContactCreate


class ContactRepository:

    def create(self, db: Session, contact: ContactCreate) -> Contact:
        db_contact = Contact(**contact.model_dump())

        db.add(db_contact)
        db.commit()
        db.refresh(db_contact)

        return db_contact

    def get_all(self, db: Session):
        return db.query(Contact).all()
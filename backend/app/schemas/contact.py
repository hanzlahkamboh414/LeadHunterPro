from pydantic import BaseModel, EmailStr
from typing import Optional


class ContactCreate(BaseModel):
    company_id: int
    full_name: str
    job_title: Optional[str] = None
    email: Optional[EmailStr] = None
    linkedin_url: Optional[str] = None
    phone: Optional[str] = None


class ContactResponse(ContactCreate):
    id: int
    verified: bool

    class Config:
        from_attributes = True
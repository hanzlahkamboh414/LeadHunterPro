from datetime import datetime

from pydantic import BaseModel


class ResearchCreate(BaseModel):
    company_id: int
    raw_data: dict


class ResearchResponse(BaseModel):

    id: int
    company_id: int
    raw_data: dict
    ai_summary: dict | None = None
    created_at: datetime

    class Config:
        from_attributes = True
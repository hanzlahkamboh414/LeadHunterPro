from pydantic import BaseModel, ConfigDict, HttpUrl
from datetime import datetime


class CompanyCreate(BaseModel):
    company_name: str
    website: HttpUrl
    industry: str | None = None
    headquarters: str | None = None
    employee_count: int | None = None


class CompanyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_name: str
    website: str
    industry: str | None
    headquarters: str | None
    employee_count: int | None
    ai_score: int
    research_completed: bool
    created_at: datetime
"""Admin-only operational dashboard response models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AdminJobSummary(BaseModel):
    id: str
    query: dict = Field(default_factory=dict)
    state: str
    error: str = ""
    created_at: str
    updated_at: str
    elapsed_s: float = 0.0


class AdminDashboardOut(BaseModel):
    generated_at: str
    database_path: str
    dossiers_total: int
    recommendations: dict[str, int] = Field(default_factory=dict)
    pending: dict[str, int] = Field(default_factory=dict)
    jobs: dict[str, int] = Field(default_factory=dict)
    risks: dict[str, int] = Field(default_factory=dict)
    recent_jobs: list[AdminJobSummary] = Field(default_factory=list)

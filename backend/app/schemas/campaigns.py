"""Campaign API schemas (Phase E3/E4)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FollowupIn(BaseModel):
    """One follow-up rung on the campaign ladder (Phase E4). Step numbers
    are assigned in list order; ``after_days`` counts from the previous
    step's SEND, not from the campaign start."""
    after_days: int = Field(ge=1, le=30)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)


class CampaignCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    account_id: int
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)
    emails: list[str] = Field(min_length=1, max_length=500)
    """ISO-8601 datetime (with offset) — the 9:00 AM start, computed in the
    user's timezone by the client."""
    start_at: str = Field(min_length=10, max_length=40)
    daily_limit: int = Field(default=30, ge=1, le=200)
    delay_min_s: int = Field(default=180, ge=30, le=3600)
    delay_max_s: int = Field(default=420, ge=60, le=7200)
    followups: list[FollowupIn] = Field(default_factory=list, max_length=3)
    """Optional follow-up emails (max 3). Each fires after_days days past
    the previous send, and is cancelled the moment the lead replies."""


class CampaignSendOut(BaseModel):
    id: int
    email: str
    step: int
    state: str
    subject: str
    sent_at: str
    not_before: str
    attempts: int
    error: str


class FollowupOut(BaseModel):
    step: int
    after_days: int
    subject: str
    body: str


class CampaignOut(BaseModel):
    id: int
    account_id: int
    name: str
    subject: str
    body: str
    status: str
    paused_reason: str
    resume_at: str
    start_at: str
    daily_limit: int
    delay_min_s: int
    delay_max_s: int
    created_at: str
    updated_at: str
    pending: int
    sent: int
    failed: int
    skipped: int
    replied: int
    account_email: str = ""


class CampaignsOut(BaseModel):
    campaigns: list[CampaignOut]


class CampaignCreateOut(BaseModel):
    campaign: CampaignOut
    """Leads dropped at create time because an earlier campaign of this user
    already emailed them (never silently re-mailed)."""
    excluded: int


class CampaignDetailOut(CampaignOut):
    sends: list[CampaignSendOut] = []
    followups: list[FollowupOut] = []

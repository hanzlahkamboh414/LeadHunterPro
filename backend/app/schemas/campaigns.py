"""Campaign API schemas (Phase E3/E4)."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class FollowupIn(BaseModel):
    """One follow-up rung on the campaign ladder (Phase E4). Step numbers
    are assigned in list order; ``after_days`` counts from the previous
    step's SEND, not from the campaign start."""
    after_days: int = Field(ge=1, le=30)
    subject: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=20000)


class CampaignCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    account_id: int
    """EXTRA sending accounts beyond the primary (E5 multi-account). The
    scheduler spreads sends across all of them — pacing and the daily cap
    apply per account. Primary + extras must total 1..5."""
    account_ids: list[int] = Field(default_factory=list, max_length=4)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)
    emails: list[str] = Field(default_factory=list)
    audience_source: str = Field(default="leads", pattern="^(leads|own_list)$")
    sender_profile: str = Field(default="", max_length=4000)
    audience_count: int | None = Field(default=None, ge=1)
    audience_folder: str = Field(default="*", max_length=200)
    audience_recommendation: str = Field(default="contact_now", max_length=40)
    """ISO-8601 datetime (with offset) — the 9:00 AM start, computed in the
    user's timezone by the client."""
    start_at: str = Field(min_length=10, max_length=40)
    daily_limit: int = Field(default=30, ge=1, le=200)
    delay_min_s: int = Field(default=180, ge=20, le=3600)
    delay_max_s: int = Field(default=420, ge=20, le=7200)
    followups: list[FollowupIn] = Field(default_factory=list, max_length=3)
    """Optional follow-up emails (max 3). Each fires after_days days past
    the previous send, and is cancelled the moment the lead replies."""
    """Prepend an AI-written opening line to each first email, built from
    the lead's VERIFIED dossier evidence only (E5)."""
    ai_personalize: bool = False
    ai_compose: bool = False
    ai_signature: str = Field(default="", max_length=2000)


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
    """Which account sent this row (0 = pending / pre-E5)."""
    account_id: int = 0
    """First open time from the tracking pixel ('' = never opened)."""
    opened_at: str = ""
    """Total opens recorded (image loads — a signal, not a proof)."""
    opened_count: int = 0
    """When this lead's reply arrived ('' = no reply yet)."""
    replied_at: str = ""


class FollowupOut(BaseModel):
    step: int
    after_days: int
    subject: str
    body: str


class CampaignOut(BaseModel):
    id: int
    account_id: int
    """All sending accounts of this campaign, primary first (E5)."""
    account_ids: list[int] = []
    name: str
    subject: str
    body: str
    status: str
    paused_reason: str
    resume_at: str
    early_resume_date: str = ""
    start_at: str
    daily_limit: int
    delay_min_s: int
    delay_max_s: int
    ai_personalize: bool = False
    ai_compose: bool = False
    ai_signature: str = ""
    audience_source: str = "leads"
    sender_profile: str = ""
    created_at: str
    updated_at: str
    pending: int
    sent: int
    failed: int
    skipped: int
    replied: int
    bounced: int = 0
    account_email: str = ""
    """Resolved addresses for account_ids, primary first (list view only —
    the detail view fills it; single-account callers keep account_email)."""
    account_emails: list[str] = []


class CampaignTestSendIn(BaseModel):
    """A draft send to your OWN address — the spam check. The drafted
    subject/body are rendered with a sample lead so you see exactly what a
    lead would receive. Creates nothing: no campaign, no send row, no CRM
    event, and the recipient is never counted as already-emailed."""
    account_id: int
    to_email: EmailStr
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)


class CampaignTestSendOut(BaseModel):
    sent: bool
    to: str
    from_email: str
    subject: str


class SpamCheckIn(BaseModel):
    """A pitch to analyze — nothing is created or sent; the script stays
    in the user's editor."""
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)


class SpamFinding(BaseModel):
    """One risky thing the analyzer found, in plain words."""
    rule: str
    severity: str  # high | medium | low
    message: str
    count: int
    """Concrete rewrite advice (AI findings carry this)."""
    fix: str = ""
    """Which judgment category this belongs to (AI findings)."""
    category: str = ""


class SpamCheckOut(BaseModel):
    """The spam-risk report: a blended AI + rules score (or rules-only
    when the AI is unavailable), the findings behind it, a one-line
    plain-words summary, and the AI's per-category risk breakdown
    (content / urgency / tone / structure / personalization / links).
    Estimated by us, not Gmail's real filter — a guide, not a
    guarantee."""
    score: int
    level: str  # low | medium | high
    findings: list[SpamFinding] = []
    summary: str = ""
    method: str = "rules"  # ai | rules
    categories: dict[str, int] = {}


class SpamImproveIn(BaseModel):
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)


class SpamImproveOut(BaseModel):
    """The one-click fix: rewritten subject/body with spam triggers
    removed. ``method`` says how — 'ai' (best-effort rewrite) or 'rules'
    (deterministic fallback). The result lands in the user's editor for
    review; nothing is scheduled or sent by this call."""
    subject: str
    body: str
    method: str
    notes: list[str] = []


class CampaignUpdateIn(BaseModel):
    """Edit unsent campaign settings; omitted fields keep their old value."""
    name: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)
    account_id: int | None = None
    account_ids: list[int] | None = Field(default=None, max_length=4)
    emails: list[str] | None = Field(default=None)
    start_at: str | None = Field(default=None, min_length=10, max_length=40)
    daily_limit: int | None = Field(default=None, ge=1, le=200)
    delay_min_s: int | None = Field(default=None, ge=20, le=3600)
    delay_max_s: int | None = Field(default=None, ge=20, le=7200)
    followups: list[FollowupIn] | None = Field(default=None, max_length=3)
    ai_personalize: bool | None = None
    ai_compose: bool | None = None
    ai_signature: str | None = Field(default=None, max_length=2000)


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

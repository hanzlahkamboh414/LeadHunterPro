"""Leads API — Pydantic request/response models (the frontend contract)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class JobCreate(BaseModel):
    """Body for ``POST /api/v1/leads/jobs`` — a query run request."""

    trade: str = Field(..., description="WHAT to search, e.g. 'general contractor'")
    location: str = Field(..., description="WHERE, e.g. 'Texas' (any region)")
    target_emails: int = Field(20, ge=1, description="HOW MANY emails/leads to aim for")
    discover_only: bool = Field(False, description="Stop after discovery, skip AI research")
    search_name: str = Field("", description="OPTIONAL label for this run, stored as a tag on every lead it produces")
    folder: str = Field("", description="OPTIONAL folder to auto-file every lead this run produces into")


class JobEventOut(BaseModel):
    """One live progress event (per discovery pass / per researched lead)."""

    phase: str
    step: int
    total: int
    message: str
    email: str = ""
    data: dict = Field(default_factory=dict)
    ts: str = ""


class JobOut(BaseModel):
    """A job's full state — what the frontend polls for live progress."""

    id: str
    query: dict = Field(default_factory=dict)
    state: str
    events: list[JobEventOut] = Field(default_factory=list)
    results: list[dict] = Field(default_factory=list)
    pass_log: list[dict] = Field(default_factory=list)
    error: str = ""
    created_at: str
    updated_at: str
    elapsed_s: float = 0.0
    # Run outcome (H1): honest delivery vs target. Defaults keep pre-H1 payloads
    # valid on the client.
    working_leads: int = 0
    leads_found: int = 0
    shortfall: int = 0
    shortfall_reason: str = ""


class JobSummary(BaseModel):
    """A job's lightweight row (list view)."""

    id: str
    query: dict = Field(default_factory=dict)
    state: str
    error: str = ""
    created_at: str
    updated_at: str
    elapsed_s: float = 0.0
    working_leads: int = 0
    leads_found: int = 0
    shortfall: int = 0
    shortfall_reason: str = ""


class LeadSummary(BaseModel):
    """One qualified-lead row (list view)."""

    email: str
    domain: str
    company: str = ""
    person: str = ""
    role: str = ""
    bound: bool = False
    linkedin: str = ""
    phone: str = ""
    score: float = 0.0
    recommendation: str = "skip"
    intent: str = ""
    reason: str = ""
    timing: str = ""
    source: str = ""
    #: Extraction date (``YYYY-MM-DD``) — the day this lead was first researched
    #: and persisted ("kis tareekh ko nikala"), for date filtering/grouping.
    created_at: str = ""
    #: User organization metadata (Phase B): one primary folder + free-form
    #: multi-tags. Stored in their own columns, independent of the research data.
    folder: str = ""
    tags: list[str] = Field(default_factory=list)


class FolderItem(BaseModel):
    """One catalog folder — a persisted, clickable group (empty allowed)."""

    name: str
    created_at: str = ""
    count: int = 0


class FolderCreate(BaseModel):
    """Body for ``POST /api/v1/leads/folders`` — make a new (possibly empty) folder."""

    name: str = Field(..., min_length=1, description="folder name, e.g. 'Monday data'")


class FolderOut(BaseModel):
    """Create response — the folder row + whether it was newly created."""

    name: str
    created_at: str = ""
    count: int = 0
    created: bool = False


class FoldersOut(BaseModel):
    """The organization mailbox overview (GET /api/v1/leads/folders).

    ``folders`` are the persisted (possibly empty) groups; ``unfiled`` is what
    the DEFAULT Companies view shows (leads still in the inbox); ``total``
    counts every dossier (folders included), so the "All" chip and Dashboard
    totals stay honest.
    """

    folders: list[FolderItem] = Field(default_factory=list)
    unfiled: int = 0
    total: int = 0


class OrganizeIn(BaseModel):
    """Body for ``PUT /api/v1/leads/{email}/organize``."""

    folder: str = Field("", description="primary folder (exclusive)")
    tags: list[str] = Field(default_factory=list, description="free-form multi-tags")


class OrganizeRenameIn(BaseModel):
    """Body for ``POST /api/v1/leads/organize/rename``."""

    kind: str = Field(..., description="'folder' or 'tag'")
    from_: str = Field("", description="current folder/tag value to rename")
    to: str = Field("", description="new value")


class OrganizeClearIn(BaseModel):
    """Body for ``POST /api/v1/leads/organize/clear``."""

    kind: str = Field(..., description="'folder' or 'tag'")
    value: str = Field("", description="folder/tag value to remove from every lead")


class LeadDetail(BaseModel):
    """Full researched dossier for one lead (detail view)."""

    email: str
    domain: str
    refined_domain: str = ""
    refined_company: str = ""
    company: dict = Field(default_factory=dict)
    person: dict = Field(default_factory=dict)
    intent: dict = Field(default_factory=dict)
    timing: dict = Field(default_factory=dict)
    fit: str = ""
    potential_score: float = 0.0
    recommendation: str = "skip"
    sources_checked: list = Field(default_factory=list)
    source_errors: dict = Field(default_factory=dict)
    #: Extraction date (``YYYY-MM-DD``) — when this lead was first researched.
    created_at: str = ""
    folder: str = ""
    tags: list[str] = Field(default_factory=list)

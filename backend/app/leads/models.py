"""Leads API — job data models (V1, additive).

The Leads API lets the frontend drive the whole lead pipeline (discovery ->
AI research) as a background *job*, then read live progress and the finished
qualified leads.

A :class:`Job` wraps one query run (WHAT/WHERE/HOW_MANY) and records:
  - the query itself
  - ``state``      queued -> running -> completed / failed / cancelled
  - ``events``     a time-ordered progress log (one entry per discovery pass
                   and per researched lead) — this is the live telemetry the
                   frontend renders
  - ``results``    the per-lead research outcomes as they complete
  - ``pass_log``   the discovery pass log (CLAUDE.md §6 honest logging)

Everything is JSON-safe so it persists to SQLite.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class JobState(str, enum.Enum):
    """Lifecycle of a lead-research job."""

    queued = "queued"
    running = "running"
    paused = "paused"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


@dataclass
class JobEvent:
    """One progress entry. ``phase`` is ``discovery`` or ``research``.

    ``step``/``total`` let the UI show ``step of total``; ``data`` carries the
    structured payload for that step (e.g. the lead's research outcome).
    """

    phase: str
    step: int
    total: int
    message: str
    email: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "step": self.step,
            "total": self.total,
            "message": self.message,
            "email": self.email,
            "data": self.data,
            "ts": self.ts,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "JobEvent":
        return JobEvent(
            phase=d.get("phase", ""),
            step=d.get("step", 0),
            total=d.get("total", 0),
            message=d.get("message", ""),
            email=d.get("email", ""),
            data=d.get("data", {}) or {},
            ts=d.get("ts", ""),
        )


@dataclass
class Job:
    """A single lead-research background job."""

    id: str
    query: dict[str, Any]  # {trade, location, target_emails, discover_only}
    state: JobState = JobState.queued
    events: list[JobEvent] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    pass_log: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    created_at: str = ""
    updated_at: str = ""
    elapsed_s: float = 0.0
    # Run outcome (H1): how many researched leads are working vs the target,
    # and the honest reason when the run under-delivered. Computed by run_full
    # and persisted here so the API/UI can surface real delivery instead of a
    # bare "Completed" (CLAUDE.md §6 honest outcome telemetry).
    working_leads: int = 0
    leads_found: int = 0
    shortfall: int = 0
    shortfall_reason: str = ""
    user_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "query": self.query,
            "state": self.state.value,
            "events": [e.to_dict() for e in self.events],
            "results": self.results,
            "pass_log": self.pass_log,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "elapsed_s": round(self.elapsed_s, 1),
            "working_leads": self.working_leads,
            "leads_found": self.leads_found,
            "shortfall": self.shortfall,
            "shortfall_reason": self.shortfall_reason,
            "user_id": self.user_id,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Job":
        return Job(
            id=d.get("id", ""),
            query=d.get("query", {}) or {},
            state=JobState(d.get("state", "queued")),
            events=[JobEvent.from_dict(e) for e in d.get("events", [])],
            results=d.get("results", []) or [],
            pass_log=d.get("pass_log", []) or [],
            error=d.get("error", ""),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            elapsed_s=d.get("elapsed_s", 0.0),
            working_leads=d.get("working_leads", 0),
            leads_found=d.get("leads_found", 0),
            shortfall=d.get("shortfall", 0),
            shortfall_reason=d.get("shortfall_reason", ""),
            user_id=d.get("user_id", ""),
        )

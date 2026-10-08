"""Upload user-supplied email lists for autonomous lead research."""

from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.leads.import_files import ImportFileError, MAX_UPLOAD_BYTES, extract_emails
from app.leads.import_research_store import ImportResearchStore

router = APIRouter(prefix="/leads/imports", tags=["Lead imports"])


@lru_cache(maxsize=1)
def _store() -> ImportResearchStore:
    return ImportResearchStore()


@router.post("", status_code=201)
def upload_leads(file: UploadFile = File(...), name: str = Form(""),
                 user: User = Depends(get_current_user)) -> dict:
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    try:
        emails, rejected = extract_emails(data, file.filename or "upload.txt")
    except ImportFileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    job = _store().create_job(user.id, file.filename or "upload.txt",
                              emails, rejected, name)
    return {"job": job, "quota": quota(user)}


@router.get("")
def list_imports(user: User = Depends(get_current_user)) -> list[dict]:
    return _store().list_jobs(user.id)


@router.get("/quota")
def quota(user: User = Depends(get_current_user)) -> dict:
    store = _store()
    from datetime import datetime, timezone

    used = store.daily_usage(user.id)
    limit = store.daily_limit(user.id, is_owner=user.is_primary_owner)
    return {"day_utc": datetime.now(timezone.utc).date().isoformat(),
            "limit": limit, "used": used,
            "remaining": None if limit is None else max(0, limit - used)}


@router.get("/{job_id}")
def get_import(job_id: str, user: User = Depends(get_current_user)) -> dict:
    job = _store().get_job(job_id, user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    return job


@router.get("/{job_id}/activity")
def import_activity(job_id: str, user: User = Depends(get_current_user)) -> list[dict]:
    rows = _store().recent_activity(job_id, user.id)
    if rows is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    return rows


class RenameImportIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


@router.patch("/{job_id}")
def rename_import(job_id: str, body: RenameImportIn,
                  user: User = Depends(get_current_user)) -> dict:
    store = _store()
    if not store.rename_job(job_id, user.id, body.name):
        raise HTTPException(status_code=404, detail="Upload not found")
    return store.get_job(job_id, user.id)


@router.post("/{job_id}/cancel")
def cancel_import(job_id: str, user: User = Depends(get_current_user)) -> dict:
    store = _store()
    if not store.cancel_job(job_id, user.id):
        raise HTTPException(status_code=409, detail="Upload cannot be cancelled")
    return store.get_job(job_id, user.id)


@router.post("/{job_id}/pause")
def pause_import(job_id: str, user: User = Depends(get_current_user)) -> dict:
    store = _store()
    if not store.pause_job(job_id, user.id):
        raise HTTPException(status_code=409, detail="Upload cannot be paused")
    return store.get_job(job_id, user.id)


@router.post("/{job_id}/resume")
def resume_import(job_id: str, user: User = Depends(get_current_user)) -> dict:
    store = _store()
    if not store.resume_job(job_id, user.id):
        raise HTTPException(status_code=409, detail="Upload cannot be resumed")
    return store.get_job(job_id, user.id)


@router.delete("/{job_id}", status_code=204)
def delete_import(job_id: str, user: User = Depends(get_current_user)) -> None:
    if not _store().delete_collection(job_id, user.id):
        raise HTTPException(status_code=409, detail="Finish or cancel the upload first")

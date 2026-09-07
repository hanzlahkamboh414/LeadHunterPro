"""Admin-only operational dashboard API."""

from fastapi import APIRouter, Depends

from app.admin_read import AdminReadRepository
from app.api.v1.leads import require_api_key
from app.schemas.admin import AdminDashboardOut

router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    dependencies=[Depends(require_api_key)],
)

_reader = AdminReadRepository()


@router.get("/dashboard", response_model=AdminDashboardOut)
def dashboard() -> AdminDashboardOut:
    """Return operational metrics from the original lead database."""
    return _reader.dashboard()

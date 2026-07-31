from fastapi import APIRouter

from app.services.leadership_service import LeadershipService

router = APIRouter(
    prefix="/leadership",
    tags=["Leadership"],
)

service = LeadershipService()


@router.get("/")
def discover(website: str):

    return service.discover(website)
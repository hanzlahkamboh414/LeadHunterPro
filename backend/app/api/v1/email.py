from fastapi import APIRouter

from app.services.email_service import EmailService

router = APIRouter(
    prefix="/email",
    tags=["Email Discovery"],
)

service = EmailService()


@router.get("/")
def discover(
    website: str,
):

    return service.discover(
        website,
    )
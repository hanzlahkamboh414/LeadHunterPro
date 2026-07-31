from fastapi import APIRouter

from app.services.crawler_service import CrawlerService

router = APIRouter(
    prefix="/crawler",
    tags=["Crawler"],
)

service = CrawlerService()


@router.get("/")
def crawl(website: str):
    return service.crawl(website)
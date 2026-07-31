from fastapi import APIRouter
from sqlalchemy import text

from app.database.database import engine

router = APIRouter(tags=["Database"])


@router.get("/database")
def database_status():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        return {
            "database": "connected",
            "status": "healthy"
        }

    except Exception as e:
        return {
            "database": "disconnected",
            "error": str(e)
        }
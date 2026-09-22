from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.schemas.entities import HealthOut
from app.services.cache import cache_service

router = APIRouter()


@router.get("/healthz", response_model=HealthOut)
def healthz(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"
    redis_status = "ok" if cache_service.available else "unavailable"
    status = "ok" if db_status == "ok" else "degraded"
    return HealthOut(status=status, database=db_status, redis=redis_status, demo_mode=settings.DEMO_MODE)

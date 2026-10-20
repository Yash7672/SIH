from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.schemas.entities import HealthOut
from app.services.cache import cache_service
from app.services.chat.llm_groq import groq_router

router = APIRouter()


def _health_state(db: Session) -> HealthOut:
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"
    redis_status = "ok" if cache_service.available else "unavailable"
    status = "ok" if db_status == "ok" else "degraded"
    # Chat reports its own shape only: enabled/provider/ready. No key, no hint of
    # one - this endpoint is unauthenticated.
    return HealthOut(
        status=status,
        database=db_status,
        redis=redis_status,
        demo_mode=settings.DEMO_MODE,
        chat={
            "enabled": settings.chat_enabled,
            "provider": "groq",
            "llm_ready": bool(settings.chat_llm_ready and groq_router.ready),
        },
    )


@router.get("/healthz", response_model=HealthOut)
def healthz(db: Session = Depends(get_db)):
    return _health_state(db)


@router.get("/health", response_model=HealthOut)
def health(db: Session = Depends(get_db)):
    return _health_state(db)

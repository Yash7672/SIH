from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.session import get_db
from app.services.bus.base import Event
from app.services.ingest_service import IngestService

logger = get_logger(__name__)
router = APIRouter()


async def _check_privacy(request: Request):
    # reject multipart/images
    content_type = request.headers.get("content-type", "")
    if "multipart" in content_type.lower():
        raise HTTPException(status_code=400, detail="Multipart not allowed; metadata only")
    return True


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
async def ingest_events(
    payload: List[Event],
    request: Request,
    _priv: bool = Depends(_check_privacy),
    db: Session = Depends(get_db),
):
    if not payload:
        raise HTTPException(status_code=400, detail="Empty batch")
    if len(payload) > 100:
        raise HTTPException(status_code=400, detail="Batch too large (max 100)")
    # camera key header optional; for now just accept
    svc = IngestService(db)
    try:
        for ev in payload:
            svc.upsert_traffic_cell(ev, synthetic=False)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="Ingest failed")
    return {"accepted": len(payload)}

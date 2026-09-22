from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_cop
from app.db.session import get_db
from app.models import Hotlist, HotlistStatus, Sighting, User

router = APIRouter()


@router.get("")
def recent_alerts(_: User = Depends(require_cop), db: Session = Depends(get_db)):
    """Recent hotlist detections for the Live Alerts page."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    stmt = (
        select(Sighting, Hotlist)
        .join(Hotlist, Sighting.hotlist_id == Hotlist.id)
        .where(Sighting.created_at >= cutoff, Hotlist.status.in_([HotlistStatus.ACTIVE, HotlistStatus.FIR_CONFIRMED]))
        .order_by(Sighting.detected_at.desc())
        .limit(50)
    )
    rows = db.execute(stmt).all()
    return [
        {
            "sighting_id": str(s.id),
            "plate": h.plate,
            "latitude": s.latitude,
            "longitude": s.longitude,
            "timestamp": s.detected_at.isoformat(),
            "confidence": s.confidence,
            "hotlist_id": str(h.id),
        }
        for s, h in rows
    ]

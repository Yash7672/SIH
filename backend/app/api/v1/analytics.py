from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import require_cop
from app.db.session import get_db
from app.models import Complaint, ComplaintStatus, Device, Hotlist, HotlistStatus, Sighting

router = APIRouter()


@router.get("/overview")
def overview(_: User = Depends(require_cop), db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    active_hotlist = db.execute(
        select(func.count()).select_from(Hotlist).where(
            Hotlist.status.in_([HotlistStatus.ACTIVE, HotlistStatus.FIR_CONFIRMED])
        )
    ).scalar_one()

    pending_complaints = db.execute(
        select(func.count()).select_from(Complaint).where(
            Complaint.status.in_([ComplaintStatus.PENDING, ComplaintStatus.UNDER_REVIEW])
        )
    ).scalar_one()

    detections_today = db.execute(
        select(func.count()).select_from(Sighting).where(Sighting.detected_at >= day_start)
    ).scalar_one()

    recovered = db.execute(
        select(func.count()).select_from(Hotlist).where(Hotlist.status == HotlistStatus.RECOVERED)
    ).scalar_one()

    active_devices = db.execute(
        select(func.count()).select_from(Device).where(Device.revoked.is_(False))
    ).scalar_one()

    return {
        "active_hotlist": active_hotlist,
        "pending_complaints": pending_complaints,
        "detections_today": detections_today,
        "hotlist_matches_today": detections_today,
        "recovered_vehicles": recovered,
        "active_devices": active_devices,
    }


@router.get("/detections")
def detections(_: User = Depends(require_cop), db: Session = Depends(get_db)):
    rows = db.execute(
        select(func.date_trunc("hour", Sighting.detected_at), func.count())
        .group_by(func.date_trunc("hour", Sighting.detected_at))
        .order_by(func.date_trunc("hour", Sighting.detected_at))
    ).all()
    return [{"hour": r[0].isoformat() if r[0] else None, "count": r[1]} for r in rows]


@router.get("/hotlist-matches")
def hotlist_matches(_: User = Depends(require_cop), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Hotlist.plate, func.count(Sighting.id))
        .join(Sighting, Sighting.hotlist_id == Hotlist.id, isouter=True)
        .group_by(Hotlist.plate)
        .order_by(func.count(Sighting.id).desc())
    ).all()
    return [{"plate": r[0], "sightings": r[1]} for r in rows]


@router.get("/locations")
def locations(_: User = Depends(require_cop), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Sighting.latitude, Sighting.longitude, Sighting.detected_at)
        .order_by(Sighting.detected_at.desc())
        .limit(500)
    ).all()
    return [{"lat": r[0], "lng": r[1], "time": r[2].isoformat()} for r in rows]

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import require_cop
from app.db.session import get_db
from app.models import Camera, Complaint, ComplaintStatus, Device, Hotlist, HotlistStatus, Sighting, TrafficCell, User

router = APIRouter()


@router.get("/overview")
def overview(_: User = Depends(require_cop), db: Session = Depends(get_db)):
    """Dashboard KPIs.

    All counters are computed as scalar subqueries in a single round trip
    instead of five separate COUNT queries.
    """
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def count_of(model, *conditions):
        return select(func.count()).select_from(model).where(*conditions).scalar_subquery()

    detections_today = count_of(Sighting, Sighting.detected_at >= day_start)

    row = db.execute(
        select(
            count_of(Hotlist, Hotlist.status.in_([HotlistStatus.ACTIVE, HotlistStatus.FIR_CONFIRMED])).label(
                "active_hotlist"
            ),
            count_of(
                Complaint,
                Complaint.status.in_([ComplaintStatus.PENDING, ComplaintStatus.UNDER_REVIEW]),
            ).label("pending_complaints"),
            detections_today.label("detections_today"),
            detections_today.label("hotlist_matches_today"),
            count_of(Hotlist, Hotlist.status == HotlistStatus.RECOVERED).label("recovered_vehicles"),
            count_of(Device, Device.revoked.is_(False)).label("active_devices"),
        )
    ).one()

    return {
        "active_hotlist": row.active_hotlist,
        "pending_complaints": row.pending_complaints,
        "detections_today": row.detections_today,
        "hotlist_matches_today": row.hotlist_matches_today,
        "recovered_vehicles": row.recovered_vehicles,
        "active_devices": row.active_devices,
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


@router.get("/traffic")
def traffic(
    hours: int = Query(default=24, ge=1, le=168),
    _: User = Depends(require_cop),
    db: Session = Depends(get_db),
):
    """Anonymous traffic summary for the Operations dashboard."""
    start = datetime.now(timezone.utc) - timedelta(hours=hours)

    hourly_rows = db.execute(
        select(
            func.date_trunc("hour", TrafficCell.hour_bucket),
            func.sum(TrafficCell.count),
        )
        .where(TrafficCell.hour_bucket >= start)
        .group_by(func.date_trunc("hour", TrafficCell.hour_bucket))
        .order_by(func.date_trunc("hour", TrafficCell.hour_bucket))
    ).all()

    by_hour = [
        {"hour": r[0].isoformat() if r[0] else None, "count": int(r[1] or 0)}
        for r in hourly_rows
    ]

    camera_rows = db.execute(
        select(Camera.name, func.sum(TrafficCell.count))
        .join(TrafficCell, TrafficCell.camera_id == Camera.id)
        .where(TrafficCell.hour_bucket >= start)
        .group_by(Camera.id, Camera.name)
        .order_by(func.sum(TrafficCell.count).desc())
    ).all()

    class_rows = db.execute(
        select(TrafficCell.vehicle_class, func.sum(TrafficCell.count))
        .where(TrafficCell.hour_bucket >= start)
        .group_by(TrafficCell.vehicle_class)
        .order_by(func.sum(TrafficCell.count).desc())
    ).all()

    busiest_hour = max(by_hour, key=lambda item: item["count"], default={"hour": None, "count": 0})
    total_volume = sum(item["count"] for item in by_hour)

    return {
        "hours": hours,
        "total_volume": total_volume,
        "busiest_hour": busiest_hour,
        "by_hour": by_hour,
        "by_camera": [
            {"camera": camera_name, "count": int(count or 0)}
            for camera_name, count in camera_rows
        ],
        "class_mix": [
            {"vehicle_class": vehicle_class or "unknown", "count": int(count or 0)}
            for vehicle_class, count in class_rows
        ],
    }

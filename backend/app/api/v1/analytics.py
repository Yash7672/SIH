from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import require_cop
from app.db.session import get_db
from app.models import Complaint, ComplaintStatus, Device, Hotlist, HotlistStatus, Sighting, TrafficCell, User
from app.services.traffic_service import MIN_FRAMES_PER_CELL

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
    """Anonymous traffic summary for the Operations dashboard.

    Reads the same `traffic_cells` grid the Maps heatmap is built from. The grid
    has no camera dimension and no vehicle identity by design - a density figure
    must not be traceable back to one car or one camera - so the summary is
    per hour and per class only.
    """
    start = datetime.now(timezone.utc) - timedelta(hours=hours)

    volume = (
        TrafficCell.two_wheeler
        + TrafficCell.car
        + TrafficCell.bus
        + TrafficCell.truck
    )

    hourly_rows = db.execute(
        select(
            func.date_trunc("hour", TrafficCell.hour_bucket),
            func.sum(volume),
        )
        .where(TrafficCell.hour_bucket >= start)
        .group_by(func.date_trunc("hour", TrafficCell.hour_bucket))
        .order_by(func.date_trunc("hour", TrafficCell.hour_bucket))
    ).all()

    by_hour = [
        {"hour": r[0].isoformat() if r[0] else None, "count": int(r[1] or 0)}
        for r in hourly_rows
    ]

    # The busiest cells, not the busiest cameras: the grid is the only place a
    # location is kept, and naming the cell is as precise as the heatmap already is.
    cell_rows = db.execute(
        select(
            TrafficCell.cell_lat,
            TrafficCell.cell_lng,
            func.sum(volume),
            func.sum(TrafficCell.frames),
        )
        .where(TrafficCell.hour_bucket >= start)
        .group_by(TrafficCell.cell_lat, TrafficCell.cell_lng)
        .having(func.sum(TrafficCell.frames) >= MIN_FRAMES_PER_CELL)
        .order_by(func.sum(volume).desc())
        .limit(5)
    ).all()

    busiest_hour = max(by_hour, key=lambda item: item["count"], default={"hour": None, "count": 0})
    total_volume = sum(item["count"] for item in by_hour)

    return {
        "hours": hours,
        "total_volume": total_volume,
        "busiest_hour": busiest_hour,
        "by_hour": by_hour,
        "busiest_cells": [
            {
                "lat": float(lat),
                "lng": float(lng),
                "count": int(count or 0),
                "frames": int(frames or 0),
            }
            for lat, lng, count, frames in cell_rows
        ],
        "class_mix": [
            {"vehicle_class": name, "count": int(total or 0)}
            for name, total in (
                ("two_wheeler", _class_total(db, TrafficCell.two_wheeler, start)),
                ("car", _class_total(db, TrafficCell.car, start)),
                ("bus", _class_total(db, TrafficCell.bus, start)),
                ("truck", _class_total(db, TrafficCell.truck, start)),
            )
        ],
    }


def _class_total(db: Session, column, start: datetime) -> int:
    return int(
        db.execute(select(func.coalesce(func.sum(column), 0)).where(TrafficCell.hour_bucket >= start)).scalar()
        or 0
    )

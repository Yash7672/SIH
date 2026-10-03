"""Read side of the maps feature.

Everything here is a query over rows the ingest pipeline already wrote. The
privacy rule holds: a heatmap cell is an anonymous count, an OD row is an
(origin, destination, hour, count, travel) aggregate, and no plate is ever
selected by these queries.

Synthetic demo traffic is included by default so a fresh clone shows a populated
map, but every response marks it and callers can exclude it with
``include_synthetic=false``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.core.config import settings
from app.models import Camera, ODFlow, Sighting, TrafficCell, utcnow


def _window_start(hours: int):
    return utcnow() - timedelta(hours=max(1, hours))


def _camera_is_online(camera: Camera) -> bool:
    if camera.last_seen_at is None:
        return False
    age = (utcnow() - camera.last_seen_at).total_seconds()
    return age <= settings.CAMERA_OFFLINE_AFTER_SECONDS


def list_cameras(db: Session, include_offline: bool = True) -> list[dict]:
    """Every camera with its live status and today's sighting count."""
    rows = db.execute(select(Camera).order_by(Camera.name)).scalars().all()

    day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    counts = dict(
        db.execute(
            select(Sighting.camera_id, func.count())
            .where(Sighting.detected_at >= day_start, Sighting.camera_id.isnot(None))
            .group_by(Sighting.camera_id)
        ).all()
    )

    out = []
    for cam in rows:
        online = _camera_is_online(cam)
        if not include_offline and not online:
            continue
        out.append(
            {
                "id": str(cam.id),
                "name": cam.name,
                "lat": cam.lat,
                "lng": cam.lng,
                "heading_deg": cam.heading_deg,
                "source_type": cam.source_type,
                "status": cam.status,
                "online": online,
                "last_seen_at": cam.last_seen_at.isoformat() if cam.last_seen_at else None,
                "sightings_today": int(counts.get(cam.id, 0)),
            }
        )
    return out


def traffic_heatmap(
    db: Session,
    hours: int = 6,
    bbox: Optional[tuple[float, float, float, float]] = None,
    include_synthetic: bool = True,
) -> dict:
    """Anonymous vehicle counts per snapped grid cell over the last ``hours``.

    ``bbox`` is ``(min_lat, min_lng, max_lat, max_lng)``. Cells are returned
    hottest-first and capped at ``GEO_MAX_CELLS`` so a wide window cannot return
    an unbounded payload; the response says whether it was truncated.
    """
    start = _window_start(hours)
    stmt = (
        select(
            TrafficCell.cell_lat,
            TrafficCell.cell_lng,
            func.sum(TrafficCell.count),
        )
        .where(TrafficCell.hour_bucket >= start)
        .group_by(TrafficCell.cell_lat, TrafficCell.cell_lng)
    )
    if not include_synthetic:
        stmt = stmt.where(TrafficCell.synthetic.is_(False))
    if bbox is not None:
        min_lat, min_lng, max_lat, max_lng = bbox
        stmt = stmt.where(
            TrafficCell.cell_lat >= min_lat,
            TrafficCell.cell_lat <= max_lat,
            TrafficCell.cell_lng >= min_lng,
            TrafficCell.cell_lng <= max_lng,
        )

    rows = db.execute(stmt).all()
    cells = [
        {"lat": float(lat), "lng": float(lng), "count": int(count or 0)}
        for lat, lng, count in rows
        if count
    ]
    cells.sort(key=lambda c: c["count"], reverse=True)
    truncated = len(cells) > settings.GEO_MAX_CELLS
    cells = cells[: settings.GEO_MAX_CELLS]
    return {
        "hours": hours,
        "cells": cells,
        "max": max((c["count"] for c in cells), default=0),
        "total": sum(c["count"] for c in cells),
        "truncated": truncated,
    }


def od_flows(
    db: Session,
    hours: int = 6,
    limit: int = 200,
    include_synthetic: bool = True,
) -> dict:
    """Origin->destination corridors aggregated over the window.

    Grouped by camera pair so the map draws one arrow per corridor with the
    summed vehicle count and the mean travel time, never one arrow per hour.
    """
    start = _window_start(hours)
    origin = aliased(Camera)
    dest = aliased(Camera)

    stmt = (
        select(
            ODFlow.origin_camera_id,
            ODFlow.dest_camera_id,
            func.sum(ODFlow.count),
            func.sum(ODFlow.total_travel_seconds),
        )
        .where(ODFlow.hour_bucket >= start)
        .group_by(ODFlow.origin_camera_id, ODFlow.dest_camera_id)
        .order_by(func.sum(ODFlow.count).desc())
        .limit(max(1, limit))
    )
    if not include_synthetic:
        stmt = stmt.where(ODFlow.synthetic.is_(False))

    rows = db.execute(stmt).all()
    if not rows:
        return {"hours": hours, "flows": [], "max": 0}

    cam_ids = {r[0] for r in rows} | {r[1] for r in rows}
    cams = {
        c.id: c
        for c in db.execute(select(Camera).where(Camera.id.in_(cam_ids))).scalars().all()
    }

    flows = []
    for origin_id, dest_id, count, travel_total in rows:
        o, d = cams.get(origin_id), cams.get(dest_id)
        if o is None or d is None or not count:
            continue
        flows.append(
            {
                "origin": {"id": str(o.id), "name": o.name, "lat": o.lat, "lng": o.lng},
                "dest": {"id": str(d.id), "name": d.name, "lat": d.lat, "lng": d.lng},
                "count": int(count),
                "avg_travel_seconds": int((travel_total or 0) / count) if count else None,
            }
        )
    return {
        "hours": hours,
        "flows": flows,
        "max": max((f["count"] for f in flows), default=0),
    }


def summary(db: Session, hours: int = 6) -> dict:
    """Header numbers for the map page."""
    start = _window_start(hours)
    cameras = list_cameras(db, include_offline=True)

    vehicles = db.execute(
        select(func.coalesce(func.sum(TrafficCell.count), 0)).where(TrafficCell.hour_bucket >= start)
    ).scalar()
    corridors = db.execute(
        select(func.count()).select_from(
            select(ODFlow.origin_camera_id, ODFlow.dest_camera_id)
            .where(ODFlow.hour_bucket >= start)
            .group_by(ODFlow.origin_camera_id, ODFlow.dest_camera_id)
            .subquery()
        )
    ).scalar()

    return {
        "hours": hours,
        "cameras": len(cameras),
        "cameras_online": sum(1 for c in cameras if c["online"]),
        "vehicles_in_window": int(vehicles or 0),
        "od_corridors": int(corridors or 0),
    }

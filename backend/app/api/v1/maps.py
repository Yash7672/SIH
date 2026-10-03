"""Maps read API: cameras, traffic heatmap, OD flow and a header summary.

Cop/admin only, like every other analytics surface. All responses are derived
from anonymous aggregates - see ``app.services.maps_service``.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import require_cop
from app.core.config import settings
from app.db.session import get_db
from app.models import User
from app.services import maps_service

router = APIRouter()


@router.get("/cameras")
def cameras(
    include_offline: bool = Query(default=True, description="Include cameras with no recent heartbeat"),
    _: User = Depends(require_cop),
    db: Session = Depends(get_db),
):
    """Camera positions and live status for the map overlay."""
    return {
        "cameras": maps_service.list_cameras(db, include_offline=include_offline),
        "offline_after_seconds": settings.CAMERA_OFFLINE_AFTER_SECONDS,
    }


@router.get("/traffic")
def traffic(
    hours: int = Query(default=6, ge=1, le=168, description="Look-back window in hours"),
    min_lat: Optional[float] = Query(default=None, ge=-90, le=90),
    min_lng: Optional[float] = Query(default=None, ge=-180, le=180),
    max_lat: Optional[float] = Query(default=None, ge=-90, le=90),
    max_lng: Optional[float] = Query(default=None, ge=-180, le=180),
    include_synthetic: bool = Query(default=True, description="Include seeded demo traffic"),
    _: User = Depends(require_cop),
    db: Session = Depends(get_db),
):
    """Vehicle-density heatmap cells for the window."""
    bbox = None
    if None not in (min_lat, min_lng, max_lat, max_lng):
        bbox = (min_lat, min_lng, max_lat, max_lng)
    return maps_service.traffic_heatmap(
        db, hours=hours, bbox=bbox, include_synthetic=include_synthetic
    )


@router.get("/od-flows")
def od_flows(
    hours: int = Query(default=6, ge=1, le=168),
    limit: int = Query(default=200, ge=1, le=1000),
    include_synthetic: bool = Query(default=True),
    _: User = Depends(require_cop),
    db: Session = Depends(get_db),
):
    """Origin->destination corridors (one arrow per camera pair)."""
    return maps_service.od_flows(
        db, hours=hours, limit=limit, include_synthetic=include_synthetic
    )


@router.get("/summary")
def summary(
    hours: int = Query(default=6, ge=1, le=168),
    _: User = Depends(require_cop),
    db: Session = Depends(get_db),
):
    """Header counters for the map page."""
    return maps_service.summary(db, hours=hours)

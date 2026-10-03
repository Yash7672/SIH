from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_cop, require_volunteer
from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_db
from app.models import Device, Hotlist, Sighting, User
from app.schemas.entities import SightingCreate, SightingOut
from app.services.plate import normalize_plate
from app.services.sighting_service import SightingService
from app.ws.manager import alert_manager

logger = get_logger(__name__)
router = APIRouter()


@router.post("", response_model=SightingOut)
async def create_sighting(
    payload: SightingCreate,
    user: User = Depends(require_volunteer),
    db: Session = Depends(get_db),
):
    """Receive a detection event from the mobile scanner / simulator.

    Privacy contract: accepts ONLY plate, lat, lng, timestamp, confidence, device_id.
    Video/raw frames must never be sent here (enforced by middleware + schema).
    """
    if not cache_allowed(user):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    device = db.get(Device, payload.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not registered")
    if device.user_id != user.id and user.role.value != "ADMIN":
        raise HTTPException(status_code=403, detail="Device does not belong to you")
    if device.revoked:
        raise HTTPException(status_code=403, detail="Device has been revoked")

    norm = normalize_plate(payload.plate)
    if not norm.valid:
        raise HTTPException(status_code=422, detail="Invalid plate format")

    svc = SightingService(db)
    try:
        sighting = svc.record_detection(
            plate=norm.normalized,
            latitude=payload.latitude,
            longitude=payload.longitude,
            timestamp=payload.timestamp,
            confidence=payload.confidence,
            device=device,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Mobile detections feed the same anonymous aggregate the cameras write to,
    # so the traffic heatmap includes phones. Only the anonymous count is
    # persisted; the plate is not (unless it was hot-listed, handled above).
    record_mobile_aggregate(db, payload.latitude, payload.longitude, payload.timestamp)

    if sighting is None:
        # Not hotlisted or throttled — no sighting, no alert, nothing persisted.
        # 202: the detection was accepted and deliberately discarded.
        return Response(
            content='{"accepted": false, "detail": "accepted_no_match"}',
            status_code=status.HTTP_202_ACCEPTED,
            media_type="application/json",
        )

    # Build alert payload and push to police dashboards via WebSocket
    hotlist_entry = db.get(Hotlist, sighting.hotlist_id)
    alert_payload = {
        "sighting_id": str(sighting.id),
        "plate": norm.normalized,
        "latitude": sighting.latitude,
        "longitude": sighting.longitude,
        "timestamp": sighting.detected_at.isoformat(),
        "confidence": sighting.confidence,
        "hotlist_id": str(sighting.hotlist_id),
        "last_seen_at": hotlist_entry.last_seen_at.isoformat() if hotlist_entry and hotlist_entry.last_seen_at else None,
    }
    await alert_manager.broadcast("hotlist_detection", alert_payload)
    logger.info("Hotlist match broadcast: plate_len=%d", len(norm.normalized))
    return sighting


def cache_allowed(user: User) -> bool:
    from app.services.cache import cache_service

    return cache_service.throttle(f"detect:{user.id}", settings.RATE_LIMIT_DETECTIONS_PER_MINUTE)


def record_mobile_aggregate(db: Session, lat: float, lng: float, ts) -> None:
    """Add one anonymous traffic count for a phone-reported detection.

    Vehicle class is recorded as ``unknown`` because a phone scan reports a
    plate, not the vehicle body. ``camera_id`` stays NULL (mobile, not a fixed
    camera); the unique index is NULLS NOT DISTINCT so these collapse into one
    row per cell/hour instead of one row per detection.
    """
    from app.services.ingest_service import IngestService

    try:
        IngestService(db).bump_cells([
            (None, _cell(lat, lng)[0], _cell(lat, lng)[1], _bucket(ts), "unknown", 1, False)
        ])
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("Mobile traffic aggregate skipped: %s", exc)


def _cell(lat: float, lng: float) -> tuple[float, float]:
    from app.services.geo_support import snap_cell
    from app.services.ingest_service import STORAGE_RESOLUTION

    return snap_cell(float(lat), float(lng), STORAGE_RESOLUTION)


def _bucket(ts) -> datetime:
    from app.services.ingest_service import hour_bucket

    return hour_bucket(ts)


@router.get("", response_model=list[SightingOut])
def list_sightings(
    hotlist_id: UUID | None = None,
    _: User = Depends(require_cop),
    db: Session = Depends(get_db),
):
    stmt = select(Sighting).order_by(Sighting.detected_at.desc()).limit(500)
    if hotlist_id:
        stmt = stmt.where(Sighting.hotlist_id == hotlist_id).order_by(Sighting.detected_at.asc())
    return list(db.execute(stmt).scalars())


@router.get("/{sighting_id}", response_model=SightingOut)
def get_sighting(
    sighting_id: UUID,
    _: User = Depends(require_cop),
    db: Session = Depends(get_db),
):
    s = db.get(Sighting, sighting_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Not found")
    return s

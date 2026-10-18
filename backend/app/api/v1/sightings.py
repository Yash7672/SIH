from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.orm import Session

from app.api.deps import require_cop, require_volunteer
from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_db
from app.models import Device, Hotlist, Sighting, User
from app.schemas.entities import SightingCreate, SightingOut
from app.services.plate import normalize_plate
from app.services.hotlist_service import HotlistService
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

    if sighting is None:
        # Not hotlisted, or throttled by the 60 s cooldown. If the plate is
        # still on the list it was a throttled repeat, and a repeat still
        # alerts: a cop watching the portal must see the car while it is still
        # there, not only the first time. No new row is written.
        entry = HotlistService(db).get_active_by_plate(norm.normalized)
    else:
        entry = db.get(Hotlist, sighting.hotlist_id)

    above_floor = (payload.confidence or 0.0) >= settings.ALERT_MIN_CONFIDENCE
    # The HTTP path has no verdict pipeline behind it, so a read under the live
    # scanner's corroborated bar is flagged for the "verify before intercepting"
    # styling. With the floor below, only reads at or above ALERT_MIN_CONFIDENCE
    # broadcast at all, so in practice this is False - the flag stays computed
    # here so the rule lives in one place if the floor ever moves.
    needs_review = (payload.confidence or 0.0) < settings.ALERT_MIN_CONFIDENCE

    if sighting is None:
        if entry is not None and above_floor:
            # Throttled repeat: broadcast-only, so the dashboard sees it again
            # without a second row. `sighting_id: None` is the same shape the
            # live scanner's review alert uses for a detection with no row.
            await alert_manager.broadcast(
                "hotlist_detection",
                {
                    "sighting_id": None,
                    "plate": norm.normalized,
                    "latitude": payload.latitude,
                    "longitude": payload.longitude,
                    "timestamp": payload.timestamp.isoformat(),
                    "confidence": payload.confidence,
                    "hotlist_id": str(entry.id),
                    "camera": device.device_name or device.device_type or "unknown",
                    "device_id": str(device.id),
                    "last_seen_at": entry.last_seen_at.isoformat() if entry.last_seen_at else None,
                    "needs_review": needs_review,
                },
            )
        # 202: no row was written - the plate is unlisted, or the cooldown is
        # running. The detection was accepted and deliberately not persisted.
        return Response(
            content='{"accepted": false, "detail": "accepted_no_match"}',
            status_code=status.HTTP_202_ACCEPTED,
            media_type="application/json",
        )

    # Below the floor nothing pages the portal; the plate row - evidence for
    # the timeline and heat layer - is still written above.
    if not above_floor:
        return sighting

    # Build alert payload and push to police dashboards via WebSocket
    alert_payload = {
        "sighting_id": str(sighting.id),
        "plate": norm.normalized,
        "latitude": sighting.latitude,
        "longitude": sighting.longitude,
        "timestamp": sighting.detected_at.isoformat(),
        "confidence": sighting.confidence,
        "hotlist_id": str(sighting.hotlist_id),
        # Same field the live scanner sends, so both paths into the dashboard's
        # alert feed look identical to whatever consumes it.
        "camera": device.device_name or device.device_type or "unknown",
        "device_id": str(device.id),
        "last_seen_at": entry.last_seen_at.isoformat() if entry and entry.last_seen_at else None,
        "needs_review": needs_review,
    }
    await alert_manager.broadcast("hotlist_detection", alert_payload)
    logger.info("Hotlist match broadcast: plate_len=%d", len(norm.normalized))
    return sighting


def cache_allowed(user: User) -> bool:
    from app.services.cache import cache_service

    return cache_service.throttle(f"detect:{user.id}", settings.RATE_LIMIT_DETECTIONS_PER_MINUTE)


@router.get("", response_model=list[SightingOut])
def list_sightings(
    hotlist_id: UUID | None = None,
    _: User = Depends(require_cop),
    db: Session = Depends(get_db),
):
    stmt = select(Sighting).order_by(Sighting.detected_at.desc()).limit(500)
    if hotlist_id:
        stmt = stmt.where(Sighting.hotlist_id == hotlist_id).order_by(Sighting.detected_at.asc())
    # SightingOut.plate and .camera are derived from these two relationships, so
    # they are eager-loaded here: without this, rendering 500 rows costs 1000
    # extra queries.
    stmt = stmt.options(selectinload(Sighting.hotlist), selectinload(Sighting.device))
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

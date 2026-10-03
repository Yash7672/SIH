"""HTTP ingestion fallback for workers that cannot reach Redis.

``POST /api/v1/ingest/events`` accepts the same :class:`DetectionEvent` payload
as the bus, authenticated with the camera's own API key in ``X-Camera-Key``.
This exists so a camera worker on a locked-down edge box can report over plain
HTTPS when it has no route to the Redis port.

Privacy: metadata only. The request body is parsed as JSON, so a frame cannot
even be expressed here, and ``privacy_guard`` in ``app.main`` additionally
rejects any multipart or video/image content type before this handler runs.
"""

from __future__ import annotations

import hashlib
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.session import get_db
from app.models.models import Camera
from app.services.bus.base import DetectionEvent
from app.services.bus.factory import build_bus

logger = get_logger(__name__)
router = APIRouter()

MAX_BATCH = 100


def hash_api_key(raw: str) -> str:
    """SHA-256 of the presented key. Only the hash is stored, never the key."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def authenticate_camera(raw_key: Optional[str], camera_id: str, db: Session) -> Camera:
    """Resolve the camera for this key, or raise 401/403."""
    if not raw_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="X-Camera-Key required")
    from app.models.models import Device
    from sqlalchemy import select

    digest = hash_api_key(raw_key)
    device = db.execute(
        select(Device).where(
            Device.device_type == "CAMERA",
            Device.api_key_hash == digest,
            Device.revoked.is_(False),
        )
    ).scalars().first()
    if device is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unknown or revoked camera key")
    if not device.device_name:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Camera key has no camera bound")

    camera = db.execute(select(Camera).where(Camera.name == device.device_name)).scalars().first()
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not registered")
    # A worker may only report for the camera its key is bound to.
    if str(camera.id) != str(camera_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Camera key does not match camera_id in payload",
        )
    return camera


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
async def ingest_events(
    payload: List[DetectionEvent],
    request: Request,
    x_camera_key: Optional[str] = Header(default=None, alias="X-Camera-Key"),
    db: Session = Depends(get_db),
):
    """Publish a batch of metadata-only detection events onto the bus.

    Returns 202 with the batch size. The actual aggregation happens
    asynchronously in the consumer, exactly as for bus-native workers, so this
    path has identical semantics and identical privacy guarantees.
    """
    if not isinstance(payload, list) or not payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty batch")
    if len(payload) > MAX_BATCH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Batch too large (max {MAX_BATCH})",
        )

    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart" in content_type or content_type.startswith(("image/", "video/")):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Frames and video are never accepted here; metadata only",
        )

    # Authenticate the first event's camera and require the whole batch to match.
    camera = authenticate_camera(x_camera_key, str(payload[0].camera_id), db)
    for event in payload:
        if str(event.camera_id) != str(camera.id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="All events in a batch must come from the authenticated camera",
            )

    try:
        bus = build_bus()
        await bus.start()
        await bus.publish(payload)
        await bus.stop()
    except Exception as exc:
        logger.error("Ingest publish failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Event bus unavailable")

    return {
        "accepted": len(payload),
        "camera_id": str(camera.id),
        "bus": "ok",
        "note": "queued for aggregation; no frames are ever transmitted",
    }


@router.get("/health")
def ingest_health():
    """Which bus is configured and whether the API is reachable.

    Also reports the simulator stats when the synthetic demo traffic generator
    is running."""
    from app.services.ingest_consumer import bus_name, get_consumer
    from app.services.traffic_sim import get_simulator

    consumer = get_consumer()
    simulator = get_simulator()
    return {
        "bus": bus_name(),
        "consumer_running": bool(consumer and consumer._running),
        "stats": consumer.stats() if consumer else None,
        "simulator": simulator.stats() if simulator else None,
    }
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import Device, HotlistStatus, Sighting, utcnow
from app.services.cache import cache_service
from app.services.hotlist_service import HotlistService

logger = get_logger(__name__)


class SightingService:
    def __init__(self, db: Session):
        self.db = db
        self.hotlist = HotlistService(db)

    def record_detection(
        self,
        plate: str,
        latitude: float,
        longitude: float,
        timestamp: datetime,
        confidence: Optional[float],
        device: Device,
    ) -> Optional[Sighting]:
        """Check hotlist, record sighting if matched, update last_seen, return sighting or None.

        Privacy: this method only ever receives plate + location + time + confidence.
        No video, no raw frames, no unrelated plates are persisted.
        """
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("Invalid coordinates")

        entry = self.hotlist.get_active_by_plate(plate)
        if entry is None:
            # Non-hotlisted plate: do NOT persist anything.
            logger.info("Non-hotlist plate detected (not stored)")
            return None

        # Cooldown / duplicate suppression via Redis (fail-open if Redis down)
        cooldown_key = f"plate:{plate}:{device.id}"
        if not cache_service.throttle(cooldown_key, 60):
            logger.info("Detection throttled for plate (cooldown active)")
            return None

        sighting = Sighting(
            id=uuid.uuid4(),
            hotlist_id=entry.id,
            device_id=device.id,
            latitude=latitude,
            longitude=longitude,
            detected_at=timestamp,
            confidence=confidence,
            created_at=utcnow(),
        )
        entry.last_seen_at = timestamp
        entry.last_seen_lat = latitude
        entry.last_seen_lng = longitude
        device.last_seen_at = utcnow()

        self.db.add(sighting)
        self.db.commit()
        self.db.refresh(sighting)

        # Keep cache warm
        cache_service.add_active_plate(plate)
        return sighting

    def get_sightings_for_hotlist(self, hotlist_id: uuid.UUID) -> list[Sighting]:
        stmt = (
            select(Sighting)
            .where(Sighting.hotlist_id == hotlist_id)
            .order_by(Sighting.detected_at.asc())
        )
        return list(self.db.execute(stmt).scalars())

    def get_route_for_plate(self, plate: str) -> list[Sighting]:
        entry = self.hotlist.get_active_by_plate(plate)
        if entry is None:
            # Also look at recovered/closed for historical route
            from app.models import Hotlist

            entry = self.db.execute(
                select(Hotlist).where(Hotlist.plate == plate).order_by(Hotlist.added_at.desc()).limit(1)
            ).scalar_one_or_none()
        if entry is None:
            return []
        return self.get_sightings_for_hotlist(entry.id)

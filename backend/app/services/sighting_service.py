import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
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

        # Fast path: the Redis set of active hotlist plates answers the common
        # case (an arbitrary non-hotlisted plate) without touching PostgreSQL.
        # PostgreSQL stays the source of truth whenever the cache is missing.
        cached = cache_service.get_active_plates()
        if cached is not None and plate not in cached:
            logger.info("Non-hotlist plate detected (cache miss, not stored)")
            return None

        entry = self.hotlist.get_active_by_plate(plate)
        if entry is None:
            # Not hotlisted: do NOT persist anything. If the cache claimed it was
            # hotlisted, drop the stale entry so the next lookup is cheap.
            if cached is not None:
                cache_service.remove_active_plate(plate)
            logger.info("Non-hotlist plate detected (not stored)")
            return None
        if cached is None:
            # Cache was cold/unavailable — warm it now that we have an answer.
            cache_service.add_active_plate(plate)

        # Cooldown / duplicate suppression via Redis (fail-open if Redis down)
        cooldown_key = f"plate:{plate}:{device.id}"
        if not cache_service.cooldown(cooldown_key, settings.DETECTION_COOLDOWN_SECONDS):
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

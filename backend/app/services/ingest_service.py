"""Turns detection events into aggregates, hot-list matches and OD flow.

Privacy contract implemented here (never weakened, only extended)
------------------------------------------------------------------
* Frames never reach this module: :class:`DetectionEvent` has no image field.
* A plate that is **not** on the hot list is never stored. The only thing that
  reaches the database is an anonymous aggregate row:
  ``(cell, hour bucket, vehicle_class, camera_id) -> count``.
* OD flow needs to know that two sightings are the same vehicle, so the plate is
  reduced to a ``HMAC-SHA256(plate, daily rotating secret)`` pseudonym that lives
  in **Redis only**, with a 6 h TTL. The database stores only
  ``(origin, destination, hour, count, avg travel seconds)``.
* Hot-listed plates keep their full sighting + trajectory, as before.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import Camera, Device, Hotlist, ODFlow, Sighting, TrafficCell, utcnow
from app.services.cache import cache_service
from app.services.geo_support import snap_cell
from app.services.hotlist_service import HotlistService
from app.services.plate_fuzzy import MATCH_EXACT, MATCH_FUZZY, FuzzyMatch, match_plate
from app.services.sighting_service import SightingService

logger = get_logger(__name__)

STORAGE_RESOLUTION = "high"


@dataclass
class IngestResult:
    """Outcome of one batch, returned to the caller and logged by the consumer."""

    events: int = 0
    aggregates: int = 0
    exact_matches: int = 0
    fuzzy_matches: int = 0
    od_links: int = 0
    ignored_plates: int = 0
    alerts: List[dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "events": self.events,
            "aggregates": self.aggregates,
            "exact_matches": self.exact_matches,
            "fuzzy_matches": self.fuzzy_matches,
            "od_links": self.od_links,
            "ignored_plates": self.ignored_plates,
            "alerts": len(self.alerts),
            "errors": self.errors,
        }


def hour_bucket(ts: datetime) -> datetime:
    """UTC hour floor. All time is stored in UTC; the UI renders Asia/Kolkata."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def od_pseudonym(plate: str, when: Optional[datetime] = None) -> str:
    """HMAC-SHA256(plate, secret rotated daily).

    Deterministic for the day, so consecutive cameras can be linked, but the
    daily rotation means yesterday's Redis keys cannot be joined to today's.
    """
    day = (when or utcnow()).astimezone(timezone.utc).strftime("%Y%m%d")
    key = f"{settings.od_pseudonym_secret}:{day}".encode("utf-8")
    msg = plate.strip().upper().encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


class IngestService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.hotlist = HotlistService(db)
        self.sightings = SightingService(db)
        self._camera_cache: dict[str, Optional[Camera]] = {}
        self._device_cache: dict[str, Optional[Device]] = {}

    # -- lookups -----------------------------------------------------------
    def get_camera(self, camera_id: str) -> Optional[Camera]:
        if camera_id in self._camera_cache:
            return self._camera_cache[camera_id]
        cam: Optional[Camera] = None
        try:
            cam = self.db.get(Camera, uuid.UUID(str(camera_id)))
        except (ValueError, AttributeError):
            cam = None
        self._camera_cache[camera_id] = cam
        return cam

    def camera_device(self, camera: Optional[Camera], camera_id: str) -> Optional[Device]:
        """The CAMERA device row that backs this camera, for the sighting path."""
        key = camera.name if camera is not None else camera_id
        if key in self._device_cache:
            return self._device_cache[key]
        device = self.db.execute(
            select(Device).where(Device.device_type == "CAMERA", Device.device_name == key)
        ).scalars().first()
        if device is None and camera is not None:
            device = self.db.execute(
                select(Device).where(Device.device_type == "CAMERA", Device.device_name == camera.name)
            ).scalars().first()
        self._device_cache[key] = device
        return device

    def hotlist_set(self) -> set[str]:
        """The hot list, from the Redis hot set when warm, else PostgreSQL."""
        cached = cache_service.get_active_plates()
        if cached is not None:
            return cached
        from app.models.models import HotlistStatus

        rows = self.db.execute(
            select(Hotlist.plate).where(Hotlist.status == HotlistStatus.ACTIVE)
        ).scalars().all()
        plates = {p for p in rows if p}
        if plates:
            cache_service.set_active_plates(plates)
        return plates

    # -- aggregates --------------------------------------------------------
    def bump_cells(self, rows: Sequence[tuple]) -> None:
        """Add counts to ``traffic_cells`` in one statement.

        ``ON CONFLICT ... DO UPDATE SET count = count + excluded.count`` so the
        operation is idempotent under replay: the same event twice adds 2, and
        a redelivered batch after a crash is harmless.

        ``rows`` is ``(camera_id, cell_lat, cell_lng, hour_bucket, vehicle_class,
        count, synthetic)``. The unique index is declared NULLS NOT DISTINCT, so
        a mobile sighting (``camera_id IS NULL``) still collapses into one row
        instead of inserting a duplicate every time.
        """
        if not rows:
            return
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(TrafficCell).values(
            [
                {
                    "id": uuid.uuid4(),
                    "camera_id": camera_id,
                    "cell_lat": cell_lat,
                    "cell_lng": cell_lng,
                    "hour_bucket": bucket,
                    "vehicle_class": vclass,
                    "count": count,
                    "synthetic": synthetic,
                }
                for (camera_id, cell_lat, cell_lng, bucket, vclass, count, synthetic) in rows
            ]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                TrafficCell.cell_lat,
                TrafficCell.cell_lng,
                TrafficCell.hour_bucket,
                TrafficCell.vehicle_class,
                TrafficCell.camera_id,
                TrafficCell.synthetic,
            ],
            set_={"count": TrafficCell.count + stmt.excluded.count},
        )
        self.db.execute(stmt)

    def bump_od(self, rows: Sequence[tuple]) -> None:
        """Add origin->destination counts and travel time in one statement.

        ``rows`` is ``(origin_id, dest_id, hour_bucket, count, travel_seconds,
        synthetic)``.
        """
        if not rows:
            return
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(ODFlow).values(
            [
                {
                    "id": uuid.uuid4(),
                    "origin_camera_id": origin_id,
                    "dest_camera_id": dest_id,
                    "hour_bucket": bucket,
                    "count": count,
                    "total_travel_seconds": travel,
                    "synthetic": synthetic,
                }
                for (origin_id, dest_id, bucket, count, travel, synthetic) in rows
            ]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                ODFlow.origin_camera_id,
                ODFlow.dest_camera_id,
                ODFlow.hour_bucket,
                ODFlow.synthetic,
            ],
            set_={
                "count": ODFlow.count + stmt.excluded.count,
                "total_travel_seconds": ODFlow.total_travel_seconds + stmt.excluded.total_travel_seconds,
            },
        )
        self.db.execute(stmt)

    # -- hot list ----------------------------------------------------------
    def check_hotlist(self, read: str, hotlist: Optional[Iterable[str]] = None) -> FuzzyMatch:
        """Score a read against the hot-list set **only** (Redis/DB, never disk
        write). Returns the classification; the caller decides what to persist."""
        plates = set(hotlist) if hotlist is not None else self.hotlist_set()
        if not plates:
            return FuzzyMatch(kind="NONE", score=0.0, plate=None, reason="hot list empty")
        return match_plate(read, plates)

    def record_match(
        self,
        event,
        camera: Optional[Camera],
        match: FuzzyMatch,
        synthetic: bool = False,
    ) -> Optional[Sighting]:
        """Persist a sighting for an EXACT or FUZZY hot-list match.

        The stored ``plate`` is always the hot-list plate, never the raw OCR
        read: a fuzzy read is a hypothesis and the officer must see the plate
        that is actually wanted.
        """
        if match.kind not in (MATCH_EXACT, MATCH_FUZZY) or match.plate is None:
            return None
        device = self.camera_device(camera, event.camera_id)
        if device is None:
            return None

        lat = event.lat if event.lat is not None else (camera.lat if camera else None)
        lng = event.lng if event.lng is not None else (camera.lng if camera else None)
        if lat is None or lng is None:
            return None

        entry = self.hotlist.get_active_by_plate(match.plate)
        if entry is None:
            return None

        # Per-plate-per-camera cooldown. Reusing the existing cache primitive
        # keeps mobile and camera detections on the same throttle.
        cooldown_key = f"plate:{match.plate}:{device.id}"
        if not cache_service.cooldown(cooldown_key, settings.DETECTION_COOLDOWN_SECONDS):
            return None

        sighting = Sighting(
            id=uuid.uuid4(),
            hotlist_id=entry.id,
            device_id=device.id,
            latitude=float(lat),
            longitude=float(lng),
            detected_at=event.ts,
            confidence=event.plate_conf,
            created_at=utcnow(),
        )
        sighting.camera_id = camera.id if camera is not None else None
        sighting.source = "CAMERA"
        sighting.match_type = match.kind
        sighting.match_score = float(match.score)
        sighting.vehicle_class = event.vehicle_class or "unknown"
        sighting.track_id = event.track_id

        entry.last_seen_at = event.ts
        entry.last_seen_lat = float(lat)
        entry.last_seen_lng = float(lng)
        device.last_seen_at = utcnow()

        self.db.add(sighting)
        return sighting

    # -- OD flow -----------------------------------------------------------
    def link_od(self, plate: str, camera: Camera, seen_at: datetime, synthetic: bool = False) -> Optional[Tuple[Camera, int]]:
        """Link this sighting to the previous one for the same pseudonym.

        Runs for **every** plate, hot-listed or not: that is the point of the
        pseudonym. The plate is reduced to a daily-rotated HMAC before it leaves
        this call, and that pseudonym lives only in Redis with a 6 h TTL. The
        database never sees a plate here.

        Returns ``(previous_camera, travel_seconds)`` when a link was made.
        """
        if not settings.ENABLE_OD_FLOW or not plate or camera is None:
            return None

        token = od_pseudonym(plate, seen_at)
        previous = cache_service.get_od_state(token) or {}

        # Remember this sighting so the next camera in the journey can link it.
        cache_service.set_od_state(
            token,
            {"camera_id": str(camera.id), "ts": seen_at.isoformat()},
            settings.OD_PSEUDONYM_TTL_SECONDS,
        )

        prev_camera_id = previous.get("camera_id")
        prev_ts = previous.get("ts")
        if not prev_camera_id or not prev_ts or prev_camera_id == str(camera.id):
            return None
        try:
            prev_camera = self.db.get(Camera, uuid.UUID(prev_camera_id))
            travel = int((seen_at - datetime.fromisoformat(prev_ts)).total_seconds())
        except Exception:
            return None
        if prev_camera is None or travel <= 0:
            return None
        return prev_camera, travel

    def record_od_flow(self, origin: Camera, dest: Camera, travel_seconds: int, when: datetime,
                       synthetic: bool = False) -> bool:
        """Persist the anonymous (origin, dest, hour, count, travel) aggregate."""
        if not settings.ENABLE_OD_FLOW or origin is None or dest is None or origin.id == dest.id:
            return False
        self.bump_od([(origin.id, dest.id, hour_bucket(when), 1, max(0, travel_seconds), synthetic)])
        return True

    # -- camera heartbeat --------------------------------------------------
    def touch_camera(self, camera: Camera, status: str = "ONLINE") -> None:
        camera.last_seen_at = utcnow()
        camera.status = status

    # -- batch entry point -------------------------------------------------
    def process_batch(self, events: Sequence, synthetic_default: bool = False) -> IngestResult:
        """Process a batch inside **one** transaction.

        The caller commits and only then acks the bus, so a crash replays the
        batch instead of losing it.
        """
        result = IngestResult(events=len(events))
        if not events:
            return result

        hotlist = self.hotlist_set()
        cells: dict[tuple, int] = {}
        od_rows: dict[tuple, tuple[int, int]] = {}
        cameras_touched: dict[uuid.UUID, Camera] = {}

        for event in events:
            synthetic = synthetic_default or bool(getattr(event, "synthetic", False))
            camera = self.get_camera(str(event.camera_id))
            if camera is not None:
                cameras_touched[camera.id] = camera

            # (a) anonymous aggregate - for every event, hot-listed or not. No
            # plate is involved at any point in this branch.
            lat, lng = event.lat, event.lng
            if (lat is None or lng is None) and camera is not None:
                lat, lng = camera.lat, camera.lng
            if lat is not None and lng is not None:
                cell_lat, cell_lng = snap_cell(float(lat), float(lng), STORAGE_RESOLUTION)
                key = (
                    camera.id if camera is not None else None,
                    cell_lat, cell_lng,
                    hour_bucket(event.ts),
                    event.vehicle_class or "unknown",
                    synthetic,
                )
                cells[key] = cells.get(key, 0) + 1

            if not event.has_plate:
                continue

            # (b) hot-list check, in memory, against the hot set only
            match = self.check_hotlist(event.plate, hotlist)

            if match.kind == "NONE":
                # Not on the hot list: the plate is discarded here. It is used
                # for nothing below except the pseudonym in link_od.
                result.ignored_plates += 1
            else:
                # (c) a sighting, plus (e) the alert payload
                sighting = self.record_match(event, camera, match, synthetic)
                if sighting is not None:
                    if match.kind == MATCH_EXACT:
                        result.exact_matches += 1
                    else:
                        result.fuzzy_matches += 1
                    result.alerts.append(self.alert_payload(event, camera, match, sighting))

            # (d) OD link. The plate is reduced to a daily-rotated HMAC here and
            # never persisted, so this is privacy-safe for every vehicle.
            if camera is not None:
                link = self.link_od(event.plate, camera, event.ts, synthetic)
                if link is not None:
                    prev_camera, travel = link
                    if prev_camera.id != camera.id and travel > 0:
                        od_key = (prev_camera.id, camera.id, hour_bucket(event.ts), synthetic)
                        seen = od_rows.get(od_key, (0, 0))
                        od_rows[od_key] = (seen[0] + 1, seen[1] + travel)

        if cells:
            self.bump_cells([(k[0], k[1], k[2], k[3], k[4], n, k[5]) for k, n in cells.items()])
            result.aggregates = sum(cells.values())
        if od_rows:
            self.bump_od([(k[0], k[1], k[2], v[0], v[1], k[3]) for k, v in od_rows.items()])
            result.od_links = sum(v[0] for v in od_rows.values())

        # camera health: a worker that reported is online
        for cam in cameras_touched.values():
            self.touch_camera(cam)

        return result

    def alert_payload(self, event, camera: Optional[Camera], match: FuzzyMatch,
                      sighting: Sighting) -> dict:
        """The WebSocket alert. Carries the match class and score so the UI can
        say CONFIRMED or POSSIBLE MATCH, verify."""
        confidence_text = match.verified_label
        return {
            "type": "hotlist_detection",
            "hotlist_id": str(sighting.hotlist_id),
            "sighting_id": str(sighting.id),
            "plate": match.plate,
            "raw_read": match.read if match.kind == MATCH_FUZZY else None,
            "match_type": match.kind,
            "match_score": float(match.score),
            "confidence": confidence_text,
            "confirmed": match.kind == MATCH_EXACT,
            "requires_verification": match.kind == MATCH_FUZZY,
            "match_reason": match.reason,
            "latitude": sighting.latitude,
            "longitude": sighting.longitude,
            "detected_at": sighting.detected_at.isoformat(),
            "source": "CAMERA",
            "camera_id": str(camera.id) if camera is not None else None,
            "camera_name": camera.name if camera is not None else None,
            "vehicle_class": sighting.vehicle_class,
            "track_id": sighting.track_id,
        }

    def commit(self) -> None:
        self.db.commit()
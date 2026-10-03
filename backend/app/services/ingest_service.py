import asyncio
import hashlib
import hmac
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import (
    Camera,
    Device,
    Hotlist,
    HotlistStatus,
    ODFlow,
    Sighting,
    TrafficCell,
    utcnow,
)
from app.services.cache import cache_service
from app.services.hotlist_service import HotlistService
from app.services.sighting_service import SightingService
from app.services.bus.base import Event

logger = get_logger(__name__)

ENABLE_OD_FLOW = os.getenv("ENABLE_OD_FLOW", "true").lower() == "true"
HEAT_TAU_HOURS = float(os.getenv("HEAT_TAU_HOURS", "6"))
OD_TTL_HOURS = int(os.getenv("OD_TTL_HOURS", "6"))


def _hour_bucket(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    return ts.replace(minute=0, second=0, microsecond=0)


def _cell_from_latlng(lat: float, lng: float, res: str = "med") -> Tuple[float, float]:
    # simple grid snapping; PostGIS would use ST_SnapToGrid but fallback uses FLOOR by step
    step = 0.002 if res == "low" else 0.001 if res == "med" else 0.0005
    return round(math_floor_step(lat, step), 6), round(math_floor_step(lng, step), 6)


def math_floor_step(v: float, step: float) -> float:
    if step <= 0:
        return v
    return round((int(v / step)) * step, 6)


def _od_pseudonym(plate: str) -> Optional[str]:
    try:
        secret = os.getenv("OD_PSEUDONYM_SECRET", "dev-od-secret")
        daily = datetime.now(timezone.utc).strftime("%Y%m%d")
        key = f"{secret}:{daily}".encode("utf-8")
        msg = plate.strip().upper().encode("utf-8")
        sig = hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]
        return sig
    except Exception:
        return None


class IngestService:
    def __init__(self, db: Session):
        self.db = db
        self.hotlist = HotlistService(db)
        self.sighting_svc = SightingService(db)

    def _get_camera(self, camera_id: str) -> Optional[Camera]:
        try:
            return self.db.get(Camera, uuid.UUID(camera_id))
        except Exception:
            return None

    def upsert_traffic_cell(self, event: Event, synthetic: bool = False) -> None:
        cam = self._get_camera(event.camera_id) if event.camera_id else None
        lat = event.lat
        lng = event.lng
        if lat is None and cam:
            lat = cam.lat
        if lng is None and cam:
            lng = cam.lng
        if lat is None or lng is None:
            return
        c_lat, c_lng = _cell_from_latlng(lat, lng, "med")
        hb = _hour_bucket(event.ts)
        vcls = event.vehicle_class or "unknown"
        camera_uuid = uuid.UUID(event.camera_id) if event.camera_id else None
        existing = self.db.execute(
            select(TrafficCell).where(
                TrafficCell.cell_lat == c_lat,
                TrafficCell.cell_lng == c_lng,
                TrafficCell.hour_bucket == hb,
                TrafficCell.vehicle_class == vcls,
                TrafficCell.camera_id == camera_uuid,
                TrafficCell.synthetic == synthetic,
            )
        ).scalar_one_or_none()
        if existing:
            existing.count = (existing.count or 0) + 1
            self.db.add(existing)
            return
        tc = TrafficCell(
            id=uuid.uuid4(),
            camera_id=camera_uuid,
            cell_lat=c_lat,
            cell_lng=c_lng,
            hour_bucket=hb,
            vehicle_class=vcls,
            count=1,
            synthetic=synthetic,
        )
        self.db.add(tc)

    def process_hotlist_match(self, event: Event, camera: Optional[Camera]) -> Optional[Tuple[Sighting, float, str]]:
        plate = (event.plate or "").strip().upper()
        if not plate:
            return None
        # use existing sighting service logic? but sighting service expects device; camera is device via name? find device
        device = self.db.execute(
            select(Device).where(Device.device_type == "CAMERA", Device.device_name == (camera.name if camera else event.camera_id))
        ).scalar_one_or_none()
        if device is None and camera:
            device = self.db.execute(
                select(Device).where(Device.device_type == "CAMERA", Device.device_name == camera.name)
            ).scalar_one_or_none()
        if device is None:
            return None
        lat = event.lat or (camera.lat if camera else None)
        lng = event.lng or (camera.lng if camera else None)
        if lat is None or lng is None:
            return None
        sighting = self.sighting_svc.record_detection(
            plate=plate,
            latitude=lat,
            longitude=lng,
            timestamp=event.ts,
            confidence=event.plate_conf or event.det_conf,
            device=device,
        )
        if sighting is None:
            return None
        # enrich
        sighting.camera_id = uuid.UUID(event.camera_id) if event.camera_id else None
        sighting.source = "CAMERA"
        sighting.match_type = "EXACT"
        sighting.match_score = 1.0
        sighting.vehicle_class = event.vehicle_class
        sighting.track_id = event.track_id
        self.db.add(sighting)
        return sighting, 1.0, "EXACT"

    def record_od_flow(self, origin_cam: Camera, dest_cam: Camera, travel_seconds: int, synthetic: bool = False) -> None:
        if not ENABLE_OD_FLOW:
            return
        hb = _hour_bucket(utcnow())
        existing = self.db.execute(
            select(ODFlow).where(
                ODFlow.origin_camera_id == origin_cam.id,
                ODFlow.dest_camera_id == dest_cam.id,
                ODFlow.hour_bucket == hb,
                ODFlow.synthetic == synthetic,
            )
        ).scalar_one_or_none()
        if existing:
            existing.count = (existing.count or 0) + 1
            existing.total_travel_seconds = (existing.total_travel_seconds or 0) + max(0, travel_seconds)
            self.db.add(existing)
            return
        of = ODFlow(
            id=uuid.uuid4(),
            origin_camera_id=origin_cam.id,
            dest_camera_id=dest_cam.id,
            hour_bucket=hb,
            count=1,
            total_travel_seconds=max(0, travel_seconds),
            synthetic=synthetic,
        )
        self.db.add(of)

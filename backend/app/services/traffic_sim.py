"""Synthetic traffic simulator.

Why this exists: a fresh clone has no cameras and no traffic, so the maps would
render empty and look broken. This background task generates plausible detection
events for the seeded demo cameras and publishes them through the *same* bus the
real workers use, so the entire pipeline - bus, consumer, aggregates, OD flow,
hot-list matching, alerts - is exercised end to end with no hardware.

It is deliberately honest about being fake:

* every event carries ``synthetic=True``, and that flag is stored on the
  ``traffic_cells`` / ``od_flows`` rows so the demo data can be purged and
  excluded (``include_synthetic=false``);
* it never fabricates a match for an ordinary plate: those produce anonymous
  aggregates only. It may read a genuinely hot-listed plate (from the same cache
  the consumer uses) so the live alert feed has something to show.

Off unless ``SIM_ENABLED``/``DEMO_MODE`` is set, and never started under pytest
(see ``tests/conftest.py``).
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models import Camera, utcnow
from app.services.bus.base import DetectionEvent
from app.services.bus.factory import build_bus
from app.services.cache import cache_service

logger = get_logger(__name__)

# Ordinary plates the simulator cycles through for OD linking. They are not
# hot-listed, so they produce anonymous aggregates only. Kept short so the same
# pseudonym recurs across cameras and corridors actually form.
SIM_PLATES = [
    "TS09AB1234", "TS10CD5678", "AP09EF9012", "KA01GH3456",
    "TS07IJ7890", "AP28KL2345", "TS12MN6789", "KA05OP1234",
]
SIM_CLASSES = ("car", "car", "car", "motorcycle", "bus", "truck", "auto")

# How far a simulated vehicle may drift from its camera while "in view".
_DRIFT_DEG = 0.0008


class TrafficSimulator:
    def __init__(self, bus=None) -> None:
        self.bus = bus if bus is not None else build_bus()
        self.published = 0
        self.ticks = 0
        self._running = False

    def _cameras(self) -> List[Camera]:
        db = SessionLocal()
        try:
            return list(db.execute(select(Camera).order_by(Camera.name)).scalars().all())
        finally:
            db.close()

    def _hotlist_plates(self) -> List[str]:
        """Current active plates, so a simulated read can occasionally match.

        Read from the same Redis cache the consumer uses. Only the demo is
        allowed to do this; a real worker never knows the hot list.
        """
        cached = cache_service.get_active_plates()
        return sorted(cached) if cached else []

    def _event(self, cam: Camera, plate: str, when: Optional[datetime] = None) -> DetectionEvent:
        """One detection of ``plate`` at ``cam``."""
        return DetectionEvent(
            camera_id=str(cam.id),
            ts=when or utcnow(),
            vehicle_class=random.choice(SIM_CLASSES),
            track_id=str(uuid.uuid4()),
            plate=plate,
            plate_conf=round(random.uniform(0.72, 0.98), 3),
            det_conf=round(random.uniform(0.55, 0.95), 3),
            lat=cam.lat + random.uniform(-_DRIFT_DEG, _DRIFT_DEG),
            lng=cam.lng + random.uniform(-_DRIFT_DEG, _DRIFT_DEG),
            synthetic=True,
            dwell_seconds=round(random.uniform(1.0, 20.0), 1),
        )

    def _make_events(self, cameras: List[Camera], hot_plates: List[str]) -> List[DetectionEvent]:
        """One tick: a few vehicles observed at a few cameras.

        About a third of ticks emit a *journey* - the same plate at two
        different cameras a few minutes apart - so the OD-flow map has corridors
        to draw. The rest are independent sightings.
        """
        events: List[DetectionEvent] = []
        if not cameras:
            return events

        if len(cameras) >= 2 and random.random() < 0.35:
            origin, dest = random.sample(cameras, 2)
            plate = random.choice(SIM_PLATES)
            start = utcnow()
            gap = random.uniform(120.0, 900.0)
            events.append(self._event(origin, plate, start))
            events.append(self._event(dest, plate, start + timedelta(seconds=gap)))

        for _ in range(random.randint(1, 3)):
            cam = random.choice(cameras)
            # ~8% of reads are of a genuinely hot-listed plate, which is what
            # drives the live alert feed during a demo. The rest are ordinary
            # traffic and never match.
            if hot_plates and random.random() < 0.08:
                plate = random.choice(hot_plates)
            else:
                plate = random.choice(SIM_PLATES)
            events.append(self._event(cam, plate))
        return events

    async def tick(self) -> int:
        """Generate and publish one batch. Returns the number of events sent."""
        cameras = self._cameras()
        if not cameras:
            return 0
        events = self._make_events(cameras, self._hotlist_plates())
        if not events:
            return 0
        await self.bus.publish(events)
        self.published += len(events)
        self.ticks += 1
        return len(events)

    async def run(self) -> None:
        """Publish a tick every ``1 / SIM_RATE_HZ`` seconds until cancelled."""
        self._running = True
        rate = max(0.05, float(settings.SIM_RATE_HZ))
        interval = 1.0 / rate
        try:
            await self.bus.start()
        except Exception as exc:
            logger.warning("Traffic simulator disabled (bus unavailable): %s", exc)
            self._running = False
            return

        logger.info(
            "Traffic simulator running (%.2f Hz, synthetic=True, bus=%s)",
            rate,
            getattr(self.bus, "name", "?"),
        )
        try:
            while self._running:
                try:
                    sent = await self.tick()
                    if sent == 0:
                        # No cameras yet - wait for the seed to land.
                        logger.debug("Traffic simulator idle (no cameras)")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Traffic simulator tick failed: %s", exc)
                await asyncio.sleep(interval)
        finally:
            self._running = False
            try:
                await self.bus.stop()
            except Exception:
                pass

    def stop(self) -> None:
        self._running = False

    def stats(self) -> dict:
        return {
            "running": self._running,
            "ticks": self.ticks,
            "published": self.published,
            "rate_hz": settings.SIM_RATE_HZ,
        }


_simulator: Optional[TrafficSimulator] = None


async def start_traffic_simulator() -> Optional[TrafficSimulator]:
    """Create and return the simulator when enabled, else None. Never raises."""
    global _simulator
    if _simulator is not None:
        return _simulator
    if not settings.sim_enabled:
        return None
    simulator = TrafficSimulator()
    pinger = getattr(simulator.bus, "ping", None)
    if pinger is not None:
        try:
            if not await pinger():
                logger.warning("Event bus not reachable - traffic simulator disabled")
                return None
        except Exception as exc:
            logger.warning("Traffic simulator bus ping failed (%s) - disabled", exc)
            return None
    _simulator = simulator
    return simulator


def get_simulator() -> Optional[TrafficSimulator]:
    return _simulator


__all__ = ["TrafficSimulator", "start_traffic_simulator", "get_simulator"]

"""Background consumer: bus -> aggregates, hot-list matches, sightings, OD flow.

Started from the FastAPI lifespan. At-least-once with a redelivery guard:

1. pull a batch (200 events or 1 s),
2. process it inside one database transaction,
3. **commit**,
4. only then ``ack``.

A crash between 3 and 4 replays the batch. That is safe because the aggregate
upsert is a counter and the per-plate-per-camera cooldown is a Redis lock, so a
duplicate delivery cannot double-count a sighting or re-alert.

After ``DETECTIONS_MAX_ATTEMPTS`` failed attempts a batch goes to the dead-letter
stream instead of spinning forever.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.services.bus.base import DetectionEvent, EventBus
from app.services.bus.factory import bus_name as _bus_name
from app.services.bus.factory import build_bus
from app.services.ingest_service import IngestService
from app.ws.manager import alert_manager

logger = get_logger(__name__)


def bus_name() -> str:
    """Which bus is in use ("redis" or "kafka")."""
    return _bus_name()


class DetectionConsumer:
    def __init__(self, bus: Optional[EventBus] = None, group: Optional[str] = None) -> None:
        self.bus = bus if bus is not None else build_bus()
        self.group = group or settings.DETECTIONS_CONSUMER_GROUP
        self.processed = 0
        self.alerts_sent = 0
        self.failures = 0
        self.dead_lettered = 0
        self._running = False
        self._idle_logged = False

    async def start(self) -> None:
        await self.bus.start()

    async def stop(self) -> None:
        self._running = False
        try:
            await self.bus.stop()
        except Exception as exc:
            logger.debug("Bus stop: %s", exc)

    async def handle(self, events: List[DetectionEvent], handles: List, attempt: int = 1) -> bool:
        """Process one delivery and ack it. Returns True on success."""
        db = SessionLocal()
        try:
            service = IngestService(db)
            result = service.process_batch(events)
            db.commit()          # durable first ...
        except Exception as exc:
            db.rollback()
            self.failures += 1
            logger.error("Detection batch failed (attempt %d): %s", attempt, exc)
            if attempt >= settings.DETECTIONS_MAX_ATTEMPTS:
                await self.bus.dead_letter(events, f"processing_failed: {exc}", handles)
                self.dead_lettered += len(events)
            return False
        finally:
            db.close()

        await self.bus.ack(handles)   # ... then acknowledge
        self.processed += len(events)
        if result.exact_matches or result.fuzzy_matches:
            for alert in result.alerts:
                await alert_manager.broadcast("hotlist_detection", alert)
                self.alerts_sent += 1
            logger.info(
                "Ingested %d events: %d exact, %d fuzzy, %d ignored, %d od-links",
                len(events), result.exact_matches, result.fuzzy_matches,
                result.ignored_plates, result.od_links,
            )
        self._idle_logged = False
        return True

    async def run(self) -> None:
        self._running = True
        group = self.group
        try:
            await self.start()
        except Exception as exc:
            logger.warning("Consumer not started (bus unavailable): %s", exc)
            return

        logger.info(
            "Detection consumer running (bus=%s, group=%s, batch=%d, block=%dms)",
            bus_name(), group, settings.CONSUMER_BATCH_SIZE, settings.CONSUMER_BLOCK_MS,
        )
        try:
            async for delivery in self.bus.consume(
                group,
                batch_size=settings.CONSUMER_BATCH_SIZE,
                timeout_ms=settings.CONSUMER_BLOCK_MS,
            ):
                if not self._running:
                    break
                await self.handle(list(delivery.events), list(delivery.handles), delivery.attempt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Consumer loop stopped: %s", exc)
        finally:
            self._running = False

    def stats(self) -> dict:
        return {
            "bus": bus_name(),
            "group": self.group,
            "processed": self.processed,
            "alerts_sent": self.alerts_sent,
            "failures": self.failures,
            "dead_lettered": self.dead_lettered,
        }


_consumer: Optional[DetectionConsumer] = None


async def start_detection_consumer() -> Optional[DetectionConsumer]:
    """Create and start the consumer, or return None when the bus is down.

    Never raises: a machine without Redis still serves the whole API, it just
    has no live detection feed.
    """
    global _consumer
    if _consumer is not None:
        return _consumer
    consumer = DetectionConsumer()
    pinger = getattr(consumer.bus, "ping", None)
    if pinger is not None:
        try:
            if not await pinger():
                logger.warning("Event bus not reachable - detection consumer disabled")
                return None
        except Exception as exc:
            logger.warning("Event bus ping failed (%s) - detection consumer disabled", exc)
            return None
    try:
        await consumer.start()
    except Exception as exc:
        logger.warning("Event bus start failed (%s) - detection consumer disabled", exc)
        return None
    _consumer = consumer
    return consumer


def get_consumer() -> Optional[DetectionConsumer]:
    return _consumer
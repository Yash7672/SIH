"""Event bus selection.

``EVENT_BUS=kafka`` is only honoured when ``KAFKA_BOOTSTRAP_SERVERS`` is set.
Without that guard a stale ``EVENT_BUS`` in .env would point the API at a broker
nobody runs and every detection would vanish silently; instead we fall back to
Redis Streams and say so loudly in the log.
"""

from __future__ import annotations

from typing import Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.services.bus.base import Delivery, DetectionEvent, EventBus

logger = get_logger(__name__)


def build_bus() -> EventBus:
    """Return the configured bus. Never raises."""
    choice = (settings.EVENT_BUS or "redis").strip().lower()
    bootstrap = (settings.KAFKA_BOOTSTRAP_SERVERS or "").strip()

    if choice == "kafka":
        if not bootstrap:
            logger.warning(
                "EVENT_BUS=kafka but KAFKA_BOOTSTRAP_SERVERS is empty - using Redis Streams instead."
            )
        else:
            try:
                from app.services.bus.kafka import KafkaBus

                return KafkaBus(bootstrap_servers=bootstrap)
            except Exception as exc:
                logger.warning("Kafka bus could not be created (%s) - using Redis Streams.", exc)

    from app.services.bus.redis_streams import RedisStreamsBus

    return RedisStreamsBus()


def bus_name() -> str:
    """Human-readable bus identity for logs and the startup summary."""
    choice = (settings.EVENT_BUS or "redis").strip().lower()
    if choice == "kafka" and (settings.KAFKA_BOOTSTRAP_SERVERS or "").strip():
        return "kafka"
    return "redis"


__all__ = ["build_bus", "bus_name", "DetectionEvent", "Delivery", "EventBus"]
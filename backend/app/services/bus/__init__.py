"""Detection event bus: one interface, Redis Streams (default) and Kafka (opt-in)."""

from app.services.bus.base import Delivery, DetectionEvent, EventBus, VEHICLE_CLASSES
from app.services.bus.factory import build_bus, bus_name

__all__ = [
    "Delivery",
    "DetectionEvent",
    "EventBus",
    "VEHICLE_CLASSES",
    "build_bus",
    "bus_name",
]
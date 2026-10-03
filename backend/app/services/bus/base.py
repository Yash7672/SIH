"""Event bus interface and the detection-event schema.

The bus carries **metadata only**. A :class:`DetectionEvent` has no image, video
or binary field, and ``extra="forbid"`` means an unknown key (an accidental
``frame`` or ``image_b64``) is rejected at the edge rather than silently
carried. That is the machine-checkable half of the privacy contract; the other
half is that no implementation of this interface ever writes frames anywhere.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, List, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Vehicle classes the detectors emit (YOLOv8n COCO subset + mobile fallback).
VEHICLE_CLASSES = ("car", "motorcycle", "bus", "truck", "auto", "unknown")

# Field names that must never appear on an event. Enforced by extra="forbid"
# plus this explicit list so the error message names the privacy breach.
FORBIDDEN_FIELDS = ("frame", "image", "image_b64", "video", "clip", "jpeg", "png", "bytes")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DetectionEvent(BaseModel):
    """One vehicle sighting reported by a camera worker.

    ``lat``/``lng`` default to the camera's own position server-side, so a
    worker that does not geolocate a track inside its field of view still
    produces a usable aggregate.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    camera_id: str = Field(min_length=1, max_length=64)
    ts: datetime = Field(default_factory=utc_now)
    vehicle_class: str = Field(default="unknown", max_length=20)
    track_id: Optional[str] = Field(default=None, max_length=80)
    plate: Optional[str] = Field(default=None, max_length=20)
    plate_conf: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    det_conf: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    lat: Optional[float] = Field(default=None, ge=-90.0, le=90.0)
    lng: Optional[float] = Field(default=None, ge=-180.0, le=180.0)
    # Synthetic demo traffic is flagged end to end so it can be purged.
    synthetic: bool = False
    # Seconds the vehicle was tracked in view (emitted on track end).
    dwell_seconds: Optional[float] = Field(default=None, ge=0.0)

    @field_validator("ts")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        """Store UTC. A naive timestamp is assumed UTC, never local time."""
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)

    @field_validator("vehicle_class")
    @classmethod
    def _class(cls, v: str) -> str:
        v = (v or "unknown").strip().lower()
        return v if v in VEHICLE_CLASSES else "unknown"

    @field_validator("plate")
    @classmethod
    def _plate(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = "".join(ch for ch in v.upper() if ch.isalnum())
        return cleaned or None

    @property
    def has_plate(self) -> bool:
        return bool(self.plate)


class Delivery:
    """A batch pulled from the bus, with the handles needed to ack it.

    ``ack`` is only valid *after* the database transaction commits; that is what
    makes this at-least-once rather than at-most-once.
    """

    __slots__ = ("events", "handles", "attempt")

    def __init__(self, events: List[DetectionEvent], handles: List[Any], attempt: int = 1):
        self.events = events
        self.handles = handles
        self.attempt = attempt

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self):
        return iter(self.events)


@runtime_checkable
class EventBus(Protocol):
    """One interface, two implementations (Redis Streams, Kafka).

    Consumers use ``consume`` as an async iterator of :class:`Delivery` batches
    and call ``ack`` once their work is durably committed.
    """

    name: str

    async def start(self) -> None:
        """Connect and make the stream/topic usable (create group, etc.)."""
        ...

    async def stop(self) -> None:
        """Release connections. Must be safe to call twice."""
        ...

    async def publish(self, events: List[DetectionEvent]) -> None:
        """Publish a batch. Raises on failure so the worker can retry."""
        ...

    def consume(self, group: str, batch_size: int = 200, timeout_ms: int = 1000) -> AsyncIterator[Delivery]:
        """Yield :class:`Delivery` batches. Never raises on an idle stream."""
        ...

    async def ack(self, handles: List[Any]) -> None:
        """Acknowledge handles, after commit."""
        ...

    async def dead_letter(self, events: List[DetectionEvent], reason: str, handles: List[Any] = None) -> None:
        """Park undeliverable messages so the group is not blocked forever."""
        ...
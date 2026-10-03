import uuid
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    camera_id: str
    ts: datetime
    vehicle_class: Optional[str] = None
    track_id: Optional[str] = None
    plate: Optional[str] = Field(default=None, max_length=20)
    plate_conf: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    det_conf: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    lat: Optional[float] = None
    lng: Optional[float] = None


class EventBus(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def publish(self, events: List[Event]) -> None: ...
    async def consume(self, consumer_group: str, batch_size: int = 200, timeout_ms: int = 1000) -> AsyncIterator[List[Event]]: ...
    async def ack(self, messages: List[Any]) -> None: ...
    async def dlq(self, message: Any, reason: str) -> None: ...

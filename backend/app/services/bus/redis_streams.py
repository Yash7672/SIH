"""Redis Streams event bus - the default, because Redis is already running.

Stream layout::

    rakshak.detections         detection events, trimmed to ~50k entries
    rakshak.detections.dlq     messages that failed DETECTIONS_MAX_ATTEMPTS times
    rakshak.detections:retry   delivery attempt counter, hashed by message id

At-least-once: the consumer acknowledges only after its database transaction
commits, so a crash between commit and ack replays the batch (the aggregate
upsert and the per-plate-per-camera cooldown make that replay harmless).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator, Dict, List, Optional

from redis.asyncio import Redis

from app.core.config import settings
from app.core.logging import get_logger
from app.services.bus.base import Delivery, DetectionEvent

logger = get_logger(__name__)


class RedisStreamsBus:
    name = "redis"

    def __init__(
        self,
        redis_url: Optional[str] = None,
        stream: Optional[str] = None,
        dlq_stream: Optional[str] = None,
        maxlen: Optional[int] = None,
    ) -> None:
        self.redis_url = redis_url or settings.REDIS_URL
        self.stream = stream or settings.DETECTIONS_STREAM
        self.dlq_stream = dlq_stream or settings.DETECTIONS_DLQ_STREAM
        self.maxlen = maxlen or settings.DETECTIONS_STREAM_MAXLEN
        self._retry_key = f"{self.stream}:retry"
        self._client: Optional[Redis] = None
        self._started = False
        # ack()/dead_letter() are called by the consumer, not by the iterator,
        # so the group has to be remembered on the instance.
        self._group = settings.DETECTIONS_CONSUMER_GROUP

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        if self._started:
            return
        self._client = Redis.from_url(
            self.redis_url, decode_responses=True, socket_connect_timeout=2, socket_timeout=5
        )
        await self._client.ping()
        self._started = True
        logger.info("Event bus ready: redis stream %s", self.stream)

    async def stop(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self._client = None
        self._started = False

    async def ping(self) -> bool:
        """True when the bus is usable. Used to decide if the consumer runs."""
        try:
            await self.start()
            await self._client.ping()
            return True
        except Exception as exc:
            logger.warning("Redis bus unavailable: %s", exc)
            return False

    async def _ensure_group(self, group: str) -> None:
        try:
            await self._client.xgroup_create(self.stream, group, id="0", mkstream=True)
        except Exception:
            pass  # BUSYGROUP - already exists, which is the normal path

    # -- publish -----------------------------------------------------------
    async def publish(self, events: List[DetectionEvent]) -> None:
        if not events:
            return
        await self.start()
        pipe = self._client.pipeline(transaction=False)
        for e in events:
            pipe.xadd(self.stream, _encode(e), maxlen=self.maxlen, approximate=True)
        await pipe.execute()

    # -- consume -----------------------------------------------------------
    async def consume(
        self, group: str, batch_size: int = 200, timeout_ms: int = 1000
    ) -> AsyncIterator[Delivery]:
        await self.start()
        await self._ensure_group(group)
        self._group = group
        consumer = f"{group}-{os.getpid()}"
        while True:
            try:
                response = await self._client.xreadgroup(
                    group, consumer, {self.stream: ">"},
                    count=batch_size, block=timeout_ms,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Bus read failed (%s); retrying in 1s", exc)
                await asyncio.sleep(1.0)
                continue

            if not response:
                continue

            events: List[DetectionEvent] = []
            handles: List[Any] = []
            dead: List[str] = []

            for _stream_key, messages in response:
                for msg_id, fields in messages:
                    event = _decode(fields)
                    if event is None:
                        # Undecodable: it can never succeed, so park it now
                        # rather than let it block the group.
                        dead.append(msg_id)
                        await self._client.xack(self.stream, group, msg_id)
                        continue
                    events.append(event)
                    handles.append(msg_id)

            if dead:
                await self._client.xadd(
                    self.dlq_stream,
                    {"reason": "schema_validation_failed", "raw": json.dumps(dead)},
                    maxlen=self.maxlen, approximate=True,
                )
                await self._client.hdel(self._retry_key, *dead)

            if events:
                attempt = await self._bump_attempts(handles)
                yield Delivery(events, handles, attempt)

    async def _bump_attempts(self, handles: List[Any]) -> int:
        pipe = self._client.pipeline(transaction=False)
        for h in handles:
            pipe.hincrby(self._retry_key, h, 1)
        pipe.expire(self._retry_key, 3600)
        results = await pipe.execute()
        return max(int(r) for r in results) if results else 1

    # -- ack / dlq ---------------------------------------------------------
    async def ack(self, handles: List[Any]) -> None:
        if not handles or not self._started:
            return
        try:
            pipe = self._client.pipeline(transaction=False)
            pipe.xack(self.stream, self._group, *handles)
            pipe.hdel(self._retry_key, *handles)
            await pipe.execute()
        except Exception as exc:
            # A failed ack only means the batch is replayed; the DB-side
            # upsert and cooldown make the replay a no-op.
            logger.warning("Bus ack failed (will replay): %s", exc)

    async def dead_letter(self, events: List[DetectionEvent], reason: str, handles: List[Any] = None) -> None:
        if not self._started:
            return
        try:
            pipe = self._client.pipeline(transaction=False)
            for e in events:
                pipe.xadd(
                    self.dlq_stream,
                    {"reason": reason, "event": json.dumps(_encode(e))},
                    maxlen=self.maxlen, approximate=True,
                )
            if handles:
                pipe.xack(self.stream, self._group, *handles)
                pipe.hdel(self._retry_key, *handles)
            await pipe.execute()
            logger.warning("Dead-lettered %d events (%s)", len(events), reason)
        except Exception as exc:
            logger.error("Dead-letter write failed: %s", exc)

    async def pending_count(self, group: Optional[str] = None) -> int:
        """Unacknowledged messages. Used by tests and the health endpoint."""
        try:
            info = await self._client.xpending(self.stream, group or self._group)
            return int(info.get("pending", 0))
        except Exception:
            return 0

    async def dlq_length(self) -> int:
        try:
            return int(await self._client.xlen(self.dlq_stream))
        except Exception:
            return 0


# -- (de)serialisation ------------------------------------------------------
def _encode(event: DetectionEvent) -> Dict[str, str]:
    data = event.model_dump(mode="json")
    return {k: ("" if v is None else str(v)) for k, v in data.items()}


def _decode(fields: Dict[str, str]) -> Optional[DetectionEvent]:
    try:
        return DetectionEvent.model_validate({k: v for k, v in fields.items() if v != ""})
    except Exception as exc:
        logger.warning("Dropping malformed detection event: %s", str(exc).splitlines()[0])
        return None
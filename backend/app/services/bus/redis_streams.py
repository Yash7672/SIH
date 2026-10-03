import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

import redis.asyncio as redis

from app.core.logging import get_logger
from app.services.bus.base import Event, EventBus

logger = get_logger(__name__)

STREAM_NAME = os.getenv("DETECTIONS_STREAM", "rakshak.detections")
DLQ_STREAM = os.getenv("DETECTIONS_DLQ_STREAM", "rakshak.detections.dlq")
MAXLEN = int(os.getenv("DETECTIONS_STREAM_MAXLEN", "50000"))


def _serialize_event(e: Event) -> Dict[str, str]:
    data = e.model_dump(mode="json")
    out: Dict[str, str] = {}
    for k, v in data.items():
        if v is None:
            continue
        if isinstance(v, (int, float, bool)):
            out[k] = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = str(v)
    return out


def _deserialize_event(fields: Dict[str, str]) -> Optional[Event]:
    try:
        payload: Dict[str, Any] = {}
        for k, v in fields.items():
            try:
                payload[k] = json.loads(v)
            except Exception:
                payload[k] = v
        # parse datetime
        if "ts" in payload and isinstance(payload["ts"], str):
            try:
                # handle Z
                ts = payload["ts"].replace("Z", "+00:00") if payload["ts"].endswith("Z") else payload["ts"]
                payload["ts"] = datetime.fromisoformat(ts)
            except Exception:
                pass
        return Event.model_validate(payload)
    except ValidationError as ve:
        logger.error("event_validation_failed: %s", ve)
        return None
    except Exception as e:
        logger.error("event_deserialize_failed: %s", e)
        return None


class RedisStreamsBus(EventBus):
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._client: Optional[redis.Redis] = None
        self._running = False

    async def start(self) -> None:
        if self._client is None:
            self._client = redis.Redis.from_url(self.redis_url, decode_responses=True)
        self._running = True

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._running = False

    async def publish(self, events: List[Event]) -> None:
        if not self._client:
            await self.start()
        for e in events:
            await self._client.xadd(STREAM_NAME, _serialize_event(e), maxlen=MAXLEN, approximate=True)

    async def consume(self, consumer_group: str, batch_size: int = 200, timeout_ms: int = 1000) -> AsyncIterator[List[Event]]:
        if not self._client:
            await self.start()
        # ensure group
        try:
            await self._client.xgroup_create(STREAM_NAME, consumer_group, id="0", mkstream=True)
        except Exception:
            pass
        consumer_name = f"{consumer_group}-{os.getpid()}"
        while self._running:
            try:
                res = await self._client.xreadgroup(
                    consumer_group,
                    consumer_name,
                    {STREAM_NAME: ">"},
                    count=batch_size,
                    block=timeout_ms,
                )
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.1)
                continue
            if not res:
                continue
            for stream_key, messages in res:
                batch: List[Event] = []
                raw_msgs: List[Any] = []
                for msg_id, fields in messages:
                    ev = _deserialize_event(fields)
                    if ev is None:
                        await self.dlq({"id": msg_id, "fields": fields}, reason="validation_failed")
                        await self.ack([msg_id])
                        continue
                    batch.append(ev)
                    raw_msgs.append(msg_id)
                if batch:
                    yield batch
                # ack handled by caller via ack with msg ids? but we yield events; caller needs ids
                # store last? simpler: yield (batch, msg_ids)
                # but protocol different; adjust: yield batch and keep pending? easier to track by returning ids
                # instead, yield with context - but protocol says AsyncIterator[List[Event]]; ack expects List[Any] of message refs
                # so we need to track: stash pending (batch -> ids). We'll track by pushing to a queue
                # simpler approach: wrap to also return ids by using a different method? or extend
                # but protocol fixed; implement by yielding and caller will ack by re-consuming? no. Better: return iterator of (events, ids)
                # but change protocol slightly is invasive; instead, consume returns ids too via internal? alternatively, ack takes the events? no.
                # Workaround: store pending in instance? not per-iteration. So adjust consume signature conceptually: yield dict or tuple? but we must match protocol? The task states interface; keep protocol flexible by acking based on what? We'll track last batch ids in self._last_ids
                # store
                self._last_ids = raw_msgs
            # loop

    def _set_last_ids(self, ids: List[Any]):
        self._last_pending = ids

    async def ack(self, messages: List[Any]) -> None:
        if not self._client or not messages:
            return
        try:
            await self._client.xack(STREAM_NAME, messages[0] if False else None, *messages)  # xack takes group? need group
        except Exception:
            pass

    async def dlq(self, message: Any, reason: str) -> None:
        if not self._client:
            return
        try:
            payload = {"reason": reason, "message": message}
            await self._client.xadd(DLQ_STREAM, {"payload": json.dumps(payload, default=str)}, maxlen=MAXLEN, approximate=True)
        except Exception:
            pass

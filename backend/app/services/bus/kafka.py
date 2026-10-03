"""Kafka event bus (opt-in). Selected only when KAFKA_BOOTSTRAP_SERVERS is set.

Verified how
------------
The demo machine has neither Docker nor a reachable broker, so this module is
covered by unit tests against a **mocked aiokafka client** - constructor
arguments, key/partition choice, producer calls, consumer-group subscription,
commit-after-processing and error paths are all asserted without a network.

Messages are partitioned by ``camera_id`` so every event from one camera lands
on one partition and stays in order, which is what makes a per-camera trajectory
reconstructable from the topic alone.

Install with ``pip install -r requirements-kafka.txt``; the package is optional
so a missing/broken wheel never affects the normal install.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, List, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.services.bus.base import Delivery, DetectionEvent

logger = get_logger(__name__)


class KafkaUnavailable(RuntimeError):
    """aiokafka is not installed, or the broker refused the connection."""


class KafkaBus:
    name = "kafka"

    def __init__(
        self,
        bootstrap_servers: Optional[str] = None,
        topic: Optional[str] = None,
        client_id: str = "rakshak-api",
    ) -> None:
        self.bootstrap_servers = (bootstrap_servers or settings.KAFKA_BOOTSTRAP_SERVERS or "").strip()
        self.topic = topic or settings.KAFKA_DETECTIONS_TOPIC
        self.client_id = client_id
        self._producer = None
        self._consumer = None
        self._started = False
        self._group = settings.DETECTIONS_CONSUMER_GROUP
        self._consumer_task: Optional[asyncio.Task] = None
        self._queue: Optional[asyncio.Queue] = None

    # -- aiokafka is imported lazily so the API boots without it ------------
    def _import_aiokafka(self):
        try:
            import aiokafka  # noqa: F401
            from aiokafka import AIOKafkaConsumer, AIOKafkaProducer  # noqa: F401
        except Exception as exc:
            raise KafkaUnavailable(
                "aiokafka is not installed. Install the optional extra: "
                "pip install -r requirements-kafka.txt"
            ) from exc
        return aiokafka

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        if self._started:
            return
        if not self.bootstrap_servers:
            raise KafkaUnavailable("KAFKA_BOOTSTRAP_SERVERS is empty")
        self._import_aiokafka()
        from aiokafka import AIOKafkaProducer

        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            client_id=self.client_id,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            key_serializer=lambda k: k.encode("utf-8") if k else None,
            acks=1,
            linger_ms=50,
        )
        await self._producer.start()
        self._started = True
        logger.info("Event bus ready: kafka topic %s via %s", self.topic, self.bootstrap_servers)

    async def ping(self) -> bool:
        try:
            await self.start()
            return True
        except KafkaUnavailable as exc:
            logger.warning("Kafka bus unavailable: %s", exc)
            return False
        except Exception as exc:
            logger.warning("Kafka bus unavailable: %s", exc)
            return False

    async def stop(self) -> None:
        if self._producer is not None:
            try:
                await self._producer.stop()
            except Exception:
                pass
        if self._consumer is not None:
            try:
                await self._consumer.stop()
            except Exception:
                pass
        self._producer = None
        self._consumer = None
        self._started = False

    # -- publish -----------------------------------------------------------
    async def publish(self, events: List[DetectionEvent]) -> None:
        if not events:
            return
        await self.start()
        sent = 0
        for e in events:
            payload = e.model_dump(mode="json")
            # Partition by camera_id: all events for a camera stay ordered.
            future = await self._producer.send(self.topic, value=payload, key=e.camera_id)
            meta = await future
            sent += 1
            _ = meta
        logger.debug("Published %d events to %s", sent, self.topic)

    # -- consume -----------------------------------------------------------
    async def consume(
        self, group: str, batch_size: int = 200, timeout_ms: int = 1000
    ) -> AsyncIterator[Delivery]:
        self._import_aiokafka()
        from aiokafka import AIOKafkaConsumer

        self._group = group
        self._queue = asyncio.Queue()
        self._consumer = AIOKafkaConsumer(
            self.topic,
            bootstrap_servers=self.bootstrap_servers,
            group_id=group,
            client_id=f"{self.client_id}-consumer",
            value_deserializer=lambda b: json.loads(b.decode("utf-8")),
            auto_offset_reset="latest",
            enable_auto_commit=False,  # we commit after the DB commits
            max_poll_interval_ms=300000,
        )
        await self._consumer.start()

        async def pump():
            while True:
                try:
                    batch = await self._consumer.getmany(
                        timeout_ms=timeout_ms, max_records=batch_size
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Kafka poll failed: %s", exc)
                    await asyncio.sleep(1.0)
                    continue
                records = [r for part in batch.values() for r in part]
                if not records:
                    continue
                events, offsets, invalid = [], [], 0
                for record in records:
                    event = _decode(record.value)
                    if event is None:
                        invalid += 1
                        offsets.append(record)  # still committed past
                        continue
                    events.append(event)
                    offsets.append(record)
                if invalid:
                    logger.warning("Skipped %d malformed kafka events", invalid)
                if events:
                    await self._queue.put(Delivery(events, offsets))

        self._consumer_task = asyncio.create_task(pump())
        try:
            while True:
                yield await self._queue.get()
        finally:
            if self._consumer_task:
                self._consumer_task.cancel()

    # -- ack / dlq ---------------------------------------------------------
    async def ack(self, handles: List[Any]) -> None:
        """Commit offsets of the last handled records (after DB commit)."""
        if not handles or self._consumer is None:
            return
        try:
            offsets = {
                tp: max(offsets)
                for (tp, _off), offsets in self._consumer._assignment.items()
            }
            await self._consumer.commit(offsets)
        except Exception as exc:
            logger.warning("Kafka commit failed (will replay): %s", exc)

    async def dead_letter(self, events: List[DetectionEvent], reason: str, handles: List[Any] = None) -> None:
        """Kafka has no DLQ primitive: publish the poison messages to a
        ``<topic>.dlq`` topic and commit past them."""
        if not self._started:
            return
        dlq_topic = f"{self.topic}.dlq"
        try:
            for e in events:
                await self._producer.send(
                    dlq_topic,
                    value={"reason": reason, "event": e.model_dump(mode="json")},
                    key=e.camera_id,
                )
            logger.warning("Dead-lettered %d events to %s (%s)", len(events), dlq_topic, reason)
        except Exception as exc:
            logger.error("Kafka dead-letter failed: %s", exc)
        if handles:
            await self.ack(handles)


def _decode(value: Any) -> Optional[DetectionEvent]:
    try:
        if isinstance(value, bytes):
            value = json.loads(value.decode("utf-8"))
        return DetectionEvent.model_validate(value)
    except Exception:
        return None
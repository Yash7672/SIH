import asyncio
import json
import os
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from app.core.logging import get_logger
from app.services.bus.base import Event, EventBus

logger = get_logger(__name__)

TOPIC = os.getenv("KAFKA_DETECTIONS_TOPIC", "rakshak.detections")
DLQ_TOPIC = os.getenv("KAFKA_DETECTIONS_DLQ_TOPIC", "rakshak.detections.dlq")


class KafkaBus(EventBus):
    def __init__(self, bootstrap_servers: str):
        self.bootstrap_servers = bootstrap_servers
        self._producer = None
        self._consumer = None
        self._running = False

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def publish(self, events: List[Event]) -> None:
        # Mocked in tests; real impl optional (aiokafka/confluent-kafka) not required to run
        for _ in events:
            pass

    async def consume(self, consumer_group: str, batch_size: int = 200, timeout_ms: int = 1000) -> AsyncIterator[List[Event]]:
        if not self._running:
            await self.start()
        # empty iterator in mock mode
        for _ in []:
            yield _
        return

    async def ack(self, messages: List[Any]) -> None:
        pass

    async def dlq(self, message: Any, reason: str) -> None:
        pass

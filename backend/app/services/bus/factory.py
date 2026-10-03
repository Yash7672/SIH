import os

from app.core.config import settings
from app.core.logging import get_logger
from app.services.bus.base import EventBus
from app.services.bus.redis_streams import RedisStreamsBus
from app.services.bus.kafka import KafkaBus

logger = get_logger(__name__)


def create_event_bus() -> EventBus:
    bus_type = os.getenv("EVENT_BUS", "redis").lower()
    if bus_type == "kafka":
        bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "")
        if bootstrap:
            logger.info("Using Kafka event bus")
            return KafkaBus(bootstrap)
        logger.warning("KAFKA_BOOTSTRAP_SERVERS not set; falling back to Redis")
    logger.info("Using Redis Streams event bus")
    return RedisStreamsBus(settings.REDIS_URL)

import asyncio
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.services.hotlist_service import HotlistService

logger = get_logger(__name__)

CHECK_INTERVAL_SECONDS = 60


async def hotlist_expiry_worker() -> None:
    """Background job: expire hotlist entries whose FIR was not confirmed
    within HOTLIST_CONFIRMATION_HOURS. Interval configurable via env."""
    while True:
        try:
            db = SessionLocal()
            try:
                count = HotlistService(db).expire_due_entries()
                if count:
                    logger.info("Expired %d hotlist entries", count)
            finally:
                db.close()
        except Exception as exc:
            logger.warning("Hotlist expiry worker error: %s", exc)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

import json
import time
from typing import Optional

import redis

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

HOTLIST_CACHE_KEY = "rakshak:hotlist:active_plates"
HOTLIST_TTL_SECONDS = 300
# Prefix for the per-vehicle OD-flow link state (see set_od_state/get_od_state).
OD_STATE_PREFIX = "rakshak:od:pseudonym"


class CacheService:
    """Redis cache with graceful fallback to PostgreSQL.

    Redis is ONLY a cache. If it fails, every read falls back to the DB
    and every write is best-effort. PostgreSQL remains source of truth.
    """

    def __init__(self) -> None:
        self._client: Optional[redis.Redis] = None
        self._available = False
        self._connect()

    def _connect(self) -> None:
        try:
            self._client = redis.Redis.from_url(
                settings.REDIS_URL,
                socket_connect_timeout=1,
                socket_timeout=1,
                decode_responses=True,
            )
            self._client.ping()
            self._available = True
            logger.info("Redis cache connected")
        except Exception as exc:
            self._available = False
            logger.warning("Redis unavailable, falling back to PostgreSQL: %s", exc)

    @property
    def available(self) -> bool:
        if not self._available:
            self._connect()
        return self._available

    def get_active_plates(self) -> Optional[set[str]]:
        """Return set of active hotlist plates, or None if cache miss/unavailable."""
        if not self.available:
            return None
        try:
            members = self._client.smembers(HOTLIST_CACHE_KEY)
            return set(members) if members else None
        except Exception as exc:
            self._available = False
            logger.warning("Redis read failed, falling back to DB: %s", exc)
            return None

    def set_active_plates(self, plates: set[str]) -> None:
        if not self.available:
            return
        try:
            pipe = self._client.pipeline()
            pipe.delete(HOTLIST_CACHE_KEY)
            if plates:
                pipe.sadd(HOTLIST_CACHE_KEY, *plates)
            pipe.expire(HOTLIST_CACHE_KEY, HOTLIST_TTL_SECONDS)
            pipe.execute()
        except Exception as exc:
            self._available = False
            logger.warning("Redis write failed: %s", exc)

    def add_active_plate(self, plate: str) -> None:
        if not self.available:
            return
        try:
            self._client.sadd(HOTLIST_CACHE_KEY, plate)
            self._client.expire(HOTLIST_CACHE_KEY, HOTLIST_TTL_SECONDS)
        except Exception as exc:
            self._available = False
            logger.warning("Redis add failed: %s", exc)

    def remove_active_plate(self, plate: str) -> None:
        if not self.available:
            return
        try:
            self._client.srem(HOTLIST_CACHE_KEY, plate)
        except Exception as exc:
            self._available = False
            logger.warning("Redis remove failed: %s", exc)

    def throttle(self, key: str, window_seconds: int) -> bool:
        """Return True if the action is allowed (under limit), False if throttled."""
        if not self.available:
            return True  # fail open — DB constraints still protect us
        try:
            rkey = f"rakshak:throttle:{key}"
            current = self._client.incr(rkey)
            if current == 1:
                self._client.expire(rkey, window_seconds)
            return current <= window_seconds
        except Exception:
            return True

    def cooldown(self, key: str, seconds: int) -> bool:
        """Return True if the caller may proceed now, and block repeats for `seconds`.

        Unlike throttle (a per-window count limit), this is a strict lock: only the
        first caller within the window is allowed.
        """
        if not self.available:
            return True  # fail open — DB remains source of truth
        try:
            rkey = f"rakshak:cooldown:{key}"
            return bool(self._client.set(rkey, "1", nx=True, ex=seconds))
        except Exception:
            return True

    def set_json(self, key: str, value: dict, ttl: int = 60) -> None:
        if not self.available:
            return
        try:
            self._client.setex(key, ttl, json.dumps(value))
        except Exception:
            pass

    def get_json(self, key: str) -> Optional[dict]:
        if not self.available:
            return None
        try:
            raw = self._client.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    # -- OD-flow pseudonyms ------------------------------------------------
    # The pseudonym is the *only* place a plate-derived identifier exists, it
    # lives in Redis only, and it expires. It exists so two sightings of the
    # same vehicle at consecutive cameras can be linked without ever storing
    # the plate or a stable hash of it in PostgreSQL.
    def set_od_state(self, pseudonym: str, value: dict, ttl: int) -> bool:
        if not self.available:
            return False
        try:
            self._client.setex(f"{OD_STATE_PREFIX}:{pseudonym}", ttl, json.dumps(value))
            return True
        except Exception as exc:
            logger.warning("OD state write failed: %s", exc)
            return False

    def get_od_state(self, pseudonym: str) -> Optional[dict]:
        if not self.available:
            return None
        try:
            raw = self._client.get(f"{OD_STATE_PREFIX}:{pseudonym}")
            return json.loads(raw) if raw else None
        except Exception:
            return None


cache_service = CacheService()

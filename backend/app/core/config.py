from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ``.env`` is written to the repository root (see scripts/start.ps1) but the
# API is launched with its working directory set to backend/, so a plain
# ``env_file=".env"`` silently resolved to backend/.env - which does not exist -
# and every setting fell back to its built-in default. Most visibly JWT_SECRET
# became "dev-secret", so the signing key depended on *where* the server was
# started: tokens minted by a root-started process were rejected by a
# backend-started one, which is exactly the intermittent 401 being chased.
BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent


def _env_files() -> list[str]:
    """Existing .env files, most specific first, regardless of the CWD."""
    candidates = [BACKEND_DIR / ".env", REPO_ROOT / ".env", Path.cwd() / ".env"]
    found: list[str] = []
    for candidate in candidates:
        text = str(candidate)
        if candidate.is_file() and text not in found:
            found.append(text)
    return found or ["backend/.env"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_files(), env_file_encoding="utf-8", extra="ignore"
    )

    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "rakshak"
    DATABASE_URL: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/rakshak"

    REDIS_URL: str = "redis://localhost:6379/0"

    JWT_SECRET: str = "dev-secret"
    JWT_ACCESS_MINUTES: int = 30
    JWT_REFRESH_DAYS: int = 7

    HOTLIST_CONFIRMATION_HOURS: int = 24
    DEMO_MODE: bool = True

    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:5174"

    # Allow any origin on a private/loopback address, not just the addresses
    # spelled out in CORS_ORIGINS.
    #
    # CORS_ORIGINS is read once at import time, so a laptop that moves to a new
    # Wi-Fi gets a new IP and every browser request from it is refused until the
    # backend is restarted with an updated list. Matching private ranges by regex
    # removes that whole failure mode: a new IP is accepted immediately, with no
    # restart and nothing to re-generate. Restricted to demo/dev because in
    # production the origin list must stay explicit.
    CORS_ALLOW_PRIVATE_NETWORK: bool = True

    STORAGE_BACKEND: str = "local"
    STORAGE_LOCAL_PATH: str = "./data/uploads"

    AI_MODEL_PATH: str = ""
    OCR_MODEL_PATH: str = ""

    API_BASE_URL: str = "http://localhost:8000"

    DETECTION_COOLDOWN_SECONDS: int = 60
    MAX_UPLOAD_MB: int = 5
    RATE_LIMIT_DETECTIONS_PER_MINUTE: int = 60
    RATE_LIMIT_AUTH_PER_MINUTE: int = 20

    # --- Maps: event bus -------------------------------------------------
    # "redis" (default, uses the Redis we already run) or "kafka". Kafka is only
    # ever selected when KAFKA_BOOTSTRAP_SERVERS is set, so a typo in EVENT_BUS
    # cannot silently send detection metadata to a broker nobody is running.
    EVENT_BUS: str = "redis"
    KAFKA_BOOTSTRAP_SERVERS: str = ""
    KAFKA_DETECTIONS_TOPIC: str = "rakshak.detections"
    DETECTIONS_STREAM: str = "rakshak.detections"
    DETECTIONS_DLQ_STREAM: str = "rakshak.detections.dlq"
    DETECTIONS_STREAM_MAXLEN: int = 50000
    DETECTIONS_CONSUMER_GROUP: str = "rakshak-analytics"
    # An event that fails this many times is parked on the dead-letter stream
    # instead of blocking the consumer group forever.
    DETECTIONS_MAX_ATTEMPTS: int = 3
    CONSUMER_BATCH_SIZE: int = 200
    CONSUMER_BLOCK_MS: int = 1000

    # --- Maps: analytics -------------------------------------------------
    ENABLE_OD_FLOW: bool = True
    OD_PSEUDONYM_TTL_SECONDS: int = 21600  # 6 h
    OD_PSEUDONYM_SECRET: str = ""
    HEAT_TAU_HOURS: float = 6.0
    GEO_CACHE_SECONDS: int = 15
    GEO_MAX_CELLS: int = 5000
    AGGREGATE_RETENTION_DAYS: int = 90
    MAX_PLAQUE_SPEED_KMH: float = 150.0
    DWELL_MINUTES: float = 5.0

    # Optional map-matching. Empty = disabled, so the public OSRM demo server is
    # never contacted unless the operator explicitly opts in.
    OSRM_URL: str = ""

    # --- Maps: edge workers ----------------------------------------------
    CAMERA_WORKERS_MAX: int = 2
    CAMERA_FPS: float = 3.0
    CAMERA_IMGSZ: int = 480
    CAMERA_DET_CONF: float = 0.35
    CAMERA_OFFLINE_AFTER_SECONDS: int = 300

    # Synthetic demo traffic. Unset (None) means "follow DEMO_MODE", so a plain
    # clone is never an empty map while tests can switch it off explicitly with
    # SIM_ENABLED=false.
    SIM_ENABLED: Optional[bool] = None
    SIM_BACKFILL_HOURS: int = 24
    SIM_RATE_HZ: float = 1.0

    # Map tiles for the dashboard.
    TILE_URL: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    TILE_ATTRIBUTION: str = "&copy; OpenStreetMap contributors"
    TILE_MAX_ZOOM: int = 19

    @field_validator("SIM_ENABLED", mode="before")
    @classmethod
    def _blank_sim_enabled(cls, v):
        """Treat an empty ``SIM_ENABLED=`` line as "unset" (follow DEMO_MODE)."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def sim_enabled(self) -> bool:
        """Synthetic demo traffic: explicit setting wins, else demo mode."""
        if self.SIM_ENABLED is not None:
            return self.SIM_ENABLED
        return self.DEMO_MODE

    @property
    def od_pseudonym_secret(self) -> str:
        """Secret for the OD-flow HMAC key. Falls back to JWT_SECRET.

        Rotated daily when used, so yesterday's pseudonyms cannot be joined to
        today's even if the Redis copy were readable.
        """
        return (self.OD_PSEUDONYM_SECRET or self.JWT_SECRET or "dev-od-secret").strip()

    @property
    def jwt_secret_is_weak_default(self) -> bool:
        """True when JWT_SECRET fell back to the built-in placeholder.

        ``.env`` is gitignored, so a fresh clone (or a deleted ``.env``)
        silently signs tokens with ``dev-secret``. If the secret then changes,
        every token the app is holding becomes invalid and every authenticated
        call fails with 401 "Invalid or expired token" - which looks like an app
        bug but is a config problem. Surfaced loudly at startup instead.
        """
        return self.JWT_SECRET.strip() in {"dev-secret", "", "changeme", "secret"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

if settings.jwt_secret_is_weak_default:
    import logging

    logging.getLogger("rakshak").warning(
        "JWT_SECRET is the built-in default. Set a fixed JWT_SECRET in .env, "
        "otherwise restarting with a different value invalidates every issued "
        "token and clients see 401 on all authenticated requests."
    )

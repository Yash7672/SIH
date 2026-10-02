from functools import lru_cache
from pathlib import Path
from typing import List

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

    STORAGE_BACKEND: str = "local"
    STORAGE_LOCAL_PATH: str = "./data/uploads"

    AI_MODEL_PATH: str = ""
    OCR_MODEL_PATH: str = ""

    API_BASE_URL: str = "http://localhost:8000"

    DETECTION_COOLDOWN_SECONDS: int = 60
    MAX_UPLOAD_MB: int = 5
    RATE_LIMIT_DETECTIONS_PER_MINUTE: int = 60
    RATE_LIMIT_AUTH_PER_MINUTE: int = 20

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

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

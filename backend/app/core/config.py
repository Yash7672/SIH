from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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

    DETECTION_COOLDOWN_SECONDS: int = 120
    MAX_UPLOAD_MB: int = 5
    RATE_LIMIT_DETECTIONS_PER_MINUTE: int = 60
    RATE_LIMIT_AUTH_PER_MINUTE: int = 20

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

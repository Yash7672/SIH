from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

import jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def _create_token(subject: str, expires_delta: timedelta, token_type: str, extra: Optional[dict] = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=ALGORITHM)


def create_access_token(user_id: UUID, role: str) -> str:
    return _create_token(
        str(user_id),
        timedelta(minutes=settings.JWT_ACCESS_MINUTES),
        "access",
        {"role": role},
    )


def create_refresh_token(user_id: UUID, role: str) -> str:
    return _create_token(
        str(user_id),
        timedelta(days=settings.JWT_REFRESH_DAYS),
        "refresh",
        {"role": role},
    )


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[ALGORITHM])

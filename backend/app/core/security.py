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


class TokenError(Exception):
    """Why a token was rejected, so the API can say which one.

    "Invalid or expired token" covered both a merely stale access token and a
    token signed with a different secret. Those need opposite responses - the
    first refreshes silently, the second means every stored token is dead and
    the user must log in again - so they must not share a message.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError(
            f"Invalid token ({type(exc).__name__}) - server secret may have changed"
        ) from exc

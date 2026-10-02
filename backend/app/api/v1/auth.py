from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import TokenError, create_access_token, create_refresh_token, decode_token, hash_password, verify_password
from app.db.session import get_db
from app.models import Role, User
from app.schemas.entities import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse, UserOut
from app.services.cache import cache_service

logger = get_logger(__name__)
router = APIRouter()

REGISTERABLE_ROLES = {Role.CITIZEN, Role.VOLUNTEER}


def _tokens(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user.id, user.role.value),
        refresh_token=create_refresh_token(user.id, user.role.value),
        user=UserOut(
            id=user.id,
            name=user.name,
            email=user.email,
            phone=user.phone,
            role=user.role.value,
            created_at=user.created_at,
        ),
    )


@router.post("/register", response_model=TokenResponse)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    if not cache_service.throttle(f"auth:register:{payload.email}", settings.RATE_LIMIT_AUTH_PER_MINUTE):
        raise HTTPException(status_code=429, detail="Too many requests")
    existing = db.execute(select(User).where(User.email == payload.email.lower())).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    try:
        role = Role(payload.role.upper())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role")
    if role not in REGISTERABLE_ROLES:
        raise HTTPException(status_code=403, detail="Only CITIZEN and VOLUNTEER can self-register")
    user = User(
        name=payload.name.strip(),
        email=payload.email.lower(),
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("User registered: role=%s", role.value)
    return _tokens(user)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    if not cache_service.throttle(f"auth:login:{payload.email}", settings.RATE_LIMIT_AUTH_PER_MINUTE):
        raise HTTPException(status_code=429, detail="Too many requests")
    user = db.execute(select(User).where(User.email == payload.email.lower())).scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not user.active:
        raise HTTPException(status_code=403, detail="Account deactivated")
    return _tokens(user)


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    try:
        data = decode_token(payload.refresh_token)
    except TokenError as exc:
        # Same distinction as the access path: a stale refresh token can be
        # replaced by logging in, a wrong-secret one means the stored session
        # is unrecoverable and must be cleared.
        raise HTTPException(status_code=401, detail=f"Refresh {exc.detail[0].lower()}{exc.detail[1:]}")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    if data.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Wrong token type")
    user = db.get(User, UUID(data["sub"]))
    if user is None or not user.active:
        raise HTTPException(status_code=401, detail="User not found")
    return _tokens(user)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return UserOut(
        id=user.id,
        name=user.name,
        email=user.email,
        phone=user.phone,
        role=user.role.value,
        created_at=user.created_at,
    )

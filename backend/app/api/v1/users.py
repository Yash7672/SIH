"""User administration: who exists, and how police accounts are created.

Police (COP) accounts cannot be self-registered - ``/auth/register`` only
accepts CITIZEN and VOLUNTEER - so this router is the only way one comes into
existence. Every write here requires ADMIN, and each one is written to the audit
log, because creating console access is exactly the kind of action that should
be attributable to a person afterwards.

Listing stays readable by COP (the console already uses it), but nothing that
changes an account is.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_admin, require_cop
from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import get_db
from app.models import Role, User
from app.schemas.entities import ActiveUpdateRequest, PasswordResetRequest, StaffCreateRequest, UserOut

logger = get_logger(__name__)

router = APIRouter()


def _out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        name=user.name,
        email=user.email,
        phone=user.phone,
        role=user.role.value,
        created_at=user.created_at,
        active=user.active,
    )


@router.get("", response_model=list[UserOut])
def list_users(_: User = Depends(require_cop), db: Session = Depends(get_db)):
    users = db.execute(select(User).order_by(User.created_at.desc())).scalars()
    return [_out(u) for u in users]


@router.post("/police", response_model=UserOut, status_code=201)
def create_police_account(
    payload: StaffCreateRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create a COP account for a police station: ADMIN only.

    The admin chooses the password and passes it to the station. The role is set
    here rather than taken from the request, so this endpoint can never mint an
    ADMIN account - a second admin has to be made deliberately, not by reusing
    this call with a different role string.
    """
    email = payload.email.lower()
    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        name=payload.name.strip(),
        email=email,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        role=Role.COP,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    from app.services.audit_service import log_action

    log_action(db, admin.id, "user.police_created", "user", str(user.id))
    logger.info("Police account created by admin=%s", admin.id)
    return _out(user)


@router.post("/{user_id}/active", response_model=UserOut)
def set_active(
    user_id: UUID,
    payload: ActiveUpdateRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Enable or disable an account. ADMIN only.

    An inactive user cannot sign in (login rejects it) and any token they hold
    stops resolving, so this is how an officer who leaves loses access.
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    # Disabling yourself would lock the last admin out of their own console, and
    # there is no recovery path from inside the app.
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")

    user.active = payload.active
    db.commit()
    db.refresh(user)

    from app.services.audit_service import log_action

    log_action(
        db,
        admin.id,
        "user.activated" if payload.active else "user.deactivated",
        "user",
        str(user.id),
    )
    return _out(user)


@router.post("/{user_id}/password", response_model=UserOut)
def reset_password(
    user_id: UUID,
    payload: PasswordResetRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Set a new password for a user who lost theirs. ADMIN only.

    Scoped to COP and ADMIN, and never to another ADMIN other than through a
    deliberate choice: resetting a citizen's password from the police console
    would be an account takeover, since nobody here can prove who owns it.
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role not in {Role.COP, Role.ADMIN}:
        raise HTTPException(
            status_code=403,
            detail="Passwords can only be reset for police and admin accounts",
        )

    user.password_hash = hash_password(payload.password)
    db.commit()
    db.refresh(user)

    from app.services.audit_service import log_action

    log_action(db, admin.id, "user.password_reset", "user", str(user.id))
    # The password itself is never logged or returned.
    return _out(user)

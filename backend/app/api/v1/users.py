from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_cop
from app.db.session import get_db
from app.models import Role, User
from app.schemas.entities import UserOut
from app.services.audit_service import log_action

router = APIRouter()


@router.get("", response_model=list[UserOut])
def list_users(_: User = Depends(require_cop), db: Session = Depends(get_db)):
    users = db.execute(select(User).order_by(User.created_at.desc())).scalars()
    return [
        UserOut(id=u.id, name=u.name, email=u.email, phone=u.phone, role=u.role.value, created_at=u.created_at)
        for u in users
    ]

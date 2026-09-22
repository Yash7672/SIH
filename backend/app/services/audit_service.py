import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog


def log_action(
    db: Session,
    actor_id: uuid.UUID | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    meta: dict | None = None,
) -> AuditLog:
    entry = AuditLog(
        id=uuid.uuid4(),
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        meta=meta,
        created_at=datetime.now(timezone.utc),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def recent_actions(db: Session, limit: int = 100) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    return list(db.execute(stmt).scalars())

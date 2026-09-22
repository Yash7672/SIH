from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin, require_volunteer
from app.db.session import get_db
from app.models import Device, User
from app.schemas.entities import DeviceOut, DeviceRegister
from app.services.audit_service import log_action

router = APIRouter()


@router.post("/register", response_model=DeviceOut)
def register_device(
    payload: DeviceRegister,
    user: User = Depends(require_volunteer),
    db: Session = Depends(get_db),
):
    device = Device(
        user_id=user.id,
        device_type=payload.device_type,
        device_name=payload.device_name,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    log_action(db, user.id, "device.registered", "device", str(device.id))
    return device


@router.get("", response_model=list[DeviceOut])
def list_devices(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return list(db.execute(select(Device).order_by(Device.created_at.desc())).scalars())


@router.post("/{device_id}/revoke", response_model=DeviceOut)
def revoke_device(
    device_id: UUID,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    device.revoked = True
    db.commit()
    db.refresh(device)
    log_action(db, user.id, "device.revoked", "device", str(device.id))
    return device

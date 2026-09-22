from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_admin, require_cop
from app.db.session import get_db
from app.models import Hotlist, Role, User
from app.schemas.entities import HotlistCreate, HotlistOut, HotlistPatch
from app.services.audit_service import log_action
from app.services.hotlist_service import HotlistService

router = APIRouter()


@router.get("", response_model=list[HotlistOut])
def list_hotlist(
    status_filter: str | None = None,
    _: User = Depends(require_cop),
    db: Session = Depends(get_db),
):
    stmt = select(Hotlist).order_by(Hotlist.added_at.desc())
    if status_filter:
        from app.models import HotlistStatus

        try:
            stmt = stmt.where(Hotlist.status == HotlistStatus(status_filter))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid status")
    return list(db.execute(stmt).scalars())


@router.post("", response_model=HotlistOut)
def create_hotlist(
    payload: HotlistCreate,
    user: User = Depends(require_cop),
    db: Session = Depends(get_db),
):
    svc = HotlistService(db)
    entry = svc.add_to_hotlist(payload.plate.upper(), complaint_id=payload.complaint_id, fir_reference=payload.fir_reference)
    log_action(db, user.id, "hotlist.created", "hotlist", str(entry.id), {"plate": entry.plate})
    return entry


@router.get("/{hotlist_id}", response_model=HotlistOut)
def get_hotlist(
    hotlist_id: UUID,
    _: User = Depends(require_cop),
    db: Session = Depends(get_db),
):
    entry = db.get(Hotlist, hotlist_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    return entry


@router.patch("/{hotlist_id}", response_model=HotlistOut)
def patch_hotlist(
    hotlist_id: UUID,
    payload: HotlistPatch,
    user: User = Depends(require_cop),
    db: Session = Depends(get_db),
):
    svc = HotlistService(db)
    entry = db.get(Hotlist, hotlist_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    if payload.status:
        if payload.status == "FIR_CONFIRMED":
            entry = svc.confirm_fir(hotlist_id, payload.fir_reference or entry.fir_reference or "FIR-PENDING")
            log_action(db, user.id, "hotlist.fir_confirmed", "hotlist", str(hotlist_id))
        elif payload.status == "RECOVERED":
            entry = svc.mark_recovered(hotlist_id)
            log_action(db, user.id, "hotlist.recovered", "hotlist", str(hotlist_id))
        elif payload.status == "CLOSED":
            entry = svc.close(hotlist_id)
            log_action(db, user.id, "hotlist.closed", "hotlist", str(hotlist_id))
        else:
            raise HTTPException(status_code=400, detail="Unsupported status transition")
    return entry


@router.delete("/{hotlist_id}")
def delete_hotlist(
    hotlist_id: UUID,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    entry = db.get(Hotlist, hotlist_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    from app.services.cache import cache_service

    cache_service.remove_active_plate(entry.plate)
    db.delete(entry)
    db.commit()
    log_action(db, user.id, "hotlist.deleted", "hotlist", str(hotlist_id))
    return {"deleted": True}

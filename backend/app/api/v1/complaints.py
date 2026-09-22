from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_citizen, require_cop
from app.core.config import settings
from app.db.session import get_db
from app.models import Complaint, ComplaintStatus, Role, User
from app.schemas.entities import ComplaintAction, ComplaintOut
from app.services.audit_service import log_action
from app.services.plate import normalize_plate

router = APIRouter()


@router.post("", response_model=ComplaintOut)
def create_complaint(
    plate: str = Form(...),
    complaint_type: str = Form(...),
    description: str = Form(""),
    proof: UploadFile | None = File(default=None),
    user: User = Depends(require_citizen),
    db: Session = Depends(get_db),
):
    norm = normalize_plate(plate)
    if not norm.valid:
        raise HTTPException(status_code=422, detail="Invalid Indian-style plate number")

    proof_path = None
    if proof is not None and proof.filename:
        allowed = {".jpg", ".jpeg", ".png", ".pdf"}
        ext = ("." + proof.filename.rsplit(".", 1)[-1].lower()) if "." in proof.filename else ""
        if ext not in allowed:
            raise HTTPException(status_code=415, detail="Unsupported proof file type")
        content = proof.file.read(settings.MAX_UPLOAD_MB * 1024 * 1024 + 1)
        if len(content) > settings.MAX_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"File exceeds {settings.MAX_UPLOAD_MB}MB limit")
        import os
        import uuid as uuid_mod

        os.makedirs(settings.STORAGE_LOCAL_PATH, exist_ok=True)
        safe_name = f"{uuid_mod.uuid4().hex}{ext}"
        proof_path = os.path.join(settings.STORAGE_LOCAL_PATH, safe_name)
        with open(proof_path, "wb") as f:
            f.write(content)

    complaint = Complaint(
        user_id=user.id,
        plate=norm.normalized,
        complaint_type=complaint_type.strip()[:60],
        description=(description or "").strip()[:2000] or None,
        proof_path=proof_path,
        status=ComplaintStatus.PENDING,
    )
    db.add(complaint)
    db.commit()
    db.refresh(complaint)
    log_action(db, user.id, "complaint.created", "complaint", str(complaint.id), {"plate": norm.normalized})
    return complaint


@router.get("/mine", response_model=list[ComplaintOut])
def my_complaints(user: User = Depends(require_citizen), db: Session = Depends(get_db)):
    stmt = select(Complaint).where(Complaint.user_id == user.id).order_by(Complaint.created_at.desc())
    return list(db.execute(stmt).scalars())


@router.get("", response_model=list[ComplaintOut])
def all_complaints(
    status_filter: str | None = None,
    _: User = Depends(require_cop),
    db: Session = Depends(get_db),
):
    stmt = select(Complaint).order_by(Complaint.created_at.desc())
    if status_filter:
        try:
            stmt = stmt.where(Complaint.status == ComplaintStatus(status_filter))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid status filter")
    return list(db.execute(stmt).scalars())


@router.get("/{complaint_id}", response_model=ComplaintOut)
def get_complaint(
    complaint_id: UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    complaint = db.get(Complaint, complaint_id)
    if complaint is None:
        raise HTTPException(status_code=404, detail="Complaint not found")
    if user.role in (Role.CITIZEN,) and complaint.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your complaint")
    return complaint


@router.post("/{complaint_id}/verify")
def verify_complaint(
    complaint_id: UUID,
    _: User = Depends(require_cop),
    db: Session = Depends(get_db),
    payload: ComplaintAction | None = None,
):
    from app.services.hotlist_service import HotlistService

    complaint = db.get(Complaint, complaint_id)
    if complaint is None:
        raise HTTPException(status_code=404, detail="Complaint not found")
    if complaint.status in (ComplaintStatus.HOTLISTED, ComplaintStatus.CLOSED):
        raise HTTPException(status_code=409, detail=f"Complaint already {complaint.status.value}")

    complaint.status = ComplaintStatus.VERIFIED
    db.commit()

    fir_ref = payload.fir_reference if payload else None
    entry = HotlistService(db).add_to_hotlist(complaint.plate, complaint_id=complaint.id, fir_reference=fir_ref)
    complaint.status = ComplaintStatus.HOTLISTED
    db.commit()
    db.refresh(complaint)
    log_action(db, None, "complaint.verified_hotlisted", "complaint", str(complaint.id), {"plate": complaint.plate})
    return {"complaint": ComplaintOut.model_validate(complaint), "hotlist_id": str(entry.id)}


@router.post("/{complaint_id}/reject")
def reject_complaint(
    complaint_id: UUID,
    user: User = Depends(require_cop),
    db: Session = Depends(get_db),
):
    complaint = db.get(Complaint, complaint_id)
    if complaint is None:
        raise HTTPException(status_code=404, detail="Complaint not found")
    if complaint.status not in (ComplaintStatus.PENDING, ComplaintStatus.UNDER_REVIEW):
        raise HTTPException(status_code=409, detail=f"Cannot reject complaint in status {complaint.status.value}")
    complaint.status = ComplaintStatus.REJECTED
    db.commit()
    db.refresh(complaint)
    log_action(db, user.id, "complaint.rejected", "complaint", str(complaint.id))
    return complaint

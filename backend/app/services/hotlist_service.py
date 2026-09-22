from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Complaint, ComplaintStatus, Hotlist, HotlistStatus, Role, User
from app.core.config import settings
from app.services.cache import cache_service


class HotlistService:
    def __init__(self, db: Session):
        self.db = db

    def get_active_by_plate(self, plate: str) -> Optional[Hotlist]:
        stmt = (
            select(Hotlist)
            .where(Hotlist.plate == plate, Hotlist.status.in_([HotlistStatus.ACTIVE, HotlistStatus.FIR_CONFIRMED]))
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def is_plate_hotlisted(self, plate: str) -> bool:
        # 1) Try Redis cache
        cached = cache_service.get_active_plates()
        if cached is not None:
            return plate in cached
        # 2) Fallback: query PostgreSQL (source of truth)
        return self.get_active_by_plate(plate) is not None

    def rebuild_cache(self) -> int:
        rows = self.db.execute(
            select(Hotlist.plate).where(Hotlist.status.in_([HotlistStatus.ACTIVE, HotlistStatus.FIR_CONFIRMED]))
        ).scalars().all()
        plates = set(rows)
        cache_service.set_active_plates(plates)
        return len(plates)

    def add_to_hotlist(
        self,
        plate: str,
        complaint_id: Optional[UUID] = None,
        fir_reference: Optional[str] = None,
    ) -> Hotlist:
        now = datetime.now(timezone.utc)
        expiry = None
        if settings.HOTLIST_CONFIRMATION_HOURS:
            from datetime import timedelta

            expiry = now + timedelta(hours=settings.HOTLIST_CONFIRMATION_HOURS)
        entry = Hotlist(
            plate=plate,
            complaint_id=complaint_id,
            status=HotlistStatus.ACTIVE,
            added_at=now,
            expiry_at=expiry,
            fir_reference=fir_reference,
        )
        self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)
        cache_service.add_active_plate(plate)
        return entry

    def confirm_fir(self, hotlist_id: UUID, fir_reference: str) -> Hotlist:
        entry = self.db.get(Hotlist, hotlist_id)
        if not entry:
            raise ValueError("Hotlist entry not found")
        entry.status = HotlistStatus.FIR_CONFIRMED
        entry.fir_reference = fir_reference
        entry.fir_verified_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(entry)
        cache_service.add_active_plate(entry.plate)
        return entry

    def mark_recovered(self, hotlist_id: UUID) -> Hotlist:
        entry = self.db.get(Hotlist, hotlist_id)
        if not entry:
            raise ValueError("Hotlist entry not found")
        entry.status = HotlistStatus.RECOVERED
        entry.recovered_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(entry)
        cache_service.remove_active_plate(entry.plate)
        return entry

    def close(self, hotlist_id: UUID) -> Hotlist:
        entry = self.db.get(Hotlist, hotlist_id)
        if not entry:
            raise ValueError("Hotlist entry not found")
        entry.status = HotlistStatus.CLOSED
        self.db.commit()
        self.db.refresh(entry)
        cache_service.remove_active_plate(entry.plate)
        return entry

    def expire_due_entries(self) -> int:
        now = datetime.now(timezone.utc)
        stmt = select(Hotlist).where(
            Hotlist.status == HotlistStatus.ACTIVE,
            Hotlist.expiry_at.isnot(None),
            Hotlist.expiry_at < now,
            Hotlist.fir_verified_at.is_(None),
        )
        due = self.db.execute(stmt).scalars().all()
        for entry in due:
            entry.status = HotlistStatus.EXPIRED
            cache_service.remove_active_plate(entry.plate)
        if due:
            self.db.commit()
        return len(due)

    def verify_complaint(self, complaint_id: UUID) -> tuple[Complaint, Hotlist]:
        complaint = self.db.get(Complaint, complaint_id)
        if not complaint:
            raise ValueError("Complaint not found")
        complaint.status = ComplaintStatus.HOTLISTED
        self.db.commit()
        entry = self.add_to_hotlist(complaint.plate, complaint_id=complaint.id)
        return complaint, entry

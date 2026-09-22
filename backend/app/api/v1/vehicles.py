from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_cop
from app.db.session import get_db
from app.models import Complaint, Hotlist, Sighting, User
from app.services.plate import normalize_plate

router = APIRouter()


@router.get("/{plate}")
def vehicle_detail(plate: str, _: User = Depends(require_cop), db: Session = Depends(get_db)):
    norm = normalize_plate(plate)
    if not norm.valid:
        raise HTTPException(status_code=422, detail="Invalid plate")
    entry = db.execute(
        select(Hotlist).where(Hotlist.plate == norm.normalized).order_by(Hotlist.added_at.desc())
    ).scalars().first()
    if entry is None:
        raise HTTPException(status_code=404, detail="Vehicle not in hotlist")
    complaint = db.get(Complaint, entry.complaint_id) if entry.complaint_id else None
    sightings = list(
        db.execute(
            select(Sighting).where(Sighting.hotlist_id == entry.id).order_by(Sighting.detected_at.asc())
        ).scalars()
    )
    return {
        "plate": entry.plate,
        "hotlist": {
            "id": str(entry.id),
            "status": entry.status.value,
            "added_at": entry.added_at.isoformat(),
            "expiry_at": entry.expiry_at.isoformat() if entry.expiry_at else None,
            "fir_reference": entry.fir_reference,
            "fir_verified_at": entry.fir_verified_at.isoformat() if entry.fir_verified_at else None,
            "recovered_at": entry.recovered_at.isoformat() if entry.recovered_at else None,
            "last_seen_at": entry.last_seen_at.isoformat() if entry.last_seen_at else None,
            "last_seen_lat": entry.last_seen_lat,
            "last_seen_lng": entry.last_seen_lng,
        },
        "complaint": {
            "id": str(complaint.id),
            "type": complaint.complaint_type,
            "description": complaint.description,
            "status": complaint.status.value,
            "created_at": complaint.created_at.isoformat(),
        }
        if complaint
        else None,
        "sightings_count": len(sightings),
    }


@router.get("/{plate}/timeline")
def vehicle_timeline(plate: str, _: User = Depends(require_cop), db: Session = Depends(get_db)):
    norm = normalize_plate(plate)
    entry = db.execute(
        select(Hotlist).where(Hotlist.plate == norm.normalized).order_by(Hotlist.added_at.desc())
    ).scalars().first()
    if entry is None:
        raise HTTPException(status_code=404, detail="Vehicle not in hotlist")
    sightings = db.execute(
        select(Sighting).where(Sighting.hotlist_id == entry.id).order_by(Sighting.detected_at.asc())
    ).scalars().all()
    return [
        {
            "id": str(s.id),
            "latitude": s.latitude,
            "longitude": s.longitude,
            "detected_at": s.detected_at.isoformat(),
            "confidence": s.confidence,
            "device_id": str(s.device_id),
        }
        for s in sightings
    ]


@router.get("/{plate}/route")
def vehicle_route(plate: str, _: User = Depends(require_cop), db: Session = Depends(get_db)):
    """Chronological route: list of [lat, lng] for polyline drawing."""
    norm = normalize_plate(plate)
    entry = db.execute(
        select(Hotlist).where(Hotlist.plate == norm.normalized).order_by(Hotlist.added_at.desc())
    ).scalars().first()
    if entry is None:
        raise HTTPException(status_code=404, detail="Vehicle not in hotlist")
    sightings = db.execute(
        select(Sighting).where(Sighting.hotlist_id == entry.id).order_by(Sighting.detected_at.asc())
    ).scalars().all()
    return {
        "plate": entry.plate,
        "route": [[s.latitude, s.longitude] for s in sightings],
        "points": [
            {
                "lat": s.latitude,
                "lng": s.longitude,
                "time": s.detected_at.isoformat(),
                "confidence": s.confidence,
                "device_id": str(s.device_id),
            }
            for s in sightings
        ],
    }

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_cop
from app.db.session import get_db
from app.models import Complaint, Hotlist, Sighting, User
from app.services.plate import NormalizedPlate, normalize_plate, plate_spellings

router = APIRouter()


def _newest_entry(db: Session, norm: NormalizedPlate) -> Hotlist | None:
    return db.execute(
        select(Hotlist).where(Hotlist.plate.in_(plate_spellings(norm.normalized))).order_by(Hotlist.added_at.desc())
    ).scalars().first()


def _plate_sightings(db: Session, norm: NormalizedPlate) -> list[Sighting]:
    """Every sighting ever recorded for this plate, oldest first.

    A plate can own more than one hot-list row: a case is closed or recovered and
    a later complaint re-lists the same car, and the test scripts that borrow the
    fixture plate leave a row behind each run. The dashboard is tracking the
    *vehicle*, so its route must span those rows - filtering by the newest row's
    id alone left the demo car with an empty route and no "last seen" the moment
    it was re-listed.
    """
    return list(
        db.execute(
            select(Sighting)
            .join(Hotlist, Sighting.hotlist_id == Hotlist.id)
            .where(Hotlist.plate.in_(plate_spellings(norm.normalized)))
            .order_by(Sighting.detected_at.asc())
        ).scalars()
    )


@router.get("/{plate}")
def vehicle_detail(plate: str, _: User = Depends(require_cop), db: Session = Depends(get_db)):
    norm = normalize_plate(plate)
    if not norm.valid:
        raise HTTPException(status_code=422, detail="Invalid plate")
    entry = _newest_entry(db, norm)
    if entry is None:
        raise HTTPException(status_code=404, detail="Vehicle not in hotlist")
    complaint = db.get(Complaint, entry.complaint_id) if entry.complaint_id else None
    sightings = _plate_sightings(db, norm)
    # "Last seen" falls back to the newest sighting: a row created by a fresh
    # complaint has no last_seen of its own yet, but the vehicle has still been
    # seen.
    last = sightings[-1] if sightings else None
    last_seen_at = entry.last_seen_at or (last.detected_at if last else None)
    last_seen_lat = entry.last_seen_lat if entry.last_seen_lat is not None else (last.latitude if last else None)
    last_seen_lng = entry.last_seen_lng if entry.last_seen_lng is not None else (last.longitude if last else None)
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
            "last_seen_at": last_seen_at.isoformat() if last_seen_at else None,
            "last_seen_lat": last_seen_lat,
            "last_seen_lng": last_seen_lng,
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
    if _newest_entry(db, norm) is None:
        raise HTTPException(status_code=404, detail="Vehicle not in hotlist")
    sightings = _plate_sightings(db, norm)
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
    entry = _newest_entry(db, norm)
    if entry is None:
        raise HTTPException(status_code=404, detail="Vehicle not in hotlist")
    sightings = _plate_sightings(db, norm)
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

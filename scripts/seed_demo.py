"""Seed realistic demo data for the RAKSHAK dashboards.

Idempotent: rows are keyed by plate (and complaint type), so re-running only
fills in what is missing. Sightings for a hotlist entry are spread across the
previous few days so the analytics charts and the vehicle route have shape.

Usage:
    python scripts/seed_demo.py
Run against the backend DB (`rakshak`) — safe to re-run anytime.
"""

import sys
import uuid
from datetime import timedelta

sys.path.append("backend")

from app.db.session import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Complaint,
    ComplaintStatus,
    Device,
    Hotlist,
    HotlistStatus,
    Role,
    Sighting,
    User,
    utcnow,
)
from app.services.plate import normalize_plate  # noqa: E402

FIR_WINDOW_HOURS = 6
BASE_LAT, BASE_LNG = 17.44, 78.35

# plate, complaint type, description, hotlist status, fir ref, sightings, day span
DEMO_CASES = [
    (
        "TS09AB1234",
        "Stolen vehicle",
        "Vehicle missing since Sunday near Ameerpet; owner reported it at the local station.",
        HotlistStatus.ACTIVE,
        None,
        8,
        3,
    ),
    (
        "MH12JK4567",
        "Hit and run",
        "Hit-and-run suspect vehicle fled the scene near Banjara Hills; FIR registered.",
        HotlistStatus.FIR_CONFIRMED,
        "FIR/2026/0102",
        6,
        2,
    ),
    (
        "DL8CBF4890",
        "Stolen vehicle",
        "Stolen cab reported missing from the airport parking lot.",
        HotlistStatus.ACTIVE,
        None,
        5,
        2,
    ),
    (
        "UP32XA1010",
        "Fraud / forged plate",
        "Plate used in a fuel fraud incident; multiple sightings reported by petrol pumps.",
        HotlistStatus.ACTIVE,
        None,
        4,
        1,
    ),
    # complaints that are still in the police queue (no hotlist entry yet)
    (
        "KA01MJ9801",
        "Snatching",
        "Chain snatching; assailants fled on a scooter with this plate.",
        None,
        None,
        0,
        0,
    ),
    (
        "TN22CD7788",
        "Stolen vehicle",
        "Two-wheeler stolen from a college parking lot overnight.",
        None,
        None,
        0,
        0,
    ),
    (
        "GJ05XY3321",
        "Missing vehicle",
        "Owner-reported missing vehicle, currently under verification.",
        None,
        None,
        0,
        0,
    ),
    (
        "RJ14KL9090",
        "Stolen vehicle",
        "Complaint rejected — plate did not match the FIR documents.",
        "REJECTED",
        None,
        0,
        0,
    ),
]

PENDING_STATUS = {
    "KA01MJ9801": ComplaintStatus.PENDING,
    "TN22CD7788": ComplaintStatus.PENDING,
    "GJ05XY3321": ComplaintStatus.UNDER_REVIEW,
    "RJ14KL9090": ComplaintStatus.REJECTED,
}


def user_role(db, role: Role):
    return db.query(User).filter(User.role == role).order_by(User.created_at.desc()).first()


def ensure_demo_devices(db) -> list[Device]:
    """A handful of volunteer capture devices so analytics/admiral views have data."""
    volunteer = user_role(db, Role.VOLUNTEER)
    if not volunteer:
        raise SystemExit("no volunteer user found — start the backend once to seed users")

    specs = [
        ("Demo-NandiCam-01", "mobile"),
        ("Demo-NandiCam-02", "mobile"),
        ("Demo-JunctionCam-03", "fixed"),
    ]
    devices: list[Device] = []
    for name, dtype in specs:
        existing = db.query(Device).filter(Device.device_name == name).first()
        if existing:
            devices.append(existing)
            continue
        dev = Device(user_id=volunteer.id, device_type=dtype, device_name=name)
        db.add(dev)
        db.commit()
        db.refresh(dev)
        print(f"created demo device {name}")
        devices.append(dev)
    return devices


def ensure_recovery_case(db) -> None:
    existing = db.query(Hotlist).filter(Hotlist.status == HotlistStatus.RECOVERED).first()
    if existing:
        return
    now = utcnow()
    db.add(
        Hotlist(
            plate="KA01AB9999",
            status=HotlistStatus.RECOVERED,
            added_at=now - timedelta(days=3),
            recovered_at=now - timedelta(hours=5),
            fir_reference="FIR/2026/0001",
            last_seen_lat=BASE_LAT,
            last_seen_lng=BASE_LNG,
        )
    )
    db.commit()
    print("created RECOVERED demo case (KA01AB9999)")


def seed_route(db, entry: Hotlist, devices: list[Device], count: int, days: int) -> None:
    """Create a chronological route of `count` sightings spread over `days` days."""
    now = utcnow()
    window = timedelta(days=days) if days else timedelta(hours=12)
    step = window / max(count, 1)

    for k in range(count):
        t = now - window + step * k + step / 2
        # a plausible drivable path: mostly north-west with a little jitter
        lat = BASE_LAT + k * 0.0075 + (0.0012 if k % 3 == 0 else -0.0009)
        lng = BASE_LNG + k * -0.0090 + (0.0011 if k % 2 == 0 else -0.0007)
        device = devices[k % len(devices)]
        db.add(
            Sighting(
                id=uuid.uuid4(),
                hotlist_id=entry.id,
                device_id=device.id,
                latitude=round(lat, 6),
                longitude=round(lng, 6),
                detected_at=t,
                confidence=round(0.84 + (k % 5) * 0.03, 2),
                created_at=t,
            )
        )
        device.last_seen_at = t
        entry.last_seen_at = t
        entry.last_seen_lat = round(lat, 6)
        entry.last_seen_lng = round(lng, 6)
    db.commit()
    print(f"seeded {count} route sightings for {entry.plate} over {days} day(s)")


def main() -> None:
    db = SessionLocal()
    try:
        if not user_role(db, Role.COP):
            raise SystemExit("no COP user found — start the backend once to seed users")

        devices = ensure_demo_devices(db)
        ensure_recovery_case(db)
        citizen = user_role(db, Role.CITIZEN)
        now = utcnow()

        for plate, ctype, desc, hotlist_status, fir_ref, n_sightings, days in DEMO_CASES:
            norm = normalize_plate(plate)
            if not norm.valid:
                print(f"skip invalid plate spec {plate}")
                continue

            on_hotlist = hotlist_status not in (None, "REJECTED")
            complaint = (
                db.query(Complaint)
                .filter(Complaint.plate == norm.normalized, Complaint.complaint_type == ctype)
                .first()
            )
            if complaint is None:
                if on_hotlist:
                    status = ComplaintStatus.HOTLISTED
                elif hotlist_status == "REJECTED":
                    status = ComplaintStatus.REJECTED
                else:
                    status = PENDING_STATUS.get(norm.normalized, ComplaintStatus.PENDING)
                complaint = Complaint(
                    user_id=citizen.id,
                    plate=norm.normalized,
                    complaint_type=ctype,
                    description=desc,
                    status=status,
                    created_at=now - timedelta(days=max(days, 1), hours=4),
                    updated_at=now - timedelta(days=max(days, 1), hours=3),
                )
                db.add(complaint)
                db.commit()
                db.refresh(complaint)
                print(f"created complaint {norm.normalized} ({status.value})")

            if not on_hotlist:
                continue

            # Keep the linked complaint consistent with the hotlist state.
            if complaint.status != ComplaintStatus.HOTLISTED:
                complaint.status = ComplaintStatus.HOTLISTED
                db.commit()
                print(f"marked complaint {norm.normalized} as HOTLISTED")

            entry = db.query(Hotlist).filter(Hotlist.plate == norm.normalized).first()
            if entry is not None and entry.status != hotlist_status:
                # converge an older entry onto the declared demo state
                entry.status = hotlist_status
                if fir_ref:
                    entry.fir_reference = fir_ref
                    entry.fir_verified_at = entry.fir_verified_at or (entry.added_at + timedelta(hours=2))
                db.commit()
                print(f"updated hotlist {norm.normalized} -> {hotlist_status}")
            if entry is None:
                added = now - timedelta(days=days or 1)
                entry = Hotlist(
                    plate=norm.normalized,
                    complaint_id=complaint.id,
                    status=hotlist_status,
                    added_at=added,
                    expiry_at=None if hotlist_status == HotlistStatus.FIR_CONFIRMED
                    else now + timedelta(hours=FIR_WINDOW_HOURS),
                    fir_reference=fir_ref,
                    fir_verified_at=added + timedelta(hours=2) if fir_ref else None,
                    last_seen_lat=BASE_LAT,
                    last_seen_lng=BASE_LNG,
                    last_seen_at=now - timedelta(minutes=30),
                )
                db.add(entry)
                db.commit()
                db.refresh(entry)
                print(f"created hotlist {norm.normalized} ({hotlist_status})")

            if n_sightings and db.query(Sighting).filter(Sighting.hotlist_id == entry.id).count() == 0:
                seed_route(db, entry, devices, n_sightings, days)

        # Report what the dashboards will show.
        active = db.query(Hotlist).filter(
            Hotlist.status.in_([HotlistStatus.ACTIVE, HotlistStatus.FIR_CONFIRMED])
        ).count()
        print(
            f"\ndemo seed complete: {db.query(Hotlist).count()} hotlist entries "
            f"({active} active), {db.query(Complaint).count()} complaints, "
            f"{db.query(Sighting).count()} sightings, {db.query(Device).count()} devices"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()

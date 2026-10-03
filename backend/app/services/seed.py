from datetime import datetime, timezone
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import Camera, Device, User, Role, utcnow
from app.db.session import engine, SessionLocal
from app.core.security import hash_password


HYDERABAD_CAMERAS = [
    {"name": "LB Nagar Junction", "lat": 17.3498, "lng": 78.5530, "source_type": "SYNTHETIC"},
    {"name": "Dilsukhnagar", "lat": 17.3650, "lng": 78.5220, "source_type": "SYNTHETIC"},
    {"name": "Nagole", "lat": 17.3730, "lng": 78.5570, "source_type": "SYNTHETIC"},
    {"name": "Uppal", "lat": 17.3980, "lng": 78.5530, "source_type": "SYNTHETIC"},
    {"name": "Kothapet", "lat": 17.3780, "lng": 78.5430, "source_type": "SYNTHETIC"},
    {"name": "Hayathnagar", "lat": 17.3320, "lng": 78.5940, "source_type": "SYNTHETIC"},
    {"name": "Charminar", "lat": 17.3610, "lng": 78.4740, "source_type": "SYNTHETIC"},
    {"name": "Saroornagar", "lat": 17.3550, "lng": 78.5270, "source_type": "SYNTHETIC"},
]


def _ensure_device_for_camera(db: Session, camera: Camera, api_key_hash: Optional[str] = None):
    existing = db.execute(
        select(Device).where(Device.device_type == "CAMERA", Device.device_name == camera.name)
    ).scalar_one_or_none()
    if existing:
        return existing
    d = Device(
        id=uuid.uuid4(),
        user_id=None,
        device_type="CAMERA",
        device_name=camera.name,
        api_key_hash=api_key_hash,
        revoked=False,
        last_seen_at=None,
        created_at=utcnow(),
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def seed_cameras(db: Session, force: bool = False) -> int:
    created = 0
    for c in HYDERABAD_CAMERAS:
        existing = db.execute(select(Camera).where(Camera.name == c["name"])).scalar_one_or_none()
        if existing:
            continue
        cam = Camera(
            id=uuid.uuid4(),
            name=c["name"],
            lat=c["lat"],
            lng=c["lng"],
            heading_deg=c.get("heading_deg"),
            source_type=c["source_type"],
            source_uri=c.get("source_uri"),
            status="OFFLINE",
            last_seen_at=None,
            created_at=utcnow(),
        )
        db.add(cam)
        db.commit()
        db.refresh(cam)
        _ensure_device_for_camera(db, cam)
        created += 1
    return created


def seed_demo_users(db: Session | None = None) -> None:
    """Ensure demo users exist (CITIZEN, COP, VOLUNTEER, ADMIN). Idempotent."""
    close = False
    if db is None:
        db = SessionLocal()
        close = True
    try:
        users = [
            ("citizen@example.com", "Citizen@123", Role.CITIZEN, "Citizen"),
            ("cop@example.com", "Police@123", Role.COP, "Police"),
            ("volunteer@example.com", "Volunteer@123", Role.VOLUNTEER, "Volunteer"),
            ("admin@example.com", "Admin@123", Role.ADMIN, "Admin"),
        ]
        for email, pw, role, name in users:
            existing = db.execute(select(User).where(User.email == email.lower())).scalar_one_or_none()
            if existing:
                continue
            u = User(
                name=name,
                email=email.lower(),
                password_hash=hash_password(pw),
                role=role,
                active=True,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            db.add(u)
        db.commit()
    finally:
        if close:
            db.close()


def main():
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        seed_demo_users(db)
        n = seed_cameras(db)
        print(f"Seeded {n} cameras; demo users ensured")
    finally:
        db.close()


if __name__ == "__main__":
    main()

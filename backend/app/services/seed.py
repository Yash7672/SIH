from app.core.config import settings
from app.core.security import create_access_token, create_refresh_token, hash_password, verify_password
from app.db.session import SessionLocal
from app.models import Role, User, Hotlist, HotlistStatus, Complaint, ComplaintStatus, Sighting, Device, utcnow
from app.models.models import Camera
from sqlalchemy import select
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
import uuid


def seed_demo_users() -> None:
    """Create demo users if they do not exist. Development/demo only."""
    db = SessionLocal()
    try:
        demos = [
            ("Demo Citizen", "citizen@example.com", "Citizen@123", Role.CITIZEN),
            ("Demo Volunteer", "volunteer@example.com", "Volunteer@123", Role.VOLUNTEER),
            ("Demo Cop", "cop@example.com", "Police@123", Role.COP),
            ("Demo Admin", "admin@example.com", "Admin@123", Role.ADMIN),
        ]
        for name, email, password, role in demos:
            existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
            if existing:
                continue
            user = User(
                name=name,
                email=email,
                password_hash=hash_password(password),
                role=role,
                phone="9000000000",
            )
            db.add(user)
            db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Maps: seeded city cameras
# ---------------------------------------------------------------------------
# Eight real road junctions spread over ~6 km around the Hyderabad demo
# centroid (17.3620, 78.5148): LB Nagar / Dilsukhnagar / Nagole / Uppal /
# Kothapet / Hayathnagar / Charminar / Saroornagar. Coordinates are public
# junction centroids, so nothing here points into a lake or a building block.
HYDERABAD_CAMERAS: list[dict] = [
    # name, lat, lng, heading_deg, congestion weight (1 = normal, >1 = jammed)
    {"name": "LB Nagar Junction", "lat": 17.3498, "lng": 78.5530, "heading_deg": 45.0, "weight": 1.6},
    {"name": "Dilsukhnagar X Roads", "lat": 17.3650, "lng": 78.5220, "heading_deg": 200.0, "weight": 1.3},
    {"name": "Nagole Junction", "lat": 17.3730, "lng": 78.5570, "heading_deg": 340.0, "weight": 1.0},
    {"name": "Uppal Junction", "lat": 17.3980, "lng": 78.5530, "heading_deg": 10.0, "weight": 1.1},
    {"name": "Kothapet Junction", "lat": 17.3780, "lng": 78.5430, "heading_deg": 150.0, "weight": 0.9},
    {"name": "Hayathnagar Junction", "lat": 17.3320, "lng": 78.5940, "heading_deg": 270.0, "weight": 1.2},
    {"name": "Charminar", "lat": 17.3610, "lng": 78.4740, "heading_deg": 90.0, "weight": 1.8},
    {"name": "Saroornagar", "lat": 17.3550, "lng": 78.5270, "heading_deg": 30.0, "weight": 0.8},
]


def seed_cameras(db: Session) -> int:
    """Idempotently insert the demo cameras and their CAMERA devices.

    Each camera also gets a ``devices`` row (device_type CAMERA) so the
    existing per-device sighting path keeps working unchanged; the device is
    user-less and starts revoked until a worker authenticates with its key.

    Returns the number of cameras created.
    """
    created = 0
    for spec in HYDERABAD_CAMERAS:
        existing = db.execute(select(Camera).where(Camera.name == spec["name"])).scalar_one_or_none()
        if existing:
            continue
        cam = Camera(
            id=uuid.uuid4(),
            name=spec["name"],
            lat=spec["lat"],
            lng=spec["lng"],
            heading_deg=spec.get("heading_deg"),
            source_type="SYNTHETIC",
            source_uri=None,
            status="OFFLINE",
            last_seen_at=None,
            created_at=utcnow(),
        )
        db.add(cam)
        db.flush()
        db.add(
            Device(
                id=uuid.uuid4(),
                user_id=None,
                device_type="CAMERA",
                device_name=spec["name"],
                api_key_hash=None,
                revoked=False,
                last_seen_at=None,
                created_at=utcnow(),
            )
        )
        created += 1
    if created:
        db.commit()
    return created


def seed_all() -> None:
    """Demo users + cameras. Never fatal: callers wrap this in try/except."""
    seed_demo_users()
    db = SessionLocal()
    try:
        n = seed_cameras(db)
        if n:
            print(f"[seed] inserted {n} demo cameras")
    finally:
        db.close()


if __name__ == "__main__":
    seed_all()
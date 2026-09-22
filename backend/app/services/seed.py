from app.core.config import settings
from app.core.security import create_access_token, create_refresh_token, hash_password, verify_password
from app.db.session import SessionLocal
from app.models import Role, User, Hotlist, HotlistStatus, Complaint, ComplaintStatus, Sighting, Device, utcnow
from sqlalchemy import select
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

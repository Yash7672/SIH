import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    JSON,
    Boolean,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import CHAR, TypeDecorator

from app.db.session import Base


class GUID(TypeDecorator):
    """Platform-independent UUID type."""

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PgUUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value, dialect):
        return value


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    CITIZEN = "CITIZEN"
    VOLUNTEER = "VOLUNTEER"
    COP = "COP"
    ADMIN = "ADMIN"


class ComplaintStatus(str, enum.Enum):
    PENDING = "PENDING"
    UNDER_REVIEW = "UNDER_REVIEW"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    HOTLISTED = "HOTLISTED"
    CLOSED = "CLOSED"


class HotlistStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    FIR_CONFIRMED = "FIR_CONFIRMED"
    RECOVERED = "RECOVERED"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(Enum(Role, name="user_role"), default=Role.CITIZEN, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    devices: Mapped[list["Device"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    complaints: Mapped[list["Complaint"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), index=True, nullable=True)
    device_type: Mapped[str] = mapped_column(String(40), default="mobile")
    device_name: Mapped[str | None] = mapped_column(String(120))
    api_key_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship(back_populates="devices")
    sightings: Mapped[list["Sighting"]] = relationship(back_populates="device")

    __table_args__ = (Index("idx_devices_user_id", "user_id"),)


class Complaint(Base):
    __tablename__ = "complaints"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"), index=True)
    plate: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    complaint_type: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    proof_path: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[ComplaintStatus] = mapped_column(
        Enum(ComplaintStatus, name="complaint_status"), default=ComplaintStatus.PENDING, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship(back_populates="complaints")
    hotlist_entry: Mapped["Hotlist | None"] = relationship(back_populates="complaint", uselist=False)

    __table_args__ = (
        Index("idx_complaints_user_id", "user_id"),
        Index("idx_complaints_status", "status"),
        Index("idx_complaints_created_at", "created_at"),
    )


class Hotlist(Base):
    __tablename__ = "hotlist"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    plate: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    complaint_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("complaints.id"), index=True)
    status: Mapped[HotlistStatus] = mapped_column(
        Enum(HotlistStatus, name="hotlist_status"), default=HotlistStatus.ACTIVE, index=True
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expiry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    fir_reference: Mapped[str | None] = mapped_column(String(60))
    fir_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_lat: Mapped[float | None] = mapped_column(Float)
    last_seen_lng: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    complaint: Mapped["Complaint | None"] = relationship(back_populates="hotlist_entry")
    sightings: Mapped[list["Sighting"]] = relationship(back_populates="hotlist", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_hotlist_plate_status", "plate", "status"),
        Index("idx_hotlist_status", "status"),
        Index("idx_hotlist_expiry", "expiry_at"),
        Index("idx_hotlist_added_at", "added_at"),
        # Covers the Redis-miss fallback lookup: active plate -> newest entry.
        Index("idx_hotlist_plate_status_added", "plate", "status", "added_at"),
    )


class Sighting(Base):
    __tablename__ = "sightings"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    hotlist_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("hotlist.id"), index=True)
    device_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("devices.id"), index=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    hotlist: Mapped["Hotlist"] = relationship(back_populates="sightings")
    device: Mapped["Device"] = relationship(back_populates="sightings")

    camera_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("cameras.id"), index=True)
    source: Mapped[str | None] = mapped_column(String(20))
    match_type: Mapped[str | None] = mapped_column(String(20))
    match_score: Mapped[float | None] = mapped_column(Float)
    vehicle_class: Mapped[str | None] = mapped_column(String(20))
    track_id: Mapped[str | None] = mapped_column(String(80))

    camera: Mapped["Camera | None"] = relationship(back_populates="sightings")

    __table_args__ = (
        Index("idx_sightings_detected_at", "detected_at"),
        Index("idx_sightings_device_id", "device_id"),
        Index("idx_sightings_hotlist_id", "hotlist_id"),
        Index("idx_sightings_created_at", "created_at"),
        # Covers the vehicle timeline/route queries (filter by hotlist, order by time).
        Index("idx_sightings_hotlist_detected", "hotlist_id", "detected_at"),
    )


class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    heading_deg: Mapped[float | None] = mapped_column(Float)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source_uri: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="OFFLINE")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sightings: Mapped[list["Sighting"]] = relationship(back_populates="camera")

    __table_args__ = (Index("idx_cameras_name", "name"),)


class TrafficCell(Base):
    __tablename__ = "traffic_cells"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    camera_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("cameras.id"), index=True)
    cell_lat: Mapped[float] = mapped_column(Float, nullable=False)
    cell_lng: Mapped[float] = mapped_column(Float, nullable=False)
    hour_bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    vehicle_class: Mapped[str | None] = mapped_column(String(20))
    count: Mapped[int] = mapped_column(Integer, default=1)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("idx_traffic_cells_hour_bucket", "hour_bucket"),
        Index("idx_traffic_cells_camera_hour", "camera_id", "hour_bucket"),
        Index(
            "uq_traffic_cells_unique",
            "cell_lat",
            "cell_lng",
            "hour_bucket",
            "vehicle_class",
            "camera_id",
            "synthetic",
            unique=True,
        ),
    )


class ODFlow(Base):
    __tablename__ = "od_flows"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    origin_camera_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("cameras.id"), index=True)
    dest_camera_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("cameras.id"), index=True)
    hour_bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    count: Mapped[int] = mapped_column(Integer, default=1)
    total_travel_seconds: Mapped[int] = mapped_column(Integer, default=0)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("idx_od_flows_hour", "hour_bucket"),
        Index("idx_od_flows_od_hour", "origin_camera_id", "dest_camera_id", "hour_bucket"),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), index=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(60))
    meta: Mapped[dict | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    __table_args__ = (Index("idx_audit_created_at", "created_at"),)

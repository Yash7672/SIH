"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.Enum("CITIZEN", "VOLUNTEER", "COP", "ADMIN", name="user_role"), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "devices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("device_type", sa.String(40), nullable=False),
        sa.Column("device_name", sa.String(120), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_devices_user_id", "devices", ["user_id"])

    op.create_table(
        "complaints",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("plate", sa.String(20), nullable=False),
        sa.Column("complaint_type", sa.String(60), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("proof_path", sa.String(500), nullable=True),
        sa.Column("status", sa.Enum("PENDING", "UNDER_REVIEW", "VERIFIED", "REJECTED", "HOTLISTED", "CLOSED", name="complaint_status"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_complaints_plate", "complaints", ["plate"])
    op.create_index("idx_complaints_user_id", "complaints", ["user_id"])
    op.create_index("idx_complaints_status", "complaints", ["status"])
    op.create_index("idx_complaints_created_at", "complaints", ["created_at"])

    op.create_table(
        "hotlist",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("plate", sa.String(20), nullable=False),
        sa.Column("complaint_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("complaints.id"), nullable=True),
        sa.Column("status", sa.Enum("ACTIVE", "FIR_CONFIRMED", "RECOVERED", "CLOSED", "EXPIRED", name="hotlist_status"), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expiry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fir_reference", sa.String(60), nullable=True),
        sa.Column("fir_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_lat", sa.Float(), nullable=True),
        sa.Column("last_seen_lng", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_hotlist_plate", "hotlist", ["plate"])
    op.create_index("idx_hotlist_plate_status", "hotlist", ["plate", "status"])
    op.create_index("idx_hotlist_status", "hotlist", ["status"])
    op.create_index("idx_hotlist_expiry", "hotlist", ["expiry_at"])
    op.create_index("idx_hotlist_added_at", "hotlist", ["added_at"])

    op.create_table(
        "sightings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("hotlist_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("hotlist.id"), nullable=False),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sightings_hotlist_id", "sightings", ["hotlist_id"])
    op.create_index("ix_sightings_device_id", "sightings", ["device_id"])
    op.create_index("idx_sightings_detected_at", "sightings", ["detected_at"])
    op.create_index("idx_sightings_device_id", "sightings", ["device_id"])
    op.create_index("idx_sightings_hotlist_id", "sightings", ["hotlist_id"])
    op.create_index("idx_sightings_created_at", "sightings", ["created_at"])

    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=True),
        sa.Column("target_id", sa.String(60), nullable=True),
        sa.Column("metadata", postgresql.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])
    op.create_index("idx_audit_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("sightings")
    op.drop_table("hotlist")
    op.drop_table("complaints")
    op.drop_table("devices")
    op.drop_table("users")
    sa.Enum(name="user_role").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="complaint_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="hotlist_status").drop(op.get_bind(), checkfirst=True)

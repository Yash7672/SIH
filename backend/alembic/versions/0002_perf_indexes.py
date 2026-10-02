"""performance indexes

Adds composite indexes used by the hot-list lookup and the vehicle
timeline/route queries.

Revision ID: 0002_perf_indexes
Revises: 0001_initial
Create Date: 2026-09-23
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002_perf_indexes"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `hotlist` composite index lives on the hotlist table; the model declares it
    # there, so create it with the same name.
    op.create_index("idx_hotlist_plate_status_added", "hotlist", ["plate", "status", "added_at"])
    op.create_index("idx_sightings_hotlist_detected", "sightings", ["hotlist_id", "detected_at"])


def downgrade() -> None:
    op.drop_index("idx_sightings_hotlist_detected", table_name="sightings")
    op.drop_index("idx_hotlist_plate_status_added", table_name="hotlist")

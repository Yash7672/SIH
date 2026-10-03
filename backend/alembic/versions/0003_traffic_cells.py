"""traffic cells for the Maps heatmap

Aggregates per-cell vehicle counts fed by the live scanner (/ws/scan). One row
per (rounded coordinate, UTC hour, real-or-synthetic flag).

Privacy: this table holds counts only. No plate, no image, no device id - a
density grid cannot be joined back to any individual vehicle.

Revision ID: 0003_traffic_cells
Revises: 0002_perf_indexes
Create Date: 2026-10-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_traffic_cells"
down_revision: Union[str, None] = "0002_perf_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "traffic_cells",
        # Coordinates are rounded to 0.001 degrees (~110 m) before insert, so the
        # grid stays coarse even though the column holds full double precision.
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("cell_lat", sa.Float(), nullable=False),
        sa.Column("cell_lng", sa.Float(), nullable=False),
        sa.Column("hour_bucket", sa.DateTime(timezone=True), nullable=False),
        # Frames counts processed frames, including frames with no vehicles, so an
        # empty road still registers as low density rather than as missing data.
        sa.Column("frames", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("two_wheeler", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("car", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bus", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truck", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("synthetic", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.PrimaryKeyConstraint("id"),
        # synthetic is part of the key so demo rows can never be merged into, or
        # purged along with, real observations.
        sa.UniqueConstraint(
            "cell_lat", "cell_lng", "hour_bucket", "synthetic",
            name="uq_traffic_cells_cell_hour",
        ),
    )
    # The heat query always filters by time range and groups by cell.
    op.create_index("idx_traffic_cells_hour_bucket", "traffic_cells", ["hour_bucket"])
    op.create_index("idx_traffic_cells_lng_lat", "traffic_cells", ["cell_lng", "cell_lat"])


def downgrade() -> None:
    op.drop_index("idx_traffic_cells_lng_lat", table_name="traffic_cells")
    op.drop_index("idx_traffic_cells_hour_bucket", table_name="traffic_cells")
    op.drop_table("traffic_cells")

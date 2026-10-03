"""maps: cameras, traffic_cells, od_flows + sighting/device extensions

Revision ID: 0003_maps_traffic_od_cameras
Revises: 0002_add_indexes
Create Date: 2026-10-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "0003_maps_traffic_od_cameras"
down_revision: Union[str, None] = "0002_perf_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_postgis() -> bool:
    conn = op.get_bind()
    try:
        res = conn.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='postgis')")
        )
        return bool(res.scalar())
    except Exception:
        return False


def _has_table(name: str) -> bool:
    return inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def _has_index(table: str, index_name: str) -> bool:
    return index_name in {i["name"] for i in inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    # cameras
    if not _has_table("cameras"):
        op.create_table(
            "cameras",
            sa.Column("id", sa.CHAR(length=36), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("lat", sa.Float(), nullable=False),
            sa.Column("lng", sa.Float(), nullable=False),
            sa.Column("heading_deg", sa.Float(), nullable=True),
            sa.Column("source_type", sa.String(length=20), nullable=False),
            sa.Column("source_uri", sa.String(length=500), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_index("cameras", "idx_cameras_name"):
        op.create_index("idx_cameras_name", "cameras", ["name"], unique=False)

    # devices extensions
    op.alter_column("devices", "user_id", existing_type=sa.CHAR(length=36), nullable=True)
    if not _has_column("devices", "api_key_hash"):
        op.add_column("devices", sa.Column("api_key_hash", sa.String(length=128), nullable=True))
    if not _has_index("devices", "ix_devices_api_key_hash"):
        op.create_index("ix_devices_api_key_hash", "devices", ["api_key_hash"], unique=False)

    # traffic_cells
    if not _has_table("traffic_cells"):
        op.create_table(
            "traffic_cells",
            sa.Column("id", sa.CHAR(length=36), nullable=False),
            sa.Column("camera_id", sa.CHAR(length=36), nullable=True),
            sa.Column("cell_lat", sa.Float(), nullable=False),
            sa.Column("cell_lng", sa.Float(), nullable=False),
            sa.Column("hour_bucket", sa.DateTime(timezone=True), nullable=False),
            sa.Column("vehicle_class", sa.String(length=20), nullable=True),
            sa.Column("count", sa.Integer(), nullable=True),
            sa.Column("synthetic", sa.Boolean(), nullable=True),
            sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_index("traffic_cells", "idx_traffic_cells_camera_hour"):
        op.create_index("idx_traffic_cells_camera_hour", "traffic_cells", ["camera_id", "hour_bucket"], unique=False)
    if not _has_index("traffic_cells", "idx_traffic_cells_hour_bucket"):
        op.create_index("idx_traffic_cells_hour_bucket", "traffic_cells", ["hour_bucket"], unique=False)
    # NULLS NOT DISTINCT (PostgreSQL 15+): mobile detections have
    # camera_id IS NULL, and with default NULL semantics every such event would
    # insert a fresh row instead of colliding with the previous one, breaking the
    # ON CONFLICT accumulator the consumer relies on.
    if not _has_index("traffic_cells", "uq_traffic_cells_unique"):
        op.create_index(
            "uq_traffic_cells_unique",
            "traffic_cells",
            ["cell_lat", "cell_lng", "hour_bucket", "vehicle_class", "camera_id", "synthetic"],
            unique=True,
            postgresql_nulls_not_distinct=True,
        )

    # od_flows
    if not _has_table("od_flows"):
        op.create_table(
            "od_flows",
            sa.Column("id", sa.CHAR(length=36), nullable=False),
            sa.Column("origin_camera_id", sa.CHAR(length=36), nullable=False),
            sa.Column("dest_camera_id", sa.CHAR(length=36), nullable=False),
            sa.Column("hour_bucket", sa.DateTime(timezone=True), nullable=False),
            sa.Column("count", sa.Integer(), nullable=True),
            sa.Column("total_travel_seconds", sa.Integer(), nullable=True),
            sa.Column("synthetic", sa.Boolean(), nullable=True),
            sa.ForeignKeyConstraint(["dest_camera_id"], ["cameras.id"], ),
            sa.ForeignKeyConstraint(["origin_camera_id"], ["cameras.id"], ),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_index("od_flows", "idx_od_flows_hour"):
        op.create_index("idx_od_flows_hour", "od_flows", ["hour_bucket"], unique=False)
    if not _has_index("od_flows", "idx_od_flows_od_hour"):
        op.create_index("idx_od_flows_od_hour", "od_flows", ["origin_camera_id", "dest_camera_id", "hour_bucket"], unique=False)
    if not _has_index("od_flows", "uq_od_flows_unique"):
        op.create_index(
            "uq_od_flows_unique",
            "od_flows",
            ["origin_camera_id", "dest_camera_id", "hour_bucket", "synthetic"],
            unique=True,
        )

    # sightings extensions
    if not _has_column("sightings", "camera_id"):
        op.add_column("sightings", sa.Column("camera_id", sa.CHAR(length=36), nullable=True))
    if not _has_column("sightings", "source"):
        op.add_column("sightings", sa.Column("source", sa.String(length=20), nullable=True))
    if not _has_column("sightings", "match_type"):
        op.add_column("sightings", sa.Column("match_type", sa.String(length=20), nullable=True))
    if not _has_column("sightings", "match_score"):
        op.add_column("sightings", sa.Column("match_score", sa.Float(), nullable=True))
    if not _has_column("sightings", "vehicle_class"):
        op.add_column("sightings", sa.Column("vehicle_class", sa.String(length=20), nullable=True))
    if not _has_column("sightings", "track_id"):
        op.add_column("sightings", sa.Column("track_id", sa.String(length=80), nullable=True))
    if not _has_index("sightings", "ix_sightings_camera_id"):
        op.create_index("ix_sightings_camera_id", "sightings", ["camera_id"], unique=False)
    # Add FK only if not already present.
    inspector = inspect(op.get_bind())
    existing_fk_cols = [tuple(fk.get("constrained_columns", [])) for fk in inspector.get_foreign_keys("sightings")]
    if ("camera_id",) not in existing_fk_cols:
        try:
            op.create_foreign_key(None, "sightings", "cameras", ["camera_id"], ["id"])
        except Exception:
            pass

    # PostGIS conditional: add geom columns + GiST indexes (guarded)
    if _has_postgis():
        conn = op.get_bind()
        # cameras: point geometry (4326)
        conn.execute(
            sa.text(
                "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS geom geometry(Point, 4326);"
            )
        )
        conn.execute(
            sa.text("CREATE INDEX IF NOT EXISTS idx_cameras_geom ON cameras USING GIST (geom);")
        )
        conn.execute(
            sa.text(
                "UPDATE cameras SET geom = ST_SetSRID(ST_MakePoint(lng, lat), 4326) WHERE geom IS NULL;"
            )
        )
        # traffic_cells: centroid point
        conn.execute(
            sa.text(
                "ALTER TABLE traffic_cells ADD COLUMN IF NOT EXISTS geom geometry(Point, 4326);"
            )
        )
        conn.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS idx_traffic_cells_geom ON traffic_cells USING GIST (geom);"
            )
        )
        conn.execute(
            sa.text(
                "UPDATE traffic_cells SET geom = ST_SetSRID(ST_MakePoint(cell_lng, cell_lat), 4326) WHERE geom IS NULL;"
            )
        )
        # sightings: point
        conn.execute(
            sa.text(
                "ALTER TABLE sightings ADD COLUMN IF NOT EXISTS geom geometry(Point, 4326);"
            )
        )
        conn.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS idx_sightings_geom ON sightings USING GIST (geom);"
            )
        )
        conn.execute(
            sa.text(
                "UPDATE sightings SET geom = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326) WHERE geom IS NULL;"
            )
        )


def downgrade() -> None:
    if _has_postgis():
        conn = op.get_bind()
        conn.execute(sa.text("DROP INDEX IF EXISTS idx_sightings_geom;"))
        conn.execute(sa.text("ALTER TABLE sightings DROP COLUMN IF EXISTS geom;"))
        conn.execute(sa.text("DROP INDEX IF EXISTS idx_traffic_cells_geom;"))
        conn.execute(sa.text("ALTER TABLE traffic_cells DROP COLUMN IF EXISTS geom;"))
        conn.execute(sa.text("DROP INDEX IF EXISTS idx_cameras_geom;"))
        conn.execute(sa.text("ALTER TABLE cameras DROP COLUMN IF EXISTS geom;"))

    op.drop_index("ix_sightings_camera_id", table_name="sightings")
    try:
        op.drop_constraint("sightings_camera_id_fkey", "sightings", type_="foreignkey")
    except Exception:
        try:
            op.drop_constraint(None, "sightings", type_="foreignkey")
        except Exception:
            pass
    op.drop_column("sightings", "track_id")
    op.drop_column("sightings", "vehicle_class")
    op.drop_column("sightings", "match_score")
    op.drop_column("sightings", "match_type")
    op.drop_column("sightings", "source")
    op.drop_column("sightings", "camera_id")

    op.drop_index("idx_od_flows_od_hour", table_name="od_flows")
    op.drop_index("idx_od_flows_hour", table_name="od_flows")
    op.drop_index("uq_od_flows_unique", table_name="od_flows")
    op.drop_table("od_flows")

    op.drop_index("idx_traffic_cells_hour_bucket", table_name="traffic_cells")
    op.drop_index("idx_traffic_cells_camera_hour", table_name="traffic_cells")
    op.drop_index("uq_traffic_cells_unique", table_name="traffic_cells")
    op.drop_table("traffic_cells")

    op.drop_index("ix_devices_api_key_hash", table_name="devices")
    op.drop_column("devices", "api_key_hash")
    op.alter_column("devices", "user_id", existing_type=sa.CHAR(length=36), nullable=False)

    op.drop_index("idx_cameras_name", table_name="cameras")
    op.drop_table("cameras")

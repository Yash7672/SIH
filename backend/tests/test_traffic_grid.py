"""The accumulator that fills the grid: cell maths, batching, and durability.

`TrafficAccumulator` sits on the frame path, so its failure modes matter more
than its size: a bad GPS fix must not become traffic, a database blip must not
lose real observations, and demo rows must never merge into live ones.
"""

from datetime import datetime, timezone

import pytest

from tests.geo_helpers import grid, put_cell  # noqa: F401  (grid is autouse)


# ------------------------------------------------------------------- cell maths


def test_a_missing_gps_fix_is_not_credited_to_a_cell():
    from app.services.traffic_service import usable_position

    assert not usable_position(0, 0)
    assert not usable_position(None, 78.4)
    assert not usable_position(17.4, None)
    assert not usable_position("17.4", 78.4)
    assert not usable_position(float("nan"), 78.4)
    assert not usable_position(17.4, float("inf"))
    assert not usable_position(True, 78.4)
    # A phone that briefly reports somewhere abroad is a bad fix, not traffic.
    assert not usable_position(51.5, -0.12)
    assert usable_position(17.44, 78.48)


def test_frames_without_a_fix_count_toward_nothing():
    from app.services.traffic_service import TrafficAccumulator

    acc = TrafficAccumulator()
    assert acc.add(0, 0, [{"cls": 3}]) is False
    assert acc.pending_cells == 0


def test_boxes_are_tallied_by_class():
    from app.services.traffic_service import tally_from_boxes

    tally = tally_from_boxes([
        {"cls": 3}, {"cls": 3},      # car
        {"cls": 1}, {"cls": 2},      # motorcycle, bicycle -> two_wheeler
        {"cls": 6},                  # bus
        {"cls": 8},                  # truck
        {"cls": 0},                  # person, not a vehicle
        {"cls": "junk"},
        "not a mapping",
        None,
    ])
    assert tally == {"two_wheeler": 2, "car": 2, "bus": 1, "truck": 1}


def test_coordinates_snap_to_the_same_cell():
    from app.services.traffic_service import round_cell

    # 110 m grid: two points 20 m apart share a cell.
    assert round_cell(17.44001, 78.48001) == round_cell(17.44002, 78.48002)
    # A different city block does not.
    assert round_cell(17.44, 78.48) != round_cell(17.45, 78.48)


def test_hour_bucket_truncates_to_the_hour():
    from app.services.traffic_service import hour_bucket

    when = datetime(2026, 10, 4, 13, 47, 31, 500, tzinfo=timezone.utc)
    assert hour_bucket(when) == datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------- durability


def test_accumulator_batches_and_sums_on_flush(client):
    """Two frames in the same cell must end up as one row, not two."""
    from app.db.session import SessionLocal
    from app.models import TrafficCell
    from app.services.traffic_service import TrafficAccumulator

    acc = TrafficAccumulator()
    acc.add(17.44, 78.48, [{"cls": 3}])
    acc.add(17.44, 78.48, [{"cls": 3}])
    assert acc.pending_cells == 1

    db = SessionLocal()
    try:
        assert acc.flush(db) == 1
        # A frame with nothing in it still counts as a frame.
        acc.add(17.44, 78.48, [])
        acc.flush(db)
        rows = db.query(TrafficCell).all()
        assert len(rows) == 1
        assert rows[0].frames == 3
        assert rows[0].car == 2
        assert rows[0].synthetic is False
        assert acc.pending_cells == 0
    finally:
        db.close()


def test_flush_survives_a_failed_write_without_losing_counts():
    """A database blip must not silently drop real observations."""
    from app.services.traffic_service import TrafficAccumulator

    acc = TrafficAccumulator()
    acc.add(17.44, 78.48, [{"cls": 3}])

    class BrokenSession:
        def execute(self, *a, **k):
            raise RuntimeError("connection reset")

        def commit(self):
            pass

        def rollback(self):
            pass

    with pytest.raises(RuntimeError):
        acc.flush(BrokenSession())

    assert acc.pending_cells == 1, "counts were dropped instead of kept for retry"


def test_synthetic_rows_are_never_merged_into_real_ones(client):
    put_cell(17.44, 78.48, car=100, synthetic=True)
    put_cell(17.44, 78.48, car=900, synthetic=False)
    from app.db.session import SessionLocal
    from app.models import TrafficCell

    db = SessionLocal()
    try:
        rows = db.query(TrafficCell).order_by(TrafficCell.synthetic).all()
        assert len(rows) == 2, "demo and live counts collapsed into one row"
        assert rows[0].synthetic is False
        assert rows[1].synthetic is True
    finally:
        db.close()


def test_retention_removes_only_old_rows(client):
    """90-day retention must not touch a cell an officer could still be looking at."""
    from datetime import timedelta

    from app.db.session import SessionLocal
    from app.models import TrafficCell
    from app.services.traffic_service import RETENTION_DAYS, purge_older_than

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        db.add(TrafficCell(
            cell_lat=17.1, cell_lng=78.1,
            hour_bucket=(now - timedelta(days=RETENTION_DAYS + 5)).replace(
                minute=0, second=0, microsecond=0),
            frames=99, two_wheeler=1, car=1, bus=0, truck=0, synthetic=True,
        ))
        db.add(TrafficCell(
            cell_lat=17.2, cell_lng=78.2, hour_bucket=now.replace(
                minute=0, second=0, microsecond=0),
            frames=99, two_wheeler=1, car=1, bus=0, truck=0, synthetic=True,
        ))
        db.commit()

        assert purge_older_than(db) == 1
        remaining = db.query(TrafficCell).all()
        assert [r.cell_lat for r in remaining] == [17.2]
    finally:
        db.close()
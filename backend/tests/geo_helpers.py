"""Shared fixtures for the density-grid tests.

Three test modules use these (`test_geo`, `test_heat_layers`, `test_traffic_grid`),
so the helpers live here rather than being imported out of one test module into
another - importing across test modules works, but it hides where a fixture
actually came from.
"""

from datetime import datetime, timedelta, timezone

import pytest

# Plates these tests use. Everything attached to them is torn down by the
# `grid` fixture, so a sighting left behind by one test cannot quietly satisfy
# the next test's assertions.
GEO_PLATES = ("TS09AB1234",)


def put_cell(lat, lng, hours_ago=0, frames=100, car=400, two_wheeler=200,
             bus=10, truck=5, synthetic=True):
    """Insert one grid cell.

    ``hours_ago=0`` lands in the current hour bucket, which is what the endpoint's
    default 15-minute window resolves to once snapped to the hour.
    """
    from app.db.session import SessionLocal
    from app.models import TrafficCell

    bucket = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).replace(
        minute=0, second=0, microsecond=0
    )
    db = SessionLocal()
    try:
        db.add(
            TrafficCell(
                cell_lat=lat, cell_lng=lng, hour_bucket=bucket, frames=frames,
                two_wheeler=two_wheeler, car=car, bus=bus, truck=truck,
                synthetic=synthetic,
            )
        )
        db.commit()
    finally:
        db.close()


def put_sightings(coords, ages_minutes):
    """Create a device + hot-list entry + one sighting per (coord, age) pair."""
    import uuid

    from app.db.session import SessionLocal
    from app.models import Device, Hotlist, HotlistStatus, Sighting, User

    db = SessionLocal()
    try:
        volunteer = db.query(User).filter(User.email == "volunteer@example.com").one()
        device = Device(user_id=volunteer.id, device_type="mobile")
        entry = Hotlist(plate=GEO_PLATES[0], status=HotlistStatus.ACTIVE)
        db.add_all([device, entry])
        db.flush()
        now = datetime.now(timezone.utc)
        for (lat, lng), age in zip(coords, ages_minutes):
            db.add(
                Sighting(
                    id=uuid.uuid4(), hotlist_id=entry.id, device_id=device.id,
                    latitude=lat, longitude=lng,
                    detected_at=now - timedelta(minutes=age), confidence=0.9,
                )
            )
        db.commit()
    finally:
        db.close()


def clean_grid() -> None:
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        from app.models import Hotlist, Sighting, TrafficCell

        ids = [
            row[0]
            for row in db.query(Hotlist.id).filter(Hotlist.plate.in_(GEO_PLATES)).all()
        ]
        if ids:
            db.query(Sighting).filter(Sighting.hotlist_id.in_(ids)).delete(
                synchronize_session=False
            )
            db.query(Hotlist).filter(Hotlist.id.in_(ids)).delete(synchronize_session=False)
        db.query(TrafficCell).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def grid():
    """Every density test starts and ends with an empty grid."""
    clean_grid()
    yield
    clean_grid()
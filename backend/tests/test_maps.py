"""Maps read API + synthetic traffic simulator tests.

These cover the read surface (cameras, heatmap, OD flows, summary), RBAC, and
the simulator's event generation. The simulator is disabled globally by
conftest, so its tick() is exercised directly rather than via a background task.
"""

from tests.conftest import auth


def _seed_camera(db):
    from app.services.seed import seed_cameras

    return seed_cameras(db)


def _aggregate(db, lat, lng, ts, count=1, synthetic=True, camera_id=None):
    from app.services.ingest_service import IngestService

    svc = IngestService(db)
    from app.services.geo_support import snap_cell

    cell_lat, cell_lng = snap_cell(lat, lng, "high")
    from app.services.ingest_service import hour_bucket

    svc.bump_cells([(camera_id, cell_lat, cell_lng, hour_bucket(ts), "car", count, synthetic)])
    db.commit()


def test_maps_routes_require_cop(client, demo_tokens):
    for path in ("/api/v1/maps/cameras", "/api/v1/maps/traffic", "/api/v1/maps/od-flows", "/api/v1/maps/summary"):
        assert client.get(path, headers=auth(demo_tokens["citizen"])).status_code == 403
        assert client.get(path, headers=auth(demo_tokens["volunteer"])).status_code == 403


def test_maps_cameras_lists_seeded(client, demo_tokens):
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        _seed_camera(db)
    finally:
        db.close()

    r = client.get("/api/v1/maps/cameras", headers=auth(demo_tokens["cop"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cameras"], "expected seeded cameras"
    cam = body["cameras"][0]
    for key in ("id", "name", "lat", "lng", "online", "sightings_today"):
        assert key in cam
    assert "offline_after_seconds" in body


def test_maps_traffic_heatmap_and_synthetic_filter(client, demo_tokens):
    from datetime import datetime, timezone

    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        _aggregate(db, 17.385, 78.4867, datetime.now(timezone.utc), count=5, synthetic=True)
    finally:
        db.close()

    r = client.get("/api/v1/maps/traffic?hours=6", headers=auth(demo_tokens["cop"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] >= 5
    assert body["max"] >= 5
    assert body["cells"] and all("lat" in c and "lng" in c and "count" in c for c in body["cells"])
    # Cells are returned hottest-first.
    counts = [c["count"] for c in body["cells"]]
    assert counts == sorted(counts, reverse=True)

    only_real = client.get(
        "/api/v1/maps/traffic?hours=6&include_synthetic=false", headers=auth(demo_tokens["cop"])
    ).json()
    assert all(c["count"] <= body["total"] for c in only_real["cells"])


def test_maps_traffic_bbox_filters(client, demo_tokens):
    from datetime import datetime, timezone

    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        _aggregate(db, 17.385, 78.4867, datetime.now(timezone.utc), count=3)
    finally:
        db.close()

    # A bbox far away must exclude the Hyderabad cell.
    far = client.get(
        "/api/v1/maps/traffic?hours=6&min_lat=10&min_lng=70&max_lat=11&max_lng=71",
        headers=auth(demo_tokens["cop"]),
    ).json()
    assert far["total"] == 0

    near = client.get(
        "/api/v1/maps/traffic?hours=6&min_lat=17&min_lng=78&max_lat=18&max_lng=79",
        headers=auth(demo_tokens["cop"]),
    ).json()
    assert near["total"] >= 3


def test_maps_od_flows_aggregate(client, demo_tokens):
    from datetime import datetime, timezone

    from app.db.session import SessionLocal
    from app.models import Camera
    from app.services.ingest_service import IngestService, hour_bucket

    db = SessionLocal()
    try:
        _seed_camera(db)
        cams = db.query(Camera).order_by(Camera.name).limit(2).all()
        assert len(cams) == 2
        origin, dest = cams
        svc = IngestService(db)
        svc.bump_od([(origin.id, dest.id, hour_bucket(datetime.now(timezone.utc)), 4, 240, True)])
        db.commit()
    finally:
        db.close()

    r = client.get("/api/v1/maps/od-flows?hours=6", headers=auth(demo_tokens["cop"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["flows"], "expected an OD corridor"
    flow = body["flows"][0]
    assert flow["count"] >= 4
    assert flow["avg_travel_seconds"] == 60  # 240 s total / 4 vehicles
    assert flow["origin"]["id"] and flow["dest"]["id"]


def test_maps_summary(client, demo_tokens):
    r = client.get("/api/v1/maps/summary?hours=6", headers=auth(demo_tokens["cop"]))
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("cameras", "cameras_online", "vehicles_in_window", "od_corridors", "hours"):
        assert key in body


def test_analytics_traffic_snapshot(client, demo_tokens):
    from datetime import datetime, timezone

    from app.db.session import SessionLocal
    from app.services.ingest_service import IngestService, hour_bucket
    from app.services.seed import seed_cameras

    db = SessionLocal()
    try:
        seed_cameras(db)
        cam = db.query(__import__("app.models", fromlist=["Camera"]).Camera).first()
        assert cam is not None
        svc = IngestService(db)
        svc.bump_cells([(cam.id, 17.38, 78.48, hour_bucket(datetime.now(timezone.utc)), "car", 7, True)])
        db.commit()
    finally:
        db.close()

    r = client.get("/api/v1/analytics/traffic?hours=24", headers=auth(demo_tokens["cop"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_volume"] >= 7
    assert body["by_hour"]
    assert body["by_camera"]
    assert body["class_mix"]


def test_simulator_events_are_flagged_synthetic_and_use_real_cameras():
    from app.db.session import SessionLocal

    from app.services.traffic_sim import TrafficSimulator

    db = SessionLocal()
    try:
        _seed_camera(db)
        cameras = TrafficSimulator()._cameras()
    finally:
        db.close()

    assert cameras, "expected seeded cameras"
    sim = TrafficSimulator(bus=object())
    events = sim._make_events(cameras, ["TS09AB1234"])
    assert events
    camera_ids = {str(c.id) for c in cameras}
    for e in events:
        assert e.synthetic is True
        assert str(e.camera_id) in camera_ids
        assert e.plate
        assert e.vehicle_class in (
            "car", "motorcycle", "bus", "truck", "auto", "unknown"
        )


def test_simulator_disabled_under_tests():
    from app.core.config import settings

    assert settings.sim_enabled is False

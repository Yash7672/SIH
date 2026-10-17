from tests.conftest import auth

import pytest


def make_hotlist(client, tokens, plate="TS09AB1234"):
    c = client.post(
        "/api/v1/complaints",
        headers=auth(tokens["citizen"]),
        data={"plate": plate, "complaint_type": "Stolen", "description": "d"},
    ).json()
    r = client.post(f"/api/v1/complaints/{c['id']}/verify", headers=auth(tokens["cop"]))
    assert r.status_code == 200, r.text
    return r.json()["hotlist_id"]


def register_device(client, tokens, name="TestCam"):
    r = client.post(
        "/api/v1/devices/register",
        headers=auth(tokens["volunteer"]),
        json={"device_type": "mobile", "device_name": name},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def sighting_body(plate, device_id, lat=17.45, lng=78.34, t="2026-09-22T09:00:00Z", conf=0.95):
    return {
        "plate": plate,
        "latitude": lat,
        "longitude": lng,
        "timestamp": t,
        "confidence": conf,
        "device_id": device_id,
    }


def test_full_detection_flow(client, demo_tokens):
    make_hotlist(client, demo_tokens)
    dev1 = register_device(client, demo_tokens, name="Cam1")
    dev2 = register_device(client, demo_tokens, name="Cam2")
    pts = [
        sighting_body("TS09AB1234", dev1, 17.4567, 78.3456, "2026-09-22T09:00:00Z"),
        sighting_body("TS09AB1234", dev2, 17.4695, 78.3601, "2026-09-22T09:05:00Z"),
    ]
    for p in pts:
        r = client.post("/api/v1/sightings", headers=auth(demo_tokens["volunteer"]), json=p)
        assert r.status_code == 200, r.text
        assert r.json()["hotlist_id"]

    active = client.get("/api/v1/hotlist?status_filter=ACTIVE", headers=auth(demo_tokens["cop"])).json()
    active_ids = {h["id"] for h in active}

    alerts = client.get("/api/v1/alerts", headers=auth(demo_tokens["cop"]))
    assert alerts.status_code == 200
    assert len(alerts.json()) >= 2
    for a in alerts.json():
        assert a["hotlist_id"] in active_ids

    route = client.get("/api/v1/vehicles/TS09AB1234/timeline", headers=auth(demo_tokens["cop"]))
    assert route.status_code == 200
    assert len(route.json()) >= 2


def test_non_hotlist_plate_not_stored(client, demo_tokens):
    dev = register_device(client, demo_tokens)
    r = client.post(
        "/api/v1/sightings",
        headers=auth(demo_tokens["volunteer"]),
        json=sighting_body("MH12JK4567", dev),
    )
    # 202 Accepted + explicitly discarded: nothing was persisted for this plate.
    assert r.status_code == 202, r.text
    assert r.json()["accepted"] is False
    alerts = client.get("/api/v1/alerts", headers=auth(demo_tokens["cop"]))
    assert all(a["plate"] != "MH12JK4567" for a in alerts.json())


def test_repeat_detection_is_cooldown_suppressed(client, demo_tokens):
    """The live detector re-reads the same parked car every frame.

    Without a cooldown one pass of the camera would produce a sighting per
    frame, so the second detection of the same plate on the same device has to
    be dropped while still returning success to the caller.
    """
    make_hotlist(client, demo_tokens)
    dev = register_device(client, demo_tokens)
    body = sighting_body("TS09AB1234", dev)

    first = client.post("/api/v1/sightings", headers=auth(demo_tokens["volunteer"]), json=body)
    assert first.status_code == 200, first.text
    assert "id" in first.json()

    second = client.post("/api/v1/sightings", headers=auth(demo_tokens["volunteer"]), json=body)
    # A throttled detection is reported the same way an unrelated plate is:
    # accepted, nothing stored, no error for the volunteer whose camera is still
    # pointed at the same car.
    assert second.status_code == 202, second.text
    assert second.json()["accepted"] is False

    rows = client.get("/api/v1/sightings", headers=auth(demo_tokens["cop"])).json()
    mine = [s for s in rows if s.get("device_id") == dev]
    assert len(mine) == 1, f"cooldown let {len(mine)} sightings through for one plate/device"


def test_hotlist_detection_updates_last_seen(client, demo_tokens):
    """A fresh sighting must move the hot-list entry's last-seen marker.

    This is the only place a reported car's position is retained, so it is what
    an officer reads off the hot-list screen.
    """
    make_hotlist(client, demo_tokens)
    dev = register_device(client, demo_tokens)
    r = client.post(
        "/api/v1/sightings",
        headers=auth(demo_tokens["volunteer"]),
        json=sighting_body("TS09AB1234", dev, lat=17.4123, lng=78.4567),
    )
    assert r.status_code == 200, r.text

    entry = client.get("/api/v1/hotlist", headers=auth(demo_tokens["cop"])).json()
    mine = [e for e in entry if e["plate"] == "TS09AB1234"]
    assert mine, "hot-list entry disappeared"
    assert mine[0]["last_seen_at"] is not None
    assert abs(mine[0]["last_seen_lat"] - 17.4123) < 1e-4
    assert abs(mine[0]["last_seen_lng"] - 78.4567) < 1e-4


def test_sighting_requires_volunteer(client, demo_tokens):
    make_hotlist(client, demo_tokens)
    dev = register_device(client, demo_tokens)
    r = client.post("/api/v1/sightings", headers=auth(demo_tokens["citizen"]), json=sighting_body("TS09AB1234", dev))
    assert r.status_code == 403


def test_sighting_unknown_device(client, demo_tokens):
    make_hotlist(client, demo_tokens)
    r = client.post(
        "/api/v1/sightings",
        headers=auth(demo_tokens["volunteer"]),
        json=sighting_body("TS09AB1234", "11111111-1111-1111-1111-111111111111"),
    )
    assert r.status_code == 404


def test_sighting_revoked_device(client, demo_tokens):
    make_hotlist(client, demo_tokens)
    dev = register_device(client, demo_tokens)
    revoke = client.post(f"/api/v1/devices/{dev}/revoke", headers=auth(demo_tokens["admin"]))
    assert revoke.status_code == 200
    r = client.post("/api/v1/sightings", headers=auth(demo_tokens["volunteer"]), json=sighting_body("TS09AB1234", dev))
    assert r.status_code == 403


def test_sighting_invalid_coords(client, demo_tokens):
    make_hotlist(client, demo_tokens)
    dev = register_device(client, demo_tokens)
    body = sighting_body("TS09AB1234", dev)
    body["latitude"] = 91.0
    r = client.post("/api/v1/sightings", headers=auth(demo_tokens["volunteer"]), json=body)
    assert r.status_code >= 400


def test_sightings_list_cop_only(client, demo_tokens):
    make_hotlist(client, demo_tokens)
    dev = register_device(client, demo_tokens)
    client.post("/api/v1/sightings", headers=auth(demo_tokens["volunteer"]), json=sighting_body("TS09AB1234", dev))
    denied = client.get("/api/v1/sightings", headers=auth(demo_tokens["volunteer"]))
    assert denied.status_code == 403
    ok = client.get("/api/v1/sightings", headers=auth(demo_tokens["cop"]))
    assert ok.status_code == 200


def test_police_ws_receives_hotlist_alert(client, demo_tokens):
    import json

    make_hotlist(client, demo_tokens)
    dev = register_device(client, demo_tokens)
    with client.websocket_connect(f"/api/v1/ws/police?token={demo_tokens['cop']}") as ws:
        r = client.post(
            "/api/v1/sightings",
            headers=auth(demo_tokens["volunteer"]),
            json=sighting_body("TS09AB1234", dev),
        )
        assert r.status_code == 200, r.text
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "hotlist_detection"
        assert msg["payload"]["plate"] == "TS09AB1234"
        assert msg["payload"]["hotlist_id"]


def test_police_ws_rejects_non_cop(client, demo_tokens):
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect(f"/api/v1/ws/police?token={demo_tokens['volunteer']}") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 4403


# --------------------------------------------------------------------------- #
# the district block, written two ways
# --------------------------------------------------------------------------- #
# An Indian district code is officially two digits and every piece of paperwork
# writes it that way, but the plate on the car - and the read off it - usually
# drops the leading zero. A report and a detection that disagree on that one zero
# are the same car, and detecting it must not depend on which side dropped it.
#
# Each test below uses its own plate pair on purpose: the harness builds the
# schema once for the whole session and never truncates between tests, so a car
# shared by two tests could be "matched" by the first test's leftover hot-list row
# and the second test would then prove nothing.


def _flush_hotlist_cache():
    """Empty Redis so the next lookup is answered by PostgreSQL.

    The live path's fast path is the Redis set of active plates; the query
    underneath is the fallback that has to work when Redis is down. They are two
    different lookups, both spelling-aware, so both are tested.
    """
    from app.services.cache import cache_service

    cache_service._client.flushdb()


def test_a_padded_report_matches_a_plain_read_when_the_cache_is_cold(client, demo_tokens):
    """A report filed as `GJ08UV5678` still catches a camera reading `GJ8UV5678`."""
    make_hotlist(client, demo_tokens, plate="GJ08UV5678")
    dev = register_device(client, demo_tokens, name="ColdCacheCam")
    _flush_hotlist_cache()

    r = client.post(
        "/api/v1/sightings",
        headers=auth(demo_tokens["volunteer"]),
        json=sighting_body("GJ8UV5678", dev),
    )
    assert r.status_code == 200, r.text
    assert r.json()["hotlist_id"]


def test_a_plain_report_matches_a_padded_read_when_the_cache_is_cold(client, demo_tokens):
    """And the other direction: filed `RJ9WX6789`, read `RJ09WX6789`.

    This is the direction the cache's canonical form cannot accidentally cover,
    because the canonical form happens to be the *plain* spelling; the read has to
    be recognised as an equivalent spelling of it.
    """
    make_hotlist(client, demo_tokens, plate="RJ9WX6789")
    dev = register_device(client, demo_tokens, name="ColdCachePaddedCam")
    _flush_hotlist_cache()

    r = client.post(
        "/api/v1/sightings",
        headers=auth(demo_tokens["volunteer"]),
        json=sighting_body("RJ09WX6789", dev),
    )
    assert r.status_code == 200, r.text
    assert r.json()["hotlist_id"]


def test_a_different_district_is_still_an_unrelated_plate(client, demo_tokens):
    """`10` and `1` are one RTO; `10` and `11` are two. The rule is that narrow."""
    make_hotlist(client, demo_tokens, plate="KA10YZ7890")
    dev = register_device(client, demo_tokens, name="OtherDistrictCam")
    _flush_hotlist_cache()

    r = client.post(
        "/api/v1/sightings",
        headers=auth(demo_tokens["volunteer"]),
        json=sighting_body("KA11YZ7890", dev),
    )
    assert r.status_code == 202, r.text
    assert r.json()["accepted"] is False


def test_a_cop_opens_the_car_under_either_spelling(client, demo_tokens):
    """The dashboard lookup follows the same rule as detection."""
    make_hotlist(client, demo_tokens, plate="TN01AB3456")
    for spelling in ("TN01AB3456", "TN1AB3456"):
        detail = client.get(f"/api/v1/vehicles/{spelling}", headers=auth(demo_tokens["cop"]))
        assert detail.status_code == 200, f"{spelling}: {detail.text}"
        assert detail.json()["plate"] == "TN01AB3456"

        timeline = client.get(f"/api/v1/vehicles/{spelling}/timeline", headers=auth(demo_tokens["cop"]))
        assert timeline.status_code == 200, f"{spelling}: {timeline.text}"
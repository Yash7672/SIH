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
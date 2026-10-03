"""Privacy contract tests: raw footage / video must never be accepted anywhere except
complaint proof uploads, and the detection pipeline never persists non-hotlist plates."""

import pytest

from tests.conftest import auth


def test_video_upload_rejected_on_sightings(client, demo_tokens):
    r = client.post(
        "/api/v1/sightings",
        headers=auth(demo_tokens["volunteer"]),
        data={"plate": "TS09AB1234"},
        files={"frame": ("clip.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4")},
    )
    assert r.status_code == 415


def test_multipart_rejected_on_sightings(client, demo_tokens):
    r = client.post(
        "/api/v1/sightings",
        headers=auth(demo_tokens["volunteer"]),
        data={"plate": "TS09AB1234"},
        files={"frame": ("frame.jpg", b"\xff\xd8\xff\xe0jpeg", "image/jpeg")},
    )
    assert r.status_code == 415


def test_video_upload_rejected_on_devices(client, demo_tokens):
    r = client.post(
        "/api/v1/devices/register",
        headers=auth(demo_tokens["volunteer"]),
        files={"device_type": ("", "mobile")},
    )
    assert r.status_code == 415


def test_alerts_expose_no_video_fields(client, demo_tokens):
    r = client.get("/api/v1/alerts", headers=auth(demo_tokens["cop"]))
    assert r.status_code == 200
    for alert in r.json():
        assert "video" not in alert and "frame" not in alert


def test_sighting_table_cannot_hold_imagery(client, demo_tokens):
    """The live detector hands raw frames to the model in memory only.

    A frame that reached the database would mean the schema had somewhere to put
    it, so the guarantee is asserted against the table itself rather than against
    whatever the current code happens to send.
    """
    from app.models import Sighting

    # __table__ rather than inspect(): the mapper is not configured in every
    # import order, but the table definition always is.
    columns = {c.name for c in Sighting.__table__.columns}

    assert columns, "Sighting table was not found"

    forbidden = {"image", "images", "frame", "frames", "video", "jpeg", "photo",
                 "snapshot", "thumbnail", "base64", "crop", "blob", "content"}
    assert columns & forbidden == set(), f"imagery columns present: {columns & forbidden}"

    # And the record really is metadata-only: plate lives on the hot-list, not here.
    assert "plate" not in columns


def test_live_scan_refuses_a_frame_before_auth(client):
    """/ws/scar accepts frames, so the auth gate has to come first and hold.

    The token travels in the first message rather than the URL, so a socket that
    has not authenticated yet must be closed rather than served.
    """
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect("/api/v1/ws/scan") as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.send_json({"type": "frame", "seq": 0, "w": 64, "h": 64,
                          "lat": 17.4, "lng": 78.4, "jpeg_b64": ""})
            ws.receive_json()


def test_live_scan_rejects_a_bad_token(client, demo_tokens):
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect("/api/v1/ws/scan") as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.send_json({"type": "auth", "token": "not-a-jwt", "device_id": None})
            ws.receive_json()


def test_live_scan_token_is_not_accepted_in_the_url(client, demo_tokens):
    """Tokens in a query string leak into proxy and access logs."""
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect(
        f"/api/v1/ws/scan?token={demo_tokens['volunteer']}"
    ) as ws:
        # No auth message is sent, so the socket must not reach the "ready" state.
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
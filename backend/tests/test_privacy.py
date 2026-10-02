"""Privacy contract tests: raw footage / video must never be accepted anywhere except
complaint proof uploads, and the detection pipeline never persists non-hotlist plates."""

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
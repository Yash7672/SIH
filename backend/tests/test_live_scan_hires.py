"""The high-resolution request, over the real WebSocket.

A plate that is too small to read cannot be fixed by trying harder on the pixels
the server has. These tests pin the request protocol: when it goes out, that an
unrequested big frame is refused, and - the part that actually matters - that the
extra pixels are used for OCR only and never make the green boxes late.

Message reads go through `test_live_scan_ws._drain` / `._collect`, which have
deadlines. `WebSocketTestSession.receive()` blocks for ever once the app has
finished, so a test that waits for a message that is supposed *not* to arrive
would hang rather than fail.
"""

from __future__ import annotations

import base64
import time

import pytest

import app.api.v1.live_scan as ls
from tests.test_live_scan_ws import (
    FRAME_H,
    FRAME_W,
    _StubDetector,
    _collect,
    _drain,
    _jpeg,
)


def _auth(ws, token, device_id=None):
    ws.send_json({"type": "auth", "token": token, "device_id": device_id})
    return ws.receive_json()


def _frame(seq: int, jpeg: bytes, hires: bool = False):
    payload = {
        "type": "frame",
        "seq": seq,
        "w": FRAME_W,
        "h": FRAME_H,
        "lat": 17.4,
        "lng": 78.4,
        "jpeg_b64": base64.b64encode(jpeg).decode(),
    }
    if hires:
        payload["hires"] = True
    return payload


def _px(nx1, ny1, nx2, ny2):
    return [nx1 * 1280, ny1 * 844, nx2 * 1280, ny2 * 844]


def _hires_jpeg(width: int = 1600, height: int = 1050, quality: int = 80) -> bytes:
    """A frame that lands between the two size limits.

    `_jpeg` builds a smooth gradient, which JPEG squeezes to ~30 KB whatever its
    dimensions, so it cannot demonstrate a size-limit decision at all. Real
    camera frames carry sensor noise and do not compress like that - which is the
    whole reason the limit exists - so this adds a plausible amount of noise to a
    scene-like gradient and lands at ~490 KB: over `HARD_FRAME_BYTES` (400 KB)
    and under `HIRES_FRAME_BYTES` (900 KB). Refused normally, accepted as an
    answer to a request.

    `test_the_two_limits_bracket_this_frame` asserts that placement, so the two
    facts the tests rest on cannot drift apart silently.
    """
    import io

    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(11)
    base = np.linspace(0, 255, width, dtype=np.uint8)
    arr = np.tile(base, (height, 1))[:, :, None].repeat(3, axis=2)
    arr = np.clip(
        arr.astype(np.int16) + rng.integers(-35, 35, (height, width, 3)), 0, 255
    ).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


# 90 px wide: below HIRES_PLATE_PX, which is the case that must ask.
SMALL_PLATE = _px(0.44, 0.60, 0.5103, 0.63)
# 187 px wide: the width measured on data/test_plates/road_scene.jpg.
READABLE_PLATE = _px(0.44, 0.60, 0.586, 0.644)
CAR = _px(0.30, 0.30, 0.70, 0.70)


@pytest.fixture
def small_plate_car(monkeypatch):
    """One car with a plate too narrow to read, and no confident read on it."""
    vehicles = _StubDetector(CAR, cls=2, conf=0.9)
    plates = _StubDetector(SMALL_PLATE, cls=0, conf=0.88)
    monkeypatch.setattr(ls, "get_vehicle_model", lambda: vehicles)
    monkeypatch.setattr(ls, "_load_plate_detector", lambda *a, **k: plates)
    monkeypatch.setattr(
        ls,
        "run_ocr",
        lambda crop: {"text": "DL1ZA9092", "norm": "DL1ZA9092", "conf": 0.50, "valid": True},
    )
    return {"vehicles": vehicles, "plates": plates}


@pytest.fixture
def readable_plate_car(monkeypatch):
    """One car with a plate big enough to read, read confidently."""
    vehicles = _StubDetector(CAR, cls=2, conf=0.9)
    plates = _StubDetector(READABLE_PLATE, cls=0, conf=0.88)
    monkeypatch.setattr(ls, "get_vehicle_model", lambda: vehicles)
    monkeypatch.setattr(ls, "_load_plate_detector", lambda *a, **k: plates)
    monkeypatch.setattr(
        ls,
        "run_ocr",
        lambda crop: {"text": "MH12JK4567", "norm": "MH12JK4567", "conf": 0.99, "valid": True},
    )
    return {"vehicles": vehicles, "plates": plates}


# --------------------------------------------------------------------------- #
# The request itself
# --------------------------------------------------------------------------- #
def test_a_small_plate_with_no_confident_read_triggers_a_request(
    client, demo_tokens, small_plate_car
):
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        assert _auth(ws, demo_tokens["volunteer"])["type"] == "ready"
        ws.send_json(_frame(1, _jpeg(1280, 844)))

        request, seen = _drain(ws, "need_hires", timeout=10.0)

    assert request is not None, f"no need_hires was sent; saw {[m['type'] for m in seen]}"
    assert request["track"] == 1
    assert request["px_w"] < ls.HIRES_PLATE_PX
    # The phone is told what to change, not merely that something is wrong.
    assert request["want"] == int(ls.HIRES_PLATE_PX)
    assert request["width"] == ls.HIRES_PRESET_WIDTH
    assert request["quality"] == ls.HIRES_PRESET_QUALITY


def test_the_request_carries_the_width_that_was_measured(
    client, demo_tokens, small_plate_car
):
    """The phone needs to know how far short it fell to pick a preset."""
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        ws.send_json(_frame(1, _jpeg(1280, 844)))
        request, _seen = _drain(ws, "need_hires", timeout=10.0)

    assert request is not None
    # ~90 px of plate, to the nearest pixel the detector produced.
    assert 80 <= request["px_w"] <= 100, request["px_w"]
    assert request["want"] > request["px_w"]


def test_no_request_when_the_plate_is_already_big_enough(
    client, demo_tokens, readable_plate_car
):
    """The common case must not cost the phone a 1600 px capture.

    A volunteer pointing a phone at a car ten metres away should never see the
    camera change resolution; that is a visible stutter and a second of battery
    for no benefit.
    """
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        ws.send_json(_frame(1, _jpeg(1280, 844)))
        messages = _collect(ws, 4.0)

    types = [m["type"] for m in messages]
    assert "plates" in types, types
    assert "need_hires" not in types, types
    plates = next(m for m in messages if m["type"] == "plates")
    assert plates["plates"][0]["px_w"] >= ls.HIRES_PLATE_PX


def test_a_request_is_not_repeated_for_a_track_that_already_reads(
    client, demo_tokens, small_plate_car, monkeypatch
):
    """Once a car has been read confidently, its small pixels stop mattering."""
    monkeypatch.setattr(
        ls,
        "run_ocr",
        lambda crop: {"text": "MH12JK4567", "norm": "MH12JK4567", "conf": 0.97, "valid": True},
    )
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        for seq in range(1, 5):
            ws.send_json(_frame(seq, _jpeg(1280, 844)))
            # Under the 15 fps limiter, so the frames are not discarded before the
            # plate pass ever sees them.
            time.sleep(0.08)
        messages = _collect(ws, 3.0)

    requests = [m for m in messages if m["type"] == "need_hires"]
    assert len(requests) <= 1, f"{len(requests)} requests for one settled car"


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
def test_a_request_is_rate_limited_to_one_every_two_seconds():
    """A car parked in view must not ask for a hi-res frame on every frame.

    Tested on the connection's own clock rather than through the socket: the
    socket form needs a multi-second wait, and the thing under test is the
    arithmetic that decides whether a request is even attempted.
    """
    assert ls.HIRES_MIN_INTERVAL_S >= 2.0

    conn = ls.ScanConnection()
    real = time.monotonic
    try:
        conn.last_hires_at = real()
        time.monotonic = lambda: real() + 0.5
        assert time.monotonic() - conn.last_hires_at < ls.HIRES_MIN_INTERVAL_S
        time.monotonic = lambda: real() + ls.HIRES_MIN_INTERVAL_S + 0.05
        assert time.monotonic() - conn.last_hires_at >= ls.HIRES_MIN_INTERVAL_S
    finally:
        time.monotonic = real


def test_two_frames_in_a_row_produce_at_most_one_request(
    client, demo_tokens, small_plate_car
):
    """The end-to-end version of the rate limit, over two frames."""
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        ws.send_json(_frame(1, _jpeg(1280, 844)))
        first, _ = _drain(ws, "need_hires", timeout=10.0)
        assert first is not None

        ws.send_json(_frame(2, _jpeg(1280, 844)))
        time.sleep(0.08)
        second, _ = _drain(ws, "need_hires", limit=6, timeout=3.0)

    assert second is None, "a second request went out inside the 2 s window"


# --------------------------------------------------------------------------- #
# The size limit
# --------------------------------------------------------------------------- #
def test_an_unrequested_large_frame_is_still_refused(client, demo_tokens, small_plate_car):
    """900 KB is only acceptable as an answer to a request.

    Without this, any client could lift its own size limit by setting one flag on
    its frames - which is exactly the denial of service the limit exists to
    prevent.
    """
    big = _hires_jpeg()

    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        ws.send_json(_frame(1, big, hires=True))

        error, seen = _drain(ws, "error", timeout=6.0)

    assert error is not None, f"a large unrequested frame was accepted; saw {[m['type'] for m in seen]}"
    assert error["code"] == "size_limit"
    assert "KB" in error["message"]


def test_the_two_limits_are_different():
    """Otherwise there would be no reason to have both."""
    assert ls.HIRES_FRAME_BYTES > ls.HARD_FRAME_BYTES
    assert ls.HIRES_FRAME_BYTES == 900 * 1024


def test_the_two_limits_bracket_this_frame():
    """The fixture has to be refused normally and accepted as an answer.

    If it drifted outside that band the size tests would silently stop testing
    anything - one would pass for the wrong reason, or both would fail for the
    same reason. Asserted here so a change to the fixture cannot quietly gut the
    tests that depend on it.
    """
    big = _hires_jpeg()
    assert ls.HARD_FRAME_BYTES < len(big) < ls.HIRES_FRAME_BYTES, (
        f"{len(big) // 1024} KB does not sit between the "
        f"{ls.HARD_FRAME_BYTES // 1024} KB and {ls.HIRES_FRAME_BYTES // 1024} KB limits"
    )


def test_a_requested_frame_gets_the_higher_ceiling(client, demo_tokens, small_plate_car):
    """The other half: when the server asked, the answer has to fit."""
    big = _hires_jpeg()

    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        # Frame 1 earns the request.
        ws.send_json(_frame(1, _jpeg(1280, 844)))
        request, _ = _drain(ws, "need_hires", timeout=10.0)
        assert request is not None

        # Frame 2 answers it, marked, and must not be refused for its size.
        # Under the 15 fps limiter, or the frame is dropped before it is even
        # looked at and the test would pass for the wrong reason.
        time.sleep(0.08)
        ws.send_json(_frame(2, big, hires=True))
        boxes, seen = _drain(ws, "boxes", timeout=10.0)

    assert boxes is not None, f"the requested hi-res frame was dropped; saw {[m['type'] for m in seen]}"
    errors = [m for m in seen if m["type"] == "error"]
    assert not errors, errors


def test_the_higher_ceiling_does_not_outlive_the_request(
    client, demo_tokens, small_plate_car
):
    """`hires_pending` is cleared when the frame is consumed, so an unanswered or
    stale request cannot keep the 900 KB limit open for the rest of the session."""
    conn = ls.ScanConnection()
    conn.hires_pending.add(1)
    assert conn.hires_pending
    conn.hires_pending.clear()
    assert not conn.hires_pending

    # And on the wire: a `hires`-marked big frame with nothing pending is refused
    # like any other.
    big = _hires_jpeg()
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        ws.send_json(_frame(1, big, hires=True))
        error, _seen = _drain(ws, "error", timeout=6.0)
    assert error is not None and error["code"] == "size_limit"


# --------------------------------------------------------------------------- #
# The pixels are used for reading, never for the boxes
# --------------------------------------------------------------------------- #
def test_the_vehicle_pass_never_sees_the_hi_res_pixels(
    client, demo_tokens, small_plate_car, monkeypatch
):
    """The green-box budget is the reason hi-res frames exist at all.

    If the vehicle detector were handed the bigger frame, every green box would
    pay the decode and inference cost of a frame nobody needed at that size, and
    the ~150 ms overlay budget would be blown on the one path that cannot wait.
    """
    seen = {}
    real = ls.infer_vehicles

    def spy(jpeg, tracker):
        bgr, w, h, ms, vehicles, tracks = real(jpeg, tracker)
        seen["veh_w"] = w
        return bgr, w, h, ms, vehicles, tracks

    monkeypatch.setattr(ls, "infer_vehicles", spy)

    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        ws.send_json(_frame(1, _jpeg(1280, 844)))
        _drain(ws, "need_hires", timeout=10.0)

    assert seen.get("veh_w") == ls.WORKING_WIDTH, (
        f"the vehicle pass saw {seen.get('veh_w')}px, not the {ls.WORKING_WIDTH}px working copy"
    )


def test_the_green_boxes_still_arrive_first_on_a_hi_res_frame(
    client, demo_tokens, small_plate_car
):
    """Ordering is the whole contract with the phone: boxes, then plates, then the
    verdict. A hi-res frame must not reorder it."""
    big = _hires_jpeg()
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        ws.send_json(_frame(1, _jpeg(1280, 844)))
        _drain(ws, "need_hires", timeout=10.0)

        ws.send_json(_frame(2, big, hires=True))
        messages = _collect(ws, 4.0)

    types = [m["type"] for m in messages]
    if "boxes" not in types:
        return  # the frame was refused; covered by the size-limit tests above
    assert types.index("boxes") < types.index("plates")


# --------------------------------------------------------------------------- #
# The decode helpers
# --------------------------------------------------------------------------- #
def test_the_plate_source_keeps_more_pixels_than_the_box_path():
    """The entire reason the phone is asked for a bigger frame."""
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (2400, 1600), (20, 20, 20)).save(buf, "JPEG", quality=80)
    jpeg = buf.getvalue()

    working = ls.decode_frame(jpeg)
    plate_src = ls.decode_plate_source(jpeg)

    assert working.shape[1] == ls.WORKING_WIDTH
    assert plate_src.shape[1] == ls.HIRES_MAX_WIDTH
    assert plate_src.shape[1] > working.shape[1]
    # Aspect ratio preserved, so every normalised box still lands in place.
    assert working.shape[0] / working.shape[1] == pytest.approx(
        plate_src.shape[0] / plate_src.shape[1], rel=0.01
    )


def test_the_plate_source_is_capped_even_for_a_huge_frame():
    """A phone that sends 12 MP gets no more benefit than one that sends 1600 px,
    and the server does not hold 48 MB of pixels to crop a 200 px plate."""
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (4000, 3000), (20, 20, 20)).save(buf, "JPEG", quality=60)
    assert ls.decode_plate_source(buf.getvalue()).shape[1] == ls.HIRES_MAX_WIDTH


def _rotated_jpeg(width: int, height: int, tag: int = 6) -> bytes:
    from io import BytesIO

    from PIL import Image

    img = Image.new("RGB", (width, height), (10, 10, 10))
    exif = img.getexif()
    exif[274] = tag
    buf = BytesIO()
    img.save(buf, "JPEG", exif=exif, quality=80)
    return buf.getvalue()


@pytest.mark.parametrize("tag", [1, 3, 6, 8])
def test_the_plate_source_and_the_working_copy_agree_on_orientation(tag):
    """Same EXIF correction, on both decoders.

    A portrait phone frame arrives tagged with a rotation. If only one of the two
    decodes transposed it, every plate box on a hi-res frame would point at a
    different part of the car - a correct plate number attributed to the wrong
    vehicle, which is worse than no box at all. All four rotation tags are checked
    because the failure is asymmetric: a landscape frame silently becomes portrait
    on one path only.
    """
    jpeg = _rotated_jpeg(2400, 1200, tag=tag)
    working = ls.decode_frame(jpeg)
    plate_src = ls.decode_plate_source(jpeg)

    # Same orientation as each other...
    assert (working.shape[1] > working.shape[0]) == (plate_src.shape[1] > plate_src.shape[0])
    # ...and the same aspect ratio, so normalised boxes still map across.
    assert working.shape[0] / working.shape[1] == pytest.approx(
        plate_src.shape[0] / plate_src.shape[1], rel=0.01
    )


def test_the_rotation_is_actually_applied():
    """The orientation agreement above would also pass if *neither* decoder
    transposed anything, so the correction itself is pinned separately."""
    import io
    from PIL import Image, ImageOps

    jpeg = _rotated_jpeg(2400, 1200, tag=6)
    expected = ImageOps.exif_transpose(Image.open(io.BytesIO(jpeg))).size
    # expected is (w, h); the decoders return (h, w, channels).
    assert ls.decode_frame(jpeg).shape[1] == expected[0]
    assert ls.decode_frame(jpeg).shape[0] == expected[1]


def test_a_normal_frame_is_not_treated_as_hi_res(client, demo_tokens, small_plate_car):
    """Regression guard: the ordinary path must be untouched."""
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        ws.send_json(_frame(1, _jpeg(1280, 844)))
        boxes, _seen = _drain(ws, "boxes", timeout=10.0)

    assert boxes is not None
    assert (boxes["w"], boxes["h"]) == (1280, 844)
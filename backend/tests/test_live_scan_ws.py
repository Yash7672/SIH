"""End-to-end behaviour of the live-scan WebSocket.

These run the real handler through the test client with the *detectors stubbed*
and the database real. Stubbing the models is deliberate: what is asserted here is
the wire contract, the tracking, the hot-list policy and the backpressure - none of
which should be re-verified by a slow, non-deterministic model on every test run.
The models themselves are exercised against real images by
`scripts/make_test_road_scene.py --check` and `scripts/live_scan_test.py`.

The privacy tests at the bottom are the ones that matter most: reading a plate off
a volunteer's camera must not write it anywhere unless it is on the hot-list.
"""

import base64
import io
import json
import queue
import threading
import time

import numpy as np
import pytest
from PIL import Image
from starlette.websockets import WebSocketDisconnect

from app.api.v1 import live_scan as ls

# Deliberately not MH12JK4567 or TS09AB1234: those are the canonical plates used by
# the other suites, one hot-listed and one not, and this file needs both states.
HOTLISTED_PLATE = "KL01QR4455"
UNLISTED_PLATE = "MH12JK4567"


# --------------------------------------------------------------------------- #
# stubs
# --------------------------------------------------------------------------- #
class _Boxes:
    """A minimal stand-in for ultralytics' Boxes container."""

    def __init__(self, xyxy, cls, conf):
        self.xyxy = xyxy
        self.cls = cls
        self.conf = conf

    def __len__(self):
        return len(self.xyxy)


class _Result:
    def __init__(self, boxes):
        self.boxes = boxes


class _StubDetector:
    """Returns fixed boxes, whatever image it is handed.

    `xyxy` is one box (`[x1, y1, x2, y2]`) or several (`[[...], [...]]`), so a test
    can put one car or three in shot without a second stub class.

    Boxes are torch tensors because that is what ultralytics actually returns,
    and `live_scan` calls `.detach().cpu().numpy()` on them. A stub that handed
    back numpy arrays would fail in a way that has nothing to do with the test.
    """

    def __init__(self, xyxy, cls, conf):
        self._xyxy = None if xyxy is None else np.asarray(xyxy, dtype=np.float32)
        self._cls = cls
        self._conf = conf
        self.calls = 0
        self.imgsz = None

    def __call__(self, *args, **kwargs):
        import torch

        self.calls += 1
        self.imgsz = kwargs.get("imgsz")
        if self._xyxy is None:
            return [_Result(None)]
        rows = self._xyxy.reshape(1, -1) if self._xyxy.ndim == 1 else self._xyxy
        n = len(rows)
        return [
            _Result(
                _Boxes(
                    torch.tensor(rows, dtype=torch.float32),
                    torch.full((n,), self._cls, dtype=torch.float32),
                    torch.full((n,), self._conf, dtype=torch.float32),
                )
            )
        ]


# The frame geometry every helper below agrees on. The app captures 1280 px wide
# and the server's WORKING_WIDTH is the same, so nothing is resampled in between
# and a normalised box here is the box the stub detector reported.
FRAME_W = 1280
FRAME_H = 844


def _jpeg(width: int = FRAME_W, height: int = FRAME_H, quality: int = 40) -> bytes:
    """A small, compressible frame - the size the app is specified to send."""
    rng = np.random.default_rng(3)
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)
    arr[:, :, 1] = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
    arr[:, :, 2] = rng.integers(0, 40, (height, width), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _recv(ws, timeout: float):
    """One parsed message, or None when the server says nothing within `timeout`.

    `WebSocketTestSession.receive()` blocks for ever once the app has finished, and
    several of these tests have to assert that something does *not* arrive - so the
    deadline has to be enforced somewhere. The send queue is read directly because
    there is no public bounded read, and the text is parsed here because
    `receive_json()` does that too.
    """
    try:
        message = ws._send_queue.get(timeout=timeout)
    except queue.Empty:
        return None
    if isinstance(message, BaseException):
        raise message
    if message["type"] == "websocket.close":
        raise WebSocketDisconnect(code=message.get("code", 1000), reason=message.get("reason", ""))
    return json.loads(message["text"])


def _drain(ws, wanted: str, limit: int = 12, timeout: float = 8.0):
    """Read until a message of type `wanted` arrives, or give up. Returns (msg, seen)."""
    deadline = time.time() + timeout
    seen = []
    while len(seen) < limit:
        left = deadline - time.time()
        if left <= 0:
            return None, seen
        try:
            msg = _recv(ws, left)
        except WebSocketDisconnect:
            return None, seen
        if msg is None:
            return None, seen
        seen.append(msg)
        if msg.get("type") == wanted:
            return msg, seen
    return None, seen


def _collect(ws, seconds: float) -> list:
    """Everything the server says within `seconds`, then stop."""
    out: list = []
    deadline = time.time() + seconds
    while True:
        left = deadline - time.time()
        if left <= 0:
            return out
        try:
            msg = _recv(ws, left)
        except (WebSocketDisconnect, Exception):  # noqa: BLE001
            return out
        if msg is None:
            return out
        out.append(msg)


def _auth(ws, token, device_id=None):
    """Authenticate and consume the server's `ready`, so the caller reads from here on."""
    ws.send_json({"type": "auth", "token": token, "device_id": device_id})
    return ws.receive_json()


def _frame(seq: int, lat=17.4, lng=78.4, jpeg: bytes | None = None):
    return {
        "type": "frame",
        "seq": seq,
        "w": FRAME_W,
        "h": FRAME_H,
        "lat": lat,
        "lng": lng,
        "jpeg_b64": base64.b64encode(jpeg if jpeg is not None else _jpeg()).decode(),
    }


def _read(plate="MH12JK4567", conf=0.94):
    """A stubbed OCR read, for the tests about what happens *around* a read."""
    return lambda crop: {"text": plate, "norm": plate, "conf": conf, "valid": True}


@pytest.fixture
def one_car(monkeypatch):
    """One car in the middle of the frame with one plate inside it.

    The stub is given *pixel* boxes because that is what ultralytics returns;
    normalising to 0..1 is the server's job and is part of what is under test.
    The pixels are derived from FRAME_W/FRAME_H rather than written out, so the
    normalised expectations below cannot drift away from the frame that is
    actually sent.
    """
    def px(nx1, ny1, nx2, ny2):
        return [nx1 * FRAME_W, ny1 * FRAME_H, nx2 * FRAME_W, ny2 * FRAME_H]

    vehicles = _StubDetector(px(0.3, 0.3, 0.7, 0.7), cls=2, conf=0.91)
    plates = _StubDetector(px(0.44, 0.6, 0.56, 0.63), cls=0, conf=0.88)
    monkeypatch.setattr(ls, "get_vehicle_model", lambda: vehicles)
    monkeypatch.setattr(ls, "_load_plate_detector", lambda *a, **k: plates)
    return {"vehicles": vehicles, "plates": plates}


@pytest.fixture
def three_cars(monkeypatch):
    """Three cars, each with its own plate.

    Needed by the OCR-queue test, and the reason it is its own fixture: with a
    single car in shot there is only ever one crop to OCR, so a queue that grows
    without limit is indistinguishable from a queue of one, and any assertion
    about queue depth passes whatever the scheduler does. Three vehicles put three
    crops per frame on the table, which is where the difference shows.
    """
    def px(nx1, ny1, nx2, ny2):
        return [nx1 * FRAME_W, ny1 * FRAME_H, nx2 * FRAME_W, ny2 * FRAME_H]

    cars = [(0.02, 0.34, 0.28, 0.74), (0.36, 0.30, 0.62, 0.70), (0.70, 0.36, 0.96, 0.76)]
    plate_centres = (0.15, 0.49, 0.83)

    vehicles = _StubDetector([px(*c) for c in cars], cls=2, conf=0.9)
    plates = _StubDetector(
        [px(c - 0.06, 0.60, c + 0.06, 0.63) for c in plate_centres], cls=0, conf=0.85
    )
    monkeypatch.setattr(ls, "get_vehicle_model", lambda: vehicles)
    monkeypatch.setattr(ls, "_load_plate_detector", lambda *a, **k: plates)
    return {"vehicles": vehicles, "plates": plates, "cars": cars}


@pytest.fixture
def register_device(client, demo_tokens):
    def _register(name="LiveCam"):
        r = client.post(
            "/api/v1/devices/register",
            headers={"Authorization": f"Bearer {demo_tokens['volunteer']}"},
            json={"device_type": "mobile", "device_name": name},
        )
        assert r.status_code == 200, r.text
        return r.json()["id"]

    return _register


@pytest.fixture
def hotlisted(client, demo_tokens):
    """Put a plate on the active hot-list through the real complaint flow.

    The plate is unique to this file, and the entry is closed again on teardown.
    Both matter: `test_sightings.py` uses MH12JK4567 as its canonical
    *not-hot-listed* plate, and a leaked ACTIVE entry for it would turn that test
    inside out (it passed alone and failed in the full run, which is exactly the
    signature of this kind of leak).
    """
    from app.db.session import SessionLocal
    from app.services.hotlist_service import HotlistService

    plate = HOTLISTED_PLATE
    complaint = client.post(
        "/api/v1/complaints",
        headers={"Authorization": f"Bearer {demo_tokens['citizen']}"},
        data={"plate": plate, "complaint_type": "Stolen", "description": "test"},
    ).json()
    verified = client.post(
        f"/api/v1/complaints/{complaint['id']}/verify",
        headers={"Authorization": f"Bearer {demo_tokens['cop']}"},
    )
    assert verified.status_code == 200, verified.text
    hotlist_id = verified.json()["hotlist_id"]

    yield hotlist_id

    db = SessionLocal()
    try:
        HotlistService(db).close(hotlist_id)
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #
def test_a_frame_is_answered_with_boxes_then_plates(client, demo_tokens, one_car):
    """Two replies, boxes first.

    The overlay needs the green boxes at ~90 ms. Folding the plate pass into the
    same message would make every green box wait for it, which measured ~190 ms
    here and made the overlay visibly stutter.
    """
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        assert _auth(ws, demo_tokens["volunteer"])["type"] == "ready"
        ws.send_json(_frame(7))

        boxes, seen = _drain(ws, "boxes")

        assert boxes is not None, f"no boxes message; saw {[m.get('type') for m in seen]}"
        assert boxes["seq"] == 7
        assert (boxes["w"], boxes["h"]) == (FRAME_W, FRAME_H)
        assert boxes["ms"] >= 0
        assert len(boxes["vehicles"]) == 1
        assert boxes["vehicles"][0]["label"] == "CAR 1"
        assert boxes["vehicles"][0]["cls"] == 2
        assert boxes["vehicles"][0]["track"] == 1
        # Pixel boxes in, normalised boxes out.
        assert boxes["vehicles"][0]["box"] == pytest.approx([0.3, 0.3, 0.7, 0.7], abs=1e-4)

        plates, _ = _drain(ws, "plates", limit=2, timeout=5.0)
        assert plates is not None, "the plate box must arrive as its own message"
        assert len(plates["plates"]) == 1
        assert plates["plates"][0]["track"] == 1
        assert plates["plates"][0]["box"] == pytest.approx([0.44, 0.6, 0.56, 0.63], abs=1e-4)


def test_the_detectors_run_at_their_pinned_sizes(client, demo_tokens, one_car):
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        ws.send_json(_frame(1))
        _drain(ws, "plates", limit=3)

    # The vehicle pass is the one size free to move (down to 384, for the 120 ms
    # budget). The plate pass must stay at 640: a looser box there means a
    # differently padded crop and a different OCR input, and plate accuracy is not
    # being traded away for a few milliseconds.
    assert one_car["vehicles"].imgsz == ls.VEHICLE_IMGSZ == 384
    assert one_car["plates"].imgsz == ls.PLATE_IMGSZ == 640


def test_a_track_keeps_its_id_across_a_stream(client, demo_tokens, one_car):
    """Every `boxes` message must key the same car by the same track id."""
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])

        seen_ids = set()
        for seq in range(3):
            ws.send_json(_frame(seq))
            # Just under the server's 15 fps rate limit, so the frame is not
            # discarded by the limiter before it ever reaches the tracker.
            time.sleep(0.07)
            boxes, _ = _drain(ws, "boxes")
            assert boxes is not None, f"no boxes for seq {seq}"
            seen_ids.add(boxes["vehicles"][0]["track"])

    assert seen_ids == {1}


def test_the_tracker_is_per_connection(client, demo_tokens, one_car):
    """Two phones must never hand each other track ids.

    A shared tracker would also let one phone's missed frames expire the other
    phone's tracks, so both connections independently see track 1 / "CAR 1".
    """
    with client.websocket_connect("/api/v1/ws/scan") as a:
        with client.websocket_connect("/api/v1/ws/scan") as b:
            _auth(a, demo_tokens["volunteer"])
            _auth(b, demo_tokens["volunteer"])

            a.send_json(_frame(1))
            first, _ = _drain(a, "boxes")
            b.send_json(_frame(1))
            second, _ = _drain(b, "boxes")

    assert first["vehicles"][0]["track"] == second["vehicles"][0]["track"] == 1
    assert first["vehicles"][0]["label"] == second["vehicles"][0]["label"] == "CAR 1"


def test_a_burst_of_frames_is_answered_at_most_once_each(client, demo_tokens, one_car):
    """Latest frame wins, and nothing is answered twice.

    Without the single in-flight slot, a phone streaming faster than the CPU can
    infer would build a queue of stale views of the road and every box would be
    seconds out of date. `seq` is echoed back, so a duplicate answer is visible.
    """
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        # Paced just under the server's 15 fps rate limit so the frames are not
        # all discarded by the limiter before the in-flight slot is contested.
        for seq in range(8):
            ws.send_json(_frame(seq))
            time.sleep(0.07)

        messages = _collect(ws, 1.5)

    seqs = [m["seq"] for m in messages if m.get("type") == "boxes"]
    assert len(seqs) == len(set(seqs)), f"a frame was answered twice: {seqs}"
    assert seqs == sorted(seqs), "frames were answered out of order"
    assert len(seqs) >= 5, f"only {len(seqs)} of 8 frames were answered"


def test_a_slow_plate_pass_does_not_cost_the_next_frame_its_boxes(client, demo_tokens, one_car, monkeypatch):
    """The vehicle slot is released at `boxes`, not at the end of the plate pass.

    The phone paces on `boxes` and offers the next frame ~120 ms later, which lands
    squarely inside the plate pass. Holding one slot across both stages therefore
    threw away roughly one frame in ten (measured 2 of 20 on the stream benchmark),
    and the phone saw it as a stalled feed. A deliberately slow plate detector is
    the worst case this has to survive: if the boxes still come back, the split
    holds.
    """
    def _slow_plates(*args, **kwargs):
        time.sleep(0.4)
        return [], {}

    monkeypatch.setattr(ls, "infer_plates", _slow_plates)

    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        for seq in range(6):
            ws.send_json(_frame(seq))
            # Just under the rate limit, and well inside the 400 ms plate pass.
            time.sleep(0.07)

        messages = _collect(ws, 4.0)

    seqs = [m["seq"] for m in messages if m.get("type") == "boxes"]
    assert len(set(seqs)) == 6, (
        f"a 400 ms plate pass swallowed frames; answered seqs {sorted(set(seqs))} of 0-5"
    )


def test_ping_is_answered_with_pong(client, demo_tokens, one_car):
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        ws.send_json({"type": "ping", "t": 12345})

        assert ws.receive_json() == {"type": "pong", "t": 12345}


def test_an_oversized_frame_is_refused_with_a_readable_error(client, demo_tokens, one_car):
    """`size_limit` is the only honest answer to a frame that cannot be used."""
    payload = b"\xff\xd8\xff\xe0" + b"\x00" * (ls.HARD_FRAME_BYTES + 1)
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        ws.send_json(_frame(1, jpeg=payload))

        msg = ws.receive_json()

    assert msg["type"] == "error"
    assert msg["code"] == "size_limit"
    assert "KB" in msg["message"]
    assert one_car["vehicles"].calls == 0, "a refused frame must not reach the model"


def test_a_malformed_frame_is_reported_not_crashed(client, demo_tokens, one_car):
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        ws.send_json({"type": "frame", "seq": 1, "w": FRAME_W, "h": FRAME_H, "jpeg_b64": "!!not base64!!"})

        assert ws.receive_json() == {
            "type": "error",
            "code": "bad_frame",
            "message": "Frame was not valid base64",
        }

        ws.send_json({"type": "ping", "t": 1})
        assert ws.receive_json()["type"] == "pong", "the socket must survive a bad frame"


def test_a_missing_vehicle_model_is_reported_honestly(client, demo_tokens, monkeypatch):
    """The app shows "vehicle model missing" instead of an empty preview."""

    def _boom():
        raise RuntimeError("vehicle model missing")

    monkeypatch.setattr(ls, "get_vehicle_model", _boom)
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        ws.send_json(_frame(1))

        assert ws.receive_json() == {
            "type": "error",
            "code": "missing_model",
            "message": "Vehicle model missing on server",
        }


def test_the_fourth_connection_is_refused_not_left_hanging(client, demo_tokens, one_car):
    """Demo mode is three phones at most, and the fourth is told so."""
    opened = []
    try:
        for _ in range(ls.MAX_CONNECTIONS):
            ws = client.websocket_connect("/api/v1/ws/scan")
            ws.__enter__()
            assert _auth(ws, demo_tokens["volunteer"])["type"] == "ready"
            opened.append(ws)

        # `__enter__` performs the handshake and raises straight away if the server
        # closes during it; the receive below covers the case where it does not.
        extra = client.websocket_connect("/api/v1/ws/scan")
        with pytest.raises(WebSocketDisconnect):
            extra.__enter__()
            extra.receive_json()
    finally:
        for ws in opened:
            ws.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# the privacy guarantee
# --------------------------------------------------------------------------- #
def test_reading_a_plate_that_is_not_hotlisted_stores_nothing(
    client, demo_tokens, one_car, register_device, monkeypatch
):
    """The core privacy contract, end to end over the socket.

    A plate the server manages to read off a volunteer's camera is shown back to
    that one phone and then forgotten: no sighting row, no police alert. Only a
    plate on the active hot-list is ever persisted.

    The read is stubbed because this is a test about what happens *around* a read,
    and a stubbed read is the only way to get a valid plate past the detector
    deterministically.
    """
    from app.db.session import SessionLocal
    from app.models import Sighting

    monkeypatch.setattr(ls, "run_ocr", _read(UNLISTED_PLATE))
    broadcasts = []

    async def _fake_broadcast(event, payload):
        broadcasts.append((event, payload))

    monkeypatch.setattr(ls.alert_manager, "broadcast", _fake_broadcast)

    device_id = register_device("PrivacyCam")

    db = SessionLocal()
    try:
        before = db.query(Sighting).count()
    finally:
        db.close()

    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"], device_id=device_id)
        ws.send_json(_frame(1))
        plate, seen = _drain(ws, "plate", limit=6, timeout=10.0)

    assert plate is not None, f"no plate message; saw {[m.get('type') for m in seen]}"
    assert plate["text"] == UNLISTED_PLATE, "the read must still reach the phone"
    assert plate["valid"] is True
    assert plate["stolen"] is False
    assert (plate["track"], plate["label"]) == (1, "CAR 1")

    db = SessionLocal()
    try:
        assert db.query(Sighting).count() == before, "an unlisted plate was persisted"
    finally:
        db.close()
    assert broadcasts == [], "a police alert was raised for a plate that is not stolen"


def test_a_hotlisted_plate_is_recorded_and_alerted(
    client, demo_tokens, one_car, register_device, hotlisted, monkeypatch
):
    """The other half of the same contract: a real hit must reach the dashboards."""
    from app.db.session import SessionLocal
    from app.models import Sighting

    monkeypatch.setattr(ls, "run_ocr", _read(HOTLISTED_PLATE))
    broadcasts = []

    async def _fake_broadcast(event, payload):
        broadcasts.append((event, payload))

    monkeypatch.setattr(ls.alert_manager, "broadcast", _fake_broadcast)

    device_id = register_device("HitCam")

    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"], device_id=device_id)
        ws.send_json(_frame(1))
        plate, seen = _drain(ws, "plate", limit=6, timeout=10.0)

    assert plate is not None, f"no plate message; saw {[m.get('type') for m in seen]}"
    assert plate["stolen"] is True

    db = SessionLocal()
    try:
        sighting = db.query(Sighting).filter(Sighting.confidence == 0.94).one()
        assert str(sighting.hotlist_id) == str(hotlisted)
        assert sighting.latitude == pytest.approx(17.4)
    finally:
        db.close()

    assert [event for event, _payload in broadcasts] == ["hotlist_detection"]
    assert broadcasts[0][1]["plate"] == HOTLISTED_PLATE


def test_an_unreadable_plate_still_draws_the_vehicle(client, demo_tokens, one_car, monkeypatch):
    """A failed read must blank nothing.

    `run_ocr` returns None for anything below the confidence floor or that fails
    to normalise, so there is no path from "unreadable" to "STOLEN VEHICLE" - and
    the green box is still there, because the overlay is not a read receipt.
    """
    monkeypatch.setattr(ls, "run_ocr", lambda crop: None)

    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        ws.send_json(_frame(1))

        boxes, seen = _drain(ws, "boxes", limit=2)
        tail = _collect(ws, 0.8)

    assert boxes is not None, f"no boxes message; saw {[m.get('type') for m in seen]}"
    assert boxes["vehicles"][0]["label"] == "CAR 1"
    assert all(m.get("type") != "plate" for m in tail), "a failed read must not report a plate"


def test_a_read_is_attempted_only_a_few_times_per_track(client, demo_tokens, one_car, monkeypatch):
    """A car parked in view must not occupy the OCR worker for ever.

    OCR costs seconds on this CPU. Without a per-track budget, one stationary car
    would be re-read on every frame for as long as it stayed in shot.
    """
    attempts = []

    def _counting_read(crop):
        attempts.append(1)
        return None  # never settles early, so only the budget can stop it

    monkeypatch.setattr(ls, "run_ocr", _counting_read)

    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        for seq in range(8):
            ws.send_json(_frame(seq))
            time.sleep(0.07)

        _collect(ws, 1.5)

    assert 0 < len(attempts) <= ls.MAX_READ_ATTEMPTS + 1, (
        f"expected at most {ls.MAX_READ_ATTEMPTS} read attempts for one track, "
        f"got {len(attempts)}"
    )


# --------------------------------------------------------------------------- #
# OCR capacity
# --------------------------------------------------------------------------- #
def test_a_slow_read_never_queues_more_ocr_than_the_gate_allows(client, demo_tokens, three_cars, monkeypatch):
    """The OCR pipeline never holds more than `OCR_MAX_IN_FLIGHT` crops.

    OCR is ~2 s per crop on this CPU while a frame is ~200 ms, so a screen left
    open for a minute offers far more crops than the single worker can retire.
    Without a gate the queue grows without limit, every queued crop keeps burning
    cores the green boxes need, and the boxes visibly slow down.

    The depth is measured at the *producer*: `run_in_executor` submits through
    `ocr_pool.submit`, and the pool has one worker, so counting calls into
    `run_ocr` itself would report a depth of 1 whatever the gate did. Counting
    submissions and completions instead sees the crops that are waiting, and so
    fails if the gate is ever removed.
    """
    guard = threading.Lock()
    counts = {"submitted": 0, "finished": 0}
    real_submit = ls.ocr_pool.submit

    def _counting_submit(*args, **kwargs):
        with guard:
            counts["submitted"] += 1
        return real_submit(*args, **kwargs)

    def _slow_read(crop):
        time.sleep(0.25)
        with guard:
            counts["finished"] += 1
        return None

    monkeypatch.setattr(ls.ocr_pool, "submit", _counting_submit)
    monkeypatch.setattr(ls, "run_ocr", _slow_read)

    peak = 0
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"])
        for seq in range(30):
            ws.send_json(_frame(seq))
            time.sleep(0.05)
            with guard:
                peak = max(peak, counts["submitted"] - counts["finished"])

        _collect(ws, 4.0)

    with guard:
        assert counts["submitted"] > 0, "the gate starved OCR completely - no crop was ever read"
        assert peak <= ls.OCR_MAX_IN_FLIGHT, (
            f"the OCR pipeline held {peak} crops at once, "
            f"the gate allows {ls.OCR_MAX_IN_FLIGHT}"
        )
        # Every submission must come back: a queue that only drains on disconnect
        # would still satisfy the peak check above.
        assert counts["submitted"] == counts["finished"], (
            f"{counts['submitted']} crops were submitted but only "
            f"{counts['finished']} came back"
        )
    assert ls.ocr_inflight() == 0, (
        f"{ls.ocr_inflight()} OCR slots leaked after the socket closed"
    )


def test_a_crop_dropped_by_the_gate_does_not_spend_the_track_its_read(client, demo_tokens, one_car, monkeypatch):
    """A gated-out crop must not count against the track's read budget.

    The budget is 3 attempts, and the gate can decline a crop on 3 consecutive
    frames. If the declined crops were charged anyway the track would be "settled"
    after three frames with no read ever attempted, and its chip would sit on
    "reading..." for the rest of the shot - the failure looks like a broken OCR
    engine when it is really a scheduling bug.
    """
    monkeypatch.setattr(ls, "OCR_MAX_IN_FLIGHT", 1)

    reads = []
    release = threading.Event()

    def _blocking_read(crop):
        reads.append(1)
        release.set()
        time.sleep(1.2)  # long enough that every later frame is gated out
        return {"text": UNLISTED_PLATE, "norm": UNLISTED_PLATE, "conf": 0.95, "valid": True}

    monkeypatch.setattr(ls, "run_ocr", _blocking_read)

    try:
        with client.websocket_connect("/api/v1/ws/scan") as ws:
            _auth(ws, demo_tokens["volunteer"])
            for seq in range(12):
                ws.send_json(_frame(seq))
                time.sleep(0.05)
            time.sleep(0.3)
            release.set()
            # Collected rather than drained: the boxes/plates pairs for 12 frames
            # would exhaust a bounded search before the read ever arrived, which
            # reads as "never read" when it is really "looked in the wrong place".
            seen = _collect(ws, 6.0)

        reads_msg = next((m for m in seen if m.get("type") == "plate"), None)
        assert reads_msg is not None, (
            f"the plate was never read; saw {[m.get('type') for m in seen]}"
        )
        assert reads_msg["norm"] == UNLISTED_PLATE
        assert len(reads) <= ls.MAX_READ_ATTEMPTS, (
            f"the track was read {len(reads)} times, budget is {ls.MAX_READ_ATTEMPTS}"
        )
        assert ls.ocr_inflight() == 0, "OCR capacity leaked"
    finally:
        release.set()


def test_an_ocr_claim_gives_its_capacity_back_exactly_once():
    """The one-shot release is what makes the two cleanup paths safe together.

    The connection's cleanup hands back any claim still open (a task cancelled
    before its first step never runs its own `finally`), while a running task
    hands its claim back itself. If both ran for the same claim, `_ocr_release`
    would take capacity that belongs to a *different* connection.
    """
    ls._ocr_release(ls.OCR_MAX_IN_FLIGHT)  # start from a known-empty gate
    assert ls.ocr_inflight() == 0

    taken = ls._ocr_reserve(ls.OCR_MAX_IN_FLIGHT)
    assert taken == ls.OCR_MAX_IN_FLIGHT
    assert ls._ocr_reserve(1) == 0, "a full gate handed out capacity it does not have"

    claim = ls.OcrClaim(taken)
    assert claim.open
    claim.release()
    claim.release()  # the double release the cleanup path could cause
    assert not claim.open
    assert ls.ocr_inflight() == 0

    # A partial claim still returns only what it took.
    part = ls._ocr_reserve(1)
    ls.OcrClaim(part).release()
    assert ls.ocr_inflight() == 0

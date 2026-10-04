"""Live-scan frame acceptance and detection coverage.

The auto-detect loop once shipped un-resized 12 MP frames, which the server
rejected outright on size. Every live frame died at the gate, so the detector
never saw a single frame and the symptom read as "detection does not work".

The fix was two thresholds rather than one. Between SOFT and HARD a frame is kept
and downscaled to the working width, because a silently dropped frame is a
missing detection with no explanation. Past HARD it is refused loudly with
`size_limit`, which is the only thing the ceiling is for: a socket-wedging
guard, not an image-quality policy.

These tests pin that behaviour, plus the decode/resize maths every box
coordinate depends on.
"""

import io

import numpy as np
import pytest
from PIL import Image

from app.api.v1 import live_scan as ls
from app.ws.scan_manager import VehicleTracker


class _EmptyResult:
    boxes = None


class _StubModel:
    def __call__(self, *args, **kwargs):
        return [_EmptyResult()]


def _jpeg(width: int, height: int, quality: int = 90) -> bytes:
    """A photo-like frame.

    Smooth gradients plus a little noise, not pure noise: uniform random pixels are
    the worst case JPEG can be handed and no camera sensor produces them, so sizing
    a limit against them would overstate what a handset really sends.
    """
    ys, xs = np.mgrid[0:height, 0:width]
    arr = np.stack(
        [
            (xs * 255 / max(width - 1, 1)),
            (ys * 255 / max(height - 1, 1)),
            ((xs + ys) % 256),
        ],
        axis=-1,
    ).astype(np.uint8)
    rng = np.random.default_rng(0)
    arr = np.clip(arr.astype(np.int16) + rng.integers(-6, 7, arr.shape), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


@pytest.fixture
def stub_models(monkeypatch):
    monkeypatch.setattr(ls, "get_vehicle_model", lambda: _StubModel())
    monkeypatch.setattr(ls, "_load_plate_detector", lambda *a, **k: _StubModel())


def test_phone_contract_frame_is_under_the_soft_limit():
    """The size the app is specified to send must never be treated as oversized.

    1280 px wide at quality 40 is what the capture loop actually produces
    (~70 KB measured on the road-scene fixture). If this ever crossed
    SOFT_FRAME_BYTES the server would start downscalling frames it has no reason
    to touch, and the test would be the only thing saying so.
    """
    frame = _jpeg(1280, 844, quality=40)

    assert len(frame) < ls.SOFT_FRAME_BYTES


def test_working_width_matches_the_width_the_app_sends():
    """The server must not downscale a frame the app sent at full contract size.

    This is not a tidiness check. Decoding a 1280 px frame down to 960 px throws
    away the pixels the plate is made of: measured on the fixture, the read goes
    from `MH12JK4567 @ 0.997` to nothing at all depending on JPEG quality, and to
    `MH12JK4567 @ 0.954` at the one quality this app happens to send. Matching
    WORKING_WIDTH to the capture width also happens to be the cheapest option -
    with no resize to do, a 1280 px frame decodes in 10.7 ms instead of 24.9 ms.
    """
    assert ls.WORKING_WIDTH == 1280


def test_soft_ceiling_stays_below_the_abuse_ceiling():
    """A soft limit above the hard limit would reject everything as 'too large'."""
    assert ls.SOFT_FRAME_BYTES < ls.HARD_FRAME_BYTES


def test_hard_ceiling_refuses_the_raw_12mp_capture():
    """A 12 MP frame at quality 90 must now be *refused*, and that is deliberate.

    This is the opposite of what the old ceiling did: the ceiling used to sit at
    several megabytes, which meant it never refused anything and a 12 MP frame
    arrived as ~10 MB of base64 on every single frame. The ceiling is now a socket
    guard at 400 KB, comfortably above the ~70 KB the app sends and comfortably
    below anything a modern handset would produce at full resolution - the app
    downscales at the source, which is the only place a limit like this belongs.
    """
    assert len(_jpeg(4000, 3000)) > ls.HARD_FRAME_BYTES


def test_frame_over_the_soft_limit_is_downscaled_not_dropped(stub_models):
    """The regression: a frame in the soft band used to be refused before any inference."""
    # Small enough to encode fast, large enough to land between the two limits.
    frame = _jpeg(1600, 1200, quality=90)
    assert ls.SOFT_FRAME_BYTES < len(frame) < ls.HARD_FRAME_BYTES

    w, h, vehicle_ms, plate_ms, vehicles, plates, crops = ls.decode_and_infer(frame, VehicleTracker())

    assert w == ls.WORKING_WIDTH
    assert h == round(1200 * (ls.WORKING_WIDTH / 1600))
    assert (vehicles, plates) == ([], [])
    assert crops == {}, "a frame with no detections cannot produce OCR crops"


def test_small_frame_is_left_at_its_native_size(stub_models):
    """Downscaling must not inflate a frame that is already small enough."""
    w, h, _, _, _, _, _ = ls.decode_and_infer(_jpeg(640, 480), VehicleTracker())

    assert (w, h) == (640, 480)


def test_aspect_ratio_survives_the_resize(stub_models):
    """A portrait phone frame stays portrait; a squashed one breaks OCR."""
    _, h, _, _, _, _, _ = ls.decode_and_infer(_jpeg(3000, 4000), VehicleTracker())

    assert h > ls.WORKING_WIDTH


def test_decode_rotates_a_tagged_portrait_frame():
    """EXIF transpose is not optional.

    A portrait phone frame arrives tagged ROTATE_90. Without the transpose the
    boxes are computed against a sideways image while the preview shows an upright
    one, and every box lands in the wrong place on screen - which looks exactly
    like a tracking bug and is not one.
    """
    upright = Image.new("RGB", (480, 640), (10, 20, 30))
    buf = io.BytesIO()
    upright.save(buf, "JPEG", quality=90, exif=Image.Exif() if hasattr(Image, "Exif") else None)

    # Same picture stored sideways with the rotate tag set: decode must un-swap it.
    sideways = upright.transpose(Image.ROTATE_270)
    exif = sideways.getexif()
    exif[274] = 6  # Orientation: rotate 90 CW to display
    side_buf = io.BytesIO()
    sideways.save(side_buf, "JPEG", quality=90, exif=exif)

    decoded = ls.decode_frame(side_buf.getvalue())

    assert decoded.shape[0] == 640, "EXIF-rotated frame came back the wrong way up"
    assert decoded.shape[1] == 480


def test_two_stages_report_separate_latencies(stub_models):
    """The split that makes the overlay smooth: boxes first, plates after.

    `decode_and_infer` returns both stage timings so the tooling can show the
    number the green boxes are actually held to, rather than the total including
    the plate pass.
    """
    _, _, vehicle_ms, plate_ms, _, _, _ = ls.decode_and_infer(_jpeg(640, 480), VehicleTracker())

    assert vehicle_ms >= 0
    assert plate_ms >= 0


def test_read_budget_can_return_more_than_two_plates():
    """Three crops per frame: a frame with three plates used to drop the third."""
    assert ls.MAX_READS_PER_FRAME >= 3


def test_min_plate_gate_admits_a_distant_plate():
    """A 45 px plate at working width must be read, not skipped as too small.

    The old 60 px gate skipped it, and at 1280-wide frames that is a plate a
    volunteer can plainly see in the viewfinder.
    """
    assert ls.MIN_PLATE_PX <= 50


def test_vehicle_and_plate_pass_sizes_are_pinned():
    """The plate detector must keep running at the size it already ran at.

    PLATE_IMGSZ is load-bearing for plate *accuracy*, not just speed: at 416 the
    detector returns a looser box (conf 0.66 vs 0.70 on the same plate), a looser
    box means a differently padded crop, and a different crop means a different
    OCR input. The plate reading is correct today and is not being risked for a
    few milliseconds.

    VEHICLE_IMGSZ is the one size that was free to move, down to 384, which is
    what keeps the green boxes inside the 120 ms budget on this CPU.
    """
    assert ls.PLATE_IMGSZ == 640
    assert ls.VEHICLE_IMGSZ == 384


def test_only_four_vehicle_classes_are_ever_announced():
    """The overlay has exactly four labels and no AUTO.

    YOLOv8n COCO has no auto-rickshaw class, so a fifth label could never be
    drawn truthfully. This asserts the vocabulary rather than trusting the dict.
    """
    from app.ws.scan_manager import CLASS_LABELS, COCO_CLASS_IDS

    assert set(CLASS_LABELS.values()) == {"CAR", "BIKE", "BUS", "TRUCK"}
    assert sorted(COCO_CLASS_IDS) == sorted(CLASS_LABELS)

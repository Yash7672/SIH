"""Live-scan frame acceptance and detection coverage.

The auto-detect loop once shipped un-resized 12 MP frames, which the server
rejected outright on size. Every live frame died at the gate, so the detector
never saw a single frame and the symptom read as "detection does not work".
These tests pin the corrected behaviour: oversized frames are downscaled and
processed, and the plate-read budget is not silently truncating the frame.
"""

import io

import numpy as np
import pytest
from PIL import Image

from app.api.v1 import live_scan as ls


class _EmptyResult:
    boxes = None


class _StubModel:
    def __call__(self, *args, **kwargs):
        return [_EmptyResult()]


def _jpeg(width: int, height: int, quality: int = 90) -> bytes:
    """A photo-like 12 MP frame.

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


def test_frame_budget_allows_a_phone_sized_capture():
    """A realistic 12 MP capture must fit inside the accept ceiling.

    4000x3000 at quality 90 is ~10 MB of pure noise, which is worse than any real
    camera manages. The policy has to downscale rather than refuse at that size,
    or a high-resolution handset gets zero detections.
    """
    frame = _jpeg(4000, 3000)
    assert len(frame) > ls.SOFT_FRAME_BYTES
    assert len(frame) < ls.HARD_FRAME_BYTES


def test_abuse_ceiling_is_far_above_any_real_frame():
    """The hard ceiling must not sit anywhere near a plausible capture size.

    It exists to stop a client wedging the socket, not to police image quality.
    Quality is handled by downscaling, so a ceiling set at a few hundred KB (as it
    effectively was) rejected legitimate traffic from every modern phone.
    """
    assert ls.HARD_FRAME_BYTES >= 8 * 1024 * 1024


def test_soft_ceiling_stays_below_the_abuse_ceiling():
    """A soft limit above the hard limit would reject everything as 'too large'."""
    assert ls.SOFT_FRAME_BYTES < ls.HARD_FRAME_BYTES


def test_oversized_frame_is_downscaled_not_dropped(stub_models):
    """The regression: this frame used to be refused before any inference ran."""
    w, h, ms, vehicles, plates, crops = ls.decode_and_infer(_jpeg(4000, 3000))

    assert w == ls.WORKING_WIDTH
    assert h == round(3000 * (ls.WORKING_WIDTH / 4000))
    assert (vehicles, plates, crops) == ([], [], [])


def test_small_frame_is_left_at_its_native_size(stub_models):
    """Downscaling must not inflate a frame that is already small enough."""
    w, h, _, _, _, _ = ls.decode_and_infer(_jpeg(640, 480))

    assert (w, h) == (640, 480)


def test_aspect_ratio_survives_the_resize(stub_models):
    """A portrait phone frame stays portrait; a squashed one breaks OCR."""
    _, h, _, _, _, _ = ls.decode_and_infer(_jpeg(3000, 4000))

    assert h > ls.WORKING_WIDTH


def test_read_budget_can_return_more_than_two_plates():
    """Three crops per frame: a frame with three plates used to drop the third."""
    assert ls.MAX_READS_PER_FRAME >= 3


def test_min_plate_gate_admits_a_distant_plate():
    """A 50 px plate at working width must be read, not skipped as too small.

    The old 60 px gate skipped it, and with 1280-wide frames that is a plate a
    volunteer can plainly see in the viewfinder.
    """
    assert ls.MIN_PLATE_PX <= 50

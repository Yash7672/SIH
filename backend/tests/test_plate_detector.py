"""The optional ONNX plate detector (ai/plate_detector.py).

The pure parts - letterbox, batching and box decoding - run with no model and no
ONNX Runtime, so they pass on a bare checkout. The end-to-end tests need the
vendored 7 MB weights and are skipped when they are absent, because CI should
not download a model to prove a letterbox divides correctly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
for extra in (ROOT, ROOT / "backend"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from ai import plate_detector  # noqa: E402

MODEL_PATH = plate_detector.MODEL_DIR / plate_detector.MODELS[plate_detector.DEFAULT_SIZE]
needs_model = pytest.mark.skipif(not MODEL_PATH.is_file(), reason="plate detector weights not present")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No env leakage, and drop the loaded detector between tests."""
    for name in (
        "RAKSHAK_PLATE_DETECTOR",
        "RAKSHAK_PLATE_DETECTOR_SIZE",
        "RAKSHAK_PLATE_DETECTOR_DIR",
        "RAKSHAK_PLATE_DETECTOR_CONF",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(plate_detector, "_detector", None)
    monkeypatch.setattr(plate_detector, "_unavailable_logged", False)
    yield


# --------------------------------------------------------------------------- #
# Environment switches
# --------------------------------------------------------------------------- #
def test_mode_defaults_to_auto():
    assert plate_detector.mode() == "auto"


def test_mode_reads_env(monkeypatch):
    monkeypatch.setenv("RAKSHAK_PLATE_DETECTOR", "oim")
    assert plate_detector.mode() == "oim"
    monkeypatch.setenv("RAKSHAK_PLATE_DETECTOR", "legacy")
    assert plate_detector.mode() == "legacy"


def test_mode_rejects_junk():
    assert plate_detector.mode() == "auto"


def test_size_defaults_to_256():
    assert plate_detector.size() == "256"


def test_size_reads_and_validates(monkeypatch):
    monkeypatch.setenv("RAKSHAK_PLATE_DETECTOR_SIZE", "416")
    assert plate_detector.size() == "416"
    monkeypatch.setenv("RAKSHAK_PLATE_DETECTOR_SIZE", "999")
    assert plate_detector.size() == "256"


def test_conf_threshold_default_and_override(monkeypatch):
    assert plate_detector.conf_threshold() == plate_detector.DEFAULT_CONF
    monkeypatch.setenv("RAKSHAK_PLATE_DETECTOR_CONF", "0.4")
    assert plate_detector.conf_threshold() == 0.4
    monkeypatch.setenv("RAKSHAK_PLATE_DETECTOR_CONF", "not-a-number")
    assert plate_detector.conf_threshold() == plate_detector.DEFAULT_CONF


def test_is_enabled_is_false_in_legacy_mode_even_with_the_model(monkeypatch):
    monkeypatch.setenv("RAKSHAK_PLATE_DETECTOR", "legacy")
    monkeypatch.setattr(plate_detector, "model_present", lambda *a, **k: True)
    assert plate_detector.is_enabled() is False


def test_is_enabled_true_in_oim_mode_when_model_present(monkeypatch):
    monkeypatch.setenv("RAKSHAK_PLATE_DETECTOR", "oim")
    monkeypatch.setattr(plate_detector, "model_present", lambda *a, **k: True)
    assert plate_detector.is_enabled() is True


def test_is_enabled_true_in_oim_mode_even_without_the_model(monkeypatch):
    # `oim` is the explicit "use it, download if missing" setting.
    monkeypatch.setenv("RAKSHAK_PLATE_DETECTOR", "oim")
    monkeypatch.setattr(plate_detector, "model_present", lambda *a, **k: False)
    assert plate_detector.is_enabled() is True


def test_is_enabled_follows_the_weights_in_auto_mode(monkeypatch):
    monkeypatch.setattr(plate_detector, "model_present", lambda *a, **k: True)
    assert plate_detector.is_enabled() is True
    monkeypatch.setattr(plate_detector, "model_present", lambda *a, **k: False)
    assert plate_detector.is_enabled() is False


# --------------------------------------------------------------------------- #
# Preprocessing
# --------------------------------------------------------------------------- #
def test_letterbox_pads_to_a_square_and_reports_the_ratio():
    img = np.zeros((100, 400, 3), dtype=np.uint8)
    out, (r, _r), (dw, dh) = plate_detector.letterbox(img, (256, 256))
    assert out.shape == (256, 256, 3)
    assert r == pytest.approx(256 / 400)  # the long side decides
    assert dw == pytest.approx(0.0, abs=1.0)
    assert dh > 0  # the short side gets the padding


def test_prepare_batch_stacks_and_converts_bgr_to_rgb():
    red = np.zeros((20, 60, 3), dtype=np.uint8)
    red[..., 2] = 255  # BGR red
    blue = np.zeros((20, 60, 3), dtype=np.uint8)
    blue[..., 0] = 255  # BGR blue
    batch, metas = plate_detector.prepare_batch([red, blue], 32)
    assert batch.shape == (2, 3, 32, 32)
    assert batch.dtype == np.float32
    assert len(metas) == 2
    # BGR red -> RGB channel 0 high; BGR blue -> RGB channel 2 high.
    assert batch[0][0].mean() > batch[0][2].mean()
    assert batch[1][2].mean() > batch[1][0].mean()


def test_prepare_batch_of_nothing():
    batch, metas = plate_detector.prepare_batch([], 256)
    assert batch is None and metas == []


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #
def _row(batch_idx, x1, y1, x2, y2, score, cls=0):
    return [batch_idx, x1, y1, x2, y2, cls, score]


def test_decode_scales_boxes_back_through_ratio_and_padding():
    # ratio 0.5, pad (10, 20): model (110,120,210,170) -> source (200,200,400,300)
    preds = np.array([[_row(0, 110, 120, 210, 170, 0.9)]], dtype=np.float32)
    (boxes,) = plate_detector.decode_output(preds, [(0.5, 10, 20)], conf=0.25)
    assert boxes[0].box == (200, 200, 400, 300)
    assert boxes[0].score == pytest.approx(0.9)


def test_decode_filters_below_confidence():
    preds = np.array([[_row(0, 10, 10, 50, 30, 0.9), _row(0, 0, 0, 20, 20, 0.1)]], dtype=np.float32)
    (boxes,) = plate_detector.decode_output(preds, [(1.0, 0, 0)], conf=0.5)
    assert len(boxes) == 1


def test_decode_sorts_by_score_descending():
    preds = np.array([[_row(0, 0, 0, 20, 20, 0.3), _row(0, 5, 5, 40, 30, 0.8)]], dtype=np.float32)
    (boxes,) = plate_detector.decode_output(preds, [(1.0, 0, 0)], conf=0.25)
    assert [b.score for b in boxes] == pytest.approx([0.8, 0.3])


def test_decode_returns_one_list_per_frame():
    preds = np.array(
        [
            [_row(0, 0, 0, 20, 20, 0.9), _row(0, 0, 0, 5, 5, 0.0)],
            [_row(1, 0, 0, 30, 30, 0.7), _row(1, 0, 0, 10, 10, 0.6)],
        ],
        dtype=np.float32,
    )
    out = plate_detector.decode_output(preds, [(1.0, 0, 0), (1.0, 0, 0)], conf=0.25)
    assert len(out) == 2
    assert len(out[0]) == 1 and len(out[1]) == 2


def test_decode_drops_degenerate_boxes():
    preds = np.array([[_row(0, 40, 40, 40, 50, 0.9)]], dtype=np.float32)
    (boxes,) = plate_detector.decode_output(preds, [(1.0, 0, 0)], conf=0.25)
    assert boxes == []


def test_decode_handles_a_two_dimensional_output():
    preds = np.array([_row(0, 0, 0, 20, 20, 0.9)], dtype=np.float32)
    out = plate_detector.decode_output(preds, [(1.0, 0, 0)], conf=0.25)
    assert len(out) == 1 and len(out[0]) == 1


# --------------------------------------------------------------------------- #
# Off by default
# --------------------------------------------------------------------------- #
def test_detect_returns_empty_when_disabled(monkeypatch):
    monkeypatch.setenv("RAKSHAK_PLATE_DETECTOR", "legacy")
    monkeypatch.setattr(plate_detector, "_detector", None)
    # legacy mode -> get_detector() is None -> no boxes
    assert plate_detector.detect(np.zeros((32, 32, 3), dtype=np.uint8)) == []
    assert plate_detector.detect_batch([np.zeros((32, 32, 3), np.uint8)]) == [[]]


# --------------------------------------------------------------------------- #
# Real model (skipped when the weights are absent)
# --------------------------------------------------------------------------- #
@needs_model
def test_real_detector_loads_and_finds_the_fixture_plate(monkeypatch):
    import cv2

    monkeypatch.setenv("RAKSHAK_PLATE_DETECTOR", "oim")
    assert plate_detector.warm() is True
    scene = ROOT / "data" / "test_plates" / "road_scene.jpg"
    if not scene.is_file():
        pytest.skip("fixture absent")
    frame = cv2.imread(str(scene))
    boxes = plate_detector.detect(frame)
    assert boxes, "expected the plate in road_scene.jpg to be detected"
    # The plate sits centre-frame; a box that covers most of the image is a bug.
    top = boxes[0]
    assert 0 < top.width < frame.shape[1]
    assert 0 < top.height < frame.shape[0]
    assert top.score >= plate_detector.conf_threshold()


@needs_model
def test_real_detector_batches_matching_single(monkeypatch):
    import cv2

    monkeypatch.setenv("RAKSHAK_PLATE_DETECTOR", "oim")
    scene = ROOT / "data" / "test_plates" / "road_scene.jpg"
    if not scene.is_file():
        pytest.skip("fixture absent")
    frame = cv2.imread(str(scene))
    batched = plate_detector.detect_batch([frame, frame])
    assert len(batched) == 2
    assert [b.box for b in batched[0]] == [b.box for b in batched[1]]


# --------------------------------------------------------------------------- #
# Wiring into ai/detector.py
# --------------------------------------------------------------------------- #
class _StubOnnxDetector:
    """Stands in for ai.plate_detector inside ai.detector."""

    def __init__(self, boxes):
        self._boxes = boxes

    def is_enabled(self) -> bool:
        return True

    def detect(self, image):
        return self._boxes


def test_detector_uses_oim_when_enabled(monkeypatch):
    from ai import detector

    stub = _StubOnnxDetector([plate_detector.PlateBox(100, 120, 220, 160, 0.91)])
    monkeypatch.setattr(detector, "_plate_detector", stub)
    frame = np.full((300, 400, 3), 128, dtype=np.uint8)
    region = detector.detect_plate_region(frame)
    assert region is not None
    assert region.source == "oim"
    assert region.confidence == pytest.approx(0.91)
    assert region.raw_box == (100, 120, 220, 160)
    # The crop is the raw box padded out, and it is a real slice of the frame.
    assert region.crop.size > 0


def test_detector_ignores_oim_when_disabled(monkeypatch):
    from ai import detector

    class _Disabled:
        def is_enabled(self) -> bool:
            return False

        def detect(self, image):  # pragma: no cover - must not be called
            raise AssertionError("detect() must not run when disabled")

    monkeypatch.setattr(detector, "_plate_detector", _Disabled())
    # Force the legacy path to a deterministic empty result so no model is loaded.
    monkeypatch.setattr(detector, "_yolo_regions", lambda bgr: [])
    monkeypatch.setattr(detector, "_legacy_regions", lambda bgr: [])
    frame = np.full((64, 64, 3), 10, dtype=np.uint8)
    assert detector.detect_plate_region(frame) is None


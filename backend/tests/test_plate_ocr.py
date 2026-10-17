"""The plate-specialised second reader (ai/plate_ocr.py).

The pure parts - config parsing, preprocessing and argmax decoding - are tested
directly, with no model on disk, so these pass on a bare checkout. The one test
that needs the real weights is skipped when they are absent rather than failed:
CI should not download 5 MB to prove an argmax works.
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

from ai import plate_ocr  # noqa: E402

CONFIG_YAML = """\
max_plate_slots: 3
alphabet: 'AB_'
pad_char: '_'
img_height: 8
img_width: 16
keep_aspect_ratio: false
interpolation: linear
image_color_mode: rgb
"""


def _cfg(**overrides) -> plate_ocr.PlateConfig:
    base = dict(
        img_height=8,
        img_width=16,
        alphabet="AB_",
        max_plate_slots=3,
        pad_char="_",
        image_color_mode="rgb",
        keep_aspect_ratio=False,
        interpolation="linear",
    )
    base.update(overrides)
    return plate_ocr.PlateConfig(**base)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_config_is_parsed_from_yaml(tmp_path: Path):
    path = tmp_path / "cfg.yaml"
    path.write_text(CONFIG_YAML, encoding="utf-8")
    cfg = plate_ocr.PlateConfig.from_yaml(path)
    assert (cfg.img_height, cfg.img_width) == (8, 16)
    assert cfg.alphabet == "AB_"
    assert cfg.max_plate_slots == 3
    assert cfg.image_color_mode == "rgb"
    assert cfg.pad_char == "_"


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #
def _one_hot(slots: int, vocab: int, picks: list[int], probs: list[float]) -> np.ndarray:
    out = np.zeros((1, slots, vocab), dtype=np.float32)
    for slot, (pick, prob) in enumerate(zip(picks, probs)):
        out[0, slot, pick] = prob
        # Give the losing classes the remaining mass so the row is a distribution.
        out[0, slot] += (1.0 - prob) / (vocab - 1)
        out[0, slot, pick] = prob
    return out.reshape(1, -1)


def test_decode_maps_argmax_to_alphabet_and_strips_pad():
    cfg = _cfg()  # alphabet "AB_", 3 slots -> vocab 3
    # "A", "B", pad  -> "AB"
    logits = _one_hot(3, 3, [0, 1, 2], [0.9, 0.8, 0.95])
    (pred,) = plate_ocr.decode_output(logits, cfg)
    assert pred.plate == "AB"
    assert pred.char_probs == (pytest.approx(0.9), pytest.approx(0.8))
    assert pred.mean_conf == pytest.approx(0.85)
    assert pred.has_confidence is True


def test_decode_trims_confidence_to_the_text_length():
    cfg = _cfg()
    logits = _one_hot(3, 3, [0, 2, 2], [0.7, 0.9, 0.9])
    (pred,) = plate_ocr.decode_output(logits, cfg)
    assert pred.plate == "A"
    assert len(pred.char_probs) == len(pred.plate) == 1


def test_decode_of_all_padding_is_empty():
    cfg = _cfg()
    logits = _one_hot(3, 3, [2, 2, 2], [0.9, 0.9, 0.9])
    (pred,) = plate_ocr.decode_output(logits, cfg)
    assert pred.plate == ""
    assert pred.char_probs == ()
    assert pred.has_confidence is False


def test_decode_handles_a_batch_of_many_rows():
    cfg = _cfg()
    row_a = _one_hot(3, 3, [0, 1, 2], [0.9, 0.8, 0.95])  # "AB"
    row_b = _one_hot(3, 3, [1, 0, 2], [0.7, 0.6, 0.95])  # "BA"
    batch = np.concatenate([row_a, row_b], axis=0)
    preds = plate_ocr.decode_output(batch, cfg)
    assert [p.plate for p in preds] == ["AB", "BA"]


# --------------------------------------------------------------------------- #
# Preprocessing
# --------------------------------------------------------------------------- #
def test_prepare_input_shape_dtype_and_channel_order():
    cfg = _cfg()
    bgr = np.zeros((10, 40, 3), dtype=np.uint8)
    bgr[..., 2] = 255  # pure red in BGR
    batch = plate_ocr.prepare_input(bgr, cfg)
    assert batch.shape == (1, 8, 16, 3)
    assert batch.dtype == np.uint8
    # rgb mode: red entered at BGR index 2 and must leave at RGB index 0.
    assert batch[0, :, :, 0].mean() > batch[0, :, :, 2].mean()


def test_prepare_input_accepts_grayscale():
    cfg = _cfg(image_color_mode="grayscale")
    gray = np.full((10, 40), 128, dtype=np.uint8)
    batch = plate_ocr.prepare_input(gray, cfg)
    assert batch.shape == (1, 8, 16, 3)


def test_prepare_input_rejects_empty():
    assert plate_ocr.prepare_input(np.zeros((0, 0, 3), dtype=np.uint8), _cfg()) is None


def test_prepare_batch_stacks_rows_in_order():
    cfg = _cfg()
    a = np.zeros((10, 40, 3), dtype=np.uint8)
    a[..., 2] = 255  # red -> high channel 0 after rgb conversion
    b = np.zeros((10, 40, 3), dtype=np.uint8)
    b[..., 0] = 255  # blue -> high channel 2 after rgb conversion
    batch = plate_ocr.prepare_batch([a, b], cfg)
    assert batch.shape == (2, 8, 16, 3)
    assert batch.dtype == np.uint8
    assert batch[0][..., 0].mean() > batch[1][..., 0].mean()
    assert batch[1][..., 2].mean() > batch[0][..., 2].mean()


def test_prepare_batch_keeps_rows_aligned_when_one_is_empty():
    cfg = _cfg()
    good = np.full((10, 40, 3), 200, dtype=np.uint8)
    batch = plate_ocr.prepare_batch([good, np.zeros((0, 0, 3), np.uint8)], cfg)
    assert batch.shape == (2, 8, 16, 3)  # the bad row became a blank frame, not dropped


# --------------------------------------------------------------------------- #
# Enable/disable logic
# --------------------------------------------------------------------------- #
def test_mode_off_disables(monkeypatch):
    monkeypatch.setenv("RAKSHAK_PLATE_OCR", "off")
    assert plate_ocr.mode() == "off"
    assert plate_ocr.is_enabled() is False


def test_mode_auto_is_off_without_weights(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RAKSHAK_PLATE_OCR", "auto")
    monkeypatch.setenv("RAKSHAK_PLATE_OCR_DIR", str(tmp_path / "absent"))
    assert plate_ocr.is_enabled() is False


def test_unknown_mode_falls_back_to_auto(monkeypatch):
    monkeypatch.setenv("RAKSHAK_PLATE_OCR", "banana")
    assert plate_ocr.mode() == "auto"


def test_model_dir_prefers_the_container_layout_when_the_repo_one_is_absent(tmp_path: Path, monkeypatch):
    # The Docker image copies backend/models -> /app/models and ai/ -> /app/ai,
    # so `model_dir()` must find the flattened layout too.
    repo_dir = tmp_path / "backend" / "models" / "fast-plate-ocr"
    flat_dir = tmp_path / "models" / "fast-plate-ocr"
    flat_dir.mkdir(parents=True)
    monkeypatch.setattr(plate_ocr, "_model_dirs", lambda: [repo_dir, flat_dir])
    assert plate_ocr.model_dir() == flat_dir


def test_model_dir_honours_an_override(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RAKSHAK_PLATE_OCR_DIR", str(tmp_path / "elsewhere"))
    assert plate_ocr.model_dir() == tmp_path / "elsewhere"


# --------------------------------------------------------------------------- #
# Vote integration (live path)
# --------------------------------------------------------------------------- #
class _StubReader:
    """Stands in for ai.plate_ocr in plate_reader, without a model."""

    def __init__(self, text: str, conf: float, weight: float = 0.6):
        self._text, self._conf, self._weight = text, conf, weight

    def is_enabled(self) -> bool:
        return True

    def predict_batch(self, images):
        return [plate_ocr.PlatePrediction(self._text, tuple([self._conf] * len(self._text)))] * len(
            images
        )

    def vote_weight(self) -> float:
        return self._weight


def test_fast_observations_are_named_and_weighted(monkeypatch):
    from app.services import plate_reader

    monkeypatch.setattr(plate_reader, "plate_ocr", _StubReader("MH12JK4567", 0.8, 0.5))
    variants = [
        plate_reader.Variant("raw", np.zeros((20, 60, 3), dtype=np.uint8)),
        plate_reader.Variant("clahe", np.zeros((20, 60, 3), dtype=np.uint8)),
    ]
    obs = plate_reader._fast_observations(variants)
    assert [o.name for o in obs] == ["fpo:raw", "fpo:clahe"]
    assert obs[0].text == "MH12JK4567"
    assert obs[0].weight == 0.5
    assert obs[0].char_confs == (0.8,) * 10


def test_fast_observations_absent_when_off(monkeypatch):
    from app.services import plate_reader

    monkeypatch.setattr(plate_reader, "plate_ocr", None)
    variants = [plate_reader.Variant("raw", np.zeros((20, 60, 3), dtype=np.uint8))]
    assert plate_reader._fast_observations(variants) == []


# --------------------------------------------------------------------------- #
# Real model (skipped when the weights are not on disk)
# --------------------------------------------------------------------------- #
needs_model = pytest.mark.skipif(
    not plate_ocr.model_present(),
    reason="fast-plate-ocr weights not present (run: python -m ai.plate_ocr --download)",
)


@needs_model
def test_real_recogniser_loads_and_reads():
    assert plate_ocr.warm() is True
    # A rendered plate-ish strip: the point is that it runs end to end, not that
    # this synthetic image decodes to anything in particular.
    strip = np.full((64, 200, 3), 255, dtype=np.uint8)
    pred = plate_ocr.predict(strip)
    assert pred is None or (isinstance(pred.plate, str) and pred.mean_conf >= 0.0)


@needs_model
def test_real_recogniser_batches_and_matches_single_reads():
    """The batched call must return one row per input, in order, matching single."""
    plate_ocr.warm()
    images = [np.full((64, 200, 3), v, dtype=np.uint8) for v in (60, 128, 200)]
    batch = plate_ocr.predict_batch(images)
    assert len(batch) == len(images)
    singles = [plate_ocr.predict(img) for img in images]
    assert [p.plate for p in batch] == [(s.plate if s else "") for s in singles]


@needs_model
def test_read_plate_returns_ocr_hits():
    from ai.ocr_engine import OcrHit
    from ai.detector import _load_plate_detector, _pad_box
    import cv2

    fixture = ROOT / "data" / "generated" / "plate_3_MH12JK4567.png"
    if not fixture.exists():
        pytest.skip("fixture missing")
    img = cv2.imread(str(fixture))
    result = _load_plate_detector()(img, conf=0.25, verbose=False, imgsz=640)[0]
    boxes = getattr(result, "boxes", None)
    if boxes is None or len(boxes) == 0:
        pytest.skip("plate detector found nothing in the fixture")
    x = boxes.xyxy[0].detach().cpu().numpy()
    h, w = img.shape[:2]
    p = _pad_box((int(x[0]), int(x[1]), int(x[2]), int(x[3])), w, h)
    hits = plate_ocr.read_plate(img[p[1] : p[3], p[0] : p[2]])
    assert hits
    assert all(isinstance(hit, OcrHit) for hit in hits)
    assert hits[0].stage == "plate_ocr"

"""Reading: the sanity filters, the variants and the per-character confidence.

The OCR itself is not mocked here where a real plate is involved, because the
whole point of these tests is that a *real* reader produces usable per-character
confidence. The filters and the correction logic are pure and are tested as such.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.services.plate_reader import (
    MAX_PLATE_AREA_FRACTION,
    MIN_PLATE_WIDTH_PX,
    build_variants,
    correct_by_grammar,
    debug_crops_enabled,
    plate_box_plausible,
    read_crop,
    suppress_vehicle_boxes,
)

FRAME = (1280, 844)


def box(x1, y1, x2, y2):
    return (x1, y1, x2, y2)


# --------------------------------------------------------------------------- #
# A plate that is a plate
# --------------------------------------------------------------------------- #
def test_a_normal_plate_passes():
    # 187x37 at 1280 wide: the real road_scene measurement.
    assert plate_box_plausible(box(500, 600, 687, 637), FRAME)[0] is True


def test_a_plate_beside_its_vehicle_passes():
    assert plate_box_plausible(
        box(500, 600, 687, 637), FRAME, box(400, 300, 800, 700)
    )[0] is True


def test_a_two_row_motorcycle_plate_passes():
    # ~1.6:1, the shape of "DL 12 / AB 1234".
    assert plate_box_plausible(box(500, 600, 600, 662), FRAME)[0] is True


# --------------------------------------------------------------------------- #
# The reported false positive
# --------------------------------------------------------------------------- #
def test_a_box_across_the_top_of_the_frame_is_rejected():
    """The screenshot: a huge red box over the shopfront.

    This is the one filter that had to be added outright - it is the only shape
    that was both drawn and read while being obviously not a plate.
    """
    ok, why = plate_box_plausible(box(40, 30, 1240, 140), FRAME)
    assert ok is False
    assert "frame width" in why


def test_the_width_cutoff_is_just_above_sixty_percent():
    """The cutoff is on width alone, so the box keeps a legal aspect ratio -
    otherwise the aspect filter would reject it first and the test would pass for
    the wrong reason."""
    height = int(0.59 * FRAME[0] / 4.0)
    just_under = box(0, 0, int(0.59 * FRAME[0]), height)
    just_over = box(0, 0, int(0.61 * FRAME[0]), height)
    assert plate_box_plausible(just_under, FRAME)[0] is True
    ok, why = plate_box_plausible(just_over, FRAME)
    assert ok is False
    assert "frame width" in why


# --------------------------------------------------------------------------- #
# Too small, too thin, too fat
# --------------------------------------------------------------------------- #
def test_a_plate_too_narrow_to_hold_a_license_number_is_rejected():
    """At 30 px the recogniser guesses rather than reads, and a guessed plate is
    worse than no plate: it is a confident wrong answer."""
    ok, why = plate_box_plausible(box(500, 600, 500 + MIN_PLATE_WIDTH_PX - 1, 620), FRAME)
    assert ok is False
    assert "wide" in why


def test_the_minimum_width_is_accepted():
    assert plate_box_plausible(box(500, 600, 500 + MIN_PLATE_WIDTH_PX, 620), FRAME)[0] is True


def test_a_very_thin_sliver_is_rejected():
    # 300x6: a wire, not a plate.
    assert plate_box_plausible(box(400, 600, 700, 606), FRAME)[0] is False


def test_an_almost_square_box_is_rejected():
    # 1:1 is neither a single-row plate nor a two-row one. Letting 1.0 through
    # would also let through whatever the detector boxes that happens to be
    # square - a light, a sign, a face.
    assert plate_box_plausible(box(500, 600, 560, 660), FRAME)[0] is False


def test_a_degenerate_box_is_rejected():
    assert plate_box_plausible(box(500, 600, 500, 640), FRAME)[0] is False


# --------------------------------------------------------------------------- #
# Against the vehicle
# --------------------------------------------------------------------------- #
def test_a_plate_covering_most_of_its_vehicle_is_rejected():
    """The detector has boxed a windscreen."""
    vehicle = box(300, 300, 1000, 700)
    huge = box(320, 320, 980, 680)
    ok, why = plate_box_plausible(huge, FRAME, vehicle)
    assert ok is False
    assert "of its vehicle" in why


def test_a_real_plate_on_a_car_passes_the_area_test():
    vehicle = box(400, 300, 800, 700)
    assert plate_box_plausible(box(500, 600, 687, 637), FRAME, vehicle)[0] is True


def test_a_plate_up_on_the_roof_is_rejected():
    """The centre is at 6% of the vehicle's height - a windscreen or the sky."""
    vehicle = box(400, 300, 800, 700)
    ok, why = plate_box_plausible(box(520, 310, 660, 340), FRAME, vehicle)
    assert ok is False
    assert "vehicle height" in why


def test_a_plate_at_bumper_height_on_a_bus_passes():
    """Measured: a real plate centre sits at 68% of its vehicle's height, and a
    large box like a bus puts it nearer 79%. Both are legal placements."""
    bus = box(200, 300, 1000, 700)
    at_68 = box(500, 300 + 0.68 * 400, 660, 300 + 0.68 * 400 + 30)
    at_79 = box(500, 300 + 0.79 * 400, 660, 300 + 0.79 * 400 + 30)
    assert plate_box_plausible(at_68, FRAME, bus)[0] is True
    assert plate_box_plausible(at_79, FRAME, bus)[0] is True


def test_a_plate_just_above_the_ceiling_is_still_rejected():
    """The boundary, from both sides."""
    vehicle = box(400, 300, 800, 700)
    vh = 400.0
    # 29% of the height: above the 30% line, so rejected.
    too_high = box(520, 300 + 0.26 * vh, 660, 300 + 0.26 * vh + 30)
    # 31%: below the line, so fine.
    fine = box(520, 300 + 0.28 * vh, 660, 300 + 0.28 * vh + 30)
    assert plate_box_plausible(too_high, FRAME, vehicle)[0] is False
    assert plate_box_plausible(fine, FRAME, vehicle)[0] is True


def test_a_plate_outside_every_vehicle_is_still_allowed():
    """An orphan - a plate on a half-visible car - is judged on its own merits."""
    assert plate_box_plausible(box(500, 600, 687, 637), FRAME, None)[0] is True


# --------------------------------------------------------------------------- #
# Vehicle box cleanup
# --------------------------------------------------------------------------- #
def test_one_car_does_not_get_three_green_boxes():
    """A car and a truck box over the same vehicle collapse to one."""
    boxes = [
        [0.10, 0.30, 0.40, 0.70],
        [0.11, 0.31, 0.39, 0.71],
        [0.12, 0.32, 0.38, 0.69],
        [0.70, 0.30, 0.98, 0.70],
    ]
    keep = suppress_vehicle_boxes(boxes, [0.9, 0.85, 0.8, 0.88])
    assert len(keep) == 2
    assert 3 in keep  # the genuinely separate car survives


def test_the_confident_box_wins_the_survivor():
    boxes = [[0.10, 0.30, 0.40, 0.70], [0.12, 0.32, 0.38, 0.69]]
    keep = suppress_vehicle_boxes(boxes, [0.4, 0.95])
    assert keep == [1]


def test_tiny_boxes_are_dropped():
    """A 'vehicle' a couple of percent of the frame is noise."""
    assert suppress_vehicle_boxes([[0.0, 0.0, 0.01, 0.01], [0.1, 0.1, 0.5, 0.7]], [0.9, 0.9]) == [1]


def test_nms_keeps_everything_when_nothing_overlaps():
    boxes = [[0.0, 0.0, 0.2, 0.2], [0.3, 0.3, 0.5, 0.5], [0.6, 0.6, 0.8, 0.8]]
    assert suppress_vehicle_boxes(boxes, [0.9, 0.8, 0.7]) == [0, 1, 2]


def test_nms_keeps_the_result_in_the_input_order():
    """Stable output keeps the ids and the wire list predictable."""
    boxes = [[0.7, 0.3, 0.98, 0.7], [0.1, 0.3, 0.4, 0.7]]
    assert suppress_vehicle_boxes(boxes, [0.88, 0.9]) == [0, 1]


# --------------------------------------------------------------------------- #
# Position-aware correction
# --------------------------------------------------------------------------- #
def test_a_letter_in_a_digit_position_is_moved_into_the_digit_class():
    """MH I2AB1234 - the I is a clipped 1."""
    best, score, _note = correct_by_grammar("MHI2AB1234")
    assert best == "MH12AB1234"
    assert score > 0


def test_a_digit_in_a_letter_position_is_moved_into_the_letter_class():
    best, score, _note = correct_by_grammar("MH1ZAB1Z34")
    assert best == "MH1ZAB1234"
    assert score > 0


def test_an_already_valid_read_is_left_exactly_as_it_is():
    """No "improvement" is allowed on a read that is already a plate.

    DL1ZA9092 is the case that matters: a 2-digit-RTO reading of it (DL12A9092)
    is also a legal plate and scores *higher* on the layout prior, so a function
    that keeps the best-scoring candidate rewrites a correct read into a
    different real car. This is the same trap that
    `test_two_character_rewrites_never_win_over_an_exact_read` pins down in
    `plate.py`, and it is why this one returns early.
    """
    for plate in ("DL1ZA9092", "MH12JK4567", "DL01ZA9092", "TS09AB1234", "DL8CBF4890"):
        best, _score, note = correct_by_grammar(plate)
        assert best == plate, plate
        assert note == "already well-formed", plate


def test_correction_never_produces_an_invalid_state_code():
    """A repair is only allowed to land on a real RTO code.

    Anything that cannot be repaired into a legal plate comes back untouched with
    a zero score, so the caller shows it as "unsure" rather than as a plate.
    """
    from app.services.plate import VALID_STATE_CODES

    for raw in ("HX1ZAB1234", "ZZ1ZAB1234", "12345678", "ABCDEFGH", "QQ12AB1234"):
        best, score, _note = correct_by_grammar(raw)
        if score > 0:
            assert best[:2] in VALID_STATE_CODES, raw
        else:
            assert best == raw, raw


def test_correction_reports_the_original_when_nothing_better_exists():
    best, score, note = correct_by_grammar("ZZZZZZZZ")
    assert score == 0.0
    assert "no repair" in note or best == "ZZZZZZZZ"


def test_an_empty_read_corrects_to_nothing():
    assert correct_by_grammar("")[0] == ""


# --------------------------------------------------------------------------- #
# Variants
# --------------------------------------------------------------------------- #
def test_variants_are_built_and_scaled_to_a_readable_height():
    crop = np.full((20, 120, 3), 255, np.uint8)
    variants = build_variants(crop, with_deskew=False)
    assert [v.name for v in variants] == ["raw", "clahe", "colour"]
    for v in variants:
        assert v.image.shape[0] >= 70, v.name
        assert v.image.ndim == 3


def test_variants_handle_a_greyscale_crop():
    crop = np.full((20, 120), 255, np.uint8)
    assert build_variants(crop, with_deskew=False)


def test_an_empty_crop_produces_no_variants():
    assert build_variants(np.zeros((0, 0, 3), np.uint8)) == []
    assert build_variants(None) == []


def test_a_huge_crop_is_shrunk_to_the_ocr_limit():
    crop = np.full((400, 2000, 3), 255, np.uint8)
    for v in build_variants(crop, with_deskew=False):
        assert max(v.image.shape[:2]) <= 736, v.name


def test_deskew_is_only_added_when_asked():
    crop = np.full((20, 120, 3), 255, np.uint8)
    assert len(build_variants(crop, with_deskew=False)) == 3
    # A blank image has no edges to find an angle in, so nothing is added.
    assert len(build_variants(crop, with_deskew=True)) == 3


def test_variants_are_all_different_images_of_the_same_plate():
    """If two variants were byte-identical the vote would be counting twice."""
    rng = np.random.default_rng(7)
    crop = rng.integers(0, 255, (24, 140, 3), dtype=np.uint8)
    variants = build_variants(crop, with_deskew=False)
    assert not np.array_equal(variants[0].image, variants[1].image)


# --------------------------------------------------------------------------- #
# The real reader, on a real plate
# --------------------------------------------------------------------------- #
ROAD_SCENE = Path(__file__).resolve().parents[2] / "data" / "test_plates" / "road_scene.jpg"


@pytest.mark.skipif(not ROAD_SCENE.exists(), reason="road_scene.jpg is not present")
def test_the_real_plate_is_read_with_per_character_confidence():
    """The whole point of the change, end to end on a real frame.

    RapidOCR's public API throws the per-character scores away, so this asserts
    they are actually obtainable from the installed version - if a future upgrade
    drops that, this fails loudly instead of quietly reverting to trusting a mean.
    """
    import cv2

    from ai.detector import _load_plate_detector, _pad_box, preprocess_crop

    img = cv2.imread(str(ROAD_SCENE))
    h, w = img.shape[:2]
    result = _load_plate_detector()(img, conf=0.25, verbose=False, imgsz=640)[0]
    xyxy = result.boxes.xyxy[0].tolist()
    p = _pad_box((int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])), w, h)
    crop = img[p[1] : p[3], p[0] : p[2]]

    read = read_crop(preprocess_crop(crop))
    assert read is not None
    assert read.norm == "MH12JK4567"
    assert read.valid is True
    # One score per character of the plate, spaces dropped.
    assert read.char_confs is not None
    assert len(read.char_confs) == len(read.norm)
    assert all(0.0 <= c <= 1.0 for c in read.char_confs)


@pytest.mark.skipif(not ROAD_SCENE.exists(), reason="road_scene.jpg is not present")
def test_the_ambiguous_characters_of_a_real_plate_are_flagged():
    """'MH12JK4567' contains H and 2; if either read weakly it must be named.

    That flag is what stops a single uncertain glyph from becoming a red banner.
    """
    import cv2

    from ai.detector import _load_plate_detector, _pad_box, preprocess_crop

    img = cv2.imread(str(ROAD_SCENE))
    h, w = img.shape[:2]
    result = _load_plate_detector()(img, conf=0.25, verbose=False, imgsz=640)[0]
    xyxy = result.boxes.xyxy[0].tolist()
    p = _pad_box((int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])), w, h)
    read = read_crop(preprocess_crop(img[p[1] : p[3], p[0] : p[2]]))
    assert read is not None and read.char_confs is not None
    # Whatever is flagged has to actually be one of the confusable glyphs.
    from app.services.plate_verdict import AMBIGUOUS_CHARS

    for i in read.ambiguous_positions:
        assert read.norm[i] in AMBIGUOUS_CHARS


# --------------------------------------------------------------------------- #
# The debug diagnostic
# --------------------------------------------------------------------------- #
def test_plate_crop_saving_is_off_by_default(monkeypatch):
    monkeypatch.delenv("DEBUG_SAVE_PLATE_CROPS", raising=False)
    assert debug_crops_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_it_switches_on_only_when_asked(monkeypatch, value):
    monkeypatch.setenv("DEBUG_SAVE_PLATE_CROPS", value)
    assert debug_crops_enabled() is True


def test_nothing_is_written_while_it_is_off(monkeypatch, tmp_path):
    monkeypatch.delenv("DEBUG_SAVE_PLATE_CROPS", raising=False)
    from app.services import plate_reader

    monkeypatch.setattr(plate_reader, "_CROP_DIR", tmp_path / "crops")
    plate_reader.save_plate_crop(np.zeros((10, 40, 3), np.uint8), "DL1ZA9092", 0.4)
    assert not (tmp_path / "crops").exists()


def test_only_the_crop_is_written_when_it_is_on(monkeypatch, tmp_path):
    """A plate crop, never a frame - that distinction is the whole safety
    property of this diagnostic."""
    monkeypatch.setenv("DEBUG_SAVE_PLATE_CROPS", "1")
    from app.services import plate_reader

    out = tmp_path / "crops"
    monkeypatch.setattr(plate_reader, "_CROP_DIR", out)
    crop = np.full((20, 100, 3), 128, np.uint8)
    plate_reader.save_plate_crop(crop, "DL12AG092", 0.42)

    written = list(out.glob("*.jpg"))
    assert len(written) == 1
    import cv2

    assert cv2.imread(str(written[0])).shape == (20, 100, 3)
    assert "DL12AG092" in written[0].name
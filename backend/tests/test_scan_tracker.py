"""Vehicle tracking behaviour for the live overlay.

This is the whole reason `app/ws/scan_manager.py` exists. The detector only ever
says "there is a car at [0.31,0.42,0.52,0.61]"; on its own that is not enough to
draw a CCTV feed, because every rectangle would be re-keyed on every frame and
the "CAR 1 / CAR 2" chips would shuffle as detections get re-ordered by
confidence.

These tests use the tracker directly with hand-written boxes. No model, no
photograph, no timing - so they are exact, fast, and they keep passing when a
checkpoint is swapped.
"""

import pytest

from app.ws.scan_manager import (
    CLASS_LABELS,
    COCO_CLASS_IDS,
    Detection,
    VehicleTracker,
    class_label,
    iou,
)

CAR = 2
BIKE = 3
BUS = 5
TRUCK = 7


def det(box, cls=CAR, conf=0.9):
    return Detection(box=list(box), cls_id=cls, conf=conf)


# --------------------------------------------------------------------------- #
# class vocabulary
# --------------------------------------------------------------------------- #
def test_only_four_classes_are_ever_labelled():
    """COCO has no auto-rickshaw, so an AUTO chip could never be true."""
    assert set(CLASS_LABELS.values()) == {"CAR", "BIKE", "BUS", "TRUCK"}
    assert sorted(COCO_CLASS_IDS) == sorted(CLASS_LABELS)


def test_unknown_class_falls_back_to_something_honest():
    """A wider checkpoint must not crash the overlay or invent a friendly name."""
    assert class_label(99) == "VEH99"
    assert class_label(CAR) == "CAR"


# --------------------------------------------------------------------------- #
# iou
# --------------------------------------------------------------------------- #
def test_iou_of_identical_boxes_is_one():
    assert iou([0.1, 0.1, 0.5, 0.5], [0.1, 0.1, 0.5, 0.5]) == pytest.approx(1.0)


def test_iou_of_disjoint_boxes_is_zero():
    assert iou([0.0, 0.0, 0.2, 0.2], [0.8, 0.8, 1.0, 1.0]) == 0.0


def test_iou_treats_a_degenerate_box_as_no_overlap():
    """A zero-area box must not produce a division by zero or a false match."""
    assert iou([0.5, 0.5, 0.5, 0.5], [0.0, 0.0, 1.0, 1.0]) == 0.0


def test_iou_of_a_box_fully_inside_another_is_area_over_union():
    # inner is half the width and height -> 25% of the area
    assert iou([0.0, 0.0, 1.0, 1.0], [0.0, 0.0, 0.5, 0.5]) == pytest.approx(0.25)


# --------------------------------------------------------------------------- #
# stable identity
# --------------------------------------------------------------------------- #
def test_a_still_vehicle_keeps_one_id_across_frames():
    """The core regression: re-keying every frame is what made boxes flicker."""
    tr = VehicleTracker()

    first = tr.update([det([0.10, 0.20, 0.40, 0.50])], now_ms=0)
    second = tr.update([det([0.11, 0.21, 0.41, 0.51])], now_ms=250)

    assert len(first) == len(second) == 1
    assert first[0].tid == second[0].tid
    assert first[0].label == second[0].label == "CAR 1"
    assert second[0].hits == 2


def test_confidence_reordering_does_not_swap_ids():
    """Two identical cars detected in different confidence order stay put.

    This is the failure the tracker exists to prevent: the detector returns
    whichever car it is surest about first, so a naive key-by-index overlay
    renames the cars every time confidence swaps.
    """
    tr = VehicleTracker()
    tr.update([det([0.10, 0.20, 0.40, 0.50], conf=0.9), det([0.60, 0.20, 0.90, 0.50], conf=0.8)], now_ms=0)
    ids = {t.tid: t.label for t in tr.live()}

    # Same two boxes, opposite confidence order.
    tr.update([det([0.60, 0.20, 0.90, 0.50], conf=0.95), det([0.10, 0.20, 0.40, 0.50], conf=0.7)], now_ms=250)
    after = {t.tid: t.label for t in tr.live()}

    assert after == ids
    assert sorted(after.values()) == ["CAR 1", "CAR 2"]


def test_track_ids_are_never_reused_within_a_connection():
    """A late `plate` reply must not land on a different car that arrived since.

    If a tid were recycled, a plate read for a car that has already left could be
    painted onto the next car to arrive at the same place - the single worst
    failure this feature could have, because it silently mislabels a stolen
    vehicle as an innocent one.
    """
    tr = VehicleTracker()
    t1 = tr.update([det([0.10, 0.20, 0.40, 0.50])], now_ms=0)[0].tid
    for i in range(5):  # let it expire
        tr.update([], now_ms=250 * (i + 2))
    t2 = tr.update([det([0.10, 0.20, 0.40, 0.50])], now_ms=2000)[0].tid

    assert t2 != t1


def test_a_class_change_creates_a_new_track():
    """Matched geometry but a different class is a different object.

    Relabelling a car into a bus mid-crossing would leave a chip that says BUS on
    a car that is still a car.
    """
    tr = VehicleTracker()
    car = tr.update([det([0.10, 0.20, 0.40, 0.50], cls=CAR)], now_ms=0)[0]

    live = {t.label: t for t in tr.update([det([0.10, 0.20, 0.40, 0.50], cls=BUS)], now_ms=250)}

    assert live["BUS 1"].tid != car.tid
    assert live["BUS 1"].cls_id == BUS


def test_a_miss_keeps_the_track_alive_so_the_box_does_not_blink():
    tr = VehicleTracker()
    tr.update([det([0.10, 0.20, 0.40, 0.50])], now_ms=0)

    for i in range(3):  # MAX_MISSES
        live = tr.update([], now_ms=250 * (i + 1))

    assert [t.label for t in live] == ["CAR 1"]
    assert live[0].misses == 3


def test_a_track_dies_after_too_many_consecutive_misses():
    tr = VehicleTracker()
    tr.update([det([0.10, 0.20, 0.40, 0.50])], now_ms=0)

    for i in range(4):
        live = tr.update([], now_ms=250 * (i + 1))

    assert live == []


def test_an_intermittent_detection_does_not_renumber_everything():
    """A missed frame in the middle must not cost the car its identity."""
    tr = VehicleTracker()
    first = tr.update([det([0.10, 0.20, 0.40, 0.50])], now_ms=0)[0].tid

    tr.update([], now_ms=250)
    tr.update([], now_ms=500)
    back = tr.update([det([0.12, 0.22, 0.42, 0.52])], now_ms=750)

    assert back[0].tid == first


# --------------------------------------------------------------------------- #
# numbering
# --------------------------------------------------------------------------- #
def test_numbers_are_lowest_free_per_class():
    """A demo should read CAR 1 / CAR 2, not drift to CAR 41 over ten minutes."""
    tr = VehicleTracker()
    tr.update([det([0.05, 0.05, 0.30, 0.30]), det([0.05, 0.40, 0.30, 0.65])], now_ms=0)

    assert sorted(t.label for t in tr.live()) == ["CAR 1", "CAR 2"]


def test_two_classes_never_share_a_number():
    tr = VehicleTracker()
    tr.update([det([0.05, 0.05, 0.30, 0.30], cls=CAR), det([0.35, 0.05, 0.60, 0.30], cls=BUS)], now_ms=0)

    assert sorted(t.label for t in tr.live()) == ["BUS 1", "CAR 1"]


def test_a_number_is_reused_only_after_the_track_is_gone():
    """Two cars on screen at once can never show the same chip."""
    tr = VehicleTracker()
    tr.update([det([0.05, 0.05, 0.30, 0.30]), det([0.35, 0.05, 0.60, 0.30])], now_ms=0)
    assert sorted(t.label for t in tr.live()) == ["CAR 1", "CAR 2"]

    # Retire CAR 1 only, then a new car arrives.
    for i in range(4):
        tr.update([det([0.35, 0.05, 0.60, 0.30])], now_ms=250 * (i + 1))
    tr.update([det([0.35, 0.05, 0.60, 0.30]), det([0.70, 0.05, 0.95, 0.30])], now_ms=1250)

    labels = sorted(t.label for t in tr.live())
    assert labels == ["CAR 1", "CAR 2"]
    assert len(set(labels)) == len(labels)


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #
def test_a_growing_box_is_still_the_same_vehicle():
    """A car approaching the camera grows fast between frames.

    Consecutive boxes then overlap far below 0.5 while being obviously the same
    object, which is why MATCH_IOU is deliberately low.
    """
    tr = VehicleTracker()
    first = tr.update([det([0.30, 0.40, 0.50, 0.60])], now_ms=0)[0].tid

    grown = tr.update([det([0.20, 0.30, 0.60, 0.70])], now_ms=250)

    assert iou([0.30, 0.40, 0.50, 0.60], [0.20, 0.30, 0.60, 0.70]) < 0.5
    assert grown[0].tid == first


def test_one_detection_cannot_be_claimed_by_two_tracks():
    """A single box must never be assigned to two cars, which would double-count."""
    tr = VehicleTracker()
    tr.update([det([0.05, 0.05, 0.35, 0.35]), det([0.06, 0.06, 0.36, 0.36])], now_ms=0)

    tr.update([det([0.05, 0.05, 0.35, 0.35])], now_ms=250)

    live = tr.live()
    assert len(live) == 2
    assert len({t.tid for t in live}) == 2


def test_the_best_overlap_wins_the_match():
    """A greedy pass in confidence order lets a big box steal a small box's detection."""
    tr = VehicleTracker()
    top_left = tr.update([det([0.10, 0.10, 0.30, 0.30]), det([0.60, 0.60, 0.90, 0.90])], now_ms=0)
    top_left_tid = min(t.tid for t in top_left if t.box[0] < 0.5)

    # Each detection is unambiguous, but they arrive in the opposite order and
    # with swapped confidence: a confidence-ordered greedy pass would hand the
    # big box's detection to the small track.
    tr.update(
        [det([0.62, 0.62, 0.92, 0.92], conf=0.99), det([0.12, 0.12, 0.32, 0.32], conf=0.4)],
        now_ms=250,
    )
    moved = {t.tid: t.box for t in tr.live()}

    assert len(moved) == 2
    assert moved[top_left_tid] == [0.12, 0.12, 0.32, 0.32]


# --------------------------------------------------------------------------- #
# read budget
# --------------------------------------------------------------------------- #
def test_settled_tracks_are_not_read_again():
    """A car parked in view must not occupy the OCR worker for ever.

    OCR costs seconds on this CPU. Without this a single stationary car would be
    re-read on every frame for as long as it stayed in shot.
    """
    tr = VehicleTracker()
    track = tr.update([det([0.10, 0.20, 0.40, 0.50])], now_ms=0)[0]
    widths = {track.tid: 120.0}

    assert tr.plates_to_read(widths, 3, 0.8) == [(track, 120.0)]

    track.plate_settled = True
    assert tr.plates_to_read(widths, 3, 0.8) == []


def test_the_biggest_plate_is_read_first():
    """It carries the most characters, so it is the likeliest usable read per second."""
    tr = VehicleTracker()
    small, big = tr.update(
        [det([0.05, 0.05, 0.25, 0.25]), det([0.40, 0.05, 0.70, 0.35])], now_ms=0
    )
    by_label = {t.label: t for t in (small, big)}

    order = tr.plates_to_read({small.tid: 60.0, big.tid: 200.0}, 2, 0.8)

    assert [t.tid for t, _px in order] == [big.tid, small.tid]
    assert set(by_label) == {"CAR 1", "CAR 2"}


def test_the_read_budget_is_respected():
    tr = VehicleTracker()
    tracks = tr.update(
        [det([0.02 * i, 0.02 * i, 0.30 + 0.02 * i, 0.40 + 0.02 * i]) for i in range(5)], now_ms=0
    )
    widths = {t.tid: 50.0 + 10.0 * i for i, t in enumerate(tracks)}

    assert len(tr.plates_to_read(widths, 2, 0.8)) == 2


def test_a_track_with_no_plate_this_frame_is_not_offered():
    tr = VehicleTracker()
    tr.update([det([0.10, 0.20, 0.40, 0.50])], now_ms=0)

    assert tr.plates_to_read({}, 3, 0.8) == []


# --------------------------------------------------------------------------- #
# wire shape
# --------------------------------------------------------------------------- #
def test_the_wire_shape_is_what_the_phone_parses():
    tr = VehicleTracker()
    payload = tr.update([det([0.123456, 0.2, 0.4, 0.5], cls=CAR, conf=0.87654)], now_ms=0)[0].as_dict()

    assert set(payload) == {"track", "label", "cls", "conf", "box"}
    assert payload["label"] == "CAR 1"
    assert payload["cls"] == CAR
    assert payload["conf"] == pytest.approx(0.8765, abs=1e-4)
    # Rounded, not raw: normalised coords are 5 dp at 960 px and 1 dp is 0.1 px.
    assert payload["box"] == [0.12346, 0.2, 0.4, 0.5]

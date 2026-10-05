"""A plate with no vehicle around it is still a plate.

The plate detector and the vehicle detector are different models looking for
different things, and they disagree: the vehicle pass can miss a car that is half
out of frame, at night, or behind a pillar, while the plate pass still sees the
rectangle it was trained to find. When that happened the plate was dropped, on the
grounds that drawing it put "stray red rectangles floating over empty road" on
screen.

That was the wrong trade. The dropped detection was not usually a false positive
in the middle of nowhere - it was the one car worth looking at, in a frame where
the only thing wrong was that the bigger model was unsure. And the phone keys its
plate boxes on nothing but the track id the server sends, so reporting one costs
it no changes at all.

What these tests pin down:

  * an orphan is reported with a negative id that cannot collide with a real one
  * it is drawn (it reaches the wire list) AND read (it costs OCR capacity)
  * it costs OCR exactly once, ever - a sign in the same place is not re-read on
    every frame for as long as the screen stays open, which is what would
    otherwise happen and what would starve the pass the phone renders
  * at most one orphan per frame, so a frame full of plate-shaped clutter cannot
    outbid a car that was actually detected
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.api.v1 import live_scan as ls  # noqa: E402
from app.ws.scan_manager import ORPHAN_MAX_ATTEMPTS, ScanConnection  # noqa: E402


def _track(tid: int, box=(0.3, 0.3, 0.7, 0.7)):
    from app.ws.scan_manager import Track

    return Track(tid=tid, cls_id=2, label="CAR 1", box=list(box), conf=0.9, first_ms=0.0, last_ms=0.0)


def _plate_box(cx: float, cy: float, w: float = 0.06, h: float = 0.02):
    """A plate-shaped box, normalised, centred on (cx, cy)."""
    return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]


def test_orphan_plate_is_reported_with_a_negative_id():
    """No vehicle contains it, so it still gets drawn - under an id of its own."""
    box = _plate_box(0.5, 0.5)
    plates, found = ls.assign_plates([], [(box, 0.9)], 1280, 720)

    assert len(plates) == 1, "the orphan was dropped, which is the bug"
    tid = plates[0]["track"]
    assert tid < 0, f"orphan track id should be negative, got {tid}"
    assert plates[0]["box"] == box
    assert tid in found, "an orphan that is drawn must also be readable"


def test_orphan_id_cannot_collide_with_a_real_track_id():
    """Real ids count up from 1. If these ever met, a plate box would be drawn on
    the wrong car - the exact failure the smallest-containing-vehicle rule exists
    to prevent."""
    box = _plate_box(0.5, 0.5)
    for _ in range(50):
        plates, _found = ls.assign_plates([], [(box, 0.9)], 1280, 720)
    assert all(p["track"] < 0 for p in plates)
    assert ls.ORPHAN_TID_BASE <= -1, "ids count down from here, so the first must be negative"


def test_a_hosted_plate_keeps_its_real_track_id():
    """The common case must be untouched: a plate inside a car stays keyed by the
    car, so the red box rides on the green one."""
    track = _track(7)
    box = _plate_box(0.5, 0.5)
    plates, found = ls.assign_plates([track], [(box, 0.9)], 1280, 720)

    assert [p["track"] for p in plates] == [7]
    assert 7 in found


def test_orphan_gets_exactly_one_read_ever():
    """The reason orphans exist at all is that a sign on a wall is not a car.

    Without a budget, one would be handed to OCR on every frame for as long as
    the screen was open, and OCR is ~2 s per crop on this CPU.
    """
    conn = ScanConnection()
    box = _plate_box(0.5, 0.5)

    assert conn.claim_orphan(-1, box, 1000.0, ORPHAN_MAX_ATTEMPTS) is True, "first look must be allowed"
    assert conn.claim_orphan(-1, box, 1001.0, ORPHAN_MAX_ATTEMPTS) is False, "second look must be refused"
    assert conn.claim_orphan(-1, box, 1002.0, ORPHAN_MAX_ATTEMPTS) is False


def test_a_slight_jitter_is_still_the_same_orphan():
    """Keyed by position, not identity - a plate that wobbles a pixel between
    frames must not earn a fresh budget every frame."""
    conn = ScanConnection()
    assert conn.claim_orphan(-1, [0.470, 0.500, 0.530, 0.520], 1000.0, ORPHAN_MAX_ATTEMPTS) is True
    assert conn.claim_orphan(-1, [0.471, 0.500, 0.531, 0.520], 1000.5, ORPHAN_MAX_ATTEMPTS) is False


def test_a_different_place_gets_its_own_budget():
    """Two signs, two chances - the budget is per object, not a global one."""
    conn = ScanConnection()
    assert conn.claim_orphan(-1, _plate_box(0.2, 0.2), 1000.0, ORPHAN_MAX_ATTEMPTS) is True
    assert conn.claim_orphan(-2, _plate_box(0.8, 0.8), 1000.0, ORPHAN_MAX_ATTEMPTS) is True


def test_the_budget_does_not_outlive_its_object():
    """A plate-shaped thing that leaves and a new one arrives later must be read,
    or a phone held on an empty wall for a minute stops seeing anything."""
    from app.ws.scan_manager import ORPHAN_MEMORY_SECONDS

    conn = ScanConnection()
    box = _plate_box(0.5, 0.5)
    assert conn.claim_orphan(-1, box, 1000.0, ORPHAN_MAX_ATTEMPTS) is True
    assert conn.claim_orphan(-1, box, 1001.0, ORPHAN_MAX_ATTEMPTS) is False
    later = 1000.0 + ORPHAN_MEMORY_SECONDS + 1
    assert conn.claim_orphan(-1, box, later, ORPHAN_MAX_ATTEMPTS) is True


def test_at_most_one_orphan_read_per_frame(monkeypatch):
    """A frame full of plate-shaped clutter must not outbid a real car.

    `MAX_READS_PER_FRAME` bounds the cars; this bounds the rest, and the cars are
    spent first because `infer_plates` fills its crop list from the tracks before
    it ever looks at the orphans.
    """
    conn = ScanConnection()
    frames = {
        "detections": [(_plate_box(0.2 + 0.05 * i, 0.5), 0.9) for i in range(6)],
        "assign": None,
        "crop": object(),
    }
    monkeypatch.setattr(ls, "detect_plates", lambda bgr: frames["detections"])

    real_assign = ls.assign_plates

    def assign(tracks, dets, w, h):
        plates, found = real_assign(tracks, dets, w, h)
        frames["assign"] = found
        return plates, found

    monkeypatch.setattr(ls, "assign_plates", assign)
    monkeypatch.setattr(ls, "_crop_for_ocr", lambda bgr, box: frames["crop"])

    _plates, crops = ls.infer_plates(
        bgr=object(), w=1280, h=720, tracks=[], tracker=ls.VehicleTracker(), ocr_busy=None, conn=conn
    )

    assert len(crops) == ls.MAX_ORPHAN_READS_PER_FRAME, (
        f"expected at most {ls.MAX_ORPHAN_READS_PER_FRAME} orphan reads, got {len(crops)}"
    )


def test_an_orphan_read_leaves_nothing_behind():
    """The box the budget needs has to be handed back when the read finishes, or
    the dict grows for as long as the screen is open."""
    conn = ScanConnection()
    box = _plate_box(0.5, 0.5)
    conn.claim_orphan(-1, box, 1000.0, ORPHAN_MAX_ATTEMPTS)
    assert conn.take_orphan_box(-1) is not None
    assert conn.take_orphan_box(-1) is None
    assert conn.orphan_boxes == {}


@pytest.mark.parametrize("tid", [-1, -2, -50])
def test_settling_an_orphan_is_final(tid):
    """Whether the read succeeded or failed, the budget is spent."""
    conn = ScanConnection()
    box = _plate_box(0.5, 0.5)
    conn.claim_orphan(tid, box, 1000.0, ORPHAN_MAX_ATTEMPTS)
    conn.settle_orphan(conn.take_orphan_box(tid), ORPHAN_MAX_ATTEMPTS)
    assert conn.claim_orphan(tid, box, 1001.0, ORPHAN_MAX_ATTEMPTS) is False
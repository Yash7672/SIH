"""Per-connection vehicle tracking for the live-scan WebSocket.

Why this exists
---------------
The detector only ever answers "there is a car at [0.31,0.42,0.52,0.61]". On
its own that is not enough to draw a CCTV feed: the overlay needs to know that
*this* car is the same one it drew 250 ms ago, otherwise every rectangle is
re-keyed on every frame and the labels ("CAR 1", "CAR 2") shuffle constantly as
detections are re-ordered by confidence.

So each WebSocket connection owns one :class:`VehicleTracker` - never a shared
one. Two volunteers pointing two phones at the same junction must not hand each
other track ids, and a shared tracker would also let one phone's frame expire
the other phone's tracks.

Numbering policy
----------------
Labels are ``"<CLASS> <n>"`` with the class taken from the detector's own class
names and ``n`` the lowest number not currently in use for that class. Reusing
the lowest free number (rather than counting up forever) keeps the chips reading
"CAR 1 / CAR 2" during a demo instead of drifting to "CAR 41", and it is safe
because a number is only released once its track is gone - two live vehicles
can therefore never show the same label at the same time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# YOLOv8n COCO class ids. This is the *whole* vocabulary: the COCO checkpoint has
# no auto-rickshaw class, so there is no "AUTO" label to emit. Inventing one
# would mean showing a chip that can never be true.
#
#   2 = car, 3 = motorcycle, 5 = bus, 7 = truck
COCO_CLASS_IDS: Tuple[int, ...] = (2, 3, 5, 7)

# cls id -> label prefix. "motorcycle" is called BIKE because that is what the
# label space in the mobile overlay and the traffic-density layer already use.
CLASS_LABELS: Dict[int, str] = {
    2: "CAR",
    3: "BIKE",
    5: "BUS",
    7: "TRUCK",
}

# How many consecutive frames a track may go unmatched before it is dropped.
# Detection on a moving vehicle is intermittent (motion blur, a pole in the way,
# the detector's own confidence threshold), so a single miss must not renumber
# every chip on screen. ~700 ms at 4 fps.
MAX_MISSES = 3
# A plate-shaped thing with no vehicle around it is read this many times, ever.
# One is the whole point: we want to see whether it reads, not to keep watching
# it. See `ScanConnection.claim_orphan`.
ORPHAN_MAX_ATTEMPTS = 1
# How long an orphan entry is remembered. Long enough to cover a car sitting at a
# light, short enough that a new sign in the same place gets a fresh look.
ORPHAN_MEMORY_SECONDS = 30.0

# IoU needed to consider a detection "the same vehicle" as a live track.
# Deliberately low: a car approaching the camera grows quickly between frames,
# so consecutive boxes overlap far less than 0.5 even though it is obviously the
# same object.
MATCH_IOU = 0.20


def class_label(cls_id: int) -> str:
    """Human label for a detector class id, or a safe generic fallback.

    A class outside the table can only reach here if the checkpoint is swapped
    for one with a wider vocabulary; showing the raw id is honest where a made
    up name would not be.
    """
    return CLASS_LABELS.get(int(cls_id), f"VEH{int(cls_id)}")


def iou(box_a: Sequence[float], box_b: Sequence[float]) -> float:
    """Intersection over union of two normalised [x1,y1,x2,y2] boxes."""
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = ix2 - ix1, iy2 - iy1
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


@dataclass
class Detection:
    """One detector hit, already normalised to 0..1."""

    box: List[float]
    cls_id: int
    conf: float


@dataclass
class Track:
    """A vehicle the overlay has been told to keep drawing."""

    tid: int
    cls_id: int
    label: str
    box: List[float]
    conf: float
    first_ms: float
    last_ms: float
    hits: int = 1
    misses: int = 0

    # Plate read state. The OCR budget is per track, not per frame: the same car
    # stays in view for many frames and re-reading it every time is what used to
    # saturate the single OCR worker.
    attempts: int = 0
    plate_text: Optional[str] = None
    plate_conf: float = 0.0
    plate_valid: bool = False
    plate_stolen: bool = False
    # Set once this track will never be read again (confident enough, or out of
    # attempts). The client keeps showing the text; the server stops paying for it.
    plate_settled: bool = False

    def as_dict(self) -> Dict[str, object]:
        """The wire shape the phone expects."""
        return {
            "track": self.tid,
            "label": self.label,
            "cls": self.cls_id,
            "conf": round(float(self.conf), 4),
            "box": [round(float(v), 5) for v in self.box],
        }


class VehicleTracker:
    """IoU tracker over normalised vehicle boxes. One per connection."""

    def __init__(self, max_misses: int = MAX_MISSES, match_iou: float = MATCH_IOU) -> None:
        self._max_misses = max_misses
        self._match_iou = match_iou
        self._tracks: Dict[int, Track] = {}
        # Monotonic, and never restarted once it has been used: a tid is never
        # recycled inside one connection, so a `plate` reply that arrives seconds
        # late cannot land on a different car that has since arrived at the same
        # place. Recycling would be the single worst failure this feature could
        # have - it would silently paint a stolen plate onto an innocent car.
        self._next_tid = 0
        # Per class, the numbers currently on screen. Keeping the numbers here
        # (rather than as a counter) is what makes "lowest free number" possible.
        self._used: Dict[int, set] = {cls: set() for cls in CLASS_LABELS}

    # ------------------------------------------------------------------ #
    # numbering
    # ------------------------------------------------------------------ #
    def _take_number(self, cls_id: int) -> int:
        used = self._used.setdefault(cls_id, set())
        n = 1
        while n in used:
            n += 1
        used.add(n)
        return n

    @staticmethod
    def label_of(cls_id: int, number: int) -> str:
        return f"{class_label(cls_id)} {number}"

    @staticmethod
    def _number_of(label: str) -> Optional[int]:
        try:
            return int(label.rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            return None

    def _new_track(self, det: Detection, now_ms: float) -> Track:
        self._next_tid += 1
        number = self._take_number(det.cls_id)
        track = Track(
            tid=self._next_tid,
            cls_id=det.cls_id,
            label=self.label_of(det.cls_id, number),
            box=list(det.box),
            conf=det.conf,
            first_ms=now_ms,
            last_ms=now_ms,
        )
        self._tracks[track.tid] = track
        return track

    def _drop(self, tid: int) -> None:
        track = self._tracks.pop(tid, None)
        if track is None:
            return
        used = self._used.get(track.cls_id)
        number = self._number_of(track.label)
        if used and number is not None:
            used.discard(number)

    # ------------------------------------------------------------------ #
    # update
    # ------------------------------------------------------------------ #
    def update(self, detections: Iterable[Detection], now_ms: float) -> List[Track]:
        """Match `detections` against live tracks and return every live track.

        Returns *all* live tracks, not just the ones just matched, so a vehicle
        that the detector missed this frame keeps a stable box (and a stable
        label) instead of blinking out and back.
        """
        dets = list(detections)

        # Score every (track, detection) pair once, then take the best available
        # matches in descending IoU. A greedy pass in confidence order would let
        # one big box steal the detection that a smaller box needed.
        pairs = []
        for tid, track in self._tracks.items():
            for di, det in enumerate(dets):
                score = iou(track.box, det.box)
                if score >= self._match_iou:
                    pairs.append((score, tid, di))
        pairs.sort(key=lambda p: (-p[0], p[1], p[2]))

        used_tracks: set = set()
        used_dets: set = set()
        for _score, tid, di in pairs:
            if tid in used_tracks or di in used_dets:
                continue
            track = self._tracks[tid]
            det = dets[di]
            if track.cls_id != det.cls_id:
                # Matched geometry but a different class: treat as a new object
                # rather than relabelling a car into a bus mid-crossing.
                continue
            track.box = list(det.box)
            track.conf = det.conf
            track.last_ms = now_ms
            track.hits += 1
            track.misses = 0
            used_tracks.add(tid)
            used_dets.add(di)

        for di, det in enumerate(dets):
            if di in used_dets:
                continue
            self._new_track(det, now_ms)
            used_tracks.add(self._next_tid)

        for tid, track in list(self._tracks.items()):
            if tid in used_tracks:
                continue
            track.misses += 1
            if track.misses > self._max_misses:
                self._drop(tid)

        return sorted(self._tracks.values(), key=lambda t: t.tid)

    # ------------------------------------------------------------------ #
    def get(self, tid: int) -> Optional[Track]:
        return self._tracks.get(tid)

    def live(self) -> List[Track]:
        return sorted(self._tracks.values(), key=lambda t: t.tid)

    def plates_to_read(self, plate_box: Dict[int, float], max_reads: int, done_conf: float) -> List[Tuple[Track, float]]:
        """Pick which tracks to send to OCR this frame.

        `plate_box` maps tid -> full-frame plate width in pixels; anything below
        the caller's minimum has already been filtered by the caller. A track is
        skipped when it has settled (read well enough, or out of attempts), which
        is what bounds the OCR load over a long stream.
        """
        candidates = [t for t in self.live() if t.tid in plate_box and not t.plate_settled]
        # Biggest plate first: it carries the most characters, so it is the most
        # likely to produce a usable read per second of OCR.
        candidates.sort(key=lambda t: plate_box.get(t.tid, 0.0), reverse=True)
        return [(t, plate_box[t.tid]) for t in candidates[:max_reads]]


@dataclass
class ScanConnection:
    """Everything one live-scan socket owns. Never shared between sockets.

    Keeping it in one object (rather than as locals inside the handler) is what
    makes the "two phones never share state" rule checkable: the handler builds
    exactly one of these and never looks at a module-level one.
    """

    tracker: VehicleTracker = field(default_factory=VehicleTracker)
    # At most one *vehicle* pass may be in flight. A frame offered while one is
    # already being inferred is dropped, not queued, so the detector never works
    # through a backlog of stale views of the road.
    frame_processing: bool = False
    # The plate pass gets its own slot rather than sharing the vehicle one, and the
    # reason is measured: the vehicle pass answers in ~50 ms but the plate pass
    # runs for another ~60-400 ms, so holding one slot across both makes the server
    # refuse the next frame for the whole of the plate pass. The phone answers its
    # own pacing on `boxes`, so it offers the next frame ~120 ms later - squarely
    # inside that window, and roughly one frame in ten was being thrown away.
    # The green boxes are what the phone renders; the plate boxes can afford to
    # lag a frame behind. Both passes still mutate the tracker, which is why each
    # has exactly one slot of its own rather than none.
    plate_processing: bool = False
    last_frame_time: float = 0.0
    device: object = None
    ocr_tasks: set = field(default_factory=set)
    # Track ids with a crop already in the OCR pipeline. A frame that finds a car
    # here skips it without spending one of the track's read attempts, so a slow
    # read never costs the car its chance to be read at all.
    ocr_tracks: set = field(default_factory=set)
    # One-shot handles for the OCR capacity each in-flight read is holding, so the
    # connection's cleanup can hand it back even for a task that never started.
    ocr_claims: set = field(default_factory=set)
    # What has already been spent on plate-shaped things with no vehicle around
    # them. Real tracks get a per-car attempt budget that ends when the car is
    # read or leaves; an orphan has no car to hang that on, so it gets this
    # instead, keyed by where it is on screen. Without it, a poster that reads as
    # a plate would be sent to OCR on every single frame for as long as the screen
    # was open, and OCR is the slowest thing in the system by an order of
    # magnitude.
    orphans: dict = field(default_factory=dict)
    # The box each in-flight orphan read belongs to, keyed by its negative track
    # id. The crop alone cannot tell the budget where the thing was, and the
    # alternative - widening `infer_plates`' return type - would reach every
    # caller of it, including the offline helper.
    orphan_boxes: dict = field(default_factory=dict)
    # Per-track read history and the verdict derived from it. Keyed by tid and
    # owned by the connection, not by `Track`, because the decision has to survive
    # the track being re-used for the next frame's box while still belonging to
    # that one car - and because a track that leaves takes its evidence with it.
    verdicts: dict = field(default_factory=dict)
    # Track ids the server has asked the phone for a higher-resolution frame
    # about. A plate narrower than the threshold is being read from too few pixels
    # to trust, and the only cure is more pixels, which only the phone can supply.
    hires_pending: set = field(default_factory=set)
    # When the last `need_hires` went out. Rate-limited process-wide per
    # connection because a hi-res frame costs ~300 KB of upload and a second of
    # encode, and a car parked in view would otherwise ask for one every frame.
    last_hires_at: float = 0.0
    # Set when the server has asked for one, so the next frame is understood to be
    # the hi-res one even if it arrives before the plate pass that asked for it.
    hires_active: bool = False

    def verdict_for(self, tid: int, now: Optional[float] = None) -> "TrackVerdict":
        """The read history for one track, created on first use.

        Imported lazily so `scan_manager` stays importable without the service
        layer - the WebSocket protocol tests build connections directly.
        """
        from app.services.plate_verdict import TrackVerdict

        existing = self.verdicts.get(tid)
        if existing is None:
            existing = TrackVerdict(tid, now=now)
            self.verdicts[tid] = existing
        return existing

    def prune_verdicts(self, live_tids: Iterable[int]) -> None:
        """Drop the evidence of tracks that are gone.

        Without this the dict grows for the lifetime of the socket - one entry per
        car that ever passed - and, worse, a tid is never recycled but memory is
        still finite.
        """
        alive = set(live_tids)
        for tid in list(self.verdicts):
            if tid not in alive:
                self.verdicts.pop(tid, None)
                self.hires_pending.discard(tid)

    def claim_orphan(self, tid: int, box: Sequence[float], now: float, max_attempts: int) -> bool:
        """True when this plate-shaped thing is allowed one more read.

        Keyed by a coarse cell rather than the exact box, so a plate that jitters
        by a pixel or two between frames is recognised as the same object and does
        not earn a fresh budget every time. Old entries are swept here rather than
        by a timer, so an idle connection holds nothing.
        """
        key = (round(box[0] * 40), round(box[1] * 40))
        # Swept before the lookup, not only on a miss. Doing it only on the
        # "brand new plate" branch meant an entry that had already spent its
        # budget - which is the common case for a phone held on a wall - was never
        # a candidate for the sweep, so the dict only ever grew.
        for stale in [k for k, (_n, seen) in self.orphans.items() if now - seen > ORPHAN_MEMORY_SECONDS]:
            self.orphans.pop(stale, None)
        spent = self.orphans.get(key)
        if spent is not None:
            if spent[0] >= max_attempts:
                return False
            self.orphans[key] = (spent[0] + 1, spent[1])
        else:
            self.orphans[key] = (1, now)
        self.orphan_boxes[tid] = tuple(float(v) for v in box)
        return True

    def take_orphan_box(self, tid: int) -> Optional[Tuple[float, ...]]:
        """The box an orphan read was claimed for, forgotten on the way out."""
        return self.orphan_boxes.pop(tid, None)

    def settle_orphan(self, box: Sequence[float], max_attempts: int) -> None:
        """Mark a plate-shaped thing as done with, so it is never offered again."""
        key = (round(box[0] * 40), round(box[1] * 40))
        self.orphans[key] = (max_attempts, self.orphans.get(key, (0, 0.0))[1])

    def begin_frame(self, now: float) -> bool:
        """Claim the single in-flight slot. False when a frame is already running."""
        if self.frame_processing:
            return False
        self.frame_processing = True
        self.last_frame_time = now
        return True

    def end_frame(self) -> None:
        self.frame_processing = False

    def begin_plates(self) -> bool:
        """Claim the plate pass's slot. False when the previous plate pass is live."""
        if self.plate_processing:
            return False
        self.plate_processing = True
        return True

    def end_plates(self) -> None:
        self.plate_processing = False

    def reclaim_ocr(self) -> None:
        """Forget finished OCR tasks so the set cannot grow for ever."""
        for done in [t for t in self.ocr_tasks if t.done()]:
            self.ocr_tasks.discard(done)
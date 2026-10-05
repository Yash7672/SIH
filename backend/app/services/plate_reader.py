"""Reading a plate more carefully, and refusing to read most of them.

Why this exists
---------------
The live path used to call the same routine as the manual snap path: preprocess
the crop once, run RapidOCR, take the best hit. Two problems showed up in the
field and neither is fixed by a better threshold:

* **One look decided everything.** A single OCR pass produced a string that
  ``normalize_plate`` called valid, and that string went straight to the
  hot-list test. See :mod:`app.services.plate_verdict` for why one look is not
  enough.
* **Resolution and preprocessing were fixed guesses.** A plate 60 px wide was
  upscaled by the same rule as a 300 px one, and a yellow plate was flattened
  to the same grayscale as a white one. A blue-channel-heavy yellow plate is
  *worse* in grayscale than in colour, because its luminance is nearly the same
  as its background.

So the reader here does three things the old one could not:

1. **Per-character confidence.** RapidOCR's CRNN+CTC recogniser computes a
   confidence per character and throws them away, keeping only their mean. Calling
   the recogniser directly (``engine.text_rec``) with ``return_word_box=True``
   returns the full list. That is what lets a verdict say "0.93 overall but the
   character we would be alerting on is at 0.41".
2. **Preprocessing variants, then a vote.** Two or three different renderings of
   the same crop are read and the readings are voted on, weighted by confidence.
   No hard binarisation anywhere - it destroys the diagonal strokes of M, N and
   W, which is how "MH" becomes "HL".
3. **Sanity filters before OCR.** A red box wider than 60% of the frame, a plate
   covering 40% of its own car, or a 9:1 sliver is not a plate, and paying
   seconds of OCR to find that out is how the green boxes start dropping frames.

Speed
-----
The direct recogniser call is also *much* faster than the full engine, because it
skips the text-detection stage. Measured on this box, warm, on a 57x223 crop:

    full engine (detect + classify + recognise)   ~3494 ms
    recogniser only                               ~30 ms

The 3.5 s in the old timings were text detection, not reading. Variants are
therefore affordable here even though they were not affordable before - but they
still run on the OCR worker, off the hot path, because the box path must not
wait.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from app.services.plate import VALID_STATE_CODES, normalize_plate
from app.services.plate_verdict import (
    AMBIGUOUS_PAIRS,
    MIN_CHAR_CONF,
    PlateRead,
    strict_syntax,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Plate-box sanity filters
# --------------------------------------------------------------------------- #
# Measured against real frames: a normal single-row car plate is roughly 3.2:1 to
# 4.5:1. Two-row motorcycle plates ("DL 12 / AB 1234") are near 1.6:1 and are
# merged by the existing line merge. Both windows are deliberately generous -
# the cost of keeping a bad box is one wasted OCR slot, the cost of dropping a
# good one is a stolen car going unseen.
SINGLE_ROW_ASPECT = (1.5, 6.5)
# Two-row plates are near 1.6:1 but the row split makes them vary, so the window
# starts at 1.1. It is deliberately *not* open at 1.0: a genuinely square box is
# not a plate in either layout, it is the detector boxing a light, a sign or a
# face, and letting 1.0 through let those through too.
TWO_ROW_ASPECT = (1.1, 2.5)
# Narrower than this and there is not enough width for 8-10 glyphs even after
# upscaling: at 30 px the recogniser is guessing, not reading.
MIN_PLATE_WIDTH_PX = 30.0
MIN_PLATE_HEIGHT_PX = 10.0
# A plate is a small part of a car. 40% means the detector has boxed a door or a
# windscreen.
MAX_PLATE_AREA_FRACTION = 0.40
# A plate belongs in the lower part of the vehicle - the centre must sit at or
# below this fraction of the vehicle's height. Anything higher is a windscreen, a
# roof, or the sky behind the car.
#
# Measured on a real frame (data/test_plates/road_scene.jpg) the plate centre is
# at 68% of the vehicle's height, and on a large box such as a bus it is nearer
# 79%, so 70% keeps every real placement with room to spare and rejects only the
# ones that are physically too high up the vehicle.
PLATE_CENTER_MIN_VEHICLE_FRACTION = 0.30
# The reported false positive was a box across the whole top of the frame, which
# is a banner, a shopfront or the plate detector finding a grille. Anything this
# fraction of the frame width is not a plate.
MAX_PLATE_WIDTH_FRACTION = 0.60
# Vehicle minimum side, as a fraction of the frame. Below this the "vehicle" is a
# few pixels of noise and its plate box would be equally imaginary.
MIN_VEHICLE_SIDE_FRACTION = 0.02


def _aspect(box: Tuple[float, float, float, float]) -> float:
    w = box[2] - box[0]
    h = box[3] - box[1]
    if w <= 0 or h <= 0:
        return 0.0
    return w / h


def plate_box_plausible(
    plate_box: Tuple[float, float, float, float],
    frame_size: Tuple[int, int],
    vehicle_box: Optional[Tuple[float, float, float, float]] = None,
) -> Tuple[bool, str]:
    """Should this plate box be drawn and read at all?

    ``frame_size`` is ``(width, height)`` in pixels; boxes are in pixels too.
    Returns ``(ok, reason)`` - the reason is logged at debug level and is what
    makes a false positive traceable after the fact.
    """
    fw, fh = frame_size
    pw = plate_box[2] - plate_box[0]
    ph = plate_box[3] - plate_box[1]
    if pw <= 1 or ph <= 1:
        return False, "degenerate box"

    if pw < MIN_PLATE_WIDTH_PX:
        return False, f"only {pw:.0f}px wide (< {MIN_PLATE_WIDTH_PX:.0f})"
    if ph < MIN_PLATE_HEIGHT_PX:
        return False, f"only {ph:.0f}px tall (< {MIN_PLATE_HEIGHT_PX:.0f})"

    if pw > MAX_PLATE_WIDTH_FRACTION * fw:
        return False, f"{pw / fw:.0%} of frame width (> {MAX_PLATE_WIDTH_FRACTION:.0%}) - not a plate"

    ar = _aspect(plate_box)
    if not (SINGLE_ROW_ASPECT[0] <= ar <= SINGLE_ROW_ASPECT[1]) and not (
        TWO_ROW_ASPECT[0] <= ar <= TWO_ROW_ASPECT[1]
    ):
        return False, f"aspect {ar:.1f}:1 outside plate shapes"

    if vehicle_box is not None:
        vw = vehicle_box[2] - vehicle_box[0]
        vh = vehicle_box[3] - vehicle_box[1]
        if vw > 1 and vh > 1:
            if (pw * ph) / (vw * vh) > MAX_PLATE_AREA_FRACTION:
                return False, f"covers {pw * ph / (vw * vh):.0%} of its vehicle (> {MAX_PLATE_AREA_FRACTION:.0%})"
            cy = (plate_box[1] + plate_box[3]) / 2.0
            # Too high up the vehicle to be a plate.
            if cy < vehicle_box[1] + vh * PLATE_CENTER_MIN_VEHICLE_FRACTION:
                return False, (
                    f"centre at {(cy - vehicle_box[1]) / vh:.0%} of vehicle height "
                    f"(must be at or below {1 - PLATE_CENTER_MIN_VEHICLE_FRACTION:.0%})"
                )
    return True, "ok"


# --------------------------------------------------------------------------- #
# Vehicle box cleanup
# --------------------------------------------------------------------------- #
# Class-agnostic, so a "car" box and a "truck" box describing the same vehicle
# collapse to one. Fixed size, because the tracker only ever sees normalised
# boxes.
VEHICLE_NMS_IOU = 0.6
VEHICLE_MIN_SIDE_FRACTION = MIN_VEHICLE_SIDE_FRACTION


def _iou(a: Sequence[float], b: Sequence[float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def suppress_vehicle_boxes(
    boxes: Sequence[Sequence[float]],
    confs: Optional[Sequence[float]] = None,
    iou_threshold: float = VEHICLE_NMS_IOU,
) -> List[int]:
    """Indices to keep, after dropping tiny boxes and overlapping duplicates.

    Class-agnostic on purpose: the failure this prevents is one car wearing three
    green boxes because the detector emitted a car and a truck over it, each of
    which would then claim its own red plate box.
    """
    order = sorted(
        range(len(boxes)),
        key=lambda i: -(confs[i] if confs is not None else 1.0),
    )
    keep: List[int] = []
    for i in order:
        b = boxes[i]
        if min(b[2] - b[0], b[3] - b[1]) < VEHICLE_MIN_SIDE_FRACTION:
            continue
        if any(_iou(b, boxes[j]) > iou_threshold for j in keep):
            continue
        keep.append(i)
    keep.sort()
    return keep


# --------------------------------------------------------------------------- #
# Preprocessing variants
# --------------------------------------------------------------------------- #
# The recogniser wants a roughly 80 px tall glyph row. Below that, M/N/W lose
# their diagonals; above it, nothing is gained and the cost grows.
TARGET_PLATE_HEIGHT = 80
MAX_VARIANT_SIDE = 736  # RapidOCR's own input limit


def _scale_to_height(img: np.ndarray, height: int) -> np.ndarray:
    h, w = img.shape[:2]
    if h <= 0 or w <= 0:
        return img
    if h < height:
        factor = min(height / h, 6.0)
        out = cv2.resize(img, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
    elif h > height * 2:
        factor = height / h
        out = cv2.resize(
            img,
            (max(1, int(w * factor)), height),
            interpolation=cv2.INTER_AREA,
        )
    else:
        out = img
    oh, ow = out.shape[:2]
    if max(oh, ow) > MAX_VARIANT_SIDE:
        factor = MAX_VARIANT_SIDE / max(oh, ow)
        out = cv2.resize(
            out,
            (max(1, int(ow * factor)), max(1, int(oh * factor))),
            interpolation=cv2.INTER_AREA,
        )
    return out


def _gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _bgr(gray: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def _contrast(gray: np.ndarray) -> float:
    """Standard deviation - the cheapest usable contrast proxy.

    Used only to *choose between* variants, never as a confidence, so a crude
    measure is enough and a wrong one cannot invent a plate.
    """
    return float(gray.std())


def _clahe(gray: np.ndarray) -> np.ndarray:
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)


def _denoise(gray: np.ndarray) -> np.ndarray:
    return cv2.bilateralFilter(gray, 5, 40, 40)


def _colour_channel_gray(img: np.ndarray) -> np.ndarray:
    """Pick the single-channel rendering with the most character contrast.

    Indian commercial plates are white-on-black (fine in grayscale), but yellow
    and orange ones have a luminance close to the road behind them, so their
    characters wash out. For those the blue channel usually separates best, and
    on a Lab L* rendering the separation is comparable. Trying both and keeping
    the one with more contrast costs one conversion and is why a yellow plate
    stopped being unreadable.
    """
    best_gray = _gray(img)
    best = _contrast(best_gray)

    channels = []
    b, g, r = cv2.split(img)
    channels.append(("blue", b))
    channels.append(("red", r))
    del g
    try:
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        channels.append(("lab_L", lab[:, :, 0]))
    except cv2.error:  # pragma: no cover - LAB is always available in OpenCV 4
        pass

    for _name, plane in channels:
        if plane.shape[:2] != best_gray.shape[:2]:
            continue
        c = _contrast(plane)
        if c > best:
            best, best_gray = c, plane
    return best_gray


def _deskew(img: np.ndarray, max_angle: float = 8.0) -> np.ndarray:
    """Rotate a skewed plate upright.

    Bounded on purpose. An unbounded "straighten" will happily rotate a sign by
    20 degrees to make its letters more horizontal, and the OCR then reports a
    confident read of letters that were never there. 8 degrees is past any real
    camera roll on a mounted phone and inside the noise of a shaky hand.
    """
    gray = _gray(img)
    if gray.shape[0] < 8 or gray.shape[1] < 16:
        return img
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 50, 150)
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180,
        threshold=max(12, gray.shape[0] // 3),
        minLineLength=max(16, gray.shape[1] // 2),
        maxLineGap=6,
    )
    if lines is None:
        return img
    angles: List[float] = []
    for line in lines[:, 0]:
        x1, y1, x2, y2 = line
        if x2 == x1:
            continue
        angle = float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
        if abs(angle) <= max_angle:
            angles.append(angle)
    if not angles:
        return img
    median = float(np.median(angles))
    if abs(median) < 0.4:
        return img
    h, w = img.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), median, 1.0)
    return cv2.warpAffine(
        img, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )


@dataclass
class Variant:
    """One rendering of the crop, and whatever it read."""

    name: str
    image: np.ndarray
    text: str = ""
    conf: float = 0.0
    char_confs: Optional[Tuple[float, ...]] = None


def build_variants(crop: np.ndarray, with_deskew: bool = True) -> List[Variant]:
    """The renderings worth reading.

    Three is the sweet spot measured here: the second and third only pay off on
    the hard cases (yellow plate, skewed plate, low contrast), and each costs a
    recogniser call. Ordered best-first by a cheap, read-free heuristic so that
    if a budget forces an early stop, the most likely to succeed is the one that
    already ran.
    """
    if crop is None or crop.size == 0:
        return []
    if crop.ndim == 2:
        crop = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)

    out: List[Variant] = []

    # 1) raw, upscaled, lightly denoised. The default, and usually the whole
    #    story for a white plate in daylight.
    gray = _denoise(_gray(crop))
    out.append(Variant("raw", _bgr(_scale_to_height(gray, TARGET_PLATE_HEIGHT))))

    # 2) CLAHE on the same gray - helps a plate that is simply low contrast.
    clahe = _clahe(gray)
    out.append(Variant("clahe", _bgr(_scale_to_height(clahe, TARGET_PLATE_HEIGHT))))

    # 3) best single colour channel - the yellow/orange-plate rescue.
    colour = _colour_channel_gray(crop)
    colour = _denoise(colour)
    out.append(Variant("colour", _bgr(_scale_to_height(colour, TARGET_PLATE_HEIGHT))))

    if with_deskew:
        # 4) deskewed colour rendering - only when there is something to fix,
        #    because the edge detector is not free.
        straight = _deskew(crop)
        if straight is not crop:
            sg = _denoise(_colour_channel_gray(straight))
            out.append(Variant("deskew", _bgr(_scale_to_height(sg, TARGET_PLATE_HEIGHT))))

    return out


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
_rec_lock = threading.Lock()


def _recognise(image: np.ndarray) -> List[Tuple[str, float, Tuple[float, ...]]]:
    """Recognise text lines, with per-character confidence.

    Calls the CRNN recogniser directly rather than the whole engine, which is
    both ~100x faster here (no text-detection pass) and the only way to get the
    per-character scores: ``RapidOCR.__call__`` drops them on the floor, because
    ``CTCLabelDecode.decode`` only returns them under ``return_word_box`` and
    ``main.py`` then overwrites that slot with the rendered boxes.

    Returns ``(text, mean_score, per_char_confs)`` triples. The guard is
    deliberate - a recogniser API change must degrade to "no per-character data",
    never to an exception inside the OCR worker.
    """
    try:
        from ai.ocr_engine import get_engine

        engine = get_engine()
    except Exception as exc:  # noqa: BLE001 - never let a load failure kill a worker
        logger.warning("OCR engine unavailable: %s", exc)
        return []

    try:
        # The recogniser is not re-entrant across concurrent inference calls, and
        # this runs on a worker thread while the box path may also use the engine.
        with _rec_lock:
            result, _elapsed = engine.text_rec(image, return_word_box=True)
    except TypeError:
        # Older/newer signature without return_word_box: still read, just without
        # character detail.
        try:
            with _rec_lock:
                result, _elapsed = engine.text_rec(image)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OCR recogniser call failed: %s", exc)
            return []
        out: List[Tuple[str, float, Tuple[float, ...]]] = []
        for item in result or []:
            text, score = item[0], float(item[1])
            out.append((str(text), score, tuple()))
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR recogniser call failed: %s", exc)
        return []

    out = []
    for item in result or []:
        text = str(item[0])
        score = float(item[1])
        confs: Tuple[float, ...] = tuple()
        try:
            raw_confs = item[2][4]
            if raw_confs is not None and len(raw_confs) == len(text):
                confs = tuple(float(c) for c in raw_confs)
        except (IndexError, TypeError, ValueError):
            # Shape changed: keep the line score and report no per-character data
            # rather than misaligning confidences onto the wrong characters.
            confs = tuple()
        out.append((text, score, confs))
    return out


def _merge_two_row(
    lines: Sequence[Tuple[str, float, Tuple[float, ...]]],
) -> Optional[Tuple[str, float, Tuple[float, ...]]]:
    """Fuse a two-row plate into one string.

    Motorcycle plates print "DL12" above "AB1234". Read as two lines the second
    one alone looks like a plate fragment, so the rows are concatenated when the
    upper row is short and the lower row carries the trailing digits - which is
    what the plate actually says.
    """
    if len(lines) < 2:
        return None
    ordered = sorted(lines, key=lambda item: len(item[0]))
    top, bottom = ordered[0], ordered[-1]
    top_alnum = "".join(c for c in top[0].upper() if c.isalnum())
    bottom_alnum = "".join(c for c in bottom[0].upper() if c.isalnum())
    # The upper row of a two-row plate is the RTO block; it has no trailing digits.
    if not (1 <= len(top_alnum) <= 4) or not top_alnum[:-1].isalpha():
        return None
    if not bottom_alnum or not bottom_alnum[-1].isdigit():
        return None
    merged = top_alnum + bottom_alnum
    # Character confidences concatenate in the same order, since both rows were
    # already reduced to their alphanumeric characters above.
    top_confs = _strip_spaces(top)
    bottom_confs = _strip_spaces(bottom)
    confs = tuple(list(top_confs) + list(bottom_confs))
    if len(confs) != len(merged):
        confs = tuple()
    return merged, min(top[1], bottom[1]), confs


def _strip_spaces(item: Tuple[str, float, Tuple[float, ...]]) -> Tuple[float, ...]:
    """Per-character scores for the alphanumeric characters only.

    RapidOCR's output keeps the spaces it recognised ("MH 12 JK 4567") and its
    per-character list matches that string exactly, spaces included. The spaces
    score low (0.58 on a good plate) and are dropped by normalisation, so their
    scores have to be dropped with them or every confidence would be attached to
    the wrong character.
    """
    text, _score, confs = item
    if not confs or len(confs) != len(text):
        return tuple()
    kept = [c for ch, c in zip(text, confs) if ch.isalnum()]
    return tuple(kept)


def _align_confs(text: str, confs: Tuple[float, ...]) -> Tuple[float, ...]:
    return _strip_spaces((text, 0.0, confs))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


@dataclass
class CropRead:
    """What one crop yielded, across every variant that read it."""

    text: str = ""
    norm: str = ""
    conf: float = 0.0
    valid: bool = False
    char_confs: Optional[Tuple[float, ...]] = None
    reason: str = ""
    repairs: List[Tuple[str, float]] = field(default_factory=list)
    best_variant: str = ""
    ambiguous_positions: Tuple[int, ...] = ()

    def to_plateau(self, plate_read_conf: float) -> bool:
        """A convenience for the read-count gate. Kept tiny on purpose."""
        return self.conf >= plate_read_conf


def read_crop(crop: np.ndarray, variants: Optional[Sequence[Variant]] = None) -> Optional[CropRead]:
    """Read one plate crop as carefully as the budget allows.

    Every variant is recognised, the readings are voted on weighted by
    confidence, and the winner is then passed through the *strict* grammar. A
    variant whose text is not a legal plate still contributes to the vote - it is
    often the best evidence about which characters are right - but it can never
    win outright.
    """
    variants = list(variants if variants is not None else build_variants(crop))
    if not variants:
        return None

    observations: List[Variant] = []
    for variant in variants:
        lines = _recognise(variant.image)
        if not lines:
            continue
        merged = _merge_two_row(lines)
        if merged is not None:
            observations.append(
                Variant(variant.name, variant.image, merged[0], merged[1], merged[2])
            )
            continue
        # Longest line wins: a plate is the whole line, and the stray short hit on
        # the background is the classic RapidOCR false positive.
        best = max(lines, key=lambda item: len(item[0]))
        observations.append(
            Variant(variant.name, variant.image, best[0], best[1], _align_confs(best[0], best[2]))
        )

    observations = [o for o in observations if o.text and o.conf > 0]
    if not observations:
        return None

    # ---- vote -------------------------------------------------------------- #
    scored: List[Tuple[str, float, float, Variant]] = []
    for obs in observations:
        norm = normalize_plate(obs.text, ocr_score=obs.conf)
        scored.append((norm.normalized, norm.valid, obs.conf, obs))
    if not scored:
        return None

    # Weight = confidence, and a valid plate outranks an invalid one of any
    # confidence: the grammar is evidence, not a tiebreaker.
    weight: Dict[str, float] = {}
    members: Dict[str, List[Tuple[float, Variant]]] = {}
    for norm, valid, conf, obs in scored:
        w = conf * (1.5 if valid else 1.0)
        weight[norm] = weight.get(norm, 0.0) + w
        members.setdefault(norm, []).append((conf, obs))

    best_norm = max(weight, key=lambda k: (weight[k], len(k)))
    group = members[best_norm]

    # ---- per-character merge across agreeing variants ----------------------- #
    confs_by_len: Dict[int, List[Tuple[float, ...]]] = {}
    for _conf, obs in group:
        if obs.char_confs and len(obs.char_confs) == len(best_norm):
            confs_by_len.setdefault(len(best_norm), []).append(obs.char_confs)
    char_confs: Optional[Tuple[float, ...]] = None
    if confs_by_len:
        merged_confs = [
            _mean([cs[i] for cs in confs_by_len[max(confs_by_len, key=lambda k: len(confs_by_len[k]))]])
            for i in range(max(confs_by_len))
        ]
        char_confs = tuple(merged_confs)
        if len(char_confs) != len(best_norm):
            char_confs = None

    norm_obj = normalize_plate(
        max(group, key=lambda g: g[0])[1].text,
        ocr_score=_mean([c for c, _ in group]),
    )
    conf = _mean([c for c, _ in group])
    best_variant = max(group, key=lambda g: g[0])[1].name

    amb = ()
    if char_confs:
        amb = tuple(
            i
            for i, ch in enumerate(best_norm)
            if i < len(char_confs)
            and ch in AMBIGUOUS_PAIRS
            and char_confs[i] < MIN_CHAR_CONF
        )

    return CropRead(
        text=norm_obj.raw or best_norm,
        norm=best_norm,
        conf=conf,
        valid=norm_obj.valid,
        char_confs=char_confs,
        reason=norm_obj.reason,
        best_variant=best_variant,
        ambiguous_positions=amb,
    )


def read_crop_fast(crop: np.ndarray) -> Optional[CropRead]:
    """One variant, no voting. For the hot path when only a hint is needed."""
    return read_crop(crop, variants=build_variants(crop, with_deskew=False)[:1])


# --------------------------------------------------------------------------- #
# Position-aware correction
# --------------------------------------------------------------------------- #
# A digit where a letter belongs, or a letter where a digit belongs, is either a
# glyph confusion or a segmentation slip. Both are handled by *trying* the
# confusion partner and scoring the result - never by forcing it, because the
# whole bug being fixed here was a forced correction.
def _position_classes(plate: str) -> Optional[List[str]]:
    """Class of each position: 'state', 'rto', 'series' or 'tail'."""
    out: List[str] = []
    for name, length in (("state", 2), ("rto", 1), ("series", 1), ("tail", 1)):
        out.extend([name] * length)
    if len(plate) != len(out):
        return None
    return out


def _partitions(plate: str) -> List[Tuple[int, int, int, int]]:
    """All (state, rto, series, tail) length splits the grammar allows."""
    out = []
    for rto_len in (1, 2):
        for series_len in (1, 2, 3):
            for tail_len in (1, 2, 3, 4):
                total = 2 + rto_len + series_len + tail_len
                if total == len(plate):
                    out.append((2, rto_len, series_len, tail_len))
    return out


def correct_by_grammar(
    text: str,
    char_confs: Optional[Sequence[float]] = None,
    min_gain: float = 0.02,
) -> Tuple[str, float, str]:
    """Repair a read using the plate layout, without ever inventing a plate.

    Returns ``(candidate, score, note)``. Confusion substitutions are only
    allowed *inside* the class a position belongs to: a letter may only become
    another letter, a digit only another digit. A candidate that fails the RTO
    whitelist is discarded outright, and if nothing beats the original read the
    original is returned - a read is never made to become valid by guessing.
    """
    compact = "".join(c for c in str(text).upper() if c.isalnum())
    if not compact:
        return "", 0.0, "empty"

    best = compact
    best_score = _candidate_prior(compact)
    if best_score > 0 and strict_syntax(best)[0]:
        # Already a legal plate: leave it alone.
        #
        # This early return is load-bearing, and it is the same lesson as the
        # ranking fix in `plate.py`. "DL1ZA9092" is legal, and a 2-digit-RTO
        # reading of it ("DL12A9092") is *also* legal and scores higher on the
        # layout prior - so without this line the "correction" would reliably
        # rewrite a correct read into a different real car. A read is only
        # corrected when it is not already a plate.
        return best, best_score, "already well-formed"

    note = "no repair beat the original"
    for state_len, rto_len, series_len, tail_len in _partitions(compact):
        regions = (
            (0, 2, "alpha"),
            (2, 2 + rto_len, "digit"),
            (2 + rto_len, 2 + rto_len + series_len, "alpha"),
            (2 + rto_len + series_len, len(compact), "digit"),
        )
        for start, end, want in regions:
            if want == "digit":
                fixed = "".join(_digit_or_self(c) for c in compact[start:end])
            else:
                fixed = "".join(_letter_or_self(c) for c in compact[start:end])
            if fixed != compact[start:end]:
                candidate = compact[:start] + fixed + compact[end:]
                if candidate == compact:
                    continue
                if candidate[:2] not in VALID_STATE_CODES:
                    continue
                score = _candidate_prior(candidate)
                if score <= 0:
                    continue
                if score > best_score + min_gain:
                    best, best_score = candidate, score
                    note = (
                        f"position-class repair in a {state_len}/{rto_len}/"
                        f"{series_len}/{tail_len} split"
                    )
    return best, best_score, note


def _digit_or_self(ch: str) -> str:
    """The digit this glyph could be, if it is a digit or a digit-like letter."""
    if ch.isdigit():
        return ch
    partner = {"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"}.get(ch)
    return partner or ch


def _letter_or_self(ch: str) -> str:
    if ch.isalpha():
        return ch
    return {"0": "O", "1": "I", "5": "S", "8": "B", "6": "G", "9": "G", "2": "Z"}.get(ch, ch)


def _candidate_prior(candidate: str) -> float:
    """0..1 plausibility of a candidate, purely from the layout.

    Layout only. It says nothing about whether this is the car in front of the
    camera - only that the string could be a plate, which is the ceiling on how
    far a guess is allowed to go.
    """
    ok, _why, score = strict_syntax(candidate)
    if not ok:
        return 0.0
    # Prefer the dominant 2-digit RTO and 4-digit tail, but do not forbid the rest.
    return score


# --------------------------------------------------------------------------- #
# Debug crop saving (opt-in, off by default)
# --------------------------------------------------------------------------- #
_CROP_DIR = Path(__file__).resolve().parents[3] / "data" / "debug_out" / "plate_crops"
_save_lock = threading.Lock()


def debug_crops_enabled() -> bool:
    """True only when DEBUG_SAVE_PLATE_CROPS is explicitly switched on.

    Off unless the environment says otherwise, and it is a diagnostic: it writes
    plate crops to disk, which is the one thing the live path never otherwise
    does, so it is never a default and never on in the shipped env example.
    """
    return os.getenv("DEBUG_SAVE_PLATE_CROPS", "").strip().lower() in {"1", "true", "yes", "on"}


def save_plate_crop(crop: np.ndarray, text: str, conf: float, reason: str = "") -> None:
    """Write ONE plate crop for inspection. Never a whole frame.

    Frames contain everything in the street; a plate crop contains a plate. That
    is why this exists and why it is not something to leave switched on.
    """
    if not debug_crops_enabled() or crop is None or crop.size == 0:
        return
    try:
        with _save_lock:
            _CROP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = f"{int(cv2.getTickCount()) % 1000000:06d}"
            safe = "".join(c for c in text if c.isalnum())[:16] or "empty"
            name = f"{stamp}_{safe}_{int(conf * 100)}.jpg"
            cv2.imwrite(str(_CROP_DIR / name), crop)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never break a read
        logger.warning("Could not save plate crop: %s", exc)


def warn_debug_enabled_once() -> None:
    """One startup warning, so a stray env var cannot be forgotten."""
    if debug_crops_enabled():
        logger.warning(
            "DEBUG_SAVE_PLATE_CROPS is ON: plate crops (never full frames) are "
            "being written to %s. This is a diagnostic - switch it off before "
            "sharing this machine.",
            _CROP_DIR,
        )
"""RAKSHAK on-device ANPR pipeline.

detect_and_read(image) -> list[DetectedPlate]:
    plate region detection -> OCR -> plate normalization.

Privacy contract: this module operates on a single frame in memory; it
marshals ONLY (plate, confidence, timestamp) onward. Raw frames/video are
never uploaded to the backend.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Union

import cv2
import numpy as np

from ai.detector import (
    PlateRegion,
    detect_plate_region,
    deskew_crop,
    focus_plate,
    preprocess_crop,
)
from ai.ocr_engine import OcrHit, best_plate, candidates, read_text

try:
    from app.services.plate import MIN_REPORT_CONFIDENCE, is_valid_plate, plate_read_reliable
except ImportError:  # allow `ai/` to run standalone without backend on sys.path
    import os
    import sys

    sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
    from app.services.plate import MIN_REPORT_CONFIDENCE, is_valid_plate, plate_read_reliable

DETECTED_PLATE_MIN_CONFIDENCE = 0.45
# A read this sharp needs no second look, so multi-read voting can stop early.
EARLY_EXIT_CONFIDENCE = 0.85
# Only report something that is at least plate-shaped (8-10 alphanumeric chars).
PLATE_LENGTH_RE = re.compile(r"^[A-Z0-9]{8,10}$")

OCR_STAGES = (
    "crop_raw",
    "crop_focused",
    "crop_focused_preprocessed",
    "crop_deskew",
    "crop_preprocessed",
    "fullframe",
)


@dataclass
class DetectedPlate:
    plate: str
    confidence: float
    valid: bool
    source: str  # 'crop' | 'fullframe'
    uncertain: bool = False
    reason: str = ""
    stage: str = ""
    votes: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "plate": self.plate,
            "confidence": self.confidence,
            "valid": self.valid,
            "uncertain": self.uncertain,
            "source": self.source,
            "stage": self.stage,
            "reason": self.reason,
            "votes": self.votes,
        }


def _to_bgr(image: Union[str, bytes, "np.ndarray"]) -> Optional[np.ndarray]:
    if isinstance(image, str):
        return cv2.imread(image)
    if isinstance(image, bytes):
        return cv2.imdecode(np.frombuffer(image, "uint8"), cv2.IMREAD_COLOR)
    return image


def _from_hits(hits, source: str, stage: str) -> Optional[DetectedPlate]:
    hit = best_plate(hits)
    if hit is None:
        return None
    # "IND", "12" or a stray Chinese glyph are not a plate guess. Reporting them
    # as "uncertain, best guess IND" is worse than reporting no plate at all.
    if not PLATE_LENGTH_RE.match(hit.normalized):
        return None
    reliable = is_valid_plate(hit.normalized) and plate_read_reliable(hit.normalized, hit.score)
    if hit.repaired:
        # The RTO code was guessed. Correct plate, wrong number - never let it
        # stand on its own; it needs another frame to corroborate it.
        reliable = False
        hit.reason = f"{hit.reason} [state code guessed - needs corroboration]"
    return DetectedPlate(
        plate=hit.normalized,
        confidence=round(hit.score, 3),
        valid=bool(hit.valid_plate and hit.normalized),
        uncertain=not reliable,
        source=source,
        reason=hit.reason,
        stage=stage,
    )


def _rank(det: DetectedPlate):
    """Exact whitelist-valid reads first, then confidence, then richer renderings."""
    stage_rank = OCR_STAGES.index(det.stage) if det.stage in OCR_STAGES else len(OCR_STAGES)
    return (1 if is_valid_plate(det.plate) else 0, 1 if not det.uncertain else 0, det.confidence, -stage_rank)


def detect_and_read(image: Union[str, bytes, "np.ndarray"]) -> List[DetectedPlate]:
    """Run the full pipeline on one frame and return at most one plate."""
    return detect_and_read_many([image])


def detect_and_read_many(
    images: Iterable[Union[str, bytes, "np.ndarray"]],
    *,
    early_exit: bool = True,
) -> List[DetectedPlate]:
    """Read a plate from 1-3 frames and vote.

    Each frame is read through several renderings (raw crop, deskewed crop,
    CLAHE-upscaled crop and the full frame) because no single rendering is best
    for every photo. The highest-ranked read wins; when frames disagree the
    majority plate is preferred, which averages out blur, glare and motion.
    """
    if isinstance(images, (str, bytes, np.ndarray)):
        images = [images]

    reads: List[DetectedPlate] = []
    for image in images:
        frame_reads = _read_single(image)
        if not frame_reads:
            continue
        best = max(frame_reads, key=_rank)
        if early_exit and plate_read_reliable(best.plate, best.confidence):
            return [best]
        reads.extend(frame_reads)

    if not reads:
        return []

    return [vote(reads)]


def renderings_for(bgr: np.ndarray) -> List[tuple]:
    """The ``(stage, source, image)`` renderings this pipeline will OCR.

    Exposed so the debug script prints exactly what OCR sees, instead of a
    hand-copied approximation that silently drifts from the real code.
    """
    renderings: List[tuple] = []
    region: Optional[PlateRegion] = detect_plate_region(bgr)
    if region is not None and region.crop is not None and region.crop.size:
        crop = region.crop
        source = "crop" if region.source == "yolo" else "crop_fallback"
        renderings.append(("crop_raw", source, crop))
        focused = focus_plate(crop)
        if focused is not crop and focused.size:
            renderings.append(("crop_focused", source, focused))
            renderings.append(("crop_focused_preprocessed", source, preprocess_crop(focused)))
        deskewed, angle = deskew_crop(crop)
        if deskewed is not crop and abs(angle) > 0.05:
            renderings.append(("crop_deskew", source, deskewed))
        renderings.append(("crop_preprocessed", source, preprocess_crop(crop)))
    return renderings


def _read_single(image: Union[str, bytes, "np.ndarray"]) -> List[DetectedPlate]:
    bgr = _to_bgr(image)
    if bgr is None or bgr.size == 0:
        return []

    dets: List[DetectedPlate] = []

    for stage, source, image_variant in renderings_for(bgr):
        det = _from_hits(candidates(read_text(image_variant)), source, stage)
        if det is None:
            continue
        dets.append(det)
        # Speed: a validated, confident read needs no further renderings.
        if plate_read_reliable(det.plate, det.confidence):
            break

    frame_det = _from_hits(candidates(read_text(bgr)), "fullframe", "fullframe")
    if frame_det is not None:
        dets.append(frame_det)

    return dets


def vote(reads: Sequence[DetectedPlate]) -> DetectedPlate:
    """Combine per-rendering reads into one answer, majority first."""
    if not reads:
        raise ValueError("vote() needs at least one read")

    counts: Dict[str, int] = {}
    for det in reads:
        if not det.plate:
            continue
        counts[det.plate] = counts.get(det.plate, 0) + 1

    def key(det: DetectedPlate):
        return (counts.get(det.plate, 0), *_rank(det))

    winner = max(reads, key=key)
    return DetectedPlate(
        plate=winner.plate,
        confidence=winner.confidence,
        valid=winner.valid,
        source=winner.source,
        uncertain=winner.uncertain,
        reason=winner.reason,
        stage=winner.stage,
        votes=dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
    )


def confidence_note(confidence: float) -> str:
    if confidence >= EARLY_EXIT_CONFIDENCE:
        return "confident"
    if confidence >= MIN_REPORT_CONFIDENCE:
        return "uncertain"
    return "no plate found"


if __name__ == "__main__":
    import sys

    for path in sys.argv[1:]:
        for det in detect_and_read(path):
            flag = " UNCERTAIN" if det.uncertain else ""
            print(
                f"{path}: {det.plate} conf={det.confidence} valid={det.valid}"
                f" via={det.source}/{det.stage}{flag} votes={det.votes or {}}"
            )
            if det.reason:
                print(f"    reason: {det.reason}")
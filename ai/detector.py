"""YOLOv8-based plate detector with a classic OpenCV fallback.

The project includes a trained model at ``backend/models/license-plate-finetune-v1n.pt``.
This module resolves that artifact, loads it once, and returns the most confident plate crop.
If the model is unavailable or inference fails, it falls back to the legacy contour logic so
pre-existing OCR flows continue to work.

Every call to :func:`detect_plate_region` appends a human readable line to
:data:`DETECTOR_EVENTS` so the debug tooling can prove *which* detector produced
the crop (a silent fallback to contours is a common cause of misreads).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

try:
    from ultralytics import YOLO
except ModuleNotFoundError:  # pragma: no cover - handled when runtime dependency is missing
    YOLO = None

MIN_ASPECT, MAX_ASPECT = 1.6, 6.5
MIN_AREA_PCT = 0.002
MODEL_NAME = "license-plate-finetune-v1n.pt"
MIN_CONF = 0.25

# Padding around the detector box. Indian plates are often boxed tight around
# the glyphs; a 1-2% clip turns "MH" into "H" / "HL", so keep real margins.
PAD_X_RATIO = 0.10
PAD_Y_RATIO = 0.18
PAD_X_MIN = 8
PAD_Y_MIN = 10

# RapidOCR's detector input limit; crops larger than this get shrunk anyway, and
# feeding it the oversized crop only loses detail.
DET_MAX_SIDE = 736
# RapidOCR recognises plate text best when the glyphs are roughly this tall.
TARGET_HEIGHT_MIN, TARGET_HEIGHT_MAX = 64, 100

# Rolling diagnostics for scripts/debug_plate.py.
DETECTOR_EVENTS: List[str] = []


def _log(message: str) -> None:
    DETECTOR_EVENTS.append(message)
    if len(DETECTOR_EVENTS) > 200:
        del DETECTOR_EVENTS[:-100]


def reset_events() -> None:
    DETECTOR_EVENTS.clear()


def resolve_model_path() -> str:
    """Return the repo-local trained plate detector path."""
    root = Path(__file__).resolve().parents[1]
    candidates = [
        root / "backend" / "models" / MODEL_NAME,
        root / "models" / MODEL_NAME,
        root / "backend" / ".cache" / MODEL_NAME,
        root / ".cache" / MODEL_NAME,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return str(candidates[0])


@lru_cache(maxsize=1)
def _load_plate_detector():
    if YOLO is None:
        raise ModuleNotFoundError("ultralytics is required for the trained plate detector. Install it with pip install ultralytics.")
    model_path = resolve_model_path()
    return YOLO(model_path)


@dataclass
class PlateRegion:
    """A located plate crop plus enough metadata to debug the read."""

    crop: np.ndarray
    source: str  # 'yolo' | 'contour'
    box: Tuple[int, int, int, int]  # padded crop box (x1, y1, x2, y2) in frame coords
    raw_box: Tuple[int, int, int, int]  # detector box before padding
    confidence: float
    notes: List[str] = field(default_factory=list)


def _pad_box(box: Tuple[int, int, int, int], width: int, height: int) -> Tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    pad_x = int(max(PAD_X_RATIO * (x2 - x1), PAD_X_MIN))
    pad_y = int(max(PAD_Y_RATIO * (y2 - y1), PAD_Y_MIN))
    return (
        max(0, x1 - pad_x),
        max(0, y1 - pad_y),
        min(width, x2 + pad_x),
        min(height, y2 + pad_y),
    )


def _crop_from_box(bgr: np.ndarray, box: Tuple[int, int, int, int]) -> Tuple[np.ndarray, Tuple[int, int, int, int]]:
    height, width = bgr.shape[:2]
    padded = _pad_box(box, width, height)
    x1, y1, x2, y2 = padded
    return bgr[y1:y2, x1:x2], padded


def _yolo_regions(bgr: np.ndarray) -> List[PlateRegion]:
    """Run the trained detector; raise on any failure so the caller can log it."""
    model = _load_plate_detector()
    results = model(bgr, conf=MIN_CONF, verbose=False, imgsz=640)
    # NB: `results` is a *list* of per-image Results, so the boxes live on
    # results[0]. Checking getattr(results, "boxes") always returned None and
    # silently routed every frame through the contour fallback.
    if not results or len(results) == 0:
        _log("yolo: no results returned")
        return []
    boxes = getattr(results[0], "boxes", None)
    if boxes is None:
        _log("yolo: no boxes attribute on results[0]")
        return []

    regions: List[PlateRegion] = []
    for box in boxes:
        try:
            coords = box.xyxy[0].detach().cpu().numpy().astype(int)
            conf = float(box.conf[0].detach().cpu().item())
        except AttributeError:
            coords = box.xyxy[0].cpu().numpy().astype(int)
            conf = float(box.conf[0].item())
        except Exception as exc:  # pragma: no cover - defensive
            _log(f"yolo: unreadable box skipped ({exc})")
            continue

        x1, y1, x2, y2 = (int(v) for v in coords[:4])
        if x2 <= x1 or y2 <= y1 or conf < MIN_CONF:
            continue
        crop, padded = _crop_from_box(bgr, (x1, y1, x2, y2))
        if crop.size == 0:
            continue
        regions.append(
            PlateRegion(
                crop=crop,
                source="yolo",
                box=padded,
                raw_box=(x1, y1, x2, y2),
                confidence=conf,
            )
        )

    regions.sort(key=lambda r: r.confidence, reverse=True)
    _log(f"yolo: {len(regions)} box(es) >= conf {MIN_CONF}")
    return regions


def _legacy_detect_plate_crop(bgr: np.ndarray) -> Optional[np.ndarray]:
    """Fallback contour-based detector retained for resilience."""
    for region in _legacy_regions(bgr):
        return region.crop
    return None


def _legacy_regions(bgr: np.ndarray) -> List[PlateRegion]:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(blur, 40, 140)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 3))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(closed, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        _log("contour: no contours found")
        return []

    h, w = bgr.shape[:2]
    frame_area = float(h * w)

    best: Optional[PlateRegion] = None
    best_score = 0.0
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        area = cw * ch
        if area < MIN_AREA_PCT * frame_area or area > 0.5 * frame_area:
            continue
        if ch == 0:
            continue
        aspect = cw / ch
        if not (MIN_ASPECT <= aspect <= MAX_ASPECT):
            continue
        score = area
        if score > best_score:
            best_score = score
            crop, padded = _crop_from_box(bgr, (x, y, x + cw, y + ch))
            best = PlateRegion(
                crop=crop,
                source="contour",
                box=padded,
                raw_box=(x, y, x + cw, y + ch),
                confidence=float("nan"),
            )
    if best is not None:
        _log(f"contour: best box={best.raw_box} aspect={best.raw_box[2] / max(best.raw_box[3] - best.raw_box[1], 1):.2f}")
    return [best] if best is not None else []


def detect_plate_region(bgr: np.ndarray) -> Optional[PlateRegion]:
    """Locate the plate region, preferring the trained detector.

    Returns ``None`` when nothing plausible is found. The returned
    :class:`PlateRegion` always records which detector produced it.
    """
    if bgr is None or not isinstance(bgr, np.ndarray) or bgr.size == 0:
        _log("detect: empty image")
        return None

    try:
        regions = _yolo_regions(bgr)
        if regions:
            best = regions[0]
            _log(
                f"detect: YOLO box={best.raw_box} conf={best.confidence:.3f} "
                f"padded={best.box} crop={best.crop.shape[1]}x{best.crop.shape[0]}"
            )
            return best
        _log("detect: YOLO found nothing - falling back to contours")
    except Exception as exc:
        _log(f"detect: YOLO FAILED ({type(exc).__name__}: {exc}) - falling back to contours")

    legacy = _legacy_regions(bgr)
    if legacy:
        best = legacy[0]
        _log(f"detect: CONTOUR fallback used box={best.raw_box} padded={best.box}")
        return best
    return None


def detect_plate_crop(bgr: np.ndarray) -> Optional[np.ndarray]:
    """Return a padded BGR plate crop, or None if none is confidently found."""
    region = detect_plate_region(bgr)
    return None if region is None else region.crop


def _estimate_text_skew(gray: np.ndarray, max_angle: float) -> Tuple[float, float, float]:
    """Estimate plate skew from the horizontal projection profile.

    Rotating the binarised text by a candidate angle and summing the ink per
    row produces the sharpest profile when the baseline is horizontal. This is
    the standard skew estimator for text lines, and unlike the contour method
    it does not need the plate outline to surround the crop.
    """
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    height, width = binary.shape[:2]
    if height < 8 or width < 8:
        return 0.0, 0.0, 0.0

    center = (width / 2.0, height / 2.0)
    best_angle, best_score, flat_score = 0.0, -1.0, 0.0
    for angle in np.arange(-max_angle, max_angle + 0.01, 0.5):
        matrix = cv2.getRotationMatrix2D(center, float(angle), 1.0)
        warped = cv2.warpAffine(
            binary,
            matrix,
            (width, height),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        profile = warped.sum(axis=1, dtype=np.float64)
        # Neighbouring-row energy: a flat baseline concentrates ink on few rows.
        score = float(np.square(np.diff(profile)).sum())
        if abs(angle) < 1e-6:
            flat_score = score
        if score > best_score:
            best_score, best_angle = score, float(angle)

    return best_angle, best_score, flat_score


def deskew_crop(crop: np.ndarray, max_angle: float = 12.0) -> Tuple[np.ndarray, float]:
    """Straighten a skewed plate crop.

    Prefers the dominant plate contour; when that is unavailable (a tight crop
    has no surrounding outline) it falls back to the text baseline, so a
    hand-held frame tilted by ~10 degrees still reads instead of arriving at OCR
    with every character on a different row.

    Returns ``(image, angle_degrees_applied)``; the input is returned untouched
    when neither method finds confident skew.
    """
    if crop is None or crop.size == 0:
        return crop, 0.0

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    estimate = _contour_skew(gray, crop, max_angle)
    if estimate is None:
        angle, best_score, flat_score = _estimate_text_skew(gray, max_angle)
        # Ignore the estimate unless it beats a perfectly level image clearly:
        # on an already-straight plate this is all noise.
        if abs(angle) < 0.5 or best_score <= flat_score * 1.15:
            return crop, 0.0
        _log(f"deskew: baseline estimate {angle:.2f} deg (contour unavailable)")
    else:
        angle = estimate

    height, width = crop.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), angle, 1.0)
    rotated = cv2.warpAffine(
        crop,
        matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    _log(f"deskew: rotated {angle:.2f} deg")
    return rotated, float(angle)


def _contour_skew(gray: np.ndarray, crop: np.ndarray, max_angle: float) -> Optional[float]:
    """Skew from the plate outline, or None when it is not usable."""
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    thresh = cv2.morphologyEx(
        thresh,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (15, 5)),
    )
    contours, _ = cv2.findContours(thresh, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    frame_area = float(crop.shape[0] * crop.shape[1])
    best = max(contours, key=cv2.contourArea)
    if cv2.contourArea(best) < 0.20 * frame_area:
        return None

    rect = cv2.minAreaRect(best)
    (_, _), (rect_w, rect_h), angle = rect
    if rect_w < 1 or rect_h < 1:
        return None
    # minAreaRect reports [-90, 0); pick the orientation closest to horizontal.
    if angle < -45:
        angle += 90
    if abs(angle) > max_angle:
        return None
    return float(angle)
    _log(f"deskew: rotated {angle:.2f} deg")
    return rotated, float(angle)


def _scale_for_ocr(image: np.ndarray) -> np.ndarray:
    """Size a plate image the way RapidOCR wants it.

    RapidOCR's detector shrinks anything with a side over ~736px anyway, so a
    big crop is fed as-is and only genuinely small plates are enlarged. The old
    "shrink the plate to 100px tall" step was destroying the glyphs it was meant
    to help - that is one way "MH" lost its H.
    """
    height, width = image.shape[:2]
    longest = max(height, width)

    if height < TARGET_HEIGHT_MIN:  # too small to recognise: enlarge
        factor = min(TARGET_HEIGHT_MIN / max(height, 1), 4.0)
        return cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)

    if longest > DET_MAX_SIDE:  # matches RapidOCR's own input limit
        factor = DET_MAX_SIDE / longest
        return cv2.resize(
            image,
            (max(1, int(width * factor)), max(1, int(height * factor))),
            interpolation=cv2.INTER_AREA,
        )
    return image


def focus_plate(crop: np.ndarray, pad_ratio: float = 0.04) -> np.ndarray:
    """Crop a detected crop down to the plate itself, dropping the background.

    The detector's padded box deliberately overshoots so a leading "M" cannot be
    clipped, which leaves plenty of car bodywork around the text. RapidOCR does
    better when the plate fills the frame, so this second pass trims the excess
    while keeping a small safety margin.
    """
    if crop is None or crop.size == 0:
        return crop
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 40, 140)
    closed = cv2.morphologyEx(
        edges,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (25, 7)),
    )
    contours, _ = cv2.findContours(closed, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return crop

    frame_area = float(crop.shape[0] * crop.shape[1])
    best, best_area = None, 0.0
    for cnt in contours:
        area = cv2.contourArea(cnt)
        x, y, w, h = cv2.boundingRect(cnt)
        if h == 0 or not (MIN_ASPECT <= w / h <= MAX_ASPECT):
            continue
        if area < 0.25 * frame_area or area > 0.99 * frame_area:
            continue
        if area > best_area:
            best_area, best = area, (x, y, w, h)
    if best is None:
        return crop

    x, y, w, h = best
    pad_x, pad_y = int(pad_ratio * w), int(pad_ratio * h)
    height, width = crop.shape[:2]
    focused = crop[max(0, y - pad_y) : min(height, y + h + pad_y), max(0, x - pad_x) : min(width, x + w + pad_x)]
    return focused if focused.size else crop


def preprocess_crop(crop: np.ndarray) -> np.ndarray:
    """Upscale + local-contrast-normalize a crop for better OCR accuracy.

    Deliberately **no hard binarization**: an adaptive threshold destroys the
    diagonal strokes of "M"/"N"/"W", which is how "MH" turns into "HL".
    """
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    gray = cv2.bilateralFilter(gray, 5, 40, 40)
    return cv2.cvtColor(_scale_for_ocr(gray), cv2.COLOR_GRAY2BGR)
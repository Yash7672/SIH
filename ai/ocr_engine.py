"""RapidOCR wrapper: single shared engine instance + plate-aware filtering."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple, Union

import numpy as np

try:
    from app.services.plate import is_valid_plate, normalize_plate
except ImportError:  # allow `ai/` to run standalone without backend package on path
    import os
    import sys

    sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
    from app.services.plate import is_valid_plate, normalize_plate

_engine = None
_engine_lock = threading.Lock()
_log = logging.getLogger("rakshak.ocr")

# Fragments produced by the blue "IND" band on the left of BH/HSRP plates. Merged
# in, they prepend junk to every candidate ("INDMH12AB1234") and can even push a
# real state code out of the first two slots.
STRIP_ARTIFACTS = frozenset({"IND", "INDIA", "IN", "ND", "I", "L", "|", "1", "II", "LL", "1ND"})
# Only a *narrow* fragment is treated as noise: a wide one is probably a real
# chunk of the plate that just happens to look like a letter.
STRIP_ARTIFACT_MAX_WIDTH_RATIO = 0.18


@dataclass
class OcrHit:
    text: str
    normalized: str
    valid_plate: bool
    score: float
    box: Optional[list]
    reason: str = ""
    state_code_valid: bool = False
    repaired: bool = False
    stage: str = ""


def get_engine():
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from rapidocr_onnxruntime import RapidOCR

                _engine = RapidOCR()
    return _engine


def read_text(image: Union[str, np.ndarray]) -> List[OcrHit]:
    """Run OCR on an image path or BGR ndarray; return hits with plate validity."""
    engine = get_engine()
    result, _ = engine(image)
    hits: List[OcrHit] = []
    if not result:
        return hits
    for item in result:
        box, text, score = item[0], item[1], float(item[2])
        norm = normalize_plate(text, ocr_score=score)
        hits.append(
            OcrHit(
                text=str(text).strip(),
                normalized=norm.normalized,
                valid_plate=norm.valid,
                score=score,
                box=box,
                reason=norm.reason,
                state_code_valid=norm.state_code_valid,
                repaired=norm.repaired,
            )
        )
    return hits


def best_plate(hits: List[OcrHit]) -> Optional[OcrHit]:
    """Pick the highest-scoring valid plate.

    A hit whose state code is not a real RTO never wins against one that is, no
    matter how confident the bad hit is - that is how "HL" beat "MH".
    """
    if not hits:
        return None
    valid = [h for h in hits if h.valid_plate]
    if valid:
        return max(valid, key=lambda h: (is_valid_plate(h.normalized), h.score))
    return max(hits, key=lambda h: h.score)


def _box_geometry(box) -> Tuple:
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return min(xs), max(xs), min(ys), max(ys)


def _width(geom) -> float:
    return max(geom[1] - geom[0], 1.0)


def is_strip_artifact(hit: OcrHit, line_width: float) -> bool:
    """True for a narrow left-edge blue-strip fragment ("IND", "I", "L", "|")."""
    text = "".join(ch for ch in str(hit.text).upper() if ch.isalnum())
    if text in STRIP_ARTIFACTS:
        return _width(_box_geometry(hit.box)) <= STRIP_ARTIFACT_MAX_WIDTH_RATIO * line_width
    return "IND" in text.upper()


def drop_strip_artifacts(hits: List[OcrHit]) -> List[OcrHit]:
    """Remove left-edge blue-strip fragments before anything is merged."""
    if not hits:
        return []
    geoms = [_box_geometry(h.box) for h in hits if h.box]
    if not geoms:
        return list(hits)
    line_width = max(g[1] for g in geoms) - min(g[0] for g in geoms)

    ordered = sorted(
        (h for h in hits if h.box),
        key=lambda h: _box_geometry(h.box)[0],
    )
    kept = [h for h in ordered if not (h is ordered[0] and is_strip_artifact(h, line_width))]
    dropped = len(ordered) - len(kept)
    if dropped:
        _log.info("dropped %d left-edge blue-strip fragment(s): %s", dropped, [h.text for h in ordered[:dropped]])
    return kept or list(hits)


def merge_line_hits(hits: List[OcrHit], y_tol_ratio: float = 0.55) -> List[OcrHit]:
    """Merge OCR hits that sit on the same text line into one candidate plate.

    RapidOCR often splits a plate into per-character regions; this fuses them
    left-to-right (ordered by box x-coordinate) so ``TS09AB1234`` becomes a single
    candidate instead of ``TS`` ``09`` ``AB`` ``1234`` fragments.
    """
    usable = [h for h in hits if h.box]
    if not usable:
        return []
    usable = drop_strip_artifacts(usable)

    with_geom = [(h, *_box_geometry(h.box)) for h in usable]

    remaining = sorted(with_geom, key=lambda g: g[3])  # by y0
    lines: List[List] = []
    for item in remaining:
        h, x0, x1, y0, y1 = item
        cy = (y0 + y1) / 2.0
        height = max(y1 - y0, 1)
        placed = False
        for line in lines:
            lh = line[0]
            _, _, _, ly0, ly1 = lh
            lcy = (ly0 + ly1) / 2.0
            lh_h = max(ly1 - ly0, 1)
            if abs(cy - lcy) <= y_tol_ratio * max(height, lh_h):
                line.append(item)
                placed = True
                break
        if not placed:
            lines.append([item])

    merged: List[OcrHit] = []
    for line in lines:
        line.sort(key=lambda g: g[1])  # by x0: the state code must stay first
        base = _render_line(line)
        merged.append(base)
        # If the full merge is not a valid plate, retry dropping one fragment
        # (handles stray doubled characters from noisy OCR). The leftmost fragment
        # is never dropped: that is the state code, and losing it is what turns a
        # correct "MH12AB1234" into an unreadable "H12AB1234".
        if not base.valid_plate and len(line) > 2:
            for drop_i in range(1, len(line)):
                variant = _render_line([g for j, g in enumerate(line) if j != drop_i])
                if variant.valid_plate:
                    variant.reason = f"{variant.reason} (dropped fragment {drop_i} {line[drop_i][0].text!r})"
                    merged.append(variant)
                    break
    return merged


def _render_line(line: Sequence[Tuple]) -> OcrHit:
    """Build one OcrHit from a sorted ``(hit, x0, x1, y0, y1)`` line cluster."""
    text = "".join(g[0].text for g in line)
    scores = [g[0].score for g in line]
    score = min(scores)
    norm = normalize_plate(text, ocr_score=score)
    xs = [g[1] for g in line] + [g[2] for g in line]
    ys = [g[3] for g in line] + [g[4] for g in line]
    box = [[min(xs), min(ys)], [max(xs), min(ys)], [max(xs), max(ys)], [min(xs), max(ys)]]
    return OcrHit(
        text=text,
        normalized=norm.normalized,
        valid_plate=norm.valid,
        score=score,
        box=box,
        reason=norm.reason,
        state_code_valid=norm.state_code_valid,
        repaired=norm.repaired,
    )


def candidates(hits: List[OcrHit]) -> List[OcrHit]:
    """Raw hits + line-merged combinations, deduped by normalized text."""
    raw = [h for h in hits]
    merged = merge_line_hits(hits)
    seen = set()
    out = []
    for h in raw + merged:
        key = (h.normalized, h.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out
"""Split a plate crop into one crop per character.

Why this module exists
----------------------
A single RapidOCR pass over a plate returns a *string* and, at best, a score
for the whole string. That is not enough for the decision this project has to
make: ``DL1ZA9092`` and ``DL12AG092`` are both legal plates, so a misread ``Z``
does not produce a string the validator can throw away - it produces a
different, also-plausible car. The only way to catch that is to look at the
characters one at a time and ask "how sure are you about *this glyph*?".

So the plate is cut into glyphs here, and each glyph is scored independently by
``ai.char_verifier``.

The properties that matter
--------------------------
* **Honesty about the count.** If the crop cannot be cut into exactly as many
  believable glyphs as the OCR string has characters, this returns ``[]``. The
  caller then treats the read as *unverified* and falls back to requiring
  consensus between frames. A confidently wrong split is far worse than no
  split: it would let a mis-segmentation masquerade as a verified read and
  override a good OCR string.
* **Scale-invariant.** A plate crop at the detector is a few dozen pixels tall
  with a one-pixel rim; the same plate in ``data/generated`` is 200 px tall
  with a four-pixel rim. Every threshold here is a fraction of the panel, and
  the rim is separated from the glyphs by an *erosion sized to the panel*
  rather than by a fixed kernel.
* **Chrome is rejected on shape, not position.** A photographed plate carries a
  raised rim, an ``IND`` band, a hologram sticker and mounting bolts, and all of
  them are ink. Each is removed by comparing it against the run of components
  (height outliers, filled blocks, things hugging a crop edge), never by a
  magic crop offset - because where they land depends on how the plate was
  framed.
* **No evenly-spaced assumption.** Indian plates are ``DL 8C BF 4890``: wider
  gaps after the state code and after the series block. Equal-width slicing
  crops the wrong pixels on every one of them.

Only the plate crop is ever touched. Nothing here writes to disk, opens a file
or looks at a frame - see the privacy note in ``ai/__init__.py``.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

import numpy as np

try:  # pragma: no cover - OpenCV is a hard dependency in practice, but this
    import cv2  # noqa: E402
except Exception:  # pragma: no cover  module must still import without it
    cv2 = None  # type: ignore[assignment]

_log = logging.getLogger("rakshak.char_segment")

# ---- erosion ladder: fractions of panel height tried, in order --------------
# The rim is thin and the glyph strokes are fat, so an erosion that deletes the
# rim keeps the strokes. Each level is tried until one gives the expected number
# of glyphs, so a plate photographed small is not thrown away because a fixed
# 3-pixel kernel was too aggressive (or too timid).
ERODE_FRACS = (0.0, 0.018, 0.030, 0.045)
MIN_STROKE_H_FRAC = 0.35  # below this height a blob is a speck, not a glyph

# ---- chrome rejection, relative to the run of components -------------------
H_OUTLIER_LO = 0.55
H_OUTLIER_HI = 1.35
W_OUTLIER_LO = 0.45
MAX_BLOCK_FILL = 0.80  # a sticker is a filled block; a glyph is strokes
MIN_BLOCK_W_FRAC = 1.25
MIN_EDGE_W_FRAC = 0.70  # narrow + pressed against the crop edge = IND band

# ---- glyph acceptance, relative to the character band ----------------------
MIN_GLYPH_H_FRAC = 0.42
MAX_GLYPH_H_FRAC = 1.05
MIN_RUN_W_FRAC = 0.35  # tightest / median width across the run
MAX_RUN_W_FRAC = 2.20  # widest / median width across the run

# ---- segmentation-failure gates --------------------------------------------
MIN_SEG_INK_FRAC = 0.15  # vs the median segment's ink: an empty band fails
MIN_SEG_W_FRAC = 0.40
MAX_SEG_W_FRAC = 2.20
MAX_DP_CANDIDATES = 48  # keeps the fallback dynamic program tiny

# ---- "is this crop a plate panel at all?" ----------------------------------
# `_ink_mask` works by finding the background and calling whatever is far from
# it ink, which is only meaningful when the characters are a minority of the
# crop - the assumption the module is built on. A detector crop is the plate box
# plus a margin, and that margin is whatever sits behind the plate: on a dark car
# that is dark paint, which is as dark as the glyphs, so *no* brightness cut can
# separate them and "far from the background" floods the entire margin. The mask
# then describes the surrounding paint, not the characters, and any split built
# on it is fiction. Above this share of ink the crop is refused outright, which
# the caller reads as "unverified" and answers with the consensus rule rather
# than with a verdict invented from a split nobody can justify.
MAX_MASK_INK_FRAC = 0.35

# ---- de-fusing a blob that swallowed a character ---------------------------
_VALLEY_FRAC = 0.30  # a seam dips below this fraction of the profile's peak
_MAX_SPLIT_DEPTH = 4  # bounds the recursion when a blob is welded shut

_LAMBDA_CUT = 4.0  # cutting *through* ink, per unit of profile height
_LAMBDA_WIDTH = 0.9  # width imbalance

Box = Tuple[int, int, int, int]
Comp = Tuple[int, int, int, int, int]  # x, y, w, h, area


# --------------------------------------------------------------------------- #
# Basic conversions
# --------------------------------------------------------------------------- #
def _gray(crop_bgr: np.ndarray) -> np.ndarray:
    if crop_bgr is None or getattr(crop_bgr, "size", 0) == 0:
        return np.zeros((1, 1), np.uint8)
    if len(crop_bgr.shape) == 3:
        return cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    return crop_bgr.astype(np.uint8, copy=False)


def _mask_from_bg(gray: np.ndarray, bg: float) -> np.ndarray:
    """Ink mask relative to one assumed background level."""
    dev = np.abs(gray.astype(np.float32) - bg)
    peak = float(dev.max())
    if peak < 8.0:
        return np.zeros(gray.shape, np.uint8)
    norm = np.clip(dev * (255.0 / peak), 0, 255).astype(np.uint8)
    smooth = cv2.GaussianBlur(norm, (3, 3), 0)
    _, mask = cv2.threshold(smooth, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return mask


def _ink_mask(gray: np.ndarray) -> np.ndarray:
    """Binary mask of "not background", independent of plate colour.

    The background of an Indian plate is whatever the plate is painted - white,
    yellow, black or green - so guessing polarity from the image mean is wrong
    about half the time. The border ring is background by construction, so its
    median *is* the background and the mask is simply "far enough from the
    background". Otsu on the deviation picks the cut point, which keeps dirt and
    low-contrast grime as ink instead of eating into the glyphs.

    The border ring is not always background, though. A detector crop is often
    rotated and padded with mid-grey, so the ring can be that padding while the
    plate inside is pure white - and then "ink" comes out as two thirds of the
    crop. So the whole-image median is tried as well and whichever assumption
    leaves *less* ink wins. Characters are the minority of any plate panel, so
    this cannot pick wrong.
    """
    h, w = gray.shape
    hy = max(1, h // 10)
    wx = max(1, w // 20)
    ring = np.concatenate(
        [
            gray[:hy, :].ravel(),
            gray[-hy:, :].ravel(),
            gray[:, :wx].ravel(),
            gray[:, -wx:].ravel(),
        ]
    )
    best: Optional[np.ndarray] = None
    for bg in (float(np.median(ring)), float(np.median(gray))):
        mask = _mask_from_bg(gray, bg)
        if best is None or int((mask > 0).sum()) < int((best > 0).sum()):
            best = mask
        if best is not None and int((best > 0).sum()) <= 0.05 * h * w:
            break
    if best is None:  # pragma: no cover - the loop always runs once
        best = np.zeros((h, w), np.uint8)
    return _drop_long_structures(best)


def _drop_long_structures(mask: np.ndarray) -> np.ndarray:
    """Erase the plate's rim and rules, which are long thin runs of ink.

    The rim is the reason the obvious component-based split fails on real
    photographs: the leftmost character is printed over the border, so it fuses
    with it into one blob spanning the whole panel, and every height test after
    that is measured against a 150-pixel-tall "character". Erosion can break the
    fusion but leaves 1-3 px shards that re-fuse with the same glyph on the far
    side, so it is not reliable on its own.

    A rim, a rule or a mounting rail is defined by *span*: it is ink in almost
    every column of its row (or almost every row of its column). A row of
    characters is not - ten glyphs cover maybe 60% of the panel, and their
    strokes are nowhere near continuous. So the two are separable by span alone,
    at any crop size, and no crop offset is assumed.

    The guard rails matter: if "long" would remove more than a quarter of the
    ink, the mask is not a plate with a rim on it (a binarisation gone wrong, a
    gradient wash) and nothing is removed at all.
    """
    out = mask
    for _ in range(2):
        ink_cols = (out > 0).sum(axis=0).astype(np.int64)
        ink_rows = (out > 0).sum(axis=1).astype(np.int64)
        n_cols = int((ink_cols > 0).sum())
        n_rows = int((ink_rows > 0).sum())
        if n_rows < 8 or n_cols < 8:
            break
        long_rows = ink_rows > 0.85 * n_cols
        long_cols = ink_cols > 0.85 * n_rows
        if not long_rows.any() and not long_cols.any():
            break
        nxt = out
        if long_rows.any() and int(long_rows.sum()) <= 0.25 * n_rows:
            nxt = nxt.copy()
            nxt[long_rows] = 0
        if long_cols.any() and int(long_cols.sum()) <= 0.25 * n_cols:
            nxt = nxt.copy()
            nxt[:, long_cols] = 0
        if nxt is out:
            break
        out = nxt
    return out


def _components(mask: np.ndarray) -> List[Comp]:
    """Every connected ink blob, left to right: ``(x, y, w, h, area)``."""
    n, _labels, stats, _c = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out: List[Comp] = []
    for i in range(1, n):
        x, y, bw, bh, area = (int(v) for v in stats[i])
        out.append((x, y, bw, bh, area))
    return sorted(out, key=lambda b: b[0])


def _merge_overlaps(boxes: Sequence[Comp]) -> List[Comp]:
    """Join components that sit on top of each other horizontally.

    ``4`` and ``8`` break into two blobs at low resolution and a diacritic lands
    next to its letter. Anything sharing more than half its width with an
    already-accepted box *of comparable height* is part of that box. The height
    guard matters: without it a wide component overlapping everything (a
    leftover rim) merges the whole panel into one "character".
    """
    out: List[List[int]] = []
    for x, y, bw, bh, area in sorted(boxes):
        placed = False
        for b in out:
            lo, hi = max(x, b[0]), min(x + bw, b[0] + b[2])
            if min(bh, b[3]) >= 0.55 * max(bh, b[3]) and (hi - lo) > 0.5 * min(bw, b[2]):
                nx, ny = min(x, b[0]), min(y, b[1])
                nx2, ny2 = max(x + bw, b[0] + b[2]), max(y + bh, b[1] + b[3])
                b[0], b[1], b[2], b[3] = nx, ny, nx2 - nx, ny2 - ny
                b[4] = b[2] * b[3]
                placed = True
                break
        if not placed:
            out.append([x, y, bw, bh, area])
    return [tuple(b) for b in out]  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Chrome rejection
# --------------------------------------------------------------------------- #
def _split_runs(profile: np.ndarray) -> List[Tuple[int, int]]:
    """Maximal runs of non-zero entries in a 1-D profile."""
    nz = np.flatnonzero(profile)
    if nz.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(nz) > 1)
    starts = np.concatenate([[nz[0]], nz[breaks + 1]])
    ends = np.concatenate([nz[breaks], [nz[-1]]]) + 1
    return [(int(a), int(b)) for a, b in zip(starts, ends)]


def _row_window(patch: np.ndarray, target_h: int) -> np.ndarray:
    """Keep the contiguous band of ``target_h`` rows carrying the most ink.

    A blob far taller than the glyphs beside it is a glyph welded to something
    that runs vertically along it - the plate rim, a mount rail. The glyph rows
    are the densest band of that height, so sliding a window over the row profile
    separates them in the row direction; the column pass below then cuts them
    apart across.
    """
    h = patch.shape[0]
    if target_h < 3 or h <= target_h:
        return patch
    prof = patch.sum(axis=1).astype(np.int64)
    csum = np.concatenate([[0], np.cumsum(prof)])
    win = csum[target_h:] - csum[:-target_h]
    if win.size == 0:  # pragma: no cover - guarded by h <= target_h above
        return patch
    return patch[int(np.argmax(win)) : int(np.argmax(win)) + target_h]


def _valley_cut(profile: np.ndarray) -> Optional[int]:
    """Deepest usable dip in a column profile, or ``None`` if there is none.

    A dip only counts as a seam when it falls well below the profile's own peak:
    the hollow of a ``D`` or the gap under a ``T``'s bar is a dip too, and
    cutting there would hand the classifier half a character.
    """
    n = int(profile.size)
    if n < 6:
        return None
    peak = float(profile.max())
    if peak <= 0:
        return None
    a = max(1, int(round(0.10 * n)))
    b = n - max(1, int(round(0.10 * n)))
    if b <= a:
        return None
    i = a + int(np.argmin(profile[a:b]))
    if float(profile[i]) > _VALLEY_FRAC * peak:
        return None
    return i


def _column_ranges(patch: np.ndarray, med_w: float) -> List[Tuple[int, int]]:
    """Break a patch into column runs, each narrow enough to be one glyph.

    Real gaps between glyphs are free cuts. Two glyphs welded together by a
    baseline rule or a smudge have no gap at all, so a run still wider than
    ``MAX_RUN_W_FRAC * med_w`` is cut at its deepest profile dip - and only a
    run that wide is cut, so a single glyph with a thin middle is never sliced.
    """
    prof = patch.sum(axis=0).astype(np.int64)
    stack: List[Tuple[int, int, int]] = [
        (int(a), int(b), 0) for a, b in reversed(_split_runs(prof > 0))
    ]
    out: List[Tuple[int, int]] = []
    while stack:
        a, b, depth = stack.pop()
        if b - a <= MAX_RUN_W_FRAC * med_w:
            out.append((a, b))
            continue
        cut = None if depth >= _MAX_SPLIT_DEPTH else _valley_cut(prof[a:b])
        if cut is None:
            out.append((a, b))
            continue
        stack.append((a, a + cut, depth + 1))
        stack.append((a + cut, b, depth + 1))
    return sorted(out)


def _split_fused(mask: np.ndarray, comp: Comp, med_h: float, med_w: float) -> List[Comp]:
    """Break a blob that has swallowed something else into real characters.

    The leftmost character of a plate is often printed over the rim and the
    rightmost one crowds the hologram, so they fuse into one blob that is far
    taller or wider than every other character. Dropping such a blob as "not a
    glyph" quietly deletes a real character from the plate - exactly the count
    mismatch this module must *report* rather than paper over - and returning
    only the largest piece of it is the same deletion with better manners.

    So the blob is split: a row window when chrome runs along it vertically,
    column runs where glyphs genuinely part, a profile dip where they do not.
    Only pieces that look like a character in *both* dimensions come back. If
    nothing qualifies the blob really was chrome, and ``[]`` says so.
    """
    x, y, bw, bh, _area = comp
    patch = mask[y : y + bh, x : x + bw] > 0
    if not patch.any():
        return []
    # A filled block is a sticker, not strokes: splitting one only produces two
    # smaller stickers, and each of those reads as a very confident glyph.
    if float(patch.sum()) / float(max(1, bw * bh)) > MAX_BLOCK_FILL:
        return []
    if bh > H_OUTLIER_HI * med_h:
        patch = _row_window(patch, int(round(med_h)))
        rows = np.flatnonzero(patch.any(axis=1))
        if rows.size == 0:
            return []
        y = y + int(rows[0])
        patch = patch[rows[0] : rows[-1] + 1]
    cols = np.flatnonzero(patch.any(axis=0))
    if cols.size == 0:
        return []
    x = x + int(cols[0])
    patch = patch[:, cols[0] : cols[-1] + 1]

    out: List[Comp] = []
    for a, b in _column_ranges(patch, med_w):
        sub = patch[:, a:b]
        rows = np.flatnonzero(sub.any(axis=1))
        cols = np.flatnonzero(sub.any(axis=0))
        if rows.size == 0 or cols.size == 0:
            continue
        sx, sy = a + int(cols[0]), int(rows[0])
        ex, ey = a + int(cols[-1]) + 1, int(rows[-1]) + 1
        cw, ch = ex - sx, ey - sy
        if not (W_OUTLIER_LO * med_w <= cw <= MAX_RUN_W_FRAC * med_w):
            continue
        if not (H_OUTLIER_LO * med_h <= ch <= H_OUTLIER_HI * med_h):
            continue
        out.append((x + sx, y + sy, cw, ch, int(sub[sy:ey, sx:ex].sum())))
    return out


def _filter_chrome(mask: np.ndarray, comps: Sequence[Comp], h: int, w: int) -> List[Comp]:
    """Keep the glyphs; drop the rim, the sticker, the IND band and the bolts.

    Each piece of furniture is identified by how it compares with the *run* of
    components rather than by where it sits:

    * the rim spans most of the panel;
    * the sticker is much taller than the glyphs;
    * bolts and screws are much shorter;
    * the IND band is much narrower than a glyph;
    * a sticker body is a *filled* block, where a glyph is strokes with holes;
    * a narrow blob pressed against a crop edge is a neighbouring plate.

    A blob that is merely the wrong shape is first handed to ``_split_fused``,
    because "too big" usually means a real character with the rim attached, not
    chrome - and sometimes two characters, which is why that helper can return
    more than one piece. The medians come from all the blobs, which is safe
    because glyphs always outnumber furniture on a plate - ten of them against
    three or four pieces of chrome.
    """
    if not comps:
        return []
    pool = [c for c in comps if not (c[2] >= 0.5 * w and c[3] >= 0.5 * h)]
    if not pool:
        pool = list(comps)
    med_h = float(np.median([c[3] for c in pool]))
    med_w = float(np.median([c[2] for c in pool]))
    if med_h <= 0 or med_w <= 0:
        return []
    keep: List[Comp] = []
    for comp in pool:
        if comp[3] < MIN_STROKE_H_FRAC * med_h:
            continue  # a speck: bolt, screw, dirt
        if comp[3] > H_OUTLIER_HI * med_h or comp[2] > MAX_RUN_W_FRAC * med_w:
            parts = _split_fused(mask, comp, med_h, med_w)
        else:
            parts = [comp]
        for part in parts:
            x, y, bw, bh, area = part
            if bw < W_OUTLIER_LO * med_w:
                continue
            fill = area / float(max(1, bw * bh))
            if fill > MAX_BLOCK_FILL and bw >= MIN_BLOCK_W_FRAC * med_w:
                continue
            if bw < MIN_EDGE_W_FRAC * med_w and (x <= 1 or x + bw >= w - 1):
                continue
            keep.append(part)
    return keep


def _erosion_levels(h: int) -> List[int]:
    out: List[int] = []
    for f in ERODE_FRACS:
        k = max(1, int(round(f * h)))
        if k not in out:
            out.append(k)
    return out


def _glyph_candidates(mask: np.ndarray, h: int, w: int, want: int) -> Tuple[List[Comp], int, List[Comp]]:
    """Best-effort glyph run across the erosion ladder.

    Returns ``(glyphs, best_erosion, best_any)``: the run found at the erosion
    that produced the closest count to ``want`` and, for the caller, which
    erosion produced it so it can grow the boxes back out to the un-eroded ink.
    """
    best: List[Comp] = []
    best_any: List[Comp] = []
    best_k = 1
    best_score: Optional[Tuple[int, int]] = None
    for k in _erosion_levels(h):
        eroded = mask if k <= 1 else cv2.erode(mask, np.ones((k, k), np.uint8))
        comps = _merge_overlaps(_components(eroded))
        keep = _filter_chrome(eroded, comps, h, w)
        if not keep:
            continue
        best_any = keep if not best_any or len(keep) > len(best_any) else best_any
        # Prefer an exact match; otherwise prefer more glyphs than wanted (a
        # split inside a glyph can be fixed by the profile pass, a merge cannot).
        score = (abs(len(keep) - want), -len(keep))
        if best_score is None or score < best_score:
            best_score = score
            best = keep
            best_k = k
    return best, best_k, best_any


# --------------------------------------------------------------------------- #
# Box construction
# --------------------------------------------------------------------------- #
def _tallen(mask: np.ndarray, col_lo: int, col_hi: int, fallback: Box) -> Box:
    """Shrink a column range to the rows that actually carry ink in it."""
    rows = np.flatnonzero((mask[:, col_lo:col_hi] > 0).any(axis=1))
    if rows.size == 0:
        return fallback
    return (col_lo, int(rows[0]), col_hi, int(rows[-1]) + 1)


def _plausible_run(comps: Sequence[Comp]) -> bool:
    if len(comps) < 2:
        return True
    widths = sorted(c[2] for c in comps)
    med = float(np.median(widths))
    if med <= 0:
        return False
    return widths[0] / med >= MIN_RUN_W_FRAC and widths[-1] / med <= MAX_RUN_W_FRAC


def _from_components(sub: np.ndarray, comps: Sequence[Comp], grow: int, want: int) -> Optional[List[Box]]:
    """One box per glyph - the fast path, and the trustworthy one.

    ``grow`` puts the box back around the ink the erosion removed. The rows come
    from the component itself, not from a re-scan of the ink in its columns:
    a glyph fused with the rim is trimmed vertically before it reaches here, and
    widening the box with the rim's rows would drag chrome back into the crop.
    """
    if len(comps) != want or not _plausible_run(comps):
        return None
    h, w = sub.shape
    boxes: List[Box] = []
    for x, y, bw, bh, _a in comps:
        a, b = max(0, x - grow), min(w, x + bw + grow)
        t, bb = max(0, y - grow), min(h, y + bh + grow)
        boxes.append((a, t, b, bb))
    return boxes


def _candidate_cuts(sub: np.ndarray, comps: Sequence[Comp]) -> np.ndarray:
    """Where a boundary could legitimately go.

    Two sources, unioned: the local minima of the ink profile (where a boundary
    cuts least) and the midpoints of the gaps between consecutive glyphs (where
    a boundary *must* go). Restricting the search to these turns the fallback
    dynamic program from an O(w^2) sweep into a few thousand operations, which is
    what makes it safe on the OCR worker.
    """
    h, w = sub.shape
    cols = (sub > 0).sum(axis=0).astype(np.float64)
    peak = float(cols.max())
    if peak <= 0:
        return np.zeros(0, dtype=np.int64)
    q = cv2.GaussianBlur((cols / peak).reshape(1, -1), (1, 3), 0).ravel()
    interior = np.arange(1, max(2, w - 1))
    mins = interior[(q[1:-1] <= q[:-2]) & (q[1:-1] <= q[2:])] if interior.size else np.zeros(0, np.int64)
    gaps = [
        (a[0] + a[2] + b[0]) // 2
        for a, b in zip(comps[:-1], comps[1:])
        if b[0] > a[0] + a[2]
    ]
    if mins.size == 0 and not gaps:
        return np.zeros(0, dtype=np.int64)
    cand = np.unique(np.concatenate([mins.astype(np.int64), np.array(gaps, dtype=np.int64)]))
    cand = cand[(cand >= 1) & (cand <= w - 1)]
    if cand.size > MAX_DP_CANDIDATES:
        cand = np.sort(cand[np.argsort(q[cand])[:MAX_DP_CANDIDATES]])
    return cand.astype(np.int64)


def _one_glyph_per_band(comps: Sequence[Comp], segs: Sequence[Tuple[int, int]]) -> bool:
    """Refuse a split that puts two glyphs inside one band.

    When the component pass already found at least as many believable glyphs as
    bands, each band must claim exactly one. A split that cannot satisfy that is
    a split this module does not understand, and guessing would put the
    classifier over half of two different characters - worse than not running it
    at all, because it would look like a clean verified read.
    """
    for a, b in segs:
        if sum(1 for x, _y, bw, _bh, _a in comps if x < b and x + bw > a) != 1:
            return False
    return True


def _profile_split(sub: np.ndarray, comps: Sequence[Comp], want: int) -> Optional[List[Box]]:
    """Cut into ``want`` bands at the best of the candidate boundaries.

    A band scores well for the ink it captures and badly for any ink it has to
    cut through, so the optimum lands in the gaps. Widths are constrained, so a
    degenerate "four glyphs in one band, one empty band" solution cannot win. The
    split is accepted only if every band carries a real share of the ink, a sane
    share of the width, and - when enough glyphs were found to check against -
    exactly one of them.
    """
    h, w = sub.shape
    if want < 2 or w < 8 * want:
        return None
    cols = (sub > 0).sum(axis=0).astype(np.float64)
    peak = float(cols.max())
    if peak <= 0:
        return None
    q = np.concatenate([cols / peak, [0.0]])  # index w == cut at the edge, free
    cand = _candidate_cuts(sub, comps)
    if cand.size < want - 1:
        return None

    pos = np.concatenate([[0], cand, [w]]).astype(np.int64)
    n = pos.size
    cum = np.concatenate([[0.0], np.cumsum(q)])[pos]
    mean_w = w / float(want)
    if mean_w < 3.0:
        return None
    minw = max(2, int(MIN_SEG_W_FRAC * mean_w))
    maxw = int(MAX_SEG_W_FRAC * mean_w)

    NEG = -np.inf
    dp = np.full((want + 1, n), NEG)
    back = np.zeros((want + 1, n), dtype=np.int64)
    dp[0, 0] = 0.0
    i_idx = np.arange(n)[None, :]
    j_idx = np.arange(n)[:, None]
    width = pos[j_idx] - pos[i_idx]
    ok = (width >= minw) & (width <= maxw)
    pen = _LAMBDA_WIDTH * np.abs(width - mean_w) / mean_w
    cut = q[pos][None, :] + q[pos][:, None]
    cov = (cum[j_idx] - cum[i_idx]) / mean_w
    for k in range(1, want + 1):
        prev = dp[k - 1][None, :]
        score = np.where(ok & np.isfinite(prev), prev + cov - _LAMBDA_CUT * cut - pen, NEG)
        arg = np.argmax(score, axis=1)
        dp[k] = score[np.arange(n), arg]
        back[k] = arg
    if not np.isfinite(dp[want, n - 1]):
        return None

    bounds = [n - 1]
    j = n - 1
    for k in range(want, 0, -1):
        j = int(back[k, j])
        bounds.append(j)
    bounds.reverse()
    edges = pos[np.array(bounds, dtype=np.int64)]
    segs = [(int(edges[i]), int(edges[i + 1])) for i in range(want)]

    widths = np.array([b - a for a, b in segs], dtype=np.float64)
    if widths.min() < MIN_SEG_W_FRAC * mean_w or widths.max() > MAX_SEG_W_FRAC * mean_w:
        return None
    inks = np.array([float(cols[a:b].sum()) for a, b in segs])
    med_ink = float(np.median(inks))
    if med_ink <= 0 or inks.min() < MIN_SEG_INK_FRAC * med_ink:
        return None
    if len(comps) >= want and not _one_glyph_per_band(comps, segs):
        return None
    return [_tallen(sub, a, b, (a, 0, b, h)) for a, b in segs]


def _clip_boxes(gray: np.ndarray, boxes: Sequence[Box], margin: int = 2) -> List[np.ndarray]:
    h, w = gray.shape
    crops: List[np.ndarray] = []
    for x0, y0, x1, y1 in boxes:
        x0 = max(0, x0 - margin)
        y0 = max(0, y0 - margin)
        x1 = min(w, x1 + margin)
        y1 = min(h, y1 + margin)
        if x1 - x0 < 2 or y1 - y0 < 4:
            return []
        crops.append(gray[y0:y1, x0:x1])
    return crops


def _want(ocr_str: str) -> int:
    return len("".join(c for c in str(ocr_str).upper() if c.isalnum()))


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def _boxes(crop_bgr: np.ndarray, ocr_str: str) -> Optional[List[Box]]:
    """Boxes in ``crop_bgr``'s coordinate system, or ``None`` if not segmentable."""
    if cv2 is None or crop_bgr is None or getattr(crop_bgr, "size", 0) == 0:
        return None
    want = _want(ocr_str)
    if want < 2 or want > 12:
        return None
    gray = _gray(crop_bgr)
    h, w = gray.shape
    if h < 12 or w < 24 or w / float(h) < 1.6:
        # Not plate-shaped. Anything this thin, or too square, is a vehicle box
        # or a fragment of one, and guessing at it would be noise.
        return None

    mask = _ink_mask(gray)
    if float((mask > 0).sum()) > MAX_MASK_INK_FRAC * h * w:
        return None
    comps, grow, _any = _glyph_candidates(mask, h, w, want)
    if not comps:
        return None

    # Everything is judged against the character band, which the surviving glyphs
    # define - not against the whole crop, which may include car paint.
    top = max(0, min(c[1] for c in comps) - grow)
    bottom = min(h, max(c[1] + c[3] for c in comps) + grow)
    left = max(0, min(c[0] for c in comps) - grow)
    right = min(w, max(c[0] + c[2] for c in comps) + grow)
    band_h = bottom - top
    if band_h < 8 or right - left < 8 * want:
        return None
    sub = mask[top:bottom, left:right]
    sub_comps = [(c[0] - left, c[1] - top, c[2], c[3], c[4]) for c in comps]
    if len(sub_comps) >= want and not _plausible_run(sub_comps):
        sub_comps = []
    if not sub_comps:
        sub_comps = _filter_chrome(sub, _merge_overlaps(_components(sub)), band_h, right - left)

    boxes = _from_components(sub, sub_comps, grow, want)
    if boxes is None:
        boxes = _profile_split(sub, sub_comps, want)
    if boxes is None:
        return None
    return [(x0 + left, y0 + top, x1 + left, y1 + top) for x0, y0, x1, y1 in boxes]


def segment_chars(crop_bgr: np.ndarray, ocr_str: str) -> List[np.ndarray]:
    """Cut ``crop_bgr`` into one BGR crop per character of ``ocr_str``.

    Returns exactly ``len(ocr_str)`` crops, or ``[]`` if the crop cannot be cut
    into that many believable glyphs. An empty list means *unverified*: the caller
    must fall back to requiring consensus between frames rather than treating
    the read as a clean one.
    """
    boxes = _boxes(crop_bgr, ocr_str)
    if boxes is None:
        return []
    crops = _clip_boxes(_gray(crop_bgr), boxes)
    if len(crops) != _want(ocr_str):
        return []
    return [cv2.cvtColor(c, cv2.COLOR_GRAY2BGR) for c in crops]


def segment_boxes(crop_bgr: np.ndarray, ocr_str: str) -> List[Box]:
    """The same segmentation as boxes, for scripts that want to draw it."""
    boxes = _boxes(crop_bgr, ocr_str)
    if boxes is None:
        return []
    gray = _gray(crop_bgr)
    h, w = gray.shape
    return [
        (max(0, x0 - 2), max(0, y0 - 2), min(w, x1 + 2), min(h, y1 + 2))
        for x0, y0, x1, y1 in boxes
    ]
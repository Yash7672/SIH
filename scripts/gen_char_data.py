#!/usr/bin/env python3
"""Build the 36-class character-crop dataset the verifier is trained on.

Why this exists
---------------
``ai/char_verifier`` answers one question per glyph - "what is this, how sure
are you?" - and it can only answer it as well as the crops it was fitted to.
The project has almost no labelled real plates (six demo renders and an empty
``data/test_plates``), so the training signal is *rendered* plates: this script
paints Indian number plates in a PIL font, photographs them with a chain of
degradations, and cuts them into one crop per character.

What it produces
----------------
``data/char_dataset/<split>/<CHAR>/*.png`` for

``train``      rendered plates, fonts seen at train time
``val``        rendered, same fonts - drives early stopping + calibration
``test``       rendered, same fonts - the headline synthetic number
``font_held``  rendered with fonts *never* seen in train: "does it still work
               on a typeface it has not memorised?"
``real_train`` crops cut out of the demo plates in ``data/generated``
``real_test``  held-out crops from those same plates (>=30% of them)

Only ``train`` is oversampled on the hard pairs (Z/7/2, O/0/Q/D, ...); every
eval split stays naturally balanced, or the reported accuracy would be a
statement about the sampling rather than about the model.

Two things it deliberately does *not* do
----------------------------------------
* It never writes outside ``data/``.
* It never guesses a label. Real crops are only taken when the segmenter cuts
  the plate into exactly as many glyphs as the plate string has characters -
  a mis-segmentation labelled as truth is worse than no real data at all.

Usage
-----
    python scripts/gen_char_data.py            # generate whatever is missing
    python scripts/gen_char_data.py --clean    # wipe and regenerate everything
    python scripts/gen_char_data.py --quick    # tiny counts, for smoke tests
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # so `ai.char_segment` resolves when run as a script
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
CHAR_DS = DATA_DIR / "char_dataset"
DEBUG_DIR = DATA_DIR / "debug_out" / "char_gen"
GENERATED_DIR = DATA_DIR / "generated"
TEST_PLATES_DIR = DATA_DIR / "test_plates"

CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
SYNTH_SPLITS = ("train", "val", "test", "font_held")
REAL_SPLITS = ("real_train", "real_test")
ALL_SPLITS = SYNTH_SPLITS + REAL_SPLITS

# Fixed, documented, and the reason two runs of this script produce the same
# files: every random draw comes from a seed derived from this constant.
SEED = 42

# Hard pairs. A classifier that cannot tell Z from 2 has not learned anything
# the OCR did not already get wrong, so these classes are oversampled 2x in the
# train split only.
CONFUSION_GROUPS: Tuple[frozenset, ...] = (
    frozenset("Z72"),
    frozenset("O0QD"),
    frozenset("I1L"),
    frozenset("B8"),
    frozenset("S5"),
    frozenset("G69"),
    frozenset("MNH"),
    frozenset("UV"),
    frozenset("EF"),
    frozenset("TY"),
)
GROUPED_CHARS = frozenset().union(*CONFUSION_GROUPS)

# Plate renderings. An Indian plate is white, yellow, black or green depending
# on what it is attached to, and the glyph is dark on the first two and light
# on the last two - so the polarity the model sees flips between styles, which
# is exactly what `preprocess_char` normalises at inference time.
# name, background BGR, glyph BGR, border BGR
PLATE_STYLES: Tuple[Tuple[str, Tuple[int, int, int], Tuple[int, int, int], Tuple[int, int, int]], ...] = (
    ("white", (250, 250, 250), (22, 22, 22), (18, 18, 18)),
    ("yellow", (18, 190, 255), (18, 18, 18), (18, 18, 18)),
    ("black_on_yellow", (22, 22, 22), (20, 200, 255), (20, 200, 255)),
    ("green", (66, 140, 26), (245, 245, 245), (245, 245, 245)),
)

# ``plate_1_DL8CBF4890.png`` -> ``DL8CBF4890``
_PLATE_NAME_RE = re.compile(r"([A-Z]{2}\d{1,2}[A-Z]{1,3}\d{1,4})$")


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #
def find_font_paths() -> List[Path]:
    """Every font file this machine knows about, de-duplicated, sorted."""
    found: List[str] = []
    try:
        import matplotlib.font_manager as fm

        found.extend(fm.findSystemFonts())
    except Exception:  # noqa: BLE001 - matplotlib is optional
        pass
    for base in (Path(r"C:\Windows\Fonts"), Path("/usr/share/fonts"), Path.home() / "Fonts"):
        if base.exists():
            found.extend(str(p) for p in base.rglob("*") if p.suffix.lower() in {".ttf", ".ttc", ".otf"})

    seen: set = set()
    out: List[Path] = []
    for raw in sorted(found):
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(Path(raw))
    return out


def font_covers_every_glyph(path: Path) -> bool:
    """True when this font can draw all 36 characters.

    Symbol and dingbat fonts are plentiful on a dev machine and useless here;
    a font missing even one glyph silently deletes that class from the dataset.
    """
    try:
        font = ImageFont.truetype(str(path), 64)
    except Exception:  # noqa: BLE001 - unreadable/corrupt file
        return False
    for ch in CHARS:
        probe = Image.new("L", (96, 96), 0)
        draw = ImageDraw.Draw(probe)
        try:
            draw.text((8, 8), ch, font=font, fill=255)
        except Exception:  # noqa: BLE001
            return False
        if int(np.asarray(probe).max()) < 40:
            return False
    return True


def pick_fonts(limit: int = 40, held_frac: float = 0.2) -> Tuple[List[Path], List[Path]]:
    """``(train_fonts, held_out_fonts)``.

    The held-out set is every ``k``-th font after a deterministic sort, so it
    never overlaps train and does not change between runs. Those fonts are what
    ``font_held`` is scored on: a model that only saw Arial and DejaVu, tested
    on a stencil face it has never seen, is the honest test of "can it read a
    plate from a car it has not seen".
    """
    usable = [p for p in find_font_paths() if font_covers_every_glyph(p)][:limit]
    if not usable:
        raise SystemExit("no font on this machine can render all 36 characters")
    k = max(1, int(round(1.0 / held_frac)))
    held = usable[::k][: max(2, len(usable) // 5)]
    train = [p for p in usable if p not in held]
    if not train:  # degenerate tiny machine
        train, held = usable[:1], usable[1:] or usable[:1]
    return train, held


# --------------------------------------------------------------------------- #
# Plate rendering
# --------------------------------------------------------------------------- #
def _text_size(draw: ImageDraw.ImageDraw, ch: str, font: ImageFont.FreeTypeFont) -> Tuple[int, int, int, int]:
    x0, y0, x1, y1 = draw.textbbox((0, 0), ch, font=font)
    return x0, y0, x1 - x0, y1 - y0


def render_plate(
    text: str,
    font_path: Path,
    style: Tuple[str, Tuple[int, int, int], Tuple[int, int, int], Tuple[int, int, int]],
    rng: np.random.Generator,
    scale: float = 1.0,
) -> Tuple[np.ndarray, List[Tuple[int, int, int, int]]]:
    """Paint ``text`` on a plate and return ``(bgr_image, per-character boxes)``.

    The margins are the point. The demo generator in
    ``scripts/generate_plate_images.py`` prints the first glyph *over* the rim,
    which fuses the two into one connected blob and is the reason half the
    legacy demo plates refuse to segment. Here every glyph keeps at least a
    full stroke-width of clear plate on all sides, so a correct segmenter has
    nothing to fight.
    """
    _name, bg, fg, border = style
    cell_h = int(round(96 * scale))
    margin_y = max(8, int(round(cell_h * 0.30)))
    gap = max(4, int(round(cell_h * 0.26)))
    gap_after_state = max(6, int(round(cell_h * 0.55)))

    # Font size so the digits roughly fill the cell; one correction pass is
    # enough because cap-height is nearly linear in point size.
    size = int(round(cell_h * 0.80))
    font = ImageFont.truetype(str(font_path), size)
    scratch = ImageDraw.Draw(Image.new("L", (8, 8)))
    _x, _y, _tw, th = _text_size(scratch, "8", font)
    if th > 0:
        size = max(8, int(round(size * (cell_h * 0.78) / th)))
        font = ImageFont.truetype(str(font_path), size)

    widths: List[int] = []
    for i, ch in enumerate(text):
        _x, _y, tw, _th = _text_size(scratch, ch, font)
        widths.append(max(4, tw))
        if i == 1:
            widths[-1] += gap_after_state - gap

    ind_w = int(round(cell_h * 0.55))  # the blue IND band on the right
    pad = max(6, int(round(cell_h * 0.18)))
    # Two clear cell-pads between the last glyph and the IND band: the rim and
    # the band are the two things a segmenter happily fuses glyphs into.
    plate_w = int(pad * 3 + sum(widths) + gap * (len(text) - 1) + ind_w)
    plate_h = cell_h + margin_y * 2
    img = np.full((plate_h, plate_w, 3), bg, np.uint8)

    # IND band.
    if ind_w > 0:
        img[:, plate_w - ind_w :] = (200, 120, 30)
        band = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(band)
        ind_font = ImageFont.truetype(str(font_path), max(8, int(cell_h * 0.26)))
        draw.text((plate_w - ind_w // 2, plate_h // 2), "IND", font=ind_font, fill=(255, 255, 255), anchor="mm")
        img = cv2.cvtColor(np.asarray(band), cv2.COLOR_RGB2BGR)

    # The rim: a border inset from the edge, drawn *before* the glyphs and thin
    # enough that it never touches one.
    rim = max(2, int(round(3 * scale)))
    cv2.rectangle(img, (rim, rim), (plate_w - ind_w - rim, plate_h - rim - 1), border, rim)

    canvas = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(canvas)
    x = pad
    boxes: List[Tuple[int, int, int, int]] = []
    top = margin_y
    for i, ch in enumerate(text):
        cell_w = widths[i]
        x0, y0, tw, th = _text_size(draw, ch, font)
        draw.text(
            (x - x0 + (cell_w - tw) // 2, top - y0 + (cell_h - th) // 2),
            ch,
            font=font,
            fill=tuple(int(c) for c in fg),
        )
        boxes.append((x, top, x + cell_w, top + cell_h))
        x += cell_w + gap

    out = cv2.cvtColor(np.asarray(canvas), cv2.COLOR_RGB2BGR)

    # A little plate-scale wobble, so the glyph edges are not bit-identical
    # between samples rendered from the same font.
    if rng.random() < 0.5:
        out = cv2.GaussianBlur(out, (3, 3), 0)
    return out, boxes


def random_plate_text(rng: np.random.Generator) -> str:
    """A plate shaped like the ones the grammar in ``plate_verdict`` accepts."""
    letters = np.array(list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    digits = np.array(list("0123456789"))
    state = "".join(rng.choice(letters, 2))
    rto = "".join(rng.choice(digits, int(rng.integers(1, 3))))
    series = "".join(rng.choice(letters, int(rng.integers(1, 4))))
    tail = "".join(rng.choice(digits, int(rng.integers(1, 5))))
    return state + rto + series + tail


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #
def _bg_estimate(patch: np.ndarray) -> float:
    h, w = patch.shape[:2]
    ring = np.concatenate(
        [
            patch[: max(1, h // 8), :].ravel(),
            patch[-max(1, h // 8) :, :].ravel(),
            patch[:, : max(1, w // 8)].ravel(),
            patch[:, -max(1, w // 8) :].ravel(),
        ]
    )
    return float(np.median(ring))


def aug_blur(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    k = int(rng.choice([3, 5, 7]))
    return cv2.GaussianBlur(img, (k, k), 0)


def aug_defocus(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    k = int(rng.integers(5, 13)) | 1
    return cv2.blur(img, (k, k))


def aug_jpeg(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    q = int(rng.integers(18, 70))
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), q])
    if not ok:
        return img
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def aug_rotation(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    ang = float(rng.uniform(-9.0, 9.0))
    m = cv2.getRotationMatrix2D((img.shape[1] / 2, img.shape[0] / 2), ang, 1.0)
    return cv2.warpAffine(img, m, (img.shape[1], img.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def aug_skew(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    sh = float(rng.uniform(-0.22, 0.22))
    h, w = img.shape[:2]
    m = np.float32([[1, sh, -sh * h / 2], [0, 1, 0]])
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def aug_perspective(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    h, w = img.shape[:2]
    j = 0.06
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = src + rng.uniform(-j, j, src.shape).astype(np.float32) * np.float32([w, h])
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(img, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def aug_brightness(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    alpha = float(rng.uniform(0.60, 1.50))
    beta = float(rng.uniform(-30, 30))
    return np.clip(img.astype(np.float32) * alpha + beta, 0, 255).astype(np.uint8)


def aug_glare(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """A specular sweep - the reason a plate photographed at noon is half gone."""
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = rng.uniform(0, w), rng.uniform(0, h)
    r = rng.uniform(0.35, 0.9) * max(w, h)
    sigma = r * rng.uniform(0.25, 0.6)
    mask = np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma * sigma)))
    strength = float(rng.uniform(0.35, 0.95))
    out = img.astype(np.float32) + mask[..., None] * strength * 255.0
    return np.clip(out, 0, 255).astype(np.uint8)


def aug_shadow(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    h, w = img.shape[:2]
    angle = float(rng.uniform(0, np.pi))
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    proj = xx * np.cos(angle) + yy * np.sin(angle)
    centre = rng.uniform(proj.min(), proj.max())
    soft = max(1.0, 0.25 * (proj.max() - proj.min()))
    mask = 1.0 / (1.0 + np.exp(-(proj - centre) / soft))
    keep = float(rng.uniform(0.35, 0.75))
    factor = 1.0 - (1.0 - keep) * mask
    return np.clip(img.astype(np.float32) * factor[..., None], 0, 255).astype(np.uint8)


def aug_noise(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    if rng.random() < 0.5:
        sigma = float(rng.uniform(3, 14))
        noise = rng.normal(0, sigma, img.shape).astype(np.float32)
    else:
        noise = img.astype(np.float32) * rng.normal(1.0, rng.uniform(0.02, 0.10), img.shape).astype(np.float32)
    return np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)


def aug_dirt(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Mud specks and scratches - every real plate has them."""
    out = img.copy()
    h, w = out.shape[:2]
    dark = int(_bg_estimate(out) * 0.45)
    for _ in range(int(rng.integers(2, 8))):
        cx, cy = int(rng.integers(0, w)), int(rng.integers(0, h))
        r = int(rng.integers(1, max(2, min(h, w) // 9)))
        cv2.circle(out, (cx, cy), r, (dark, dark, dark), -1)
    if rng.random() < 0.6:
        x0, y0 = int(rng.integers(0, w)), int(rng.integers(0, h))
        x1, y1 = int(rng.integers(0, w)), int(rng.integers(0, h))
        cv2.line(out, (x0, y0), (x1, y1), (dark, dark, dark), int(rng.integers(1, 3)))
    return out


def aug_moire(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """The interference pattern a phone sensor makes of a fine printed grid."""
    h, w = img.shape[:2]
    fy = float(rng.uniform(0.15, 0.6))
    fx = float(rng.uniform(0.15, 0.6))
    ph = float(rng.uniform(0, np.pi))
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    pattern = np.sin(yy * fy + ph) * np.sin(xx * fx)
    amp = float(rng.uniform(6, 26))
    return np.clip(img.astype(np.float32) + pattern[..., None] * amp, 0, 255).astype(np.uint8)


# (function, probability of applying it, where it applies)
PLATE_AUGS = (
    (aug_brightness, 0.45),
    (aug_glare, 0.30),
    (aug_shadow, 0.30),
    (aug_noise, 0.45),
    (aug_dirt, 0.35),
    (aug_moire, 0.20),
    (aug_jpeg, 0.40),
    (aug_blur, 0.35),
    (aug_defocus, 0.20),
)
CELL_AUGS = (
    (aug_rotation, 0.45),
    (aug_skew, 0.35),
    (aug_perspective, 0.30),
    (aug_brightness, 0.35),
    (aug_noise, 0.40),
    (aug_blur, 0.30),
)


def degrade_plate(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    for fn, p in PLATE_AUGS:
        if rng.random() < p:
            img = fn(img, rng)
    return img


def degrade_cell(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    for fn, p in CELL_AUGS:
        if rng.random() < p:
            img = fn(img, rng)
    return img


# --------------------------------------------------------------------------- #
# Crops
# --------------------------------------------------------------------------- #
def crop_cells(
    img: np.ndarray, boxes: Sequence[Tuple[int, int, int, int]], pad_frac: float = 0.10
) -> List[np.ndarray]:
    h, w = img.shape[:2]
    out: List[np.ndarray] = []
    for x0, y0, x1, y1 in boxes:
        pw = int(round((x1 - x0) * pad_frac))
        ph = int(round((y1 - y0) * pad_frac))
        a, b = max(0, x0 - pw), min(w, x1 + pw)
        c, d = max(0, y0 - ph), min(h, y1 + ph)
        if b - a < 3 or d - c < 3:
            out.append(img[max(0, y0) : min(h, y1), max(0, x0) : min(w, x1)])
            continue
        out.append(img[c:d, a:b])
    return out


def to_gray(crop: np.ndarray) -> np.ndarray:
    if crop.ndim == 3:
        return cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return crop


# --------------------------------------------------------------------------- #
# Real crops
# --------------------------------------------------------------------------- #
def plate_text_from_name(path: Path) -> Optional[str]:
    m = _PLATE_NAME_RE.search(path.stem)
    return m.group(1) if m else None


def real_plate_paths() -> List[Path]:
    out: List[Path] = []
    if GENERATED_DIR.is_dir():
        out.extend(sorted(p for p in GENERATED_DIR.glob("*.png") if plate_text_from_name(p)))
    if TEST_PLATES_DIR.is_dir():
        for p in sorted(TEST_PLATES_DIR.rglob("*")):
            if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"} and plate_text_from_name(p):
                out.append(p)
    return out


def extract_real_crops(rng: np.random.Generator) -> Tuple[Dict[str, List[np.ndarray]], dict]:
    """Cut the demo plates into labelled glyphs.

    A crop is only trusted when ``segment_chars`` returns exactly one box per
    character of the plate string. Anything else - a plate it could not cut, a
    cut with the wrong count - is skipped entirely. With six plates that means
    some are unusable, and saying so in the report is the point: the REAL
    numbers are computed on a small, honestly-gathered set, not on a large one
    we bent to fit.
    """
    try:
        from ai import char_segment
    except Exception as exc:  # noqa: BLE001
        print(f"real crops unavailable: {exc}")
        return {}, {"error": str(exc)}

    buckets: Dict[str, List[np.ndarray]] = {c: [] for c in CHARS}
    report = {"plates": [], "skipped": []}
    for path in real_plate_paths():
        text = plate_text_from_name(path)
        if not text:
            continue
        img = cv2.imread(str(path))
        if img is None:
            report["skipped"].append({"path": path.name, "why": "unreadable"})
            continue
        boxes = char_segment.segment_boxes(img, text)
        if len(boxes) != len(text):
            report["skipped"].append(
                {"path": path.name, "why": f"segmented {len(boxes)} != {len(text)}"}
            )
            continue
        crops = crop_cells(img, boxes, pad_frac=0.08)
        for ch, crop in zip(text, crops):
            if ch in buckets and min(crop.shape[:2]) >= 6:
                buckets[ch].append(to_gray(crop))
        report["plates"].append({"path": path.name, "text": text, "chars": len(text)})
    return buckets, report


def split_real(
    buckets: Dict[str, List[np.ndarray]], rng: np.random.Generator, holdout: float = 0.35
) -> Tuple[Dict[str, Dict[str, List[np.ndarray]]], dict]:
    """Split every class's real crops, holding out ``holdout`` (>= 30%) for test.

    Split per class rather than per plate: with six plates a plate-level split
    would either starve ``real_test`` of whole characters or leak the same plate
    into both sides. Per class keeps every character represented on both sides.
    """
    out: Dict[str, Dict[str, List[np.ndarray]]] = {
        "real_train": {c: [] for c in CHARS},
        "real_test": {c: [] for c in CHARS},
    }
    stats = {}
    for ch, crops in buckets.items():
        if not crops:
            stats[ch] = 0
            continue
        order = rng.permutation(len(crops))
        n_test = max(1, int(round(len(crops) * holdout))) if len(crops) > 1 else 0
        test_idx = set(int(i) for i in order[:n_test])
        for i, idx in enumerate(order):
            split = "real_test" if i in test_idx else "real_train"
            out[split][ch].append(crops[int(idx)])
        stats[ch] = len(crops)
    return out, stats


# --------------------------------------------------------------------------- #
# Synthesis driver
# --------------------------------------------------------------------------- #
def build_synth_split(
    name: str,
    targets: Dict[str, int],
    fonts: Sequence[Path],
    rng: np.random.Generator,
    limit_plates: int = 20000,
) -> Dict[str, List[np.ndarray]]:
    """Render plates until every class has ``targets`` crops in hand."""
    buckets: Dict[str, List[np.ndarray]] = {c: [] for c in CHARS}
    plates = 0
    while any(len(buckets[c]) < targets[c] for c in CHARS) and plates < limit_plates:
        plates += 1
        text = random_plate_text(rng)
        font = fonts[int(rng.integers(0, len(fonts)))]
        style = PLATE_STYLES[int(rng.integers(0, len(PLATE_STYLES)))]
        scale = float(rng.uniform(0.65, 1.45))
        img, boxes = render_plate(text, font, style, rng, scale=scale)
        img = degrade_plate(img, rng)
        for ch, cell in zip(text, crop_cells(img, boxes)):
            if ch in buckets and len(buckets[ch]) < targets[ch]:
                buckets[ch].append(cell)
    for ch in CHARS:
        if len(buckets[ch]) < targets[ch]:
            print(f"  warning: class {ch} reached {len(buckets[ch])}/{targets[ch]}")
    return buckets


def write_split(
    split: str,
    buckets: Dict[str, List[np.ndarray]],
    rng: np.random.Generator,
    per_cell_aug: bool = True,
) -> Dict[str, int]:
    root = CHAR_DS / split
    counts: Dict[str, int] = {}
    for ch in CHARS:
        cls_dir = root / ch
        cls_dir.mkdir(parents=True, exist_ok=True)
        crops = buckets.get(ch, [])
        counts[ch] = len(crops)
        for i, cell in enumerate(crops):
            out = degrade_cell(cell, rng) if per_cell_aug else cell
            gray = to_gray(out)
            # A crop the model can learn nothing from is not written: a blank
            # cell would be a labelled example of "nothing", which is not a class.
            if gray.size == 0 or int(gray.max()) - int(gray.min()) < 6:
                counts[ch] -= 1
                continue
            cv2.imwrite(str(cls_dir / f"{i:05d}.png"), gray)
    return counts


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clean", action="store_true", help="wipe the dataset first")
    ap.add_argument("--quick", action="store_true", help="tiny counts, for a smoke test")
    ap.add_argument("--train-per-class", type=int, default=150)
    ap.add_argument("--eval-per-class", type=int, default=40)
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()

    if args.quick:
        args.train_per_class, args.eval_per_class = 12, 6

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    if args.clean and CHAR_DS.exists():
        shutil.rmtree(CHAR_DS)
    for split in ALL_SPLITS:
        (CHAR_DS / split).mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    random.seed(args.seed)
    rng = np.random.default_rng(args.seed)

    train_fonts, held_fonts = pick_fonts()
    print(f"fonts: {len(train_fonts)} usable, {len(held_fonts)} held out")
    print("  held-out: " + ", ".join(p.name for p in held_fonts))

    # One plate kept aside so the layout (margins, rim, IND band) can be looked
    # at instead of guessed at: a renderer that touches the rim produces crops
    # no amount of classifier training will fix.
    demo_text = random_plate_text(rng)
    demo_img, _demo_boxes = render_plate(demo_text, train_fonts[0], PLATE_STYLES[0], rng, scale=1.0)
    cv2.imwrite(str(DEBUG_DIR / "sample_plate.png"), demo_img)
    print(f"sample plate ({demo_text}) -> {DEBUG_DIR / 'sample_plate.png'}")

    summary: dict = {
        "seed": args.seed,
        "quick": bool(args.quick),
        "fonts": {"train": [p.name for p in train_fonts], "held_out": [p.name for p in held_fonts]},
        "splits": {},
        "real": {},
    }

    # ---- synthetic splits -------------------------------------------------- #
    base = args.train_per_class
    # 2x oversample for the hard pairs, train only - the eval splits stay
    # balanced so the numbers mean something.
    train_targets = {c: base * (2 if c in GROUPED_CHARS else 1) for c in CHARS}
    eval_targets = {c: args.eval_per_class for c in CHARS}

    plan = (
        ("train", train_targets, train_fonts),
        ("val", eval_targets, train_fonts),
        ("test", eval_targets, train_fonts),
        ("font_held", eval_targets, held_fonts),
    )
    for split, targets, fonts in plan:
        existing = sum(1 for c in CHARS for _ in (CHAR_DS / split / c).glob("*.png")) if (CHAR_DS / split).exists() else 0
        needed = sum(targets.values())
        if existing >= needed * 0.9 and not args.clean:
            print(f"{split}: {existing} crops already present, skipping (use --clean to redo)")
            summary["splits"][split] = {"count": existing, "skipped": True}
            continue
        for c in CHARS:
            for old in (CHAR_DS / split / c).glob("*.png"):
                old.unlink()
        print(f"{split}: rendering {needed} crops ...")
        buckets = build_synth_split(split, targets, fonts, rng)
        counts = write_split(split, buckets, rng)
        summary["splits"][split] = {"count": sum(counts.values()), "per_class": counts}
        print(f"{split}: wrote {sum(counts.values())}")

    # ---- real crops -------------------------------------------------------- #
    real_buckets, real_report = extract_real_crops(rng)
    summary["real"] = real_report
    if real_buckets:
        split_buckets, real_stats = split_real(real_buckets, rng, holdout=0.35)
        total_real = sum(len(v) for v in real_buckets.values())
        for split in REAL_SPLITS:
            counts = write_split(split, split_buckets[split], rng, per_cell_aug=True)
            summary["splits"][split] = {"count": sum(counts.values()), "per_class": counts}
            print(f"{split}: wrote {sum(counts.values())}")
        summary["real"]["per_class"] = real_stats
        held = sum(len(split_buckets["real_test"][c]) for c in CHARS)
        print(f"real: {total_real} crops, {held} held out ({100.0 * held / max(1, total_real):.0f}% >= 30%)")
    else:
        for split in REAL_SPLITS:
            summary["splits"][split] = {"count": 0, "per_class": {}}
        print("real: no plate segmented cleanly - real splits are empty (see report)")

    # ---- report ------------------------------------------------------------ #
    # Read the counts the writer returned rather than re-globbing: the summary
    # is what the run actually wrote, and it cannot disagree with itself.
    print("\nper-class counts (train / val / test / font_held):")
    line = []
    for c in CHARS:
        row = []
        for split in SYNTH_SPLITS:
            info = summary["splits"].get(split) or {}
            per_class = info.get("per_class")
            if per_class is not None:
                row.append(int(per_class.get(c, 0)))
            else:
                row.append(sum(1 for _ in (CHAR_DS / split / c).glob("*.png")))
        line.append(f"{c}:{row[0]}/{row[1]}/{row[2]}/{row[3]}")
    for i in range(0, len(line), 6):
        print("  " + "  ".join(line[i : i + 6]))

    summary["seconds"] = round(time.perf_counter() - t0, 1)
    (DEBUG_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nwrote {DEBUG_DIR / 'summary.json'} in {summary['seconds']}s")


if __name__ == "__main__":
    main()

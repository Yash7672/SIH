"""Build the detection-bearing test frame used by the live-view tooling.

Why a generated fixture at all
------------------------------
``scripts/live_scan_test.py`` and ``scripts/render_live_view.py`` both need a
frame the detector actually finds something in, and it has to be a still image on
disk so every run is reproducible. The only real multi-vehicle Indian road scene
in the repo (``data/generated/demo_road.mp4``) is a synthetic dark strip with a
floating plate: YOLOv8n returns *no* COCO vehicle in it, so it can only ever
exercise the plate path.

So this script composites a licence-safe fixture out of two things already in the
repo:

* the sample car photo under ``plate_model_source/data/sample_images/``, and
* a plate drawn by ``scripts/generate_plate_images.py`` - the project's own
  synthetic renderer, not a photograph of anybody's actual number plate.

The result is a still that yields a vehicle box and one plate that reads back at
high confidence, which is exactly what the overlay and the hot-list path need to
be shown end to end. One vehicle is enough here: multi-vehicle behaviour (stable
track ids, labels not shuffling) is covered deterministically by
``backend/tests/test_scan_tracker.py`` against the tracker directly, which needs
no model and no photograph.

The composite parameters below are not arbitrary. They were swept at the exact
size and JPEG quality the phone streams (1280 px, quality 40), because a fixture
that only works at quality 92 would prove nothing. ``--check`` re-runs the whole
server pipeline on the finished file and fails loudly if any of it stops being
true, so a silent model change cannot leave the tooling quietly testing nothing.

Usage:
    python scripts/make_test_road_scene.py            # (re)build the fixture
    python scripts/make_test_road_scene.py --check    # verify only, no write
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path
from typing import List, Tuple

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from scripts.generate_plate_images import render_plate  # noqa: E402

# Where the pieces come from and where the result goes.
BASE_PHOTO = ROOT / "plate_model_source" / "data" / "sample_images" / "step1_original.png"
OUT_PATH = ROOT / "data" / "test_plates" / "road_scene.jpg"
PLATE_TEXT = "MH12JK4567"

# The composite, as measured on this box. See the module docstring.
#
# The sample photo already has its own (blurred, unreadable) number plate, and the
# plate detector finds *that* one at ~0.70 confidence - higher than anything a
# pasted plate manages. So the synthetic plate has to be pasted exactly over it,
# not somewhere else on the car: cover it and the only plate in the image is the
# synthetic one. Pasting it a few dozen pixels away leaves the real plate winning
# the confidence sort and OCR then reads nothing at all.
#
# The centre below is the centre of the plate box the detector reports in the
# unscaled 514x338 original - [175, 204, 214, 221] - mapped through the 1280-wide
# upscale. It is recomputed at build time rather than hard-coded so the fixture
# keeps working if the source photo changes.
#
# PLATE_WIDTH 190 px at the finished 1280 px width: the smallest size that still
# reads MH12JK4567 at all after the q40 re-encode the phone does. 110 px reads as
# ML12JK4567 and 130 px often reads as nothing at all.
PLATE_WIDTH = 190

# Final size and the JPEG quality the fixture is stored at. 1280 px wide is the
# same width the phone captures and the same width the server works at, so the
# fixture goes through the pipeline with nothing resampled on the way.
FINAL_WIDTH = 1280
FINAL_QUALITY = 92

# The simulation actually streams at what the phone streams at, not at the
# fixture's native size.
STREAM_WIDTH = 1280
STREAM_QUALITY = 40


def build_plate_image(tmp_dir: Path) -> Path:
    """Render the plate with the project's own synthetic renderer.

    Rendered far larger than it is pasted: ``render_plate`` sizes its font from
    ``width``, so asking for 190 px directly produces a small, aliased strip.
    Rendering at 1400 and letting LANCZOS downscale gives the crisp edges the
    OCR needs at the pasted size.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    plate_path = tmp_dir / f"plate_{PLATE_TEXT}.png"
    render_plate(
        PLATE_TEXT,
        str(plate_path),
        width=PLATE_WIDTH * 7,
        rotation=0.0,
        blur=0,
        noise=0,
    )
    return plate_path


def source_plate_centre(scale: float) -> tuple:
    """Centre of the plate already in the source photo, in the 1280-wide frame.

    Asked of the plate detector rather than hard-coded, because that is the only
    honest way to know where the thing we have to cover actually is.
    """
    import cv2
    import numpy as np

    from ai.detector import _load_plate_detector

    base = Image.open(BASE_PHOTO).convert("RGB")
    bgr = cv2.cvtColor(np.asarray(base), cv2.COLOR_RGB2BGR)
    results = _load_plate_detector()(bgr, conf=0.25, verbose=False, imgsz=640)
    if not results or results[0].boxes is None or len(results[0].boxes) == 0:
        raise SystemExit(
            f"FAIL: no plate found in {BASE_PHOTO}. This script composites *over* the "
            f"photo's own plate; without one there is nothing to cover and the fixture "
            f"would silently test the wrong thing."
        )
    boxes = results[0].boxes
    # Highest confidence: the same choice the live pipeline makes.
    best = max(range(len(boxes)), key=lambda i: float(boxes.conf[i]))
    x1, y1, x2, y2 = (float(v) for v in boxes.xyxy[best].cpu().numpy())
    return (round((x1 + x2) / 2 * scale), round((y1 + y2) / 2 * scale))


def composite(plate_path: Path) -> Image.Image:
    """Paste the plate over the photo's own plate and scale to the fixture width."""
    base = Image.open(BASE_PHOTO).convert("RGB")
    plate = Image.open(plate_path).convert("RGB")

    scale = FINAL_WIDTH / base.width
    out = base.resize((FINAL_WIDTH, max(1, round(base.height * scale))), Image.BICUBIC)

    cx, cy = source_plate_centre(scale)
    ph = max(1, round(PLATE_WIDTH * plate.height / plate.width))
    pasted = plate.resize((PLATE_WIDTH, ph), Image.LANCZOS)
    out.paste(pasted, (cx - PLATE_WIDTH // 2, cy - ph // 2))
    return out


def stream_jpeg(image: Image.Image) -> bytes:
    """Encode the fixture the way the phone sends it: 1280 px wide, quality 40."""
    h = max(1, round(image.height * (STREAM_WIDTH / image.width)))
    buf = io.BytesIO()
    image.resize((STREAM_WIDTH, h), Image.BICUBIC).save(buf, "JPEG", quality=STREAM_QUALITY)
    return buf.getvalue()


def check(jpeg: bytes) -> Tuple[List[Tuple[str, float]], List[Tuple[str, float]], dict]:
    """Run the real server pipeline over the frame and report what it found."""
    from app.api.v1 import live_scan as ls
    from app.ws.scan_manager import VehicleTracker

    tracker = VehicleTracker()
    bgr, w, h, ms, vehicles, tracks = ls.infer_vehicles(jpeg, tracker)
    plates, crops = ls.infer_plates(bgr, w, h, tracks, tracker)
    labels = {t.tid: t.label for t in tracks}

    reads = {}
    for tid, crop in crops.items():
        result = ls.run_ocr(crop)
        if result:
            reads[labels.get(tid, str(tid))] = (result["norm"], round(result["conf"], 3))

    return (
        [(v["label"], round(v["conf"], 3)) for v in vehicles],
        [(labels.get(p["track"], str(p["track"])), round(p["conf"], 3)) for p in plates],
        {"w": w, "h": h, "stage1_ms": ms, "ocr": reads},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify the existing fixture instead of writing one")
    parser.add_argument("--out", type=Path, default=OUT_PATH, help="output path")
    args = parser.parse_args()

    out_path: Path = args.out

    if args.check:
        if not out_path.exists():
            print(f"FAIL: {out_path} does not exist - run without --check to build it")
            return 1
        image = Image.open(out_path).convert("RGB")
    else:
        plate_path = build_plate_image(ROOT / "data" / "generated")
        image = composite(plate_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(out_path, "JPEG", quality=FINAL_QUALITY)
        print(f"wrote {out_path.relative_to(ROOT)} ({out_path.stat().st_size // 1024} KB, {image.width}x{image.height})")

    jpeg = stream_jpeg(image)
    print(f"stream frame: {len(jpeg) // 1024} KB, {STREAM_WIDTH}px wide, quality {STREAM_QUALITY}")

    vehicles, plates, extra = check(jpeg)
    print(f"detected {extra['w']}x{extra['h']} in {extra['stage1_ms']} ms")
    for label, conf in vehicles:
        print(f"  vehicle {label} conf={conf}")
    for label, conf in plates:
        print(f"  plate   {label} conf={conf}")
    for label, (norm, conf) in extra["ocr"].items():
        print(f"  ocr     {label} -> {norm} conf={conf}")

    problems = []
    if not vehicles:
        problems.append(f"expected a vehicle box, got none")
    if not plates:
        problems.append("no plate was assigned to a vehicle")
    if PLATE_TEXT not in {norm for norm, _conf in extra["ocr"].values()}:
        problems.append(f"OCR did not read {PLATE_TEXT}; reads were {extra['ocr']}")
    elif min(c for norm, c in extra["ocr"].values() if norm == PLATE_TEXT) < 0.8:
        problems.append(f"OCR read {PLATE_TEXT} below 0.8: {extra['ocr']}")

    if problems:
        print("\nFAIL:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"\nOK: {len(vehicles)} vehicle box, plate on a vehicle, OCR reads {PLATE_TEXT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

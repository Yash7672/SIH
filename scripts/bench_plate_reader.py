"""Measure plate reading, old reader against new, on the fixtures that exist.

Run from the repo root:

    python scripts/bench_plate_reader.py

What it answers
---------------
* Of the plates on disk, how many does each reader get exactly right?
* Which *wrong* answer does the old one give, and does the new one still give it?
* How much does the new reader cost in time, and where does that time go?
* Is the green-box path still fast?

The "before" column is the reader as it was on `main`: one CLAHE preprocessing pass
and one full `engine()` call (detect + classify + recognise). The "after" column is
`plate_reader.read_crop`: three preprocessing variants, the recogniser called
directly, and a confidence-weighted vote. Both run over the same crops, cut from
the same detector output, so the only thing being compared is the reading.

Honesty about the fixtures
--------------------------
Plate filenames state the expected plate, and that is used as the expected value.
`road_scene.jpg` has no plate in its name, so it is read and its reading is
reported as observed rather than scored - inventing an expectation for it would
be guessing at the answer the test exists to measure.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "backend"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import app.api.v1.live_scan as ls  # noqa: E402
from ai.detector import _load_plate_detector, _pad_box, preprocess_crop  # noqa: E402
from ai.ocr_engine import best_plate, candidates, read_text  # noqa: E402
from app.services.plate_reader import build_variants, read_crop  # noqa: E402

PLATE_DIR = ROOT / "data" / "test_plates"
GENERATED_DIR = ROOT / "data" / "generated"

# The six plates the brief names. Reported when present, skipped loudly when not,
# so a missing fixture is never mistaken for a passing one.
NAMED_SIX = (
    "TS09FY3287",
    "TS07JH6611",
    "TS13EX9142",
    "TS07GF2210",
    "TS11UA2384",
    "TS08KD7721",
)


# --------------------------------------------------------------------------- #
# The two readers
# --------------------------------------------------------------------------- #
def old_read(crop: np.ndarray) -> Optional[Dict[str, object]]:
    """Exactly what the live path did before: preprocess once, one engine call."""
    t0 = time.perf_counter()
    pre = preprocess_crop(crop)
    hits = candidates(read_text(pre))
    hit = best_plate(hits) if hits else None
    ms = (time.perf_counter() - t0) * 1000
    if hit is None:
        return {"norm": None, "conf": 0.0, "ms": ms}
    return {"norm": hit.normalized, "conf": float(hit.score), "ms": ms}


def new_read(crop: np.ndarray) -> Optional[Dict[str, object]]:
    """The new reader, with the recogniser called once per variant."""
    t0 = time.perf_counter()
    read = read_crop(crop)
    ms = (time.perf_counter() - t0) * 1000
    if read is None:
        return {"norm": None, "conf": 0.0, "ms": ms}
    return {
        "norm": read.norm,
        "conf": float(read.conf),
        "ms": ms,
        "char_confs": tuple(read.char_confs or ()),
        "per_char": len(read.char_confs or ()),
        "ambiguous": len(read.ambiguous_positions),
        "variant": read.best_variant,
    }


def warm() -> None:
    """Load both models and the OCR engine before anything is timed."""
    ls.get_vehicle_model()
    _load_plate_detector()
    from ai.ocr_engine import get_engine

    get_engine()


# --------------------------------------------------------------------------- #
# Finding a plate in a picture
# --------------------------------------------------------------------------- #
def plate_crop(path: Path) -> Optional[Tuple[np.ndarray, int, int]]:
    """(crop, width_px, height_px), or None when no plate is found."""
    img = cv2.imread(str(path))
    if img is None:
        return None
    h, w = img.shape[:2]
    result = _load_plate_detector()(img, conf=ls.PLATE_CONF, verbose=False, imgsz=ls.PLATE_IMGSZ)[0]
    boxes = getattr(result, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return None
    xyxy = boxes.xyxy[0].detach().cpu().numpy()
    p = _pad_box((int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])), w, h)
    crop = img[p[1] : p[3], p[0] : p[2]]
    if not crop.size:
        return None
    return crop, int(xyxy[2] - xyxy[0]), int(xyxy[3] - xyxy[1])


def expected_plate(path: Path) -> Optional[str]:
    """The plate a filename promises, if it promises one."""
    stem = path.stem
    if stem.startswith("plate_") and "_" in stem[6:]:
        return stem.split("_", 2)[2].upper()
    return None


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
def bench_one(path: Path, repeats: int) -> Optional[Dict[str, object]]:
    found = plate_crop(path)
    if found is None:
        return None
    crop, px_w, px_h = found

    # One untimed pass each, so the first-call model warm-up is not counted as
    # the cost of the approach.
    old_read(crop)
    new_read(crop)

    old_runs = [old_read(crop) for _ in range(repeats)]
    new_runs = [new_read(crop) for _ in range(repeats)]

    old = _majority(old_runs)
    new = _majority(new_runs)
    # The per-character score of the weakest glyph, which is the number the
    # verdict's ambiguity rule actually turns on.
    per_char = new.get("char_confs") or ()
    new["weakest_char"] = round(min(per_char), 3) if per_char else None
    return {
        "file": path.name,
        "expected": expected_plate(path),
        "px": f"{px_w}x{px_h}",
        "old": old,
        "new": new,
        "old_ms": statistics.median(r["ms"] for r in old_runs),
        "new_ms": statistics.median(r["ms"] for r in new_runs),
    }


def _majority(runs: List[Dict[str, object]]) -> Dict[str, object]:
    """The reading that appeared most often; ties go to the higher confidence.

    Both readers are deterministic, so this is normally unanimous - it exists so a
    row never reports a reading that only happened on one of N runs.

    The full record of the winning run is returned, not just its string: the
    per-character detail (how many scores, how many ambiguous) belongs to one
    specific reading, and carrying it across from a different one would report
    character confidences for a string that was not read.
    """
    tally: Dict[Optional[str], float] = {}
    records: Dict[Optional[str], Dict[str, object]] = {}
    for r in runs:
        key = r["norm"]
        tally[key] = tally.get(key, 0.0) + float(r["conf"])
        if key not in records or float(r["conf"]) > float(records[key]["conf"]):
            records[key] = r
    if not tally:
        return {"norm": None, "conf": 0.0}
    winner = max(tally, key=lambda k: tally[k])
    out = dict(records[winner])
    out["norm"] = winner
    return out


def box_path_latency(path: Path, frames: int = 6) -> Dict[str, float]:
    """How long the green boxes take on a real frame.

    This is the number the ~150 ms budget is about, and the new hi-res path must
    not have moved it - so it is measured rather than assumed.
    """
    img_bytes = cv2.imencode(".jpg", cv2.imread(str(path)))[1].tobytes()
    from app.ws.scan_manager import VehicleTracker

    tracker = VehicleTracker()
    ls.infer_vehicles(img_bytes, tracker)  # warm
    timings = []
    for _ in range(frames):
        t0 = time.perf_counter()
        ls.infer_vehicles(img_bytes, tracker)
        timings.append((time.perf_counter() - t0) * 1000)
    timings.sort()
    return {
        "p50": statistics.median(timings),
        "p95": timings[int(len(timings) * 0.95) - 1] if len(timings) > 1 else timings[0],
        "min": timings[0],
        "max": timings[-1],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repeats", type=int, default=3, help="timed runs per crop")
    args = ap.parse_args()

    print("Loading models and warming the OCR engine...")
    warm()

    targets: List[Path] = []
    for d in (PLATE_DIR, GENERATED_DIR):
        if d.exists():
            targets.extend(sorted(p for p in d.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg"}))

    rows = []
    missing = []
    for path in targets:
        row = bench_one(path, args.repeats)
        if row is None:
            missing.append(path.name)
        else:
            rows.append(row)

    print()
    print("=" * 100)
    print("PLATE READING: old reader vs new reader")
    print("=" * 100)
    print(
        f"{'file':<28} {'expected':<12} {'plate px':>10} "
        f"{'old read':<13} {'new read':<13} {'verdict':<10} {'old ms':>8} {'new ms':>8}"
    )
    print("-" * 100)

    old_hits = new_hits = scoreable = 0
    for r in rows:
        exp = r["expected"]
        old_norm, new_norm = r["old"]["norm"], r["new"]["norm"]
        if exp:
            scoreable += 1
            old_ok = old_norm == exp
            new_ok = new_norm == exp
            old_hits += old_ok
            new_hits += new_ok
            verdict = "same" if old_ok == new_ok else ("FIXED" if new_ok else "BROKE")
        else:
            verdict = "-"
        print(
            f"{r['file']:<28} {exp or '(unnamed)':<12} {r['px']:>10} "
            f"{str(old_norm):<13} {str(new_norm):<13} {verdict:<10} "
            f"{r['old_ms']:>8.0f} {r['new_ms']:>8.0f}"
        )

    print("-" * 100)
    if scoreable:
        print(
            f"exact reads: old {old_hits}/{scoreable}  "
            f"new {new_hits}/{scoreable}  "
            f"({100 * old_hits / scoreable:.0f}% -> {100 * new_hits / scoreable:.0f}%)"
        )
    else:
        print("no filename-encoded expectations to score against")
    if missing:
        print(f"no plate detected in: {', '.join(missing)}")

    print()
    print("=" * 100)
    print("THE SIX NAMED PLATES")
    print("=" * 100)
    by_expected = {r["expected"]: r for r in rows if r["expected"]}
    for plate in NAMED_SIX:
        row = by_expected.get(plate)
        if row is None:
            print(f"{plate:<12} NOT PRESENT on disk - not run, not scored")
        else:
            ok_old = row["old"]["norm"] == plate
            ok_new = row["new"]["norm"] == plate
            print(
                f"{plate:<12} old {str(row['old']['norm']):<13} {'OK' if ok_old else 'MISS':<5} "
                f"new {str(row['new']['norm']):<13} {'OK' if ok_new else 'MISS'}"
            )

    print()
    print("=" * 100)
    print("PER-CHARACTER CONFIDENCE (new reader only)")
    print("=" * 100)
    print(f"{'file':<28} {'read':<13} {'chars':>6} {'weakest':>9} {'ambiguous':>10} {'winning variant':<18}")
    print("-" * 100)
    for r in rows:
        if r["new"].get("norm"):
            weakest = r["new"].get("weakest_char")
            print(
                f"{r['file']:<28} {str(r['new']['norm']):<13} {r['new'].get('per_char', 0):>6} "
                f"{(f'{weakest:.3f}' if weakest is not None else '-'):>9} "
                f"{r['new'].get('ambiguous', 0):>10} {r['new'].get('variant', ''):<18}"
            )

    print()
    print("=" * 100)
    print("GREEN-BOX PATH (the ~150 ms budget)")
    print("=" * 100)
    scene = PLATE_DIR / "road_scene.jpg"
    if scene.exists():
        lat = box_path_latency(scene)
        print(
            f"road_scene.jpg  decode + vehicle pass: "
            f"p50 {lat['p50']:.0f} ms   p95 {lat['p95']:.0f} ms   "
            f"(min {lat['min']:.0f}, max {lat['max']:.0f})"
        )
        print(
            "  This path does not touch the plate reader, the plate detector or "
            "the hi-res decode."
        )
    else:
        print("road_scene.jpg is not present; not measured")

    print()
    print("=" * 100)
    print("COST OF THE NEW READER")
    print("=" * 100)
    if rows:
        old_tot = sum(r["old_ms"] for r in rows)
        new_tot = sum(r["new_ms"] for r in rows)
        print(
            f"total across {len(rows)} crops: old {old_tot:.0f} ms -> "
            f"new {new_tot:.0f} ms  ({new_tot / old_tot:.2f}x)"
        )
        print(
            "All of this is on the OCR worker, off the hot path: the green boxes "
            "were already on the wire before the first crop was cut."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
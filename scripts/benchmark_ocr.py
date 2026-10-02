"""Accuracy benchmark for the ANPR pipeline.

Runs the full pipeline on every image in ``data/test_plates`` and
``data/generated`` and compares the read against the plate encoded in the
filename (``MH12AB1234.jpg`` -> expected ``MH12AB1234``).

Usage:
    python scripts/benchmark_ocr.py
    python scripts/benchmark_ocr.py --dirs data/test_plates --json report.json

Prints one line per image plus an overall pass rate. Uncertain reads are counted
as failures: a read we are not sure about must not be reported as a hit.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from ai.pipeline import detect_and_read  # noqa: E402
from app.services.plate import is_valid_plate, normalize_plate  # noqa: E402

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
DEFAULT_DIRS = [ROOT / "data" / "test_plates", ROOT / "data" / "generated"]
# "plate_3_MH12JK4567.png" as well as "MH12JK4567.jpg"
FILENAME_PLATE = re.compile(r"([A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4})")


def expected_plate(path: Path) -> Optional[str]:
    match = FILENAME_PLATE.search(path.stem.upper())
    return match.group(1) if match else None


def iter_images(dirs: List[Path]):
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() in IMAGE_SUFFIXES and not path.name.startswith("."):
                yield path


def main() -> int:
    ap = argparse.ArgumentParser(description="ANPR accuracy benchmark")
    ap.add_argument("--dirs", nargs="*", type=Path, default=DEFAULT_DIRS)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--verbose", action="store_true", help="also print the reason per image")
    args = ap.parse_args()

    images = list(iter_images(args.dirs))
    if not images:
        print("no images found - put photos in data/test_plates/ named after the plate, e.g. MH12AB1234.jpg")
        return 1

    rows = []
    passed = 0
    checked = 0
    started = time.time()

    header = f"{'image':<34} {'expected':<12} {'got':<12} {'conf':>5}  status"
    print(header)
    print("-" * len(header))
    for path in images:
        expected = expected_plate(path)
        reads = detect_and_read(str(path))
        det = reads[0] if reads else None
        got = det.plate if det else None
        confidence = det.confidence if det else 0.0
        uncertain = bool(det.uncertain) if det else True

        if expected is None:
            status = "SKIP (no plate in filename)"
        else:
            checked += 1
            # A read only counts as a pass when it is exact, whitelisted and not
            # flagged uncertain - an "uncertain but right" plate is still a miss.
            if got == expected and is_valid_plate(got) and not uncertain:
                passed += 1
                status = "PASS"
            elif got == expected and uncertain:
                status = "FAIL (right plate, uncertain)"
            else:
                status = "FAIL"
        rows.append(
            {
                "image": str(path),
                "expected": expected,
                "got": got,
                "confidence": confidence,
                "uncertain": uncertain,
                "status": status,
                "stage": det.stage if det else None,
                "reason": det.reason if det else None,
            }
        )
        print(f"{path.name:<34} {str(expected or '-'):<12} {str(got or '-'):<12} {confidence:>5.2f}  {status}")
        if args.verbose and det and det.reason:
            print(f"{'':<34} reason: {det.reason}")

    elapsed = time.time() - started
    rate = (passed / checked * 100.0) if checked else 0.0
    print("-" * len(header))
    print(f"pass {passed}/{checked} = {rate:.1f}%   ({elapsed:.1f}s, {elapsed / max(len(images), 1):.2f}s per image)")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"pass": passed, "checked": checked, "pass_rate": rate, "rows": rows}, indent=2), encoding="utf-8")
        print(f"report -> {args.json}")

    return 0 if (checked == 0 or passed == checked) else 2


if __name__ == "__main__":
    raise SystemExit(main())
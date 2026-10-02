"""Stage-by-stage plate read debugger.

Usage:
    python scripts/debug_plate.py <image> [<image> ...]

For every image it writes into ``data/debug_out/<name>/``:

    01_input.jpg           after EXIF rotation fix
    02_crop.jpg            the crop that went to OCR (+ detector/box/confidence)
    03_preprocessed.jpg    exactly what went into the OCR engine
    04_ocr_raw.jpg         deskewed rendering, when deskew kicked in
    debug_report.json      raw hits, merged candidates, normalize_plate verdicts

and prints the same information to stdout, so the stage where "MH" turned into
"HL" is visible without opening the images.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from ai import detector as detector_mod  # noqa: E402
from ai.detector import deskew_crop, detect_plate_region, preprocess_crop  # noqa: E402
from ai.ocr_engine import candidates, merge_line_hits, read_text  # noqa: E402
from ai.pipeline import detect_and_read, renderings_for  # noqa: E402
from app.services.plate import is_valid_plate, normalize_plate  # noqa: E402

OUT_ROOT = ROOT / "data" / "debug_out"


def load_image(path: Path) -> Optional["np.ndarray"]:
    """Read the file and apply EXIF orientation, the same way a phone frame needs."""
    data = np.fromfile(str(path), dtype="uint8")
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        return None
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as pil:
            fixed = ImageOps.exif_transpose(pil).convert("RGB")
            image = cv2.cvtColor(np.asarray(fixed), cv2.COLOR_RGB2BGR)
    except Exception as exc:  # PIL missing or no EXIF - cv2 is good enough
        print(f"  (EXIF: not applied, {exc})")
    return image


def _fmt_box(hit) -> str:
    if not hit.box:
        return "no box"
    xs = [p[0] for p in hit.box]
    ys = [p[1] for p in hit.box]
    return f"x[{min(xs):.0f}-{max(xs):.0f}] y[{min(ys):.0f}-{max(ys):.0f}]"


def debug_image(path: Path, out_root: Path = OUT_ROOT) -> dict:
    name = path.stem
    out_dir = out_root / name
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print(f"IMAGE  {path}")
    image = load_image(path)
    if image is None:
        print("  cannot decode image")
        return {"image": str(path), "error": "cannot decode"}

    height, width = image.shape[:2]
    print(f"  01_input        {width}x{height}px (EXIF rotation applied)")
    cv2.imwrite(str(out_dir / "01_input.jpg"), image, [int(cv2.IMWRITE_JPEG_QUALITY), 92])

    report: dict = {
        "image": str(path),
        "size": [width, height],
        "detector_events": [],
        "stages": {},
    }

    detector_mod.reset_events()
    region = detect_plate_region(image)
    report["detector_events"] = list(detector_mod.DETECTOR_EVENTS)
    for line in report["detector_events"]:
        print(f"      detector | {line}")

    if region is None:
        print("  02_crop         NO PLATE REGION FOUND")
        report["no_region"] = True
    else:
        crop = region.crop
        print(
            f"  02_crop         detector={region.source} box={region.raw_box} "
            f"conf={'n/a (contours)' if region.confidence != region.confidence else round(region.confidence, 3)} "
            f"-> {crop.shape[1]}x{crop.shape[0]}px"
        )
        report["stages"]["crop"] = {
            "detector": region.source,
            "raw_box": list(region.raw_box),
            "padded_box": list(region.box),
            "confidence": None if region.confidence != region.confidence else round(region.confidence, 3),
            "crop_size": [crop.shape[1], crop.shape[0]],
        }
        cv2.imwrite(str(out_dir / "02_crop.jpg"), crop, [int(cv2.IMWRITE_JPEG_QUALITY), 95])

        deskewed, angle = deskew_crop(crop)
        preprocessed = preprocess_crop(crop)
        print(f"  03_preprocessed {preprocessed.shape[1]}x{preprocessed.shape[0]}px (deskew {angle:.2f} deg)")
        report["stages"]["preprocessed"] = {
            "deskew_deg": round(angle, 2),
            "size": [preprocessed.shape[1], preprocessed.shape[0]],
        }
        cv2.imwrite(str(out_dir / "03_preprocessed.jpg"), preprocessed, [int(cv2.IMWRITE_JPEG_QUALITY), 95])

        # Exactly the renderings the pipeline feeds to OCR, straight from the code.
        renderings = [(stage, variant) for stage, _source, variant in renderings_for(image)]
        for stage_name, variant in renderings:
            safe = stage_name.replace("crop_", "").replace("_", "")
            cv2.imwrite(str(out_dir / f"03_{safe}.jpg"), variant, [int(cv2.IMWRITE_JPEG_QUALITY), 95])

        for stage_name, image_in in renderings:
            hits = read_text(image_in)
            print(f"\n  -- {stage_name}: {len(hits)} raw RapidOCR fragment(s)")
            raw_rows = []
            for hit in hits:
                norm = normalize_plate(hit.text, ocr_score=hit.score)
                print(
                    f"     text={hit.text!r:<14} conf={hit.score:.3f} "
                    f"box={_fmt_box(hit)} -> {norm.normalized!r} valid={norm.valid}"
                )
                raw_rows.append(
                    {
                        "text": hit.text,
                        "conf": round(hit.score, 4),
                        "box": _fmt_box(hit),
                        "normalized": norm.normalized,
                        "valid": norm.valid,
                        "reason": norm.reason,
                    }
                )
            report["stages"].setdefault("raw_hits", {})[stage_name] = raw_rows

            merged = merge_line_hits(hits)
            print(f"  -- merged line candidates ({len(merged)}):")
            merged_rows = []
            for hit in merged:
                print(
                    f"     {hit.text!r:<20} conf={hit.score:.3f} -> {hit.normalized!r} "
                    f"valid={hit.valid_plate}  [{hit.reason}]"
                )
                merged_rows.append(
                    {
                        "text": hit.text,
                        "conf": round(hit.score, 4),
                        "normalized": hit.normalized,
                        "valid": hit.valid_plate,
                        "reason": hit.reason,
                    }
                )
            report["stages"].setdefault("merged", {})[stage_name] = merged_rows

            all_candidates = candidates(hits)
            print(f"  -- all candidates ({len(all_candidates)}):")
            for hit in all_candidates:
                mark = "OK " if is_valid_plate(hit.normalized) else "   "
                print(f"     {mark}{hit.text!r:<20} -> {hit.normalized!r} valid={hit.valid_plate}")
            report["stages"].setdefault("candidates", {})[stage_name] = [
                {"text": h.text, "conf": round(h.score, 4), "normalized": h.normalized, "valid": h.valid_plate, "reason": h.reason}
                for h in all_candidates
            ]

    print("\n  -- full frame OCR")
    frame_hits = read_text(image)
    for hit in frame_hits:
        print(f"     text={hit.text!r:<14} conf={hit.score:.3f} -> {hit.normalized!r} valid={hit.valid_plate}")
    report["stages"]["fullframe_hits"] = [
        {"text": h.text, "conf": round(h.score, 4), "normalized": h.normalized, "valid": h.valid_plate, "reason": h.reason}
        for h in frame_hits
    ]

    print("\n  -- normalize_plate() variants")
    variants_seen: List[str] = list({h.text for h in frame_hits})
    if region is not None:
        variants_seen += [h.text for h in merge_line_hits(read_text(region.crop))]
    norm_rows = []
    for raw in dict.fromkeys(variants_seen):
        norm = normalize_plate(raw)
        print(f"     {raw!r:<22} -> {norm.normalized!r:<14} valid={norm.valid} reason={norm.reason}")
        norm_rows.append({"raw": raw, "normalized": norm.normalized, "valid": norm.valid, "reason": norm.reason})
    report["stages"]["normalize"] = norm_rows

    print("\n  -- FINAL")
    reads = detect_and_read(image)
    if not reads:
        print("     no plate found")
        report["final"] = None
    else:
        det = reads[0]
        report["final"] = det.as_dict()
        flag = " [UNCERTAIN]" if det.uncertain else ""
        print(
            f"     {det.plate!r} conf={det.confidence} valid={det.valid}{flag} "
            f"via={det.source}/{det.stage}"
        )
        print(f"     reason: {det.reason}")
        print(f"     votes: {det.votes}")

    with open(out_dir / "debug_report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(f"  saved -> {out_dir}")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage-by-stage ANPR read debugger")
    ap.add_argument("images", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=OUT_ROOT)
    args = ap.parse_args()

    for path in args.images:
        if not path.exists():
            print(f"skip {path}: not found")
            continue
        debug_image(path, args.out)


if __name__ == "__main__":
    main()
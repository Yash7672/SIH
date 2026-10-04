#!/usr/bin/env python
"""Render the live-scan boxes onto an image, exactly as the phone draws them.

Why this exists: the overlay is the part of the feature that cannot be asserted
from a test. A green box three pixels to the left of the car looks perfectly
plausible in a JSON payload and completely wrong on a phone. So this runs the
SAME server code the WebSocket runs - the real decoder, the real vehicle model,
the real plate detector, the real tracker and the real OCR - and draws the result
with the same colours, widths and chip text the app uses.

    python scripts/render_live_view.py
    python scripts/render_live_view.py --image data/test_plates/road_scene.jpg
    python scripts/render_live_view.py --repeat 3 --motion 0.04

Images land in data/debug_out/. Nothing is written outside that directory and
nothing on the server is changed; run with --hotlist to see the stolen banner
path as well.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
for candidate in (REPO_ROOT, REPO_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault("RAKSHAK_DEMO_MODE", "1")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

# The app's palette. These are the literal values in
# mobile_app/src/components/liveOverlayMath.js; if one changes, change both or
# the render stops being evidence about what the phone draws.
VEHICLE_COLOR = (76, 197, 34)  # #22C55E in BGR
PLATE_COLOR = (31, 31, 255)  # #FF1F1F in BGR
VEHICLE_WIDTH = 3
PLATE_WIDTH = 3
STOLEN_COLOR = (31, 31, 255)

DEFAULT_IMAGE = REPO_ROOT / "data" / "test_plates" / "road_scene.jpg"
OUT_DIR = REPO_ROOT / "data" / "debug_out"

# Pillow is not a project dependency and is not needed here: OpenCV draws the
# rectangles, and the chips are drawn with cv2's Hershey fonts so this script
# runs with exactly the packages the backend already has.


def _load_backend():
    """Import the live-scan pieces lazily so --help works without torch."""
    from app.api.v1.live_scan import infer_plates, infer_vehicles, run_ocr
    from app.ws.scan_manager import ScanConnection, VehicleTracker

    return infer_vehicles, infer_plates, run_ocr, ScanConnection, VehicleTracker


def _chips(img, items, scale: float = 0.45):
    """Draw a filled chip with dark bold text, like the app's chip."""
    for item in items:
        text = item.get("chip") or ""
        if not text:
            continue
        (tw, th), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
        pad = 4
        bw, bh = tw + pad * 2, th + baseline + pad
        x = int(round(item["chip_x"]))
        y = int(round(item["chip_y"]))
        H, W = img.shape[:2]

        if item.get("chip_below"):
            # Below the box.
            top = y
            if top + bh > H:
                top = max(0, H - bh)
            left = min(max(0, x), max(0, W - bw))
        else:
            # Above the box: sit on the bottom edge of the chip.
            top = y - bh
            if top < 0:
                top = min(H - bh, max(0, y))
            left = min(max(0, x), max(0, W - bw))

        cv2.rectangle(img, (left, top), (left + bw, top + bh), item["color_bgr"], -1)
        cv2.putText(
            img,
            text,
            (left + pad, top + th + 1),
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            item["text_bgr"],
            2,
            cv2.LINE_AA,
        )
        item["chip_rect"] = (left, top, bw, bh)


def _to_px(box, width: int, height: int) -> tuple[int, int, int, int]:
    """Normalised box -> pixel box, x against the width and y against the height.

    Both axes scale independently. Treating the box as one flat list against one
    number was a real bug here: on a 960x632 frame it stretched every box 1.52x
    down the image, which still looked like a plausible rectangle.
    """
    return (
        int(round(box[0] * width)),
        int(round(box[1] * height)),
        int(round(box[2] * width)),
        int(round(box[3] * height)),
    )


def render(
    jpeg: bytes,
    tracker,
    infer_vehicles,
    infer_plates,
    run_ocr,
    label: str,
    hotlisted: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Run one frame through the server pipeline and draw what the phone draws.

    `hotlisted` is the active hot-list. The WebSocket decides `stolen` after OCR by
    asking the hot-list, so passing the same set here is what makes this render the
    stolen path (thicker box) rather than only the ordinary one.
    """
    # The two real stages, called exactly as the WebSocket handler calls them, so
    # the boxes come from the code path that actually serves the phone.
    bgr, w, h, vehicle_ms, vehicles, tracks = infer_vehicles(jpeg, tracker)
    plates, crops = infer_plates(bgr, w, h, tracks, tracker)

    canvas = bgr.copy()
    H, W = canvas.shape[:2]

    items: list[dict[str, Any]] = []
    for v in vehicles:
        x0, y0, x1, y1 = _to_px(v["box"], W, H)
        cv2.rectangle(canvas, (x0, y0), (x1, y1), VEHICLE_COLOR, VEHICLE_WIDTH)
        items.append(
            {
                "kind": "vehicle",
                "label": v.get("label"),
                "chip": v.get("label"),
                "track": v.get("track"),
                "conf": v.get("conf"),
                "color_bgr": VEHICLE_COLOR,
                "text_bgr": (15, 23, 16),
                "chip_x": x0,
                "chip_y": y0 - 2,
                "chip_below": False,
            }
        )

    reads: dict[int, dict[str, Any]] = {}
    for tid, crop in crops.items():
        read = run_ocr(crop)
        if read is None:
            continue
        reads[tid] = read

    for p in plates:
        x0, y0, x1, y1 = _to_px(p["box"], W, H)
        read = reads.get(p["track"])
        pct = round(read["conf"] * 100) if read else None
        chip = (
            f"{read['norm']} {pct}%" if read and read.get("norm") else "reading..."
        )
        stolen = bool(read and read.get("valid") and read.get("norm") in hotlisted)
        color = STOLEN_COLOR if stolen else PLATE_COLOR
        thickness = PLATE_WIDTH + 2 if stolen else PLATE_WIDTH
        cv2.rectangle(canvas, (x0, y0), (x1, y1), color, thickness)
        items.append(
            {
                "kind": "plate",
                "label": read.get("norm") if read else None,
                "chip": chip,
                "track": p.get("track"),
                "conf": read["conf"] if read else None,
                "color_bgr": color,
                "text_bgr": (255, 255, 255),
                "chip_x": x0,
                # Below the box when there is room, above it when there is not -
                # the same rule the overlay uses.
                "chip_y": (y1 + 2) if (y1 + 22) <= H else (y0 - 2),
                "chip_below": (y1 + 22) <= H,
            }
        )

    _chips(canvas, items)

    # A legend so a screenshot is self-explanatory.
    banner = f"{label}  {W}x{H}  {time.strftime('%H:%M:%S')}"
    cv2.rectangle(canvas, (0, 0), (canvas.shape[1], 26), (0, 0, 0), -1)
    cv2.putText(
        canvas, banner, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA
    )

    out = OUT_DIR / f"{label}.jpg"
    cv2.imwrite(str(out), canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    return {
        "image": str(out.relative_to(REPO_ROOT)),
        "vehicle_ms": vehicle_ms,
        "vehicles": vehicles,
        "plates": plates,
        "reads": reads,
    }


def _shift(jpeg: bytes, dx: float, dy: float) -> bytes:
    """Pan the frame, so a repeat run exercises the tracker rather than one pose.

    The overlay extrapolates from two positions. A single frame cannot show that
    working, so --motion walks the same scene across several frames and the
    saved images are the sequence a moving car would produce.
    """
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jpeg
    h, w = img.shape[:2]
    m = np.float32([[1, 0, dx * w], [0, 1, dy * h]])
    # BORDER_REPLICATE rather than black: a black band would be a real detection
    # difference, not a rendering artefact.
    out = cv2.warpAffine(img, m, (w, h), borderMode=cv2.BORDER_REPLICATE)
    ok, buf = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return buf.tobytes() if ok else jpeg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", type=Path, default=DEFAULT_IMAGE, help="frame to render (default: the road-scene fixture)")
    ap.add_argument("--repeat", type=int, default=1, help="how many frames to render")
    ap.add_argument("--motion", type=float, default=0.0, help="per-frame pan, as a fraction of the frame")
    ap.add_argument("--out", type=Path, default=OUT_DIR, help="output directory")
    ap.add_argument("--no-ocr", action="store_true", help="skip the OCR stage (boxes only)")
    ap.add_argument("--hotlist", action="store_true", help="flag any plate the demo hotlist knows about")
    ap.add_argument("--clean", action="store_true", help="empty the output directory first")
    args = ap.parse_args()

    if not args.image.exists():
        print(f"no image at {args.image}", file=sys.stderr)
        print("build the fixture with: python scripts/make_test_road_scene.py", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    if args.clean:
        for old in args.out.glob("live_view_*.jpg"):
            old.unlink()

    infer_vehicles, infer_plates, run_ocr, ScanConnection, VehicleTracker = _load_backend()

    jpeg = args.image.read_bytes()
    print(f"image   {args.image.relative_to(REPO_ROOT)}  {len(jpeg) // 1024} KB")
    print("loading models (first run pays the YOLO + OCR warm-up)...")
    t0 = time.perf_counter()

    conn = ScanConnection()
    conn.tracker = VehicleTracker()
    hotlisted: set[str] = set()
    if args.hotlist:
        try:
            from sqlalchemy import select

            from app.db.session import SessionLocal
            from app.models import Hotlist, HotlistStatus

            db = SessionLocal()
            try:
                hotlisted = set(
                    db.execute(
                        select(Hotlist.plate).where(
                            Hotlist.status.in_([HotlistStatus.ACTIVE, HotlistStatus.FIR_CONFIRMED])
                        )
                    )
                    .scalars()
                    .all()
                )
            finally:
                db.close()
            print(f"hotlist {len(hotlisted)} active plate(s): {', '.join(sorted(hotlisted)) or '-'}")
        except Exception as exc:  # noqa: BLE001
            print(f"could not read the hotlist: {exc}")

    results = []
    for i in range(max(1, args.repeat)):
        frame = jpeg
        if args.motion:
            frame = _shift(jpeg, args.motion * i, 0)
        label = f"live_view_{i:02d}"
        result = render(
            frame,
            conn.tracker,
            infer_vehicles,
            infer_plates,
            (lambda c: None) if args.no_ocr else run_ocr,
            label,
            frozenset(hotlisted),
        )
        results.append(result)

    print(f"warmed in {time.perf_counter() - t0:.1f}s")

    for result in results:
        print(f"\n{result['image']}  (vehicles stage {result['vehicle_ms']} ms)")
        if not result["vehicles"]:
            print("  no vehicles detected")
        for v in result["vehicles"]:
            print(
                f"  green  {v.get('label'):>7} track={v.get('track'):<3} "
                f"conf={v.get('conf'):.2f}  box={[round(c, 3) for c in v['box']]}"
            )
        if not result["plates"]:
            print("  no plates detected")
        for p in result["plates"]:
            read = result["reads"].get(p["track"])
            text = read["norm"] if read else "-"
            conf = f"{read['conf']:.3f}" if read else "-"
            flag = "  <-- HOTLISTED" if text in hotlisted else ""
            print(
                f"  red    track={p['track']:<3} detector_conf={p['conf']:.2f}  "
                f"ocr={text} @ {conf}{flag}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""Generate a synthetic road-scene demo video with a sliding number plate.

Usage: python scripts/generate_demo_video.py [--out data/generated/demo_road.mp4] [--plate TS09AB1234]
Renders a dark road strip with a plate entering frame and drifting slowly,
saved as an H.264-compatible .mp4 via OpenCV.
"""

import argparse
import os
import sys

import cv2
import numpy as np

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from scripts.generate_plate_images import render_plate  # reuse the plate renderer


def build_plate_image(plate: str, width: int = 460) -> np.ndarray:
    tmp = "data/generated/_plate_tmp.png"
    os.makedirs("data/generated", exist_ok=True)
    import random

    # reuse the renderer with a stable seed-free spec (width, rotation, blur, noise)
    render_plate(plate, tmp, width, 0.0, 0.0, 0)
    bgr = cv2.imread(tmp)
    os.remove(tmp)
    return bgr


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/generated/demo_road.mp4")
    ap.add_argument("--plate", default="TS09AB1234")
    ap.add_argument("--width", type=int, default=460)
    ap.add_argument("--height", type=int, default=260)
    ap.add_argument("--frames", type=int, default=180)
    ap.add_argument("--fps", type=int, default=24)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    plate = build_plate_image(args.plate, args.width)

    writer = cv2.VideoWriter(args.out, cv2.VideoWriter_fourcc(*"mp4v"), args.fps, (args.width + 200, args.height))

    x_start = 0
    x_end = args.width
    plate_h, plate_w = plate.shape[:2]
    for i in range(args.frames):
        # linear drift right, then wrap (looping dashcam feel)
        t = (i % (args.frames // 2)) / (args.frames // 2)
        x = int(x_start + (x_end - x_start) * t)
        bg = np.full((args.height, args.width + 200, 3), (18, 22, 30), dtype=np.uint8)

        y = int(args.height * 0.38)
        if y + plate_h > args.height:
            crop_h = args.height - y
        else:
            crop_h = plate_h
        ph = plate[:crop_h]
        ph_w = ph.shape[1]

        x0 = max(x, 0)
        y0 = y
        x1 = min(x + ph_w, bg.shape[1])
        if x1 > x0:
            px0 = x0 - x
            px1 = px0 + (x1 - x0)
            bg[y0 : y0 + ph.shape[0], x0:x1] = ph[:, px0:px1]

        # synthetic headlight glow + noise for "camera" feel
        glow = int(60 * (1 - abs(t - 0.5) * 2))
        overlay = bg * 0.85 + np.full_like(bg, glow) * 0.15 if glow > 0 else bg
        noise = np.random.normal(0, 3, overlay.shape).astype(np.int16)
        frame = np.clip(overlay.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        writer.write(frame)

    writer.release()
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
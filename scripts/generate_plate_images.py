"""Generate synthetic Indian number-plate images for testing/demo the ANPR pipeline.

Usage:  python scripts/generate_plate_images.py [--out data/generated] [--plates TS09AB1234 MH12JK4567 ...]
Default: renders a handful of plates at varying sizes/rotations/noise into data/generated/.
"""

import argparse
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

random.seed(7)

TOP_SERIES = {
    "TS09AB1234",
    "MH12JK4567",
    "KA01MJ9801",
    "DL8CBF4890",
    "UP32XA1010",
}


def font(size: int):
    for path in ("courbd.ttf", "arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_plate(plate: str, out_path: str, width: int, rotation: float, blur: int, noise: int) -> None:
    text = plate[:2] + " " + plate[2:4] + " " + plate[4:6] + " " + plate[6:]
    font_size = int(width * 0.18)
    fnt = font(font_size)

    dummy = Image.new("RGB", (1, 1))
    dd = ImageDraw.Draw(dummy)
    tw, th = dd.textbbox((0, 0), text, font=fnt)[2:4]
    pad_x, pad_y = int(width * 0.12), int(font_size * 0.5)
    img = Image.new("RGB", (tw + 2 * pad_x, th + 2 * pad_y), (255, 255, 255))
    d = ImageDraw.Draw(img)

    outer = "black"
    border = 4
    d.rectangle([0, 0, img.width - 1, img.height - 1], outline=outer, width=border)

    band = int(img.height * 0.16)
    d.rectangle([0, 0, img.width - 1, band - 1], fill="#7fc8ff")
    d.rectangle([0, img.height - band, img.width - 1, img.height - 1], fill="#7fc8ff")

    d.rectangle([border, band, img.width - 1 - border, img.height - 1 - band], outline=outer, width=2)

    # India registration mark (simplified) — reserve its horizontal space first
    rw, rh = int(font_size * 0.9), int(font_size * 0.9)
    rx, ry = img.width - rw - 20, (img.height - rh) // 2
    reserve = rw + 40
    d.rounded_rectangle([rx, ry, rx + rw, ry + rh], radius=6, fill="#1a4d1c")
    d.text((rx + rw // 2, ry + rh // 2 - 4), "IND", fill="white", font=fnt)
    d.text((rx + rw // 2, ry + rh // 2 + 8), "2026", fill="white", font=font(int(font_size * 0.45)))

    # Plate text centered in the region left of the registration mark
    text_area_left = pad_x
    text_area_right = img.width - reserve - pad_x
    tx = text_area_left + (text_area_right - tw - text_area_left) / 2
    d.text((max(tx, 0), pad_y), text, fill="black", font=fnt)

    noise_level = noise
    if noise_level:
        noise_layer = Image.effect_noise(img.size, noise_level).convert("RGB")
        img = Image.blend(img, noise_layer, 0.03)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    if rotation:
        img = img.rotate(rotation, expand=True, fillcolor=(200, 200, 200))

    img.save(out_path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/generated")
    ap.add_argument("--plates", nargs="*", default=None)
    args = ap.parse_args()

    plates = args.plates or sorted(TOP_SERIES)
    import os

    os.makedirs(args.out, exist_ok=True)

    specs = [
        (520, 2.0, 0.0, 0),
        (560, -2.0, 0.2, 4),
        (680, 2.0, 0.4, 3),
        (460, 0.0, 0.0, 0),
    ]
    for i, plate in enumerate(plates):
        w, rot, blur, noise = specs[i % len(specs)]
        out = os.path.join(args.out, f"plate_{i + 1}_{plate}.png")
        render_plate(plate, out, w, rot, blur, noise)
        print(out)


if __name__ == "__main__":
    main()
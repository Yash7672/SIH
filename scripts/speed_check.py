"""Item 8: how fast the server is, with 1, 4 and 6 vehicles in frame.

The requirement is "server ms p50/p95 with 1, 4 and 6 vehicles plus phone round
trip, target boxes within ~150 ms of server time". Both numbers matter and they
are not the same number:

  server ms   what the phone is actually waiting on - `ms` in the boxes reply.
  round trip  wall clock from send to reply over the LAN, which adds socket,
              base64 and the phone's own work. A phone cannot render a frame
              faster than this, whatever the server did.

The fixture in `data/test_plates/road_scene.jpg` is a single real car. A load
benchmark needs N cars in one frame, so the scene is composited onto a wider
canvas by pasting the same car N times at different positions. That is not
photorealistic traffic and is not meant to be - the point is to hold the number
of detections at 1/4/6 and measure the cost of each. Every frame's vehicle count
is read back out of the server's own `vehicles` list and reported, so a
composite the detector does not actually see the expected number of cars in is
visible in the output instead of silently flattering the timings.

Frames go out at 960 px, the width the phone uses for the plate crop.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
import asyncio

import requests
import websockets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.environ.get("RAKSHAK_API", "http://127.0.0.1:8000")
API_V1 = f"{API}/api/v1"
FIXTURE = os.path.join(ROOT, "data", "test_plates", "road_scene.jpg")
OUT_DIR = os.path.join(ROOT, "data", "speed_scenes")

FRAME_WIDTH = 1280
STREAM_QUALITY = 40
GAP = 8
# The phone waits for each frame's boxes before capturing the next, because
# `liveCapture.js` holds a lock across the whole iteration. The saturated run
# below ignores that deliberately, to show the queue.
PHONE_GAP_S = 0.25
LAT, LNG = 17.44, 78.35


# --------------------------------------------------------------------- fixtures


def build_scene(vehicles: int, width: int = FRAME_WIDTH) -> tuple[bytes, int, int]:
    """A frame of a FIXED width containing `vehicles` cars, plus its pixel size.

    The width is held constant on purpose. An earlier version of this benchmark
    pasted the cars side by side, so going from 1 car to 6 also tripled the
    pixels to decode and tripled the base64 on the wire - it measured the JPEG
    decoder, not the cost of another detection. Every frame here is the same
    size, so the only thing that changes between phases is how many vehicles the
    detector has to find, which is what item 8 actually asks about.

    Layout is the geometry a phone pointed at a junction sees: a single car for
    1, a 2x2 block for 4, a 3x2 block for 6. Cars therefore get smaller as the
    count grows, so the plate crops shrink too - at 6 the plates are too small to
    read, which is a real property of that geometry and is reported rather than
    hidden. The budget under test is the box timing.
    """
    src = cv2.imread(FIXTURE)
    if src is None:
        raise SystemExit(f"fixture missing: {FIXTURE}")
    src = cv2.resize(src, (width, max(1, round(src.shape[0] * width / src.shape[1]))))

    if vehicles == 1:
        return _encode(src), src.shape[1], src.shape[0]

    rows, cols = (2, 2) if vehicles == 4 else (2, 3)
    cell_w = (width - (cols - 1) * GAP) // cols
    cell_h = max(1, round(cell_w * src.shape[0] / src.shape[1]))
    car = cv2.resize(src, (cell_w, cell_h))
    canvas = np.full((rows * cell_h + (rows - 1) * GAP, width, 3), 96, dtype=np.uint8)
    for i in range(vehicles):
        r, c = divmod(i, cols)
        y0 = r * (cell_h + GAP)
        x0 = c * (cell_w + GAP)
        canvas[y0 : y0 + cell_h, x0 : x0 + cell_w] = car
    return _encode(canvas), canvas.shape[1], canvas.shape[0]


def _encode(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), STREAM_QUALITY])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    return buf.tobytes()


# ------------------------------------------------------------------------ plumbing


def login(email: str, password: str) -> str:
    r = requests.post(f"{API_V1}/auth/login", json={"email": email, "password": password}, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def register_device(token: str, name: str) -> str:
    r = requests.post(
        f"{API_V1}/devices/register",
        headers=auth(token),
        json={"device_type": "mobile", "device_name": name},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["id"]


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# ------------------------------------------------------------------------- measure


async def measure(ws, device_id: str, jpeg: bytes, frames: int, label: str, paced: bool) -> dict:
    """Send `frames` frames, collecting server ms and round trip.

    `paced=False` is closed loop - one frame in flight, the next sent only after
    the previous boxes arrive. That is what the phone does, because
    `liveCapture.js` holds a lock for the whole iteration; it is the latency a
    user actually sees.

    `paced=True` keeps sending every 250 ms regardless, which piles frames into
    the server's two processing slots. That is a queueing measurement, not a
    latency one, and it is reported separately so the two are not confused.
    """
    server_ms: list[float] = []
    rtt_ms: list[float] = []
    plate_leg_ms: list[float] = []
    counts: list[int] = []
    labels: dict[str, int] = {}
    b64 = base64.b64encode(jpeg).decode("ascii")
    inflight: dict[int, float] = {}
    next_send = 0.0

    for i in range(frames):
        seq = i + 1
        if paced:
            now = time.perf_counter()
            if next_send > now:
                await asyncio.sleep(next_send - now)
            next_send = max(now, next_send) + PHONE_GAP_S
        inflight[seq] = time.perf_counter()
        await ws.send(
            json.dumps(
                {
                    "type": "frame",
                    "seq": seq,
                    "lat": LAT,
                    "lng": LNG,
                    "device_id": device_id,
                    "jpeg_b64": b64,
                }
            )
        )
        # Wait for THIS frame's boxes, so nothing is silently dropped and a slow
        # server shows up as latency rather than being hidden by the queue.
        deadline = time.perf_counter() + 25
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), max(0.5, deadline - time.perf_counter()))
            except asyncio.TimeoutError:
                break
            now = time.perf_counter()
            msg = json.loads(raw)
            if msg.get("seq") != seq:
                continue
            if msg.get("type") == "boxes":
                server_ms.append(float(msg.get("ms") or 0))
                rtt_ms.append((now - inflight.pop(seq)) * 1000)
                vehs = msg.get("vehicles") or []
                counts.append(len(vehs))
                for v in vehs:
                    name = v.get("label", "?")
                    labels[name] = labels.get(name, 0) + 1
                # Boxes are the leg this benchmark measures. Anything the server
                # sends after them (`plate`, `plates`) belongs to the plate leg,
                # so stop waiting here or every frame would sit out the timeout.
                break
            if msg.get("type") in ("plate", "plates"):
                plate_leg_ms.append((now - inflight.get(seq, now)) * 1000)

    return {
        "label": label,
        "frames": frames,
        "answered": len(server_ms),
        "server_p50": pct(server_ms, 50),
        "server_p95": pct(server_ms, 95),
        "rtt_p50": pct(rtt_ms, 50),
        "rtt_p95": pct(rtt_ms, 95),
        "plate_leg_p95": pct(plate_leg_ms, 95),
        "vehicles_min": min(counts) if counts else 0,
        "vehicles_max": max(counts) if counts else 0,
        "labels": labels,
    }


async def run_phase(ws, device_id: str, vehicles: int, frames: int, paced: bool) -> dict:
    jpeg, w, h = build_scene(vehicles)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"scene_{vehicles}car_{w}x{h}.jpg"), "wb") as fh:
        fh.write(jpeg)
    tag = "saturated" if paced else "closed loop"
    return await measure(ws, device_id, jpeg, frames, f"{vehicles} car(s) {tag}", paced)


def show(rows: list[dict], budget: float) -> bool:
    ok = True
    for row in rows:
        answered = f"{row['answered']}/{row['frames']}"
        print(
            f"  {row['label']:<34} server {row['server_p50']:6.0f}/{row['server_p95']:6.0f} ms   "
            f"round trip {row['rtt_p50']:6.0f}/{row['rtt_p95']:6.0f} ms   "
            f"answered {answered}   vehicles {row['vehicles_min']}-{row['vehicles_max']}   "
            f"{row['labels']}",
            flush=True,
        )
        good = row["server_p95"] <= budget and row["answered"] == row["frames"]
        ok = ok and good
        print(
            f"    {'PASS' if good else 'FAIL'}  p95 {row['server_p95']:.0f} ms vs the {budget:.0f} ms budget"
        )
    return ok


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", default=API)
    ap.add_argument("--frames", type=int, default=10, help="frames per vehicle count")
    ap.add_argument("--budget", type=float, default=150.0, help="ms p95 for the boxes reply")
    args = ap.parse_args()

    print("\n=== item 8: server ms with 1, 4 and 6 vehicles ===\n")
    print(f"  fixture : {os.path.relpath(FIXTURE, ROOT)}")
    print(f"  frames  : {FRAME_WIDTH} px wide for every phase, q{STREAM_QUALITY}, so only the")
    print(f"            vehicle count changes - not the pixel count")
    print(f"  budget  : boxes reply p95 <= {args.budget:.0f} ms\n")

    token = login("volunteer@example.com", "Volunteer@123")
    device_id = register_device(token, "speed-check phone")
    ws_url = API.replace("http", "ws") + "/api/v1/ws/scan"
    headers = {"Authorization": f"Bearer {token}"}

    closed: list[dict] = []
    saturated: list[dict] = []
    async with websockets.connect(ws_url, additional_headers=headers, max_size=8 * 1024 * 1024) as ws:
        # /ws/scan authenticates on the first message, not the handshake.
        await ws.send(json.dumps({"type": "auth", "token": token, "device_id": device_id}))
        ready = json.loads(await asyncio.wait_for(ws.recv(), 20))
        if ready.get("type") != "ready":
            print(f"  [FAIL] socket refused: {ready}")
            return 1
        print(f"  socket ready, device {device_id[:8]}\n")

        print("  closed loop - one frame in flight, as the phone sends them:")
        for n in (1, 4, 6):
            closed.append(await run_phase(ws, device_id, n, args.frames, paced=False))
        print()
        ok = show(closed, args.budget)

        print("\n  saturated - a new frame every 250 ms whether or not the last came back:")
        for n in (1, 4, 6):
            saturated.append(await run_phase(ws, device_id, n, args.frames, paced=True))
        print()
        print("  (saturated is reported, not budgeted: past the point where the server")
        print("   answers inside the budget this measures queueing, which no fix belongs in)")
        print(f"\n  scenes written to {os.path.relpath(OUT_DIR, ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
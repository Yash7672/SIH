"""Is the box reply slowed by a stationary car, or only by how many cars there are?

Two measurements from the same process, same model, same machine, both at the
phone's real rate of about 4 frames a second, which is what
`mobile_app/src/services/liveCapture.js` actually produces:

  moving    `live_scan_test.py`'s stream: each frame is nudged a few pixels, so
            every track is new and settles or leaves on its own.
  static    the identical frame 40 times, which is what a phone sees when it is
            pointed at a car stopped at a light.

If the static case is worse, the extra time is not the cost of more vehicles -
it is the plate leg: a car that does not move keeps producing plate crops, and
OCR is the slowest thing in the system (~2 s per crop on this CPU). The two
legs share `torch.set_num_threads(2)`, so OCR competes with the pass the phone
renders.

Nothing here waits for a reply before sending the next frame, so a backlog shows
up as box latency instead of being hidden by the send loop. Every reply is
recorded with its arrival time and type.

Run: python scripts/probe_static_scene.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from collections import Counter

import cv2
import numpy as np
import requests
import websockets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.environ.get("RAKSHAK_API", "http://127.0.0.1:8000")
API_V1 = f"{API}/api/v1"
FIXTURE = os.path.join(ROOT, "data", "test_plates", "road_scene.jpg")

FPS = 4.0
FRAMES = 40


def pct(values, p):
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def jpeg(img) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 40])
    return buf.tobytes()


def load_scene():
    src = cv2.imread(FIXTURE)
    if src is None:
        raise SystemExit(f"fixture missing: {FIXTURE}")
    return cv2.resize(src, (1280, round(src.shape[0] * 1280 / src.shape[1])))


def moving_frames(scene):
    """`live_scan_test.py`'s stream: the car walks across the frame."""
    out = []
    for i in range(FRAMES):
        dx = int(2 * i) % 90
        canvas = np.full_like(scene, 96)
        h, w = scene.shape[:2]
        x0 = min(dx, w - 1)
        canvas[:, x0 : x0 + w - dx] = scene[:, : w - dx]
        out.append(jpeg(canvas))
    return out


def static_frames(scene):
    return [jpeg(scene)] * FRAMES


async def stream(ws, device_id, frames, label):
    b64s = [base64.b64encode(f).decode("ascii") for f in frames]
    sent_at: dict[int, float] = {}
    box_ms: list[float] = []
    rtt_ms: list[float] = []
    kinds: Counter = Counter()
    plate_texts: list[str] = []
    unread = 0
    start = time.perf_counter()

    async def reader():
        nonlocal unread
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), 30)
            except asyncio.TimeoutError:
                return
            now = time.perf_counter()
            msg = json.loads(raw)
            kind = msg.get("type")
            kinds[kind] += 1
            seq = msg.get("seq")
            if kind == "boxes":
                box_ms.append(float(msg.get("ms") or 0))
                if seq in sent_at:
                    rtt_ms.append((now - sent_at[seq]) * 1000)
                else:
                    unread += 1
            elif kind == "plate":
                plate_texts.append(f"{msg.get('norm')}@{float(msg.get('conf') or 0):.3f}")

    task = asyncio.create_task(reader())
    for i, b64 in enumerate(b64s):
        seq = i + 1
        target = start + i / FPS
        delay = target - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        sent_at[seq] = time.perf_counter()
        await ws.send(
            json.dumps(
                {
                    "type": "frame",
                    "seq": seq,
                    "lat": 17.44,
                    "lng": 78.35,
                    "device_id": device_id,
                    "jpeg_b64": b64,
                }
            )
        )
    await asyncio.sleep(6)  # let the last plate pass finish
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    print(f"  {label}:")
    print(f"    sent {len(b64s)}, boxes back {len(box_ms)}, no matching send {unread}")
    print(f"    box ms p50/p95/max : {pct(box_ms, 50):.0f} / {pct(box_ms, 95):.0f} / {max(box_ms or [0]):.0f}")
    print(f"    round trip p50/p95 : {pct(rtt_ms, 50):.0f} / {pct(rtt_ms, 95):.0f} ms")
    print(f"    message types      : {dict(kinds)}")
    print(f"    plate reads        : {len(plate_texts)} {sorted(set(plate_texts))[:3]}")
    print()
    return {"p50": pct(box_ms, 50), "p95": pct(box_ms, 95), "plates": len(plate_texts)}


async def main() -> int:
    print(f"\n=== static vs moving scene at {FPS:.0f} fps, one car ===\n")
    token = requests.post(
        f"{API_V1}/auth/login",
        json={"email": "volunteer@example.com", "password": "Volunteer@123"},
        timeout=20,
    ).json()["access_token"]
    device_id = requests.post(
        f"{API_V1}/devices/register",
        headers={"Authorization": f"Bearer {token}"},
        json={"device_type": "mobile", "device_name": "static-scene probe"},
        timeout=20,
    ).json()["id"]

    scene = load_scene()
    rows = {}
    async with websockets.connect(
        API.replace("http", "ws") + "/api/v1/ws/scan",
        additional_headers={"Authorization": f"Bearer {token}"},
        max_size=8 * 1024 * 1024,
    ) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token, "device_id": device_id}))
        if json.loads(await asyncio.wait_for(ws.recv(), 20)).get("type") != "ready":
            print("  socket refused")
            return 1
        rows["moving"] = await stream(ws, device_id, moving_frames(scene), "moving car (each frame new)")
        rows["static"] = await stream(ws, device_id, static_frames(scene), "stopped car (identical frames)")

    print("  same frame, same rate, the only difference is whether the car moves:\n")
    print(f"    box p50   moving {rows['moving']['p50']:6.0f} ms   static {rows['static']['p50']:6.0f} ms")
    print(f"    box p95   moving {rows['moving']['p95']:6.0f} ms   static {rows['static']['p95']:6.0f} ms")
    print(f"    plate reads  moving {rows['moving']['plates']}   static {rows['static']['plates']}")
    print(
        "\n  a stopped car keeps a live, unsettled track, so the plate leg keeps"
        "\n  offering crops to a 2 s-per-crop OCR worker that shares the two torch"
        "\n  threads with the pass the phone renders. That is the p95."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
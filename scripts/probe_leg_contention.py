"""Why is the boxes reply slower for ONE readable car than for SIX unreadable ones?

That is backwards - more detections should cost more - so one of the two
assumptions in the speed check is wrong. This isolates which.

Hypothesis: the vehicle leg and the plate leg share one process with
`torch.set_num_threads(2)` (backend/app/api/v1/live_scan.py:57). When the plate
leg gets a readable plate it runs the plate detector plus OCR over the full
1280 px crop, in parallel, and takes both torch threads. The vehicle leg is then
blocked waiting for a thread, so its own measured `ms` inflates. Six tiny cars
have unreadable plates, the plate leg fails fast, nothing competes, and the
vehicle leg looks cheap.

If that is what is happening then the numbers are not a per-vehicle cost curve -
they are two legs sharing a thread pool - and the honest report says so instead
of claiming six cars are faster than one.

Run: python scripts/probe_leg_contention.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time

import cv2
import numpy as np
import requests
import websockets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.environ.get("RAKSHAK_API", "http://127.0.0.1:8000")
API_V1 = f"{API}/api/v1"
FIXTURE = os.path.join(ROOT, "data", "test_plates", "road_scene.jpg")

FRAMES = 8


def pct(values, p):
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def enc(img) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 40])
    return buf.tobytes()


def variant(name: str):
    """(label, frame) pairs. Same car, three different plate outcomes."""
    src = cv2.imread(FIXTURE)
    if src is None:
        raise SystemExit(f"fixture missing: {FIXTURE}")
    src = cv2.resize(src, (1280, round(src.shape[0] * 1280 / src.shape[1])))

    if name == "readable":
        # The whole scene, plate included: the plate leg does its full work.
        return "1 car, plate readable (full frame)", enc(src)
    if name == "no-plate":
        # Crop the bottom of the frame away, where the plate sits. Same car,
        # same detection count, no plate crop for the OCR to chew on.
        h = src.shape[0]
        cut = enc(src[: int(h * 0.72), :])
        return "1 car, plate cropped out", cut
    if name == "blurred":
        # Same whole frame, but the lower third is smeared so any plate text is
        # gone while the car's silhouette and the vehicle box survive.
        out = src.copy()
        h = out.shape[0]
        out[int(h * 0.7) :, :] = cv2.GaussianBlur(out[int(h * 0.7) :, :], (41, 41), 0)
        return "1 car, plate region blurred", enc(out)
    raise SystemExit(f"unknown variant {name}")


async def run(ws, device_id, label, jpeg, frames):
    b64 = base64.b64encode(jpeg).decode("ascii")
    boxes_ms, plate_seen = [], 0
    for i in range(frames):
        seq = i + 1
        sent = time.perf_counter()
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
        deadline = time.perf_counter() + 25
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), max(0.5, deadline - time.perf_counter()))
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            if msg.get("seq") != seq:
                continue
            if msg.get("type") == "boxes":
                boxes_ms.append(float(msg.get("ms") or 0))
                print(
                    f"    frame {seq:>2}: boxes {msg.get('ms'):>6.0f} ms, "
                    f"{len(msg.get('vehicles') or [])} vehicles, wall {(time.perf_counter() - sent) * 1000:.0f} ms",
                    flush=True,
                )
                break
            if msg.get("type") in ("plate", "plates"):
                plate_seen += 1
    return {
        "label": label,
        "p50": pct(boxes_ms, 50),
        "p95": pct(boxes_ms, 95),
        "plate_msgs": plate_seen,
        "vehicles": len(msg.get("vehicles") or []),
    }


async def main() -> int:
    print("\n=== is the boxes reply inflated by the plate leg sharing torch's threads? ===\n")
    token = requests.post(
        f"{API_V1}/auth/login",
        json={"email": "volunteer@example.com", "password": "Volunteer@123"},
        timeout=20,
    ).json()["access_token"]
    r = requests.post(
        f"{API_V1}/devices/register",
        headers={"Authorization": f"Bearer {token}"},
        json={"device_type": "mobile", "device_name": "contention probe"},
        timeout=20,
    )
    device_id = r.json()["id"]

    rows = []
    async with websockets.connect(
        API.replace("http", "ws") + "/api/v1/ws/scan",
        additional_headers={"Authorization": f"Bearer {token}"},
        max_size=8 * 1024 * 1024,
    ) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token, "device_id": device_id}))
        ready = json.loads(await asyncio.wait_for(ws.recv(), 20))
        if ready.get("type") != "ready":
            print(f"  socket refused: {ready}")
            return 1
        for name in ("readable", "no-plate", "blurred"):
            label, jpeg = variant(name)
            print(f"  {label}:")
            rows.append(await run(ws, device_id, label, jpeg, FRAMES))
            print()

    print("  summary (closed loop, 1 vehicle, 1280 px):\n")
    for row in rows:
        print(
            f"    {row['label']:<38} boxes p50 {row['p50']:6.0f} ms  p95 {row['p95']:6.0f} ms  "
            f"plate msgs {row['plate_msgs']}"
        )
    base = rows[0]["p50"]
    quiet = rows[1]["p50"]
    if quiet > 0:
        print(
            f"\n  the box reply is {base - quiet:.0f} ms slower with a readable plate "
            f"({base / quiet:.1f}x)"
        )
    print(
        "\n  torch.set_num_threads(2) is shared by both legs in one process, so the two"
        "\n  overlap and contend. The per-vehicle cost curve has to be read from the"
        "\n  no-plate column; the readable column is plate-leg contention on top."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
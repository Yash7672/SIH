"""Simulated RAKSHAK ANPR device.

Streams frames from a camera or the generated demo video, runs the on-device
ANPR pipeline, and reports detections to the backend — but NEVER uploads
frames/video, only (plate, lat, lng, ts, confidence, device_id).

Usage:
    python scripts/device_simulator.py --email volunteer@example.com --password Volunteer@123
    python scripts/device_simulator.py --video data/generated/demo_road.mp4 --plate-only TS09AB1234
"""

import argparse
import json
import random
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone

import cv2

sys.path.append(".")
from ai.pipeline import detect_and_read  # noqa: E402

DEFAULT_BASE = "http://127.0.0.1:8000"


def http(method, url, headers=None, body=None):
    headers = dict(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    if body is not None and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode())


def login(base, email, password):
    _, tok = http("POST", f"{base}/api/v1/auth/login", body={"email": email, "password": password})
    return tok["access_token"], tok["user"]


def register_device(base, token):
    status, dev = http(
        "POST",
        f"{base}/api/v1/devices/register",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        body={"device_type": "mobile", "device_name": "Simulator-ANPR"},
    )
    return dev["id"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--email", default="volunteer@example.com")
    ap.add_argument("--password", default="Volunteer@123")
    ap.add_argument("--device-id", default=None, help="reuse an existing device id")
    ap.add_argument("--video", default=None, help="path to demo video; else camera index (default 0)")
    ap.add_argument("--base-lat", type=float, default=17.44)
    ap.add_argument("--base-lng", type=float, default=78.34)
    ap.add_argument("--frame-skip", type=int, default=3, help="process every Nth frame")
    args = ap.parse_args()

    token, user = login(args.base, args.email, args.password)
    print(f"logged in as {user['email']} [{user['role']}]")
    device_id = args.device_id or register_device(args.base, token)
    print(f"device_id={device_id}")

    cap = cv2.VideoCapture(args.video if args.video else (args.video if args.video is not None else 0))
    if args.video is None:
        cap = cv2.VideoCapture(0)
    elif not cap.isOpened():
        print("could not open video source")
        return

    plat = args.base_lat
    plng = args.base_lng
    frame_idx = 0
    last_report = {}
    while True:
        ok, frame = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # loop video
            ok, frame = cap.read()
        if not ok:
            break
        frame_idx += 1
        if frame_idx % (args.frame_skip + 1) != 0:
            continue

        dets = detect_and_read(frame)
        if not dets:
            time.sleep(0.05)
            continue

        for det in dets:
            now = time.time()
            if last_report.get(det.plate, 0) > now - 60:  # backend cooldown mirror
                continue
            last_report[det.plate] = now
            plat += random.uniform(-0.002, 0.002)
            plng += random.uniform(-0.002, 0.002)
            body = {
                "plate": det.plate,
                "latitude": round(plat, 6),
                "longitude": round(plng, 6),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "confidence": det.confidence,
                "device_id": device_id,
            }
            status, resp = http(
                "POST",
                f"{args.base}/api/v1/sightings",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                body=body,
            )
            print(
                f"[{time.strftime('%H:%M:%S')}] {det.plate} conf={det.confidence:.2f} "
                f"@ {plat:.5f},{plng:.5f} -> HTTP {status} {resp.get('detail', 'ok')}"
            )
        time.sleep(0.4)


if __name__ == "__main__":
    main()
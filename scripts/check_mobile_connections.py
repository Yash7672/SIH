"""Reproduce exactly what the Expo app sends to the backend, from the PC.

Mirrors mobile_app/src/services/api.js byte for byte:
  POST {API_BASE}/api/v1/scanner/scan
  Authorization: Bearer <access token>
  multipart/form-data, field name "image", filename "frame.jpg",
  content-type image/jpeg  (api.js scanImage -> FormData.append)

Used to tell apart the two possible causes of "Cannot reach backend":
a broken capture/request build on the phone, versus a broken scan endpoint.

Usage:
  python scripts/check_mobile_connections.py
  python scripts/check_mobile_connections.py --base http://10.175.26.85:8000
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLATE_DIR = REPO / "data" / "generated"

DEMO_USER = "volunteer@example.com"
DEMO_PASS = "Volunteer@123"

# 45 000 ms is what ScannerScreen passes as timeoutMs on the scan call.
SCAN_TIMEOUT_MS = 45000


def _request(url, *, method="GET", data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            elapsed = (time.perf_counter() - started) * 1000
            return resp.status, body, elapsed, None
    except urllib.error.HTTPError as exc:
        body = exc.read()
        elapsed = (time.perf_counter() - started) * 1000
        return exc.code, body, elapsed, None
    except Exception as exc:  # network-level failure
        elapsed = (time.perf_counter() - started) * 1000
        return None, b"", elapsed, exc


def _multipart(field: str, filename: str, content_type: str, blob: bytes) -> tuple[bytes, str]:
    """Build the same multipart body React Native's FormData produces."""
    boundary = "----RAKSHAKFormBoundary7MA4YWxkTrZu0gW"
    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode()
    )
    buf.write(f"Content-Type: {content_type}\r\n\r\n".encode())
    buf.write(blob)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def login(base: str) -> str:
    payload = json.dumps(
        {"email": DEMO_USER, "password": DEMO_PASS}
    ).encode()
    status, body, elapsed, err = _request(
        f"{base}/api/v1/auth/login",
        method="POST",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    if status != 200:
        raise SystemExit(f"login failed: HTTP {status} {body[:200]!r} err={err}")
    token = json.loads(body)["access_token"]
    print(f"  login ok  {DEMO_USER}  ({elapsed:.0f} ms)")
    return token


def health(base: str) -> None:
    status, body, elapsed, err = _request(f"{base}/health", timeout=10)
    print(f"  GET  /health              -> HTTP {status}  {elapsed:.0f} ms  {body[:120].decode(errors='replace')}")
    if err:
        print(f"      transport error: {err}")


def sighting(base: str, token: str) -> None:
    """The call 'Simulate detection' makes - proof the POST+auth path works."""
    payload = json.dumps(
        {
            "plate": "MH12JK4567",
            "latitude": 17.385,
            "longitude": 78.4867,
            "timestamp": "2026-01-01T00:00:00Z",
            "confidence": 0.97,
        }
    ).encode()
    status, body, elapsed, err = _request(
        f"{base}/api/v1/sightings",
        method="POST",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    print(f"  POST /api/v1/sightings    -> HTTP {status}  {elapsed:.0f} ms  {body[:100].decode(errors='replace')}")
    if err:
        print(f"      transport error: {err}")


def scan(base: str, token: str, path: Path) -> None:
    blob = path.read_bytes()
    body, ctype = _multipart("image", "frame.jpg", "image/jpeg", blob)
    expected = path.stem.split("_")[-1]
    status, resp, elapsed, err = _request(
        f"{base}/api/v1/scanner/scan",
        method="POST",
        data=body,
        headers={"Content-Type": ctype, "Authorization": f"Bearer {token}"},
        timeout=SCAN_TIMEOUT_MS / 1000,
    )
    label = f"POST /api/v1/scanner/scan  {path.name} ({len(blob) // 1024} KB, want {expected})"
    if err:
        print(f"  {label}\n      TRANSPORT ERROR after {elapsed:.0f} ms: {err}")
        return
    try:
        js = json.loads(resp)
        keys = sorted(js.keys())
        got = js.get("plate")
        mark = "OK " if (got and expected[-4:] in str(got).replace(" ", "")) else "?  "
        print(
            f"  {mark}{label}\n      HTTP {status}  {elapsed:.0f} ms  plate={got!r} "
            f"conf={js.get('confidence')} keys={keys}"
        )
    except Exception:
        print(f"  !! {label}\n      HTTP {status}  {elapsed:.0f} ms  non-JSON: {resp[:300]!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print(f"\n=== backend {base} ===")
    print("[health + POST auth path]")
    health(base)
    token = login(base)
    sighting(base, token)

    plates = sorted(PLATE_DIR.glob("*.png"))
    if not plates:
        print(f"no plates in {PLATE_DIR}")
        return 1

    print("\n[scan - field 'image', multipart, Bearer token]")
    for i, p in enumerate(plates):
        scan(base, token, p)
        if i == 0:
            print("      ^ first call pays the model cold start")

    print("\n[warm re-scan of the first plate]")
    scan(base, token, plates[0])
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
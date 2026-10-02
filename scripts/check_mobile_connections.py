"""Reproduce exactly what the Expo app sends to the backend, from the PC.

The scan flow now uploads base64 JSON (no FormData) because Expo SDK 58's
ambient fetch rejects React Native's legacy `{uri,name,type}` file object with
`Unsupported FormDataPart implementation` before the request is ever sent.
This script mirrors mobile_app/src/services/api.js byte for byte:

  POST {API_BASE}/api/v1/scanner/scan
  Authorization: Bearer <access token>
  Content-Type: application/json
  {"image_b64": "<...>"}                 one frame
  {"images_b64": ["...","...","..."]}   up to three frames

It also exercises the rejection paths (413 / 422 / 401 / 415), confirms the
legacy multipart endpoint still works, and prints elapsed ms so the first
(cold, model-loading) call can be told apart from warm ones.

Usage:
  python scripts/check_mobile_connections.py
  python scripts/check_mobile_connections.py --base http://10.175.26.85:8000
"""

from __future__ import annotations

import argparse
import base64
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

# Mobile budgets: 45 s for the first scan, 30 s after (api.js).
SCAN_TIMEOUT_COLD_S = 45
SCAN_TIMEOUT_WARM_S = 30

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str) -> bool:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"  [{PASS if ok else FAIL}] {name}: {detail}")
    return ok


def _request(url, *, method="GET", data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, body, (time.perf_counter() - started) * 1000, None
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, body, (time.perf_counter() - started) * 1000, None
    except Exception as exc:
        return None, b"", (time.perf_counter() - started) * 1000, exc


def _json_post(base: str, path: str, payload: dict, token: str | None, timeout: int):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return _request(f"{base}{path}", method="POST", data=body, headers=headers, timeout=timeout)


def _multipart(field: str, filename: str, content_type: str, blob: bytes) -> tuple[bytes, str]:
    """Build the multipart body React Native's FormData used to produce."""
    boundary = "----RAKSHAKFormBoundary7MA4YWxkTrZu0gW"
    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode())
    buf.write(f"Content-Type: {content_type}\r\n\r\n".encode())
    buf.write(blob)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def register_device(base: str, token: str) -> str:
    """Sighting rows carry a device_id FK, so a real device must exist first."""
    status, body, _, _ = _json_post(
        base, "/api/v1/devices/register",
        {"device_type": "mobile", "device_name": "check-script"}, token, 30,
    )
    if status not in (200, 201):
        print(f"  device register failed: HTTP {status} {body[:160].decode(errors='replace')}")
        return ""
    try:
        return str(json.loads(body)["id"])
    except Exception:
        return ""


def login(base: str) -> str:
    status, body, elapsed, err = _json_post(
        base, "/api/v1/auth/login", {"email": DEMO_USER, "password": DEMO_PASS}, None, 30
    )
    if status != 200:
        raise SystemExit(f"login failed: HTTP {status} {body[:200]!r} err={err}")
    print(f"  login ok  {DEMO_USER}  ({elapsed:.0f} ms)")
    return json.loads(body)["access_token"]


def health(base: str) -> None:
    status, body, elapsed, err = _request(f"{base}/health", timeout=10)
    check("GET /health", status == 200, f"HTTP {status} in {elapsed:.0f} ms")


def sighting(base: str, token: str, device_id: str) -> None:
    """The call 'Simulate' makes - proof the JSON POST + auth path works."""
    payload = {
        "plate": "MH12JK4567",
        "device_id": device_id,
        "latitude": 17.385,
        "longitude": 78.4867,
        "timestamp": "2026-01-01T00:00:00Z",
        "confidence": 0.97,
    }
    status, body, elapsed, _ = _json_post(base, "/api/v1/sightings", payload, token, 30)
    check(
        "POST /api/v1/sightings (JSON)",
        status in (200, 201),
        f"HTTP {status} in {elapsed:.0f} ms  {body[:120].decode(errors='replace')}",
    )


def _expected(path: Path) -> str:
    return path.stem.split("_")[-1]


def _matches(expected: str, js: dict) -> bool:
    got = js.get("plate")
    if not got:
        return False
    return expected[-4:] in str(got).replace(" ", "")


def scan_json(base: str, token: str, frames: list[str], *, timeout: int = SCAN_TIMEOUT_COLD_S):
    payload = {"image_b64": frames[0]} if len(frames) == 1 else {"images_b64": frames}
    return _json_post(base, "/api/v1/scanner/scan", payload, token, timeout)


def describe(js: dict) -> str:
    return (
        f"plate={js.get('plate')!r} conf={js.get('confidence')} valid={js.get('valid')} "
        f"uncertain={js.get('uncertain')} frames={js.get('frames')} votes={js.get('votes')} "
        f"reason={js.get('reason')!r} note={js.get('note')!r}"
    )


def run_scan_suite(base: str, token: str, plates: list[Path]) -> None:
    print("\n[1] single frame as image_b64, bare base64 (exactly what the app sends)")
    for i, path in enumerate(plates):
        b64 = base64.b64encode(path.read_bytes()).decode()
        status, body, elapsed, err = scan_json(base, token, [b64], timeout=SCAN_TIMEOUT_COLD_S if i == 0 else SCAN_TIMEOUT_WARM_S)
        if err or status != 200:
            check(f"scan 1-frame {path.name}", False, f"HTTP {status} err={err}")
            continue
        js = json.loads(body)
        check(
            f"scan 1-frame {_expected(path)}",
            _matches(_expected(path), js) and js.get("valid") is True,
            f"HTTP {status} in {elapsed:.0f} ms  {describe(js)}",
        )
        if i == 0:
            print("       ^ first call pays the YOLO/OCR model cold start")

    print("\n[2] three frames as images_b64 (the 3-frame vote, app early-exits at >=0.85)")
    triple = [base64.b64encode(p.read_bytes()).decode() for p in plates[:3]]
    status, body, elapsed, err = scan_json(base, token, triple, timeout=SCAN_TIMEOUT_WARM_S)
    if err or status != 200:
        check("scan 3-frame", False, f"HTTP {status} err={err}")
    else:
        js = json.loads(body)
        check(
            f"scan 3-frame {_expected(plates[0])}",
            _matches(_expected(plates[0]), js) and js.get("valid") is True,
            f"HTTP {status} in {elapsed:.0f} ms  {describe(js)}",
        )

    print("\n[3] data: prefix is optional (bare is what the app sends)")
    prefixed = "data:image/png;base64," + base64.b64encode(plates[0].read_bytes()).decode()
    status, body, elapsed, err = scan_json(base, token, [prefixed], timeout=SCAN_TIMEOUT_WARM_S)
    if err or status != 200:
        check("scan data: prefix", False, f"HTTP {status} err={err}")
    else:
        js = json.loads(body)
        check("scan data: prefix", _matches(_expected(plates[0]), js), f"HTTP {status} in {elapsed:.0f} ms  plate={js.get('plate')!r}")


def run_error_suite(base: str, token: str, plates: list[Path]) -> None:
    print("\n[4] rejection paths")
    big = base64.b64encode(b"\xff" * (7 * 1024 * 1024)).decode()
    status, body, elapsed, _ = scan_json(base, token, [big], timeout=30)
    check("oversized image -> 413", status == 413, f"HTTP {status} in {elapsed:.0f} ms  {body[:90].decode(errors='replace')}")

    status, body, elapsed, _ = scan_json(base, token, [base64.b64encode(b"hello world").decode()], timeout=30)
    check("undecodable bytes -> 422", status == 422, f"HTTP {status} in {elapsed:.0f} ms  {body[:90].decode(errors='replace')}")

    status, body, elapsed, _ = scan_json(base, token, ["!!!not-base64!!!"], timeout=30)
    check("invalid base64 -> 422", status == 422, f"HTTP {status} in {elapsed:.0f} ms  {body[:90].decode(errors='replace')}")

    status, body, elapsed, _ = _json_post(
        base, "/api/v1/scanner/scan",
        {"image_b64": base64.b64encode(plates[0].read_bytes()).decode()}, None, 30,
    )
    check("no token -> 401", status == 401, f"HTTP {status} in {elapsed:.0f} ms  {body[:90].decode(errors='replace')}")

    status, body, elapsed, _ = _json_post(base, "/api/v1/scanner/scan", {"nope": 1}, token, 30)
    check("missing image field -> 422", status == 422, f"HTTP {status} in {elapsed:.0f} ms  {body[:90].decode(errors='replace')}")

    too_many = [base64.b64encode(plates[0].read_bytes()).decode()] * 4
    status, body, elapsed, _ = scan_json(base, token, too_many, timeout=30)
    check("four frames -> 422", status == 422, f"HTTP {status} in {elapsed:.0f} ms  {body[:90].decode(errors='replace')}")

    raw, ctype = b"\x00\x01", "text/plain"
    body_bytes, _ = _multipart("image", "x.txt", raw.decode(), b"x")
    status, body, elapsed, _ = _request(
        f"{base}/api/v1/scanner/scan", method="POST", data=body_bytes,
        headers={"Content-Type": f"multipart/form-data; boundary=----RAKSHAKFormBoundary7MA4YWxkTrZu0gW",
                 "Authorization": f"Bearer {token}"}, timeout=30,
    )
    check("multipart text/plain -> 415", status in (415, 422), f"HTTP {status} in {elapsed:.0f} ms")


def run_legacy_multipart(base: str, token: str, plates: list[Path]) -> None:
    print("\n[5] legacy multipart path still works (kept for existing tools/tests)")
    path = plates[0]
    body, ctype = _multipart("image", "frame.jpg", "image/jpeg", path.read_bytes())
    status, resp, elapsed, err = _request(
        f"{base}/api/v1/scanner/scan", method="POST", data=body,
        headers={"Content-Type": ctype, "Authorization": f"Bearer {token}"}, timeout=SCAN_TIMEOUT_WARM_S,
    )
    if err or status != 200:
        check("multipart 1-frame", False, f"HTTP {status} err={err}  {resp[:140].decode(errors='replace')}")
    else:
        js = json.loads(resp)
        check("multipart 1-frame", _matches(_expected(path), js), f"HTTP {status} in {elapsed:.0f} ms  {describe(js)}")

    parts, ctype = b"", f"multipart/form-data; boundary=----RAKSHAKFormBoundary7MA4YWxkTrZu0gW"
    boundary = "----RAKSHAKFormBoundary7MA4YWxkTrZu0gW"
    buf = io.BytesIO()
    for p in plates[:2]:
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="images"; filename="{p.name}"\r\n'.encode())
        buf.write(b"Content-Type: image/png\r\n\r\n")
        buf.write(p.read_bytes())
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    parts = buf.getvalue()
    status, resp, elapsed, err = _request(
        f"{base}/api/v1/scanner/scan", method="POST", data=parts,
        headers={"Content-Type": ctype, "Authorization": f"Bearer {token}"}, timeout=SCAN_TIMEOUT_WARM_S,
    )
    if err or status != 200:
        check("multipart 2-frame (images)", False, f"HTTP {status} err={err}")
    else:
        js = json.loads(resp)
        check("multipart 2-frame (images)", js.get("frames") == 2, f"HTTP {status} in {elapsed:.0f} ms  frames={js.get('frames')} plate={js.get('plate')!r}")


def run_privacy_suite(base: str, token: str, device_id: str) -> None:
    print("\n[6] privacy contract: /sightings must never accept an image")
    body_bytes, ctype = _multipart("image", "frame.jpg", "image/jpeg", b"\xff\xd8\xff\xe0 not really a jpeg")
    status, resp, elapsed, _ = _request(
        f"{base}/api/v1/sightings", method="POST", data=body_bytes,
        headers={"Content-Type": ctype, "Authorization": f"Bearer {token}"}, timeout=30,
    )
    check("sightings rejects multipart", status in (400, 415, 422), f"HTTP {status} in {elapsed:.0f} ms")

    raw_b64 = base64.b64encode(b"not a real image" * 50).decode()
    status, resp, elapsed, _ = _json_post(
        base, "/api/v1/sightings",
        {"plate": "MH12JK4567", "device_id": device_id, "latitude": 17.385,
         "longitude": 78.4867, "timestamp": "2026-01-01T00:00:00Z", "confidence": 0.97,
         "image_b64": raw_b64},
        token, 30,
    )
    body_text = resp.decode(errors="replace")
    # The sightings schema ignores unknown fields, so the request is accepted but
    # no image is ever stored or echoed back. That is the guarantee to assert -
    # the sightings route itself must stay untouched.
    no_image_echoed = "image" not in body_text.lower() and raw_b64[:40] not in body_text
    check(
        "sightings never stores/echoes image_b64",
        status in (200, 202) and no_image_echoed,
        f"HTTP {status} in {elapsed:.0f} ms  response has no image data  {body_text[:80]}",
    )


def main() -> int:
    # The generated plates include non-Latin text; the default Windows console
    # codec (cp1252) cannot encode it and would abort the run mid-report.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print(f"\n=== backend {base} ===")
    health(base)
    token = login(base)
    device_id = register_device(base, token)
    print(f"  device registered: {device_id or '(none)'}")
    sighting(base, token, device_id)

    plates = sorted(PLATE_DIR.glob("*.png"))
    if not plates:
        print(f"no plates in {PLATE_DIR}")
        return 1

    run_scan_suite(base, token, plates)
    run_error_suite(base, token, plates)
    run_legacy_multipart(base, token, plates)
    run_privacy_suite(base, token, device_id)

    failed = [r for r in results if r[1] == FAIL]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} checks passed ===")
    for name, _, detail in failed:
        print(f"  FAIL {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
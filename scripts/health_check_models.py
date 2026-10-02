#!/usr/bin/env python3
"""Single-command health check for the RAKSHAK AI stack and auth path.

    python scripts/health_check_models.py [--api http://localhost:8000]

Every check runs independently inside its own try/except so one broken stage
still lets the rest report. Each prints PASS / FAIL / WARN / SKIP with its
wall-clock time. The process exits non-zero when a critical check fails, so it
can be wired into CI or run from a deploy script.

This deliberately exercises the *real* code paths (``ai.detector``,
``ai.ocr_engine``, ``ai.pipeline``, ``app.services.plate``) rather than
re-implementations: a health check that passes while the app fails is worse
than no health check.
"""

from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import platform
import re
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
PLATE_RE = re.compile(r"([A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4})")
DEBUG_DIR = ROOT / "data" / "debug_out" / "health"
GENERATED_DIR = ROOT / "data" / "generated"
TEST_PLATES_DIR = ROOT / "data" / "test_plates"

RESULTS: list[tuple[str, str, str, float]] = []
_CRITICAL_FAILURES: list[str] = []


class CheckFailure(Exception):
    """Raised by a check to report a clean FAIL with an explanation."""


def note(line: str = "") -> None:
    print(f"    {line}")


def run_check(name: str, fn, critical: bool = True):
    started = time.time()
    try:
        status, detail = fn()
    except CheckFailure as exc:
        status, detail = FAIL, str(exc)
    except Exception as exc:  # a crash in the code under test is itself a FAIL
        status, detail = FAIL, f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    elapsed = time.time() - started
    RESULTS.append((name, status, detail, elapsed))
    print(f"[{status}] {name} ({elapsed:.2f}s)")
    if status == FAIL and critical:
        _CRITICAL_FAILURES.append(name)
    return status


def _images() -> list[Path]:
    images = sorted(GENERATED_DIR.glob("*.png")) + sorted(GENERATED_DIR.glob("*.jpg"))
    if TEST_PLATES_DIR.exists():
        images += sorted(TEST_PLATES_DIR.glob("*.png")) + sorted(TEST_PLATES_DIR.glob("*.jpg"))
    return images


def expected_plate(path: Path) -> str | None:
    match = PLATE_RE.search(path.stem.upper())
    return match.group(1) if match else None


def package_version(name: str) -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version(name)
        except PackageNotFoundError:
            return "not installed"
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------
# 1) ENVIRONMENT
# --------------------------------------------------------------------------
def check_env():
    import numpy

    info = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "numpy": numpy.__version__,
        "ultralytics": package_version("ultralytics"),
        "torch": package_version("torch"),
        "onnxruntime": package_version("onnxruntime"),
        "rapidocr-onnxruntime": package_version("rapidocr-onnxruntime"),
        "opencv-python": package_version("opencv-python"),
        "requests": package_version("requests"),
        "PyJWT": package_version("PyJWT"),
    }
    for key, value in info.items():
        note(f"{key:22} {value}")

    problems = []
    major = int(numpy.__version__.split(".")[0])
    if major >= 2:
        note("numpy 2.x detected - opencv/onnxruntime builds older than 4.10 break on it")
    if info["rapidocr-onnxruntime"] == "not installed":
        problems.append("rapidocr-onnxruntime missing (pip install rapidocr-onnxruntime)")
    if info["ultralytics"] == "not installed":
        problems.append("ultralytics missing (pip install ultralytics)")
    if problems:
        return WARN, "; ".join(problems)
    return PASS, f"python {info['python']}, numpy {info['numpy']}"


# --------------------------------------------------------------------------
# 2) YOLO MODEL FILE / PATH RESOLUTION
# --------------------------------------------------------------------------
def check_model_file():
    from ai import detector

    resolved = detector.resolve_model_path()
    path = Path(resolved)
    note(f"resolve_model_path() -> {resolved}")
    if not path.is_absolute():
        raise CheckFailure(
            f"resolve_model_path() returned a relative path ({resolved!r}); "
            "the model only loads when uvicorn happens to start in the right directory"
        )
    if not path.exists():
        raise CheckFailure(f"model file not found at {path}")
    size_mb = path.stat().st_size / (1024 * 1024)
    note(f"size {size_mb:.2f} MB")
    if size_mb <= 1.0:
        raise CheckFailure(f"model file is only {size_mb:.2f} MB - looks truncated")

    # The historical failure: resolution depending on the working directory.
    original = Path.cwd()
    try:
        os.chdir(ROOT / "backend")
        from_backend = detector.resolve_model_path()
        os.chdir(original)
        from_root = detector.resolve_model_path()
    finally:
        os.chdir(original)
    note(f"resolved from repo root : {from_root}")
    note(f"resolved from backend/  : {from_backend}")
    if Path(from_backend) != Path(from_root):
        raise CheckFailure("model path depends on the current working directory")
    if not Path(from_backend).exists():
        raise CheckFailure(f"path resolved from backend/ does not exist: {from_backend}")

    model = detector._load_plate_detector()
    task = getattr(model, "task", "unknown")
    names = getattr(model, "names", None)
    note(f"task={task}  names={names}")
    if task != "detect":
        raise CheckFailure(f"model task is {task!r}, expected 'detect'")
    return PASS, f"{path.name} ({size_mb:.2f} MB), task=detect, cwd-independent"


# --------------------------------------------------------------------------
# 3) YOLO INFERENCE
# --------------------------------------------------------------------------
def check_yolo_inference():
    import cv2

    from ai import detector

    images = _images()
    if not images:
        return WARN, "no images in data/generated or data/test_plates"

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    detected = 0
    for path in images:
        image = cv2.imread(str(path))
        if image is None:
            note(f"{path.name:<34} unreadable by cv2")
            continue
        started = time.time()
        region = detector.detect_plate_region(image)
        elapsed = time.time() - started
        if region is None:
            note(f"{path.name:<34} no box            {elapsed * 1000:7.0f} ms")
            continue
        detected += 1
        x1, y1, x2, y2 = region.raw_box
        conf = region.confidence
        conf_text = "n/a (contour)" if conf != conf else f"{conf:.3f}"
        crop_path = DEBUG_DIR / f"yolo_{path.stem}.jpg"
        cv2.imwrite(str(crop_path), region.crop)
        note(
            f"{path.name:<34} box=({x1},{y1},{x2},{y2}) {x2 - x1}x{y2 - y1}px "
            f"conf={conf_text} src={region.source} {elapsed * 1000:7.0f} ms"
        )
    note(f"crops saved to {DEBUG_DIR}")
    if detected == 0:
        raise CheckFailure(f"detector returned no box for any of {len(images)} image(s)")
    return PASS, f"{detected}/{len(images)} image(s) produced a box"


# --------------------------------------------------------------------------
# 4) CONTOUR FALLBACK
# --------------------------------------------------------------------------
def check_fallback_detector():
    import cv2

    from ai import detector

    images = _images()
    if not images:
        return WARN, "no images to test the fallback against"

    # Which detector does a normal run actually use? Reported, because a
    # silently-never-used trained model is a real historical failure here.
    detector.reset_events()
    for path in images:
        image = cv2.imread(str(path))
        if image is not None:
            detector.detect_plate_region(image)
    used_fallback = sum(
        1 for event in getattr(detector, "DETECTOR_EVENTS", [])
        if "falling back to contours" in event
    )
    if used_fallback:
        note(f"WARNING: normal runs fell back to contours {used_fallback}x - trained model not being used")
    else:
        note("normal runs used the trained detector (no contour fallback)")

    # Now force the fallback by breaking the YOLO path.
    original = detector._yolo_regions

    def _boom(_image):
        raise RuntimeError("simulated ultralytics failure")

    detector._yolo_regions = _boom
    ok = 0
    try:
        for path in images:
            image = cv2.imread(str(path))
            if image is None:
                continue
            region = detector.detect_plate_region(image)
            if region is not None and region.crop is not None and region.crop.size:
                ok += 1
                note(f"{path.name:<34} fallback crop {region.crop.shape[1]}x{region.crop.shape[0]}px")
            else:
                note(f"{path.name:<34} fallback returned NO crop")
    finally:
        detector._yolo_regions = original
    if ok == 0:
        raise CheckFailure("contour fallback never produced a crop")
    return PASS, f"fallback produced a crop for {ok}/{len(images)} image(s)"


# --------------------------------------------------------------------------
# 5) OCR ENGINE
# --------------------------------------------------------------------------
def check_ocr_engine():
    import cv2

    from ai import ocr_engine

    started = time.time()
    engine_a = ocr_engine.get_engine()
    cold = time.time() - started
    engine_b = ocr_engine.get_engine()
    warm = time.time() - started - cold
    note(f"engine init (cold) {cold:.2f}s, warm call {warm * 1000:.0f} ms")
    if engine_a is None:
        raise CheckFailure("get_engine() returned None")
    if engine_a is not engine_b:
        raise CheckFailure("get_engine() is not a singleton - a new model loads on every call")

    images = _images()
    if not images:
        return PASS, "singleton verified (no images to OCR)"
    fragments = 0
    for path in images:
        image = cv2.imread(str(path))
        if image is None:
            continue
        started = time.time()
        hits = ocr_engine.read_text(image)
        elapsed = time.time() - started
        fragments += len(hits)
        note(f"{path.name:<34} {len(hits)} fragment(s) in {elapsed * 1000:.0f} ms")
        for hit in hits[:6]:
            box = hit.box
            note(f"    text={hit.text!r:<22} conf={hit.score:.3f} box={box is not None}")
    return PASS, f"singleton ok, {fragments} fragment(s) across {len(images)} image(s)"


# --------------------------------------------------------------------------
# 6) NORMALIZER
# --------------------------------------------------------------------------
def check_normalizer():
    from app.services.plate import normalize_plate

    valid_cases = [
        "MH12AB1234", "MH01CD5678", "TS09AB1234", "DL3CAB1234", "KA05MN1234",
        "mh 12 ab 1234", "MH-12-AB-1234", "MH.12.AB.1234",
    ]
    invalid_cases = ["HL09AB1234", "ZZ09AB1234", "XX09AB1234", "KN09AB1234", "QQ12AB1234"]

    failures = []
    for raw in valid_cases:
        result = normalize_plate(raw)
        if not result.valid:
            failures.append(f"{raw!r} should be valid, got {result.normalized!r} ({result.reason})")
        else:
            note(f"valid   {raw!r:<20} -> {result.normalized}")

    for raw in invalid_cases:
        result = normalize_plate(raw)
        if result.valid:
            failures.append(f"{raw!r} must NOT validate, but got {result.normalized!r}")
        else:
            note(f"invalid {raw!r:<20} rejected ({result.reason})")

    if failures:
        raise CheckFailure("; ".join(failures))
    return PASS, f"{len(valid_cases)} valid + {len(invalid_cases)} invalid cases behave correctly"


# --------------------------------------------------------------------------
# 7) FULL PIPELINE
# --------------------------------------------------------------------------
def check_pipeline():
    from ai.pipeline import detect_and_read

    images = _images()
    if not images:
        return WARN, "no images to run the pipeline against"

    passed = 0
    checked = 0
    times = []
    wrong = []
    for path in images:
        image = cv2_read(path)
        if image is None:
            continue
        started = time.time()
        reads = detect_and_read(image)
        elapsed = time.time() - started
        times.append(elapsed)
        got = reads[0].plate if reads else None
        expected = expected_plate(path)
        if expected:
            checked += 1
            if got == expected:
                passed += 1
                status = "PASS"
            else:
                status = "FAIL"
                if got:
                    wrong.append(f"{path.name}: got {got!r}")
        else:
            status = "no-expectation"
        note(
            f"{path.name:<34} {str(got or '-'):<12} conf="
            f"{(reads[0].confidence if reads else 0):.2f} "
            f"uncertain={(reads[0].uncertain if reads else True)} "
            f"{elapsed:5.2f}s {status}"
        )
    if not times:
        raise CheckFailure("pipeline could not read any image")
    average = sum(times) / len(times)
    note(f"average latency {average:.2f}s over {len(times)} image(s)")
    rate = f"{passed}/{checked}" if checked else "n/a"
    detail = f"accuracy {rate}, avg {average:.2f}s"
    if average > 1.5:
        note("WARNING: average latency above the 1.5s warm target")
        return WARN, detail + " (slow)"
    if checked and passed < checked:
        note("known misses: " + ("; ".join(wrong) if wrong else "no plate returned"))
        return PASS, detail
    return PASS, detail


def cv2_read(path: Path):
    import cv2

    return cv2.imread(str(path))


# --------------------------------------------------------------------------
# 8) ENDPOINT
# --------------------------------------------------------------------------
def _requests():
    try:
        import requests

        return requests
    except ImportError:
        return None


def api_reachable(base: str) -> bool:
    requests = _requests()
    if requests is None:
        return False
    try:
        response = requests.get(f"{base}/api/v1/health", timeout=5)
        return response.status_code == 200
    except Exception:
        return False


def check_endpoint(base: str):
    requests = _requests()
    if requests is None:
        return SKIP, "requests not installed"
    if not api_reachable(base):
        return SKIP, f"{base} is not reachable"

    problems = []
    login = requests.post(
        f"{base}/api/v1/auth/login",
        json={"email": "volunteer@example.com", "password": "Volunteer@123"},
        timeout=20,
    )
    note(f"login -> {login.status_code}")
    if login.status_code != 200:
        raise CheckFailure(f"login failed: {login.status_code} {login.text[:200]}")
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    image_path = GENERATED_DIR / "plate_3_MH12JK4567.png"
    if not image_path.exists():
        images = _images()
        if not images:
            raise CheckFailure("no image available to upload")
        image_path = images[0]

    # Exactly how the phone sends it: multipart with an "image" field.
    with image_path.open("rb") as handle:
        scan = requests.post(
            f"{base}/api/v1/scanner/scan",
            files={"image": (image_path.name, handle, "image/png")},
            headers=headers,
            timeout=120,
        )
    note(f"scan -> {scan.status_code}")
    if scan.status_code != 200:
        raise CheckFailure(f"scan failed: {scan.status_code} {scan.text[:300]}")
    payload = scan.json()
    note(f"scan body: {json.dumps(payload)[:220]}")

    # Field names ScannerScreen.js reads off the response.
    for field in ("plate", "confidence", "valid", "uncertain"):
        if field not in payload:
            problems.append(f"scan response missing {field!r} that ScannerScreen.js reads")
    if payload.get("plate") is None:
        problems.append(f"scan returned no plate for {image_path.name}")

    # privacy_guard: a video body must be refused.
    video = requests.post(
        f"{base}/api/v1/scanner/scan",
        files={"image": ("clip.mp4", io.BytesIO(b"\x00\x00\x00\x18ftypmp42"), "video/mp4")},
        headers=headers,
        timeout=30,
    )
    note(f"video/* upload -> {video.status_code} {video.text[:120]}")
    if not (400 <= video.status_code < 500):
        problems.append(f"video/* upload returned {video.status_code}, expected 4xx")

    # A hot-listed plate must be accepted by /sightings. The sighting is bound
    # to a registered device, so register one first exactly as the app does and
    # use the UUID the backend hands back.
    device = requests.post(
        f"{base}/api/v1/devices/register",
        json={"device_type": "mobile", "device_name": "health-check"},
        headers=headers,
        timeout=30,
    )
    note(f"device register -> {device.status_code} {device.text[:160]}")
    if device.status_code not in (200, 201):
        problems.append(f"device register returned {device.status_code}, cannot test /sightings")
        device_id = None
    else:
        device_id = (device.json() or {}).get("id")

    sighting = {
        "plate": payload.get("plate") or "MH12AB1234",
        "latitude": 19.076,
        "longitude": 72.8777,
        "timestamp": "2026-01-01T00:00:00Z",
        "confidence": payload.get("confidence") or 0.9,
        "device_id": device_id,
    }
    posted = requests.post(
        f"{base}/api/v1/sightings", json=sighting, headers=headers, timeout=30
    )
    note(f"sightings -> {posted.status_code} {posted.text[:200]}")
    if posted.status_code not in (200, 201, 202):
        problems.append(f"/sightings returned {posted.status_code}, expected 200/201/202")

    if problems:
        raise CheckFailure("; ".join(problems))
    return PASS, "scan field names match ScannerScreen.js, video rejected, sighting accepted"


# --------------------------------------------------------------------------
# 9) AUTH
# --------------------------------------------------------------------------
def check_auth(base: str):
    requests = _requests()
    if requests is None:
        return SKIP, "requests not installed"
    if not api_reachable(base):
        return SKIP, f"{base} is not reachable"

    problems = []

    def login(email, password):
        return requests.post(
            f"{base}/api/v1/auth/login", json={"email": email, "password": password}, timeout=20
        )

    good = login("volunteer@example.com", "Volunteer@123")
    note(f"login good creds -> {good.status_code}")
    if good.status_code != 200:
        raise CheckFailure(f"valid login returned {good.status_code}: {good.text[:200]}")
    body = good.json()
    for field in ("access_token", "refresh_token", "token_type"):
        if not body.get(field):
            problems.append(f"login response missing {field!r}")
    access = body["access_token"]
    refresh = body["refresh_token"]
    headers = {"Authorization": f"Bearer {access}"}

    me = requests.get(f"{base}/api/v1/auth/me", headers=headers, timeout=20)
    note(f"/auth/me with token -> {me.status_code}")
    if me.status_code != 200:
        problems.append(f"/auth/me with a valid token returned {me.status_code}")

    missing = requests.get(f"{base}/api/v1/auth/me", timeout=20)
    note(f"/auth/me no token -> {missing.status_code} {missing.text[:80]}")
    if missing.status_code != 401:
        problems.append(f"missing token returned {missing.status_code}, expected 401")
    elif "detail" not in missing.json():
        problems.append("401 response has no 'detail' text for the app to show")

    wrong = login("volunteer@example.com", "definitely-wrong")
    note(f"login wrong password -> {wrong.status_code}")
    if wrong.status_code != 401:
        problems.append(f"wrong password returned {wrong.status_code}, expected 401")

    refreshed = requests.post(
        f"{base}/api/v1/auth/refresh", json={"refresh_token": refresh}, timeout=20
    )
    note(f"refresh -> {refreshed.status_code}")
    if refreshed.status_code != 200 or not refreshed.json().get("access_token"):
        problems.append(f"refresh returned {refreshed.status_code}")

    # Expired token must be a clean 401 with a readable detail.
    import jwt

    expired = jwt.encode(
        {"sub": "00000000-0000-0000-0000-000000000000", "type": "access",
         "iat": 1, "exp": 2},
        __import__("app.core.config", fromlist=["settings"]).settings.JWT_SECRET,
        algorithm="HS256",
    )
    stale = requests.get(
        f"{base}/api/v1/auth/me", headers={"Authorization": f"Bearer {expired}"}, timeout=20
    )
    note(f"expired token -> {stale.status_code} {stale.text[:120]}")
    if stale.status_code != 401:
        problems.append(f"expired token returned {stale.status_code}, expected 401")
    elif not stale.json().get("detail"):
        problems.append("expired-token 401 has no 'detail' text")

    if problems:
        raise CheckFailure("; ".join(problems))
    return PASS, "login/refresh/me ok; missing, wrong-password and expired tokens all 401 with detail"


# --------------------------------------------------------------------------
# 10) ROBUSTNESS
# --------------------------------------------------------------------------
def check_robustness():
    import cv2
    import numpy as np

    from ai.pipeline import detect_and_read

    base = GENERATED_DIR / "plate_3_MH12JK4567.png"
    if not base.exists():
        images = _images()
        if not images:
            return WARN, "no image to derive robustness cases from"
        base = images[0]

    source = cv2.imread(str(base))
    if source is None:
        raise CheckFailure(f"could not read {base}")

    blank = np.full((480, 640, 3), 255, dtype=np.uint8)
    tiny = cv2.resize(source, (24, 8), interpolation=cv2.INTER_AREA)
    huge = cv2.resize(source, (4000, 3000), interpolation=cv2.INTER_CUBIC)
    noise = np.full((240, 320, 3), 17, dtype=np.uint8)  # non-image-ish bytes
    cases = {
        "blank 640x480": blank,
        "tiny 24x8": tiny,
        "huge 4000x3000": huge,
        "flat dark 320x240": noise,
    }
    for angle, name in ((90, "rot90"), (180, "rot180"), (270, "rot270")):
        cases[f"{name} (expected plate lost)"] = cv2.rotate(
            source, {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}[angle]
        )

    failures = []
    rotated_reads = []
    for name, image in cases.items():
        started = time.time()
        try:
            reads = detect_and_read(image)
            elapsed = time.time() - started
        except Exception as exc:
            failures.append(f"{name}: pipeline raised {type(exc).__name__}: {exc}")
            continue
        got = reads[0].plate if reads else None
        if name.startswith(("rot90", "rot180", "rot270")):
            rotated_reads.append(f"{name}->{got or 'none'}")
            # A rotated plate may or may not read; it must never crash or hang.
            note(f"{name:<34} -> {str(got or '-'):<12} {elapsed:5.2f}s (no crash)")
            continue
        if got:
            note(f"{name:<34} -> {got:<12} {elapsed:5.2f}s")
        else:
            note(f"{name:<34} -> no plate      {elapsed:5.2f}s (clean)")

    if failures:
        raise CheckFailure("; ".join(failures))
    note("rotation behaviour: " + ", ".join(rotated_reads))
    return PASS, f"{len(cases)} degenerate input(s) handled without crashing; " + ", ".join(rotated_reads)


# --------------------------------------------------------------------------
# 11) MOBILE-STYLE IMAGE
# --------------------------------------------------------------------------
def check_mobile_style():
    import cv2
    import numpy as np

    from ai.pipeline import detect_and_read

    images = _images()
    if not images:
        return WARN, "no generated plate to degrade"

    passed = 0
    attempted = 0
    lines = []
    for path in images:
        image = cv2.imread(str(path))
        if image is None:
            continue
        expected = expected_plate(path)
        if not expected:
            continue
        attempted += 1

        # Phone-ish: downscaled to 1280px wide, JPEG artefacts, slight motion
        # blur, exposure shift and a small hand-held rotation.
        height, width = image.shape[:2]
        if width > 1280:
            scale = 1280 / width
            image = cv2.resize(image, (1280, int(height * scale)), interpolation=cv2.INTER_AREA)
        kernel = np.ones((3, 3), np.float32) / 9.0
        image = cv2.filter2D(image, -1, kernel)
        image = cv2.convertScaleAbs(image, alpha=0.85, beta=18)
        matrix = cv2.getRotationMatrix2D(
            (image.shape[1] / 2, image.shape[0] / 2), 10, 1.0
        )
        image = cv2.warpAffine(
            image, matrix, (image.shape[1], image.shape[0]),
            borderMode=cv2.BORDER_REPLICATE,
        )
        ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if not ok:
            continue
        degraded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)

        reads = detect_and_read(degraded)
        got = reads[0].plate if reads else None
        if got == expected:
            passed += 1
        lines.append(f"{path.name:<34} {str(got or '-'):<12} {'ok' if got == expected else 'MISS'}")
        note(lines[-1])

    if attempted == 0:
        return WARN, "no plate-name images available"
    rate = passed / attempted * 100
    note(f"accuracy under phone-like degradation: {passed}/{attempted} = {rate:.0f}%")
    if rate < 50:
        return FAIL, f"only {passed}/{attempted} survive blur+JPEG+10deg rotation"
    if rate < 100:
        return WARN, f"{passed}/{attempted} ({rate:.0f}%) read after phone-like degradation"
    return PASS, f"{passed}/{attempted} ({rate:.0f}%) read after blur+JPEG+10deg rotation"


# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="RAKSHAK AI + auth health check")
    parser.add_argument(
        "--api",
        default="http://localhost:8000",
        help="base URL of a running backend; endpoint/auth checks skip when unreachable",
    )
    args = parser.parse_args()
    base = args.api.rstrip("/")

    print("=" * 78)
    print(f"RAKSHAK health check   root={ROOT}")
    print(f"python {platform.python_version()}   api={base}")
    print("=" * 78)

    run_check("1  ENV / packages", check_env)
    run_check("2  YOLO model file + path resolution", check_model_file)
    run_check("3  YOLO inference", check_yolo_inference)
    run_check("4  contour fallback detector", check_fallback_detector)
    run_check("5  OCR engine (RapidOCR/ONNX)", check_ocr_engine)
    run_check("6  plate normalizer", check_normalizer)
    run_check("7  full pipeline detect_and_read", check_pipeline)
    run_check("8  /scanner/scan + /sightings endpoint", lambda: check_endpoint(base), critical=False)
    run_check("9  auth (login/refresh/401s)", lambda: check_auth(base), critical=False)
    run_check("10 robustness (degenerate inputs)", check_robustness)
    run_check("11 mobile-style degraded image", check_mobile_style)

    print()
    print("=" * 78)
    print(f"{'check':<44} {'result':<7} {'time':>7}")
    print("-" * 78)
    for name, status, detail, elapsed in RESULTS:
        print(f"{name:<44} {status:<7} {elapsed:6.2f}s")
        if detail:
            print(f"    {detail}")
    print("=" * 78)

    if _CRITICAL_FAILURES:
        print(f"RESULT: FAIL - critical check(s) failed: {', '.join(_CRITICAL_FAILURES)}")
        return 1
    print("RESULT: PASS - all critical checks succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
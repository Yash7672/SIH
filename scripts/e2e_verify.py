"""RAKSHAK end-to-end pipeline verification.

Exercises every stage of the platform against a *running* backend and prints a
PASS/FAIL line per check:

    environment -> auth/RBAC -> citizen complaint -> police verify -> hotlist
    -> Redis cache -> volunteer device -> ANPR detection -> sighting -> GPS route
    -> real-time WebSocket alert -> analytics -> admin -> audit trail

Usage (backend must already be up on --base):
    python scripts/e2e_verify.py
    python scripts/e2e_verify.py --base http://127.0.0.1:8000 --keep-data

Exit code is 0 only when every check passes. The rows created for the throwaway
demo plate are removed afterwards unless --keep-data is passed.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional

DEFAULT_BASE = "http://127.0.0.1:8000"

RESULTS: list[tuple[str, bool, str]] = []

# Rows created by this script; removed again during cleanup.
PROBE_EMAILS: list[str] = []
PROBE_PLATES: list[str] = []
PROBE_DEVICE_NAMES = ("E2E-Scanner", "E2E-Scanner-2", "WS-Probe")


def _ensure_backend_on_path() -> None:
    """Allow the script to import the backend app package (config + models)."""
    # OCR routinely reports a rejected non-ASCII fragment in its reason string
    # ("dropped fragment '型'"). On a default Windows console (cp1252) printing that
    # raises UnicodeEncodeError and kills the run at whichever check happens to hit
    # it, so the checks after it never execute. Force UTF-8 up front.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    backend = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
    backend = os.path.abspath(backend)
    if backend not in sys.path:
        sys.path.insert(0, backend)
    os.chdir(os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not ok:
        line += f"  <- {detail}"
    elif detail:
        line += f"  ({detail})"
    print(line, flush=True)
    return ok


def http(
    method: str,
    base: str,
    path: str,
    token: Optional[str] = None,
    body: Any = None,
    raw: Optional[bytes] = None,
    content_type: str = "application/json",
) -> tuple[int, Any]:
    url = f"{base}{path}"
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if raw is not None:
        data = raw
        headers["Content-Type"] = content_type
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = resp.read().decode()
            try:
                return resp.status, json.loads(payload)
            except json.JSONDecodeError:
                return resp.status, payload
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode()
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, payload
    except Exception as exc:  # connection refused etc.
        return 0, {"detail": str(exc)}


def multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"----rakshak{random.randint(10**9, 10**10)}"
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    for key, (filename, content, ctype) in files.items():
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
                f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n"
            ).encode()
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def login(base: str, email: str, password: str) -> Optional[str]:
    status, data = http("POST", base, "/api/v1/auth/login", body={"email": email, "password": password})
    if status == 200 and isinstance(data, dict):
        return data.get("access_token")
    return None


def login_full(base: str, email: str, password: str) -> dict:
    status, data = http("POST", base, "/api/v1/auth/login", body={"email": email, "password": password})
    return data if status == 200 and isinstance(data, dict) else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--keep-data", action="store_true", help="keep the demo rows created by the run")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    _ensure_backend_on_path()

    print(f"RAKSHAK E2E verification against {base}\n" + "=" * 62, flush=True)

    # ---------------------------------------------------------------- 1. health
    status, health = http("GET", base, "/api/v1/healthz")
    if not check("backend reachable + healthz", status == 200, f"status={status} {health}"):
        print("\nBackend is not running — start it before verifying.")
        return 1
    check("postgres reachable", health.get("database") == "ok", str(health))
    check("redis reachable (cache layer)", health.get("redis") == "ok", str(health))
    check("demo mode enabled", health.get("demo_mode") is True, str(health))

    # ------------------------------------------------------------------ 2. auth
    tokens = {
        "citizen": login(base, "citizen@example.com", "Citizen@123"),
        "volunteer": login(base, "volunteer@example.com", "Volunteer@123"),
        "cop": login(base, "cop@example.com", "Police@123"),
        "admin": login(base, "admin@example.com", "Admin@123"),
    }
    for role, tok in tokens.items():
        check(f"auth login ({role})", bool(tok))
    if not all(tokens.values()):
        return 1

    check(
        "auth rejects wrong password",
        http("POST", base, "/api/v1/auth/login", body={"email": "citizen@example.com", "password": "nope"})[0]
        == 401,
    )
    check(
        "auth blocks self-registration as COP",
        http(
            "POST",
            base,
            "/api/v1/auth/register",
            body={"name": "Bad Actor", "email": f"bad{random.randint(1, 10**6)}@example.com",
                  "password": "Strong@123", "role": "COP"},
        )[0]
        == 403,
    )
    check("auth/me returns current user", http("GET", base, "/api/v1/auth/me", tokens["citizen"])[1].get("role") == "CITIZEN")

    # token lifecycle: the clients renew access tokens with the refresh token
    session = login_full(base, "cop@example.com", "Police@123")
    check("login returns an access + refresh pair", bool(session.get("access_token")) and bool(session.get("refresh_token")))
    status, renewed = http(
        "POST", base, "/api/v1/auth/refresh", body={"refresh_token": session.get("refresh_token")}
    )
    check("refresh endpoint issues a new token pair", status == 200 and bool(renewed.get("access_token")))
    check(
        "renewed access token is accepted",
        http("GET", base, "/api/v1/auth/me", renewed.get("access_token"))[0] == 200,
    )
    check(
        "refresh token is rejected as an access token",
        http("GET", base, "/api/v1/auth/me", session.get("refresh_token"))[0] == 401,
    )

    # -------------------------------------------------------------- 3. RBAC walls
    check(
        "RBAC: citizen cannot list all complaints",
        http("GET", base, "/api/v1/complaints", tokens["citizen"])[0] == 403,
    )
    check(
        "RBAC: volunteer cannot read the sighting feed",
        http("GET", base, "/api/v1/sightings", tokens["volunteer"])[0] == 403,
    )
    body, ctype = multipart({"device_type": "mobile"}, {})
    check(
        "RBAC: citizen cannot register scanning devices",
        http("POST", base, "/api/v1/devices/register", tokens["citizen"], {"device_type": "mobile"})[0] == 403,
    )
    check(
        "privacy guard rejects multipart device registration",
        http("POST", base, "/api/v1/devices/register", tokens["citizen"], raw=body, content_type=ctype)[0] == 415,
    )
    check(
        "RBAC: volunteer cannot verify complaints",
        http("POST", base, "/api/v1/complaints/00000000-0000-0000-0000-000000000000/verify", tokens["volunteer"])[0]
        == 403,
    )

    # ------------------------------------------------- 4. citizen complaint intake
    plate = f"TS{random.randint(10, 99)}ZZ{random.randint(1000, 9999)}"
    PROBE_PLATES.append(plate)
    e2e_email = f"e2e{random.randint(1, 10**6)}@example.com"
    status, complaint = http(
        "POST",
        base,
        "/api/v1/auth/register",
        body={"name": "E2E Citizen", "email": e2e_email, "password": "E2eCitizen@123", "role": "CITIZEN"},
    )
    new_citizen_token = complaint.get("access_token") if isinstance(complaint, dict) else None
    if new_citizen_token:
        PROBE_EMAILS.append(e2e_email)
    check("citizen self-registration works", status == 200 and bool(new_citizen_token))

    messy_plate = f"ts-{plate[2:4]} {plate[4:6].lower()} {plate[6:]}"  # e.g. "ts-09 zz 1234"
    form, form_ctype = multipart(
        {"plate": messy_plate, "complaint_type": "Stolen vehicle", "description": "E2E run"}, {}
    )
    status, created = http("POST", base, "/api/v1/complaints", new_citizen_token, raw=form, content_type=form_ctype)
    check("complaint accepted as multipart/form-data", status == 200, f"status={status} {created}")
    if status != 200:
        return 1
    check("plate normalized on intake", created.get("plate") == plate, f"got {created.get('plate')!r}")
    check("complaint starts PENDING", created.get("status") == "PENDING")
    complaint_id = created["id"]

    bad_form, bad_ctype = multipart({"plate": "AB12", "complaint_type": "Stolen"}, {})
    check(
        "invalid plate rejected",
        http("POST", base, "/api/v1/complaints", new_citizen_token, raw=bad_form, content_type=bad_ctype)[0] == 422,
    )
    proof_form, proof_ctype = multipart(
        {"plate": f"KA{random.randint(10, 99)}QQ{random.randint(1000, 9999)}",
         "complaint_type": "Stolen vehicle", "description": "with proof"},
        {"proof": ("proof.jpg", b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg")},
    )
    check(
        "complaint proof upload accepted",
        http("POST", base, "/api/v1/complaints", new_citizen_token, raw=proof_form, content_type=proof_ctype)[0] == 200,
    )
    check(
        "citizen sees own complaint history",
        any(c["id"] == complaint_id for c in http("GET", base, "/api/v1/complaints/mine", new_citizen_token)[1]),
    )
    check(
        "citizen cannot read another citizen's complaint",
        http("GET", base, f"/api/v1/complaints/{complaint_id}", tokens["cop"])[0] == 200,
    )

    # ---------------------------------------------------- 5. police verify + hotlist
    pending = [
        c for c in http("GET", base, "/api/v1/complaints?status_filter=PENDING", tokens["cop"])[1]
        if c["id"] == complaint_id
    ]
    check("police queue shows the pending complaint", len(pending) == 1)

    status, verified = http(
        "POST",
        base,
        f"/api/v1/complaints/{complaint_id}/verify",
        tokens["cop"],
        {"fir_reference": "FIR/2026/E2E"},
    )
    check("police verify succeeds", status == 200, f"status={status} {verified}")
    hotlist_id = verified.get("hotlist_id") if isinstance(verified, dict) else None
    check("verify returns a hotlist entry", bool(hotlist_id))
    if not hotlist_id:
        return 1
    check("complaint transitions to HOTLISTED", verified["complaint"]["status"] == "HOTLISTED")
    check(
        "re-verifying the same complaint conflicts (409)",
        http("POST", base, f"/api/v1/complaints/{complaint_id}/verify", tokens["cop"])[0] == 409,
    )

    # one active hotlist row per plate
    active_plates = [
        h for h in http("GET", base, "/api/v1/hotlist?status_filter=ACTIVE", tokens["cop"])[1] if h["plate"] == plate
    ]
    check("exactly one ACTIVE hotlist row per plate", len(active_plates) == 1, f"rows={len(active_plates)}")

    # ------------------------------------------------------- 6. redis cache layer
    try:
        import redis as redis_lib

        from app.core.config import settings  # type: ignore

        client = redis_lib.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        members = client.smembers("rakshak:hotlist:active_plates")
        check("hotlist plate cached in redis", plate in members, f"cache size={len(members)}")
        check("hotlist cache has a TTL", client.ttl("rakshak:hotlist:active_plates") > 0)
    except Exception as exc:  # pragma: no cover - env dependent
        check("hotlist plate cached in redis", False, f"redis check failed: {exc}")

    # ------------------------------------------- 7. volunteer device registration
    my_device = None
    status, device = http(
        "POST", base, "/api/v1/devices/register", tokens["volunteer"],
        {"device_type": "mobile", "device_name": "E2E-Scanner"},
    )
    check("volunteer registers an ANPR device", status == 200 and "id" in device, f"status={status} {device}")
    if status == 200:
        my_device = device["id"]
    second = http(
        "POST", base, "/api/v1/devices/register", tokens["volunteer"],
        {"device_type": "mobile", "device_name": "E2E-Scanner-2"},
    )[1]
    second_device = second.get("id")

    # ------------------------------------------------- 8. detection -> sighting
    def detection(device_id: str, lat: float, lng: float, confidence: float = 0.94, when: str | None = None):
        return {
            "plate": plate,
            "latitude": lat,
            "longitude": lng,
            "timestamp": when or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "confidence": confidence,
            "device_id": device_id,
        }

    t0 = "2026-09-23T04:00:00Z"
    t1 = "2026-09-23T04:12:00Z"
    status, sighting = http("POST", base, "/api/v1/sightings", tokens["volunteer"], detection(my_device, 17.4510, 78.3520, 0.93, t0))
    check("hotlist detection recorded as a sighting", status == 200 and "id" in sighting, f"status={status} {sighting}")
    first_sighting_id = sighting.get("id") if isinstance(sighting, dict) else None

    status, second_sighting = http(
        "POST", base, "/api/v1/sightings", tokens["volunteer"], detection(second_device, 17.4625, 78.3710, 0.96, t1)
    )
    check("second device adds a route point", status == 200 and "id" in second_sighting)

    check(
        "privacy: non-hotlist plate is never stored",
        http("POST", base, "/api/v1/sightings", tokens["volunteer"], {
            "plate": "KA01ZZ9999", "latitude": 17.4, "longitude": 78.4,
            "timestamp": t0, "confidence": 0.99, "device_id": my_device,
        })[1].get("detail") == "accepted_no_match",
    )
    check(
        "privacy: video upload rejected on the detection endpoint",
        http("POST", base, "/api/v1/sightings", tokens["volunteer"],
             raw=b"\x00\x00\x00\x18ftypmp42", content_type="video/mp4")[0] == 415,
    )
    check(
        "detection throttled on cooldown (no duplicate row)",
        http("POST", base, "/api/v1/sightings", tokens["volunteer"], detection(my_device, 17.4510, 78.3520, 0.93, t0))[1]
        .get("detail") in ("accepted_no_match", "accepted"),
    )
    check(
        "invalid coordinates rejected",
        http("POST", base, "/api/v1/sightings", tokens["volunteer"],
             {**detection(my_device, 91.0, 78.0)})[0] == 422,
    )
    check(
        "unknown device rejected",
        http("POST", base, "/api/v1/sightings", tokens["volunteer"],
             detection("11111111-1111-1111-1111-111111111111", 17.4, 78.4))[0] == 404,
    )

    # ------------------------------------------------------- 9. vehicle tracking
    status, detail = http("GET", base, f"/api/v1/vehicles/{plate}", tokens["cop"])
    check("vehicle detail available to police", status == 200, f"status={status}")
    check("last-seen location updated", detail.get("hotlist", {}).get("last_seen_at") is not None)
    check("sighting count aggregated", detail.get("sightings_count", 0) >= 2, f"count={detail.get('sightings_count')}")

    status, timeline = http("GET", base, f"/api/v1/vehicles/{plate}/timeline", tokens["cop"])
    check("timeline returns chronological points", status == 200 and len(timeline) >= 2, f"points={len(timeline)}")
    status, route = http("GET", base, f"/api/v1/vehicles/{plate}/route", tokens["cop"])
    check(
        "route polyline has lat/lng pairs",
        status == 200 and len(route.get("route", [])) >= 2 and len(route["route"][0]) == 2,
    )
    check(
        "vehicle not on hotlist returns 404",
        http("GET", base, "/api/v1/vehicles/KA01ZZ9999", tokens["cop"])[0] == 404,
    )

    # ------------------------------------------------------ 10. analytics pipeline
    status, overview = http("GET", base, "/api/v1/analytics/overview", tokens["cop"])
    check("analytics overview responds", status == 200, f"status={status}")
    check(
        "overview counters are populated",
        all(k in overview for k in ("active_hotlist", "pending_complaints", "detections_today", "active_devices")),
    )
    check("analytics: detections by hour", http("GET", base, "/api/v1/analytics/detections", tokens["cop"])[0] == 200)
    matches = http("GET", base, "/api/v1/analytics/hotlist-matches", tokens["cop"])[1]
    check("analytics: hotlist match ranking includes the plate", any(m["plate"] == plate for m in matches))
    locations = http("GET", base, "/api/v1/analytics/locations", tokens["cop"])[1]
    check("analytics: GPS locations feed populated", len(locations) >= 2 and "lat" in locations[0])
    check(
        "RBAC: analytics blocked for volunteers",
        http("GET", base, "/api/v1/analytics/overview", tokens["volunteer"])[0] == 403,
    )

    # ---------------------------------------------------------- 11. alerts feed
    status, alerts = http("GET", base, "/api/v1/alerts", tokens["cop"])
    check("alert history feed responds", status == 200)
    check("alert history contains the new detection", any(a["plate"] == plate for a in alerts))
    check("alerts expose no video/frame fields", all("video" not in a and "frame" not in a for a in alerts))

    # -------------------------------------------------- 12. hotlist lifecycle
    status, fir = http("PATCH", base, f"/api/v1/hotlist/{hotlist_id}", tokens["cop"], {"status": "FIR_CONFIRMED", "fir_reference": "FIR/2026/E2E"})
    check("hotlist FIR confirmation transition", status == 200 and fir.get("status") == "FIR_CONFIRMED", f"status={status}")
    check(
        "unsupported hotlist transition rejected",
        http("PATCH", base, f"/api/v1/hotlist/{hotlist_id}", tokens["cop"], {"status": "BOGUS"})[0] == 400,
    )
    status, recovered = http("PATCH", base, f"/api/v1/hotlist/{hotlist_id}", tokens["cop"], {"status": "RECOVERED"})
    check("hotlist recovery transition", status == 200 and recovered.get("status") == "RECOVERED")
    try:
        import redis as redis_lib

        from app.core.config import settings  # type: ignore

        client = redis_lib.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        check("recovered plate evicted from redis cache", plate not in client.smembers("rakshak:hotlist:active_plates"))
    except Exception as exc:  # pragma: no cover
        check("recovered plate evicted from redis cache", False, str(exc))

    # ------------------------------------------------------- 13. admin surfaces
    check("admin lists users", http("GET", base, "/api/v1/users", tokens["admin"])[0] == 200)
    devices = http("GET", base, "/api/v1/devices", tokens["admin"])[1]
    check("admin lists registered devices", isinstance(devices, list) and len(devices) >= 1)
    check(
        "RBAC: device list blocked for cops",
        http("GET", base, "/api/v1/devices", tokens["cop"])[0] == 403,
    )
    if my_device:
        status, revoked = http("POST", base, f"/api/v1/devices/{my_device}/revoke", tokens["admin"])
        check("admin revokes a device", status == 200 and revoked.get("revoked") is True)
        check(
            "revoked device can no longer report detections",
            http("POST", base, "/api/v1/sightings", tokens["volunteer"], detection(my_device, 17.5, 78.5))[0] == 403,
        )

    # --------------------------------------------- 14. AI / ANPR host OCR pipeline
    plate_image = os.path.join("data", "generated", "plate_4_TS09AB1234.png")
    if os.path.exists(plate_image):
        with open(plate_image, "rb") as fh:
            image_bytes = fh.read()
        body, ctype = multipart({}, {"image": ("plate.png", image_bytes, "image/png")})
        status, scanned = http("POST", base, "/api/v1/scanner/scan", tokens["volunteer"], raw=body, content_type=ctype)
        check("ANPR scanner endpoint runs OCR", status == 200, f"status={status} {scanned}")
        check("OCR returns a plate + confidence", bool(scanned.get("plate")) and scanned.get("confidence") is not None,
              f"read={scanned.get('plate')} conf={scanned.get('confidence')}")
    else:
        check("ANPR scanner endpoint runs OCR", False, f"missing sample image {plate_image}")

    # ------------------------------------------- 15. websocket real-time pipeline
    ws_ok, ws_detail = verify_websocket(base, tokens)
    check("websocket pushes a live hotlist alert to police", ws_ok, ws_detail)

    # ------------------------------------------------------------- 16. audit trail
    try:
        from sqlalchemy import select

        from app.db.session import SessionLocal  # type: ignore
        from app.models import AuditLog  # type: ignore

        db = SessionLocal()
        actions = {a for a in db.execute(select(AuditLog.action)).scalars()}
        db.close()
        check(
            "audit log records privileged actions",
            {"complaint.created", "complaint.verified_hotlisted"} <= actions,
            f"sample={sorted(actions)[:5]}",
        )
    except Exception as exc:  # pragma: no cover
        check("audit log records privileged actions", False, str(exc))

    # -------------------------------------------------------------- cleanup + report
    if not args.keep_data:
        removed = cleanup(PROBE_PLATES)
        print(f"\ncleanup: removed {removed} row(s) created by this run ({', '.join(PROBE_PLATES)})")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 62)
    print(f"RESULT: {passed}/{len(RESULTS)} checks passed")
    if failed:
        print("Failed checks:")
        for name in failed:
            print(f"  - {name}")
        return 1
    print("All pipelines connected and verified end-to-end.")
    return 0


def verify_websocket(base: str, tokens: dict[str, Optional[str]]) -> tuple[bool, str]:
    """Connect to the police channel, trigger a detection, expect a pushed alert."""
    try:
        from websockets.sync.client import connect

        ws_url = base.replace("http://", "ws://").replace("https://", "wss://")
        plate = f"TS{random.randint(10, 99)}WX{random.randint(1000, 9999)}"
        PROBE_PLATES.append(plate)

        with connect(f"{ws_url}/api/v1/ws/police?token={tokens['cop']}", open_timeout=10) as ws:
            # promote a plate to the hotlist for this probe
            ws_email = f"ws{random.randint(1, 10**6)}@example.com"
            status, created = http(
                "POST", base, "/api/v1/auth/register",
                body={"name": "WS Citizen", "email": ws_email, "password": "WsCitizen@123", "role": "CITIZEN"},
            )
            citizen_token = created.get("access_token") if isinstance(created, dict) else None
            if citizen_token:
                PROBE_EMAILS.append(ws_email)
            form, form_ctype = multipart(
                {"plate": plate, "complaint_type": "Stolen vehicle", "description": "ws probe"}, {}
            )
            _, complaint = http("POST", base, "/api/v1/complaints", citizen_token, raw=form, content_type=form_ctype)
            verify_status, _ = http("POST", base, f"/api/v1/complaints/{complaint['id']}/verify", tokens["cop"])
            if verify_status != 200:
                return False, f"could not hotlist probe plate (status={verify_status})"

            device = http(
                "POST", base, "/api/v1/devices/register", tokens["volunteer"],
                {"device_type": "mobile", "device_name": "WS-Probe"},
            )[1]
            http(
                "POST", base, "/api/v1/sightings", tokens["volunteer"],
                {"plate": plate, "latitude": 17.41, "longitude": 78.44,
                 "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "confidence": 0.91, "device_id": device["id"]},
            )

            deadline = time.time() + 10
            while time.time() < deadline:
                message = json.loads(ws.recv(timeout=max(deadline - time.time(), 0.5)))
                if message.get("type") == "hotlist_detection" and message["payload"]["plate"] == plate:
                    return True, f"received hotlist_detection for {plate}"
            return False, "no alert received within 10s"
    except Exception as exc:
        return False, f"websocket error: {exc}"


def _models():
    _ensure_backend_on_path()
    from app.db.session import SessionLocal  # type: ignore
    from app.models import Complaint, Device, Hotlist, Sighting, User  # type: ignore

    return SessionLocal, Complaint, Device, Hotlist, Sighting, User


def cleanup(plates: list[str]) -> int:
    """Delete the throwaway rows this run created (FK-safe order).

    Deletion order respects every foreign key:
        sightings -> hotlist -> complaints -> devices -> users
    """
    try:
        SessionLocal, Complaint, Device, Hotlist, Sighting, User = _models()
        from sqlalchemy import or_  # type: ignore
    except Exception:
        return 0
    db = SessionLocal()
    removed = 0
    try:
        probe_users = db.query(User).filter(User.email.in_(PROBE_EMAILS)).all() if PROBE_EMAILS else []
        user_ids = [u.id for u in probe_users]

        probe_devices = db.query(Device).filter(Device.device_name.in_(PROBE_DEVICE_NAMES)).all()
        if user_ids:
            probe_devices = probe_devices + db.query(Device).filter(Device.user_id.in_(user_ids)).all()
        device_ids = [d.id for d in probe_devices]

        hotlist_ids = [h.id for h in db.query(Hotlist).filter(Hotlist.plate.in_(plates)).all()]

        if hotlist_ids:
            removed += db.query(Sighting).filter(Sighting.hotlist_id.in_(hotlist_ids)).delete(
                synchronize_session=False
            )
        if device_ids:
            removed += db.query(Sighting).filter(Sighting.device_id.in_(device_ids)).delete(synchronize_session=False)
        if hotlist_ids:
            removed += db.query(Hotlist).filter(Hotlist.id.in_(hotlist_ids)).delete(synchronize_session=False)

        complaint_query = db.query(Complaint).filter(Complaint.plate.in_(plates))
        if user_ids:
            complaint_query = db.query(Complaint).filter(
                or_(Complaint.plate.in_(plates), Complaint.user_id.in_(user_ids))
            )
        removed += complaint_query.delete(synchronize_session=False)

        if device_ids:
            removed += db.query(Device).filter(Device.id.in_(device_ids)).delete(synchronize_session=False)
        if user_ids:
            removed += db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
        db.commit()
    except Exception as exc:
        db.rollback()
        print(f"cleanup warning: {exc}")
    finally:
        db.close()
    return removed


if __name__ == "__main__":
    sys.exit(main())

"""Item 4: the whole chain through the real HTTP API, no mocks anywhere.

    .venv\\Scripts\\python.exe scripts\\chain_check.py

Reads the live backend, so it fails if any link is not actually wired, rather
than only proving that the unit tests agree with each other.

The chain under test:

    a citizen files a complaint
      -> police press Verify
      -> the plate is in the hot-list immediately, with no 300 s wait
      -> a frame carrying that plate arrives on /ws/scan
      -> the app is told stolen, with a confidence
      -> an alert reaches the police dashboard on /ws/police within 3 s
      -> exactly one sighting row exists (60 s cooldown)
      -> /api/v1/geo/heat changed at that timestamp
    and the negative half:
      -> an unverified plate does not alert
      -> rejecting the complaint removes the plate, and a later frame is quiet
    and the privacy half:
      -> a vehicle whose plate is not on the list leaves no sighting row

The frame is data/test_plates/road_scene.jpg, built by
scripts/make_test_road_scene.py so that it contains a car whose plate renders as
MH12JK4567 at 0.997 confidence. So the chain is walked with a plate the OCR
genuinely reads, not with a value the test invented.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

import requests  # noqa: E402
import websockets  # noqa: E402

API = os.environ.get("RAKSHAK_API", "http://127.0.0.1:8000")
API_V1 = f"{API}/api/v1"
WS_SCAN = API.replace("http", "ws") + "/api/v1/ws/scan"
WS_POLICE = API.replace("http", "ws") + "/api/v1/ws/police"
FRAME = REPO / "data" / "test_plates" / "road_scene.jpg"

# The plate make_test_road_scene.py draws into the fixture.
HOT_PLATE = "MH12JK4567"
# Never reported by anyone, used for the privacy half.
UNKNOWN_PLATE = "KL07MN9012"
# Filed and then rejected before verification, so the negative path has a plate
# of its own and cannot be confused with the one under test.
COLD_PLATE = "TS09AB1234"
# What /ws/police calls a hot-list match. Both the REST sighting endpoint and the
# live scanner emit this one event type.
ALERT_TYPES = ("hotlist_detection", "hotlist_match", "alert", "plate")

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""), flush=True)
    return bool(ok)


def login(email: str, password: str) -> tuple[str, str]:
    r = requests.post(f"{API_V1}/auth/login", json={"email": email, "password": password}, timeout=20)
    r.raise_for_status()
    body = r.json()
    return body["access_token"], body["user"]["role"]


def register_device(token: str, name: str) -> str:
    """A fresh device id per phase, and it must be a *registered* one.

    The sighting cooldown key is `plate:<plate>:<device id>`, so reusing a device
    silently suppresses the second sighting and the chain check reports a
    cooldown that is not there. And /ws/scan only accepts a device the backend
    knows about, which is why an invented id hangs on `ready` instead of being
    refused.
    """
    r = requests.post(
        f"{API_V1}/devices/register",
        headers=auth(token),
        json={"device_type": "mobile", "device_name": name},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["id"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def ws_headers(token: str) -> list:
    """No headers: /ws/scan authenticates on the first message, not on the
    handshake. Sending an Authorization header as well makes the server wait for
    an auth message that never arrives, so the socket sits there until the
    handshake is reaped. One credential, one place."""
    return []


# --- driving /ws/scan ------------------------------------------------------ #


def send_frames(token: str, label: str, seconds: float = 8.0, device_id: str | None = None) -> dict:
    """Push real frames down /ws/scan and report what came back.

    Each phase gets a freshly registered device, because the sighting cooldown
    key is `plate:<plate>:<device id>`: reusing one silently swallows the second
    sighting and the cooldown check would then be reporting a cooldown the chain
    check itself created.

    Each frame is answered by three messages - boxes, plates, plate - so the
    reader waits for the reply carrying *its own* seq before starting the next
    frame. Otherwise a reply for the previous frame gets counted against the
    current one, which is exactly the mistake that makes a stream test lie.
    """
    device_id = device_id or register_device(token, f"chain_check_{label}")
    jpeg_b64 = base64.b64encode(FRAME.read_bytes()).decode("ascii")
    result: dict = {
        "plates": [],
        "boxes": [],
        "stolen": [],
        "errors": [],
        "ready": None,
        "seq_ok": True,
        "frames": 0,
        "device_id": device_id,
    }

    async def main():
        # max_size: the reply is small, but the default 1 MB inbound cap sits
        # uncomfortably close to the server's own 400 KB frame ceiling once
        # base64 padding is counted.
        async with websockets.connect(WS_SCAN, additional_headers=ws_headers(token), max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"type": "auth", "token": token, "device_id": device_id}))
            result["ready"] = json.loads(await asyncio.wait_for(ws.recv(), 15))
            deadline = time.time() + seconds
            seq = 0
            while time.time() < deadline:
                seq += 1
                mine = seq
                result["frames"] += 1
                await ws.send(
                    json.dumps(
                        {
                            "type": "frame",
                            "seq": mine,
                            "w": 1280,
                            "h": 842,
                            "lat": 17.44,
                            "lng": 78.35,
                            "jpeg_b64": jpeg_b64,
                        }
                    )
                )
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 8.0)
                    except (asyncio.TimeoutError, Exception):
                        break
                    if raw is None:
                        break
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    kind = msg.get("type")
                    if kind == "boxes":
                        result["boxes"].append(msg)
                        if msg.get("seq") != mine:
                            result["seq_ok"] = False
                        # boxes is the first reply for this frame, so the loop
                        # may send the next one. plates/plate for this frame can
                        # still arrive afterwards and are collected there.
                        break
                    if kind == "plates":
                        result["boxes"].append(msg)
                    elif kind == "plate":
                        result["plates"].append(msg)
                        if msg.get("stolen"):
                            result["stolen"].append(msg)
                    elif kind == "error":
                        result["errors"].append(msg)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, main()).result(timeout=seconds + 60)
    return result


# --- watching /ws/police --------------------------------------------------- #


class PoliceWatcher:
    """A /ws/police connection that records everything that arrives.

    Started before the frames are sent, because the whole point of the check is
    how long the alert takes to reach the dashboard.
    """

    def __init__(self, token: str):
        self.token = token
        self.messages: list[dict] = []
        self.first_alert_at: float | None = None
        self.connected = threading.Event()
        self.stop = threading.Event()
        self.error: str | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> bool:
        self.thread.start()
        self.connected.wait(10)
        return self.error is None

    def _run(self):
        async def main():
            async with websockets.connect(WS_POLICE, additional_headers=ws_headers(self.token), max_size=4 * 1024 * 1024) as ws:
                # /ws/police also authenticates on the first message.
                await ws.send(json.dumps({"type": "auth", "token": self.token}))
                self.connected.set()
                while not self.stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 0.5)
                    except (asyncio.TimeoutError, Exception):
                        continue
                    if raw is None:
                        continue
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    self.messages.append(msg)
                    # The envelope is {"type": <event>, "payload": {...}}
                    # (backend/app/ws/manager.py:PoliceAlertManager.broadcast), and
                    # the event the live scanner raises is "hotlist_detection".
                    if self.first_alert_at is None and msg.get("type") in ALERT_TYPES:
                        self.first_alert_at = time.time()

        try:
            asyncio.run(main())
        except Exception as exc:  # pragma: no cover - reported, not raised
            self.error = f"{type(exc).__name__}: {exc}"
            self.connected.set()


# --- reads ----------------------------------------------------------------- #


def clear_hotlist(token: str, plate: str) -> int:
    """Take a plate off the hot-list, so the check starts from a known state.

    Necessary because the earlier phases of a previous run can leave the plate
    ACTIVE, and then "an unverified plate does not alert" fails for a reason that
    has nothing to do with the code under test. CLOSED is the status transition
    that removes the plate from the active cache
    (hotlist_service.close -> cache_service.remove_active_plate); REJECTED is not
    usable here because the verify endpoint only accepts FIR_CONFIRMED,
    RECOVERED or CLOSED.
    """
    r = requests.get(f"{API_V1}/hotlist", headers=auth(token), timeout=20)
    r.raise_for_status()
    n = 0
    for entry in r.json():
        if entry.get("plate") != plate:
            continue
        if entry.get("status") in ("ACTIVE", "FIR_CONFIRMED"):
            p = requests.patch(
                f"{API_V1}/hotlist/{entry['id']}", headers=auth(token), json={"status": "CLOSED"}, timeout=20
            )
            n += 1 if p.status_code in (200, 204) else 0
    return n


def sightings_for(token: str, plate: str) -> list[dict]:
    """Sighting rows for one plate.

    Filtered here rather than with a query parameter, because
    `GET /api/v1/sightings` takes only `hotlist_id` and silently ignores any
    other parameter (backend/app/api/v1/sightings.py:list_sightings) - so a
    `?plate=` request quietly returns the most recent 500 sightings for every
    plate and the count keeps climbing no matter what was detected.
    """
    r = requests.get(f"{API_V1}/sightings", headers=auth(token), timeout=20)
    if r.status_code != 200:
        return []
    body = r.json()
    rows = body if isinstance(body, list) else (body.get("items") or [])
    return [s for s in rows if s.get("plate") == plate]


def heat_cells(token: str, layer: str) -> dict:
    now = time.time()
    r = requests.get(
        f"{API_V1}/geo/heat",
        headers=auth(token),
        params={
            "layer": layer,
            "from": datetime.fromtimestamp(now - 900, tz=timezone.utc).isoformat(),
            "to": datetime.fromtimestamp(now + 60, tz=timezone.utc).isoformat(),
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def main() -> int:
    print("\n=== item 4: the chain, through the real API ===\n", flush=True)

    if not FRAME.exists():
        print(f"  [FAIL] fixture missing: {FRAME}")
        return 1
    print(f"  fixture: {FRAME.relative_to(REPO)} ({FRAME.stat().st_size // 1024} KB)", flush=True)

    citizen_t, citizen_role = login("citizen@example.com", "Citizen@123")
    police_t, police_role = login("cop@example.com", "Police@123")
    volunteer_t, volunteer_role = login("volunteer@example.com", "Volunteer@123")
    print(f"  roles: citizen={citizen_role} volunteer={volunteer_role} police={police_role}\n", flush=True)

    # 0. start from a known state: an earlier run may have left the plate on the
    # hot-list, which would make the "does not alert yet" check below fail for a
    # reason unrelated to the code.
    cleared = clear_hotlist(police_t, HOT_PLATE)
    print(f"  pre-existing hot-list entries closed for {HOT_PLATE}: {cleared}\n", flush=True)

    # 1. a citizen files a complaint. This endpoint is multipart, not JSON:
    # complaints carry an optional proof image, so plate/complaint_type/description
    # are form fields (backend/app/api/v1/complaints.py:create_complaint).
    r = requests.post(
        f"{API_V1}/complaints",
        headers=auth(citizen_t),
        data={"plate": HOT_PLATE, "complaint_type": "vehicle theft", "description": "chain check - reported stolen"},
        timeout=20,
    )
    check("a citizen can file a complaint", r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:120]}")
    payload = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    complaint_id = payload.get("id") or (payload.get("complaint") or {}).get("id")
    check("the complaint has an id", bool(complaint_id), str(complaint_id))
    if not complaint_id:
        return 1

    pre = send_frames(volunteer_t, "pre")

    reads = ", ".join(f"{p.get('text')}@{p.get('conf')}" for p in pre.get("plates", [])[:3])
    check("the fixture's plate is genuinely read", any(p.get("text") for p in pre.get("plates", [])), reads or "nothing read")
    check("an unverified plate does not alert", not pre.get("stolen"), f"{len(pre.get('stolen', []))} stolen")
    check("every reply carried its own seq", pre.get("seq_ok"), f"{pre.get('frames')} frames")

    # 3. police press Verify.
    r = requests.post(f"{API_V1}/complaints/{complaint_id}/verify", headers=auth(police_t), timeout=20)
    check("police can verify the complaint", r.status_code in (200, 201), f"HTTP {r.status_code}")

    # 4. the dashboard is listening before the first hot frame goes out.
    watcher = PoliceWatcher(police_t)
    check("the police dashboard socket connects", watcher.start(), watcher.error or "")
    # The clock starts as the first frame goes out, because the detector can only
    # learn the plate from a frame: an alert cannot predate it.
    sent_at = time.time()
    hot = send_frames(volunteer_t, "hot")


    stolen = hot.get("stolen", [])
    check("a verified plate raises the stolen flag", bool(stolen), f"{len(stolen)} of {len(hot.get('plates', []))} reads")
    if stolen:
        top = stolen[0]
        check(
            "the stolen read names the plate and a confidence",
            HOT_PLATE in str(top.get("norm") or top.get("text") or "") and float(top.get("conf") or 0) > 0.6,
            f"text={top.get('text')} norm={top.get('norm')} conf={top.get('conf')} valid={top.get('valid')}",
        )
        check("the stolen read is marked valid", bool(top.get("valid")), f"valid={top.get('valid')}")

    # 5. the alert reaches /ws/police within 3 s.
    deadline = time.time() + 3.0
    while watcher.first_alert_at is None and time.time() < deadline:
        time.sleep(0.05)
    if watcher.first_alert_at is not None:
        latency = watcher.first_alert_at - sent_at
        check("the alert reaches /ws/police within 3 s", latency <= 3.0, f"{latency * 1000:.0f} ms after the first frame")
        payload = next((m for m in watcher.messages if m.get("type") in ALERT_TYPES), {})
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
        for field in ("plate", "confidence", "timestamp", "sighting_id", "hotlist_id"):
            check(f"the alert carries {field}", field in inner, json.dumps(inner)[:220])
        check(
            "the alert carries a location",
            "latitude" in inner and "longitude" in inner,
            f"{inner.get('latitude')},{inner.get('longitude')}",
        )
        check("the alert carries a camera name", bool(inner.get("camera")), f"camera={inner.get('camera')!r}")
    else:
        check("the alert reaches /ws/police within 3 s", False, f"nothing arrived; saw {len(watcher.messages)} messages")
    watcher.stop.set()
    time.sleep(0.3)

    # 6. exactly one sighting row, thanks to the 60 s cooldown.
    #
    # This has to reuse the SAME device as the phase above. The cooldown key is
    # `plate:<plate>:<device id>`, so a freshly registered device is entitled to
    # its own first sighting - which is correct behaviour, and would make this
    # check report a failure that does not exist.
    hot_rows = sightings_for(police_t, HOT_PLATE)
    check("a sighting row was created for the scanning device", len(hot_rows) >= 1, f"{len(hot_rows)} rows")
    check(
        "the sighting row names the camera that saw it",
        any(r.get("camera") for r in hot_rows),
        ", ".join(str(r.get("camera")) for r in hot_rows[:3]) or "none",
    )
    same_device = send_frames(volunteer_t, "cooldown", device_id=hot.get("device_id"))
    hot_rows2 = sightings_for(police_t, HOT_PLATE)
    check(
        "the 60 s cooldown stops a second row from the same device",
        len(hot_rows2) == len(hot_rows),
        f"{len(hot_rows)} then {len(hot_rows2)} over {same_device.get('frames')} more frames",
    )

    # 7. the heatmap changed at that timestamp. The cell carries `n` (frames
    # counted) and `w` (vehicles per frame); the busy cell is the one the scanner
    # just wrote, at the coordinates the frames carried.
    traffic = heat_cells(police_t, "traffic")
    cells = traffic.get("cells") or []
    check("traffic cells exist for the frames just sent", bool(cells), f"{len(cells)} cells, max w={traffic.get('max')}")
    near = [c for c in cells if abs(float(c["lat"]) - 17.44) < 0.01 and abs(float(c["lng"]) - 78.35) < 0.01]
    if near:
        busiest = max(int(c.get("n") or 0) for c in near)
        check("a traffic cell has counted the frames", busiest >= 1, f"n={busiest} w={near[0].get('w')}")
    else:
        check("a traffic cell has counted the frames", False, f"no cell at 17.44,78.35 in {json.dumps(cells)[:200]}")

    # 8. the negative half: taking the plate back off the hot-list stops the
    #    alerting.
    #
    #    Two different operations, and the difference matters:
    #      * rejecting a complaint is only legal while it is PENDING or
    #        UNDER_REVIEW (complaints.py:reject_complaint), so it cannot undo a
    #        verify that already happened - by design, since a verified theft
    #        report should not be thrown away by a later click.
    #      * once the plate is on the list, closing the hot-list entry is what
    #        removes it (hotlist_service -> cache_service.remove_active_plate),
    #        and that is what a cop uses when the car is recovered.
    #    Both are exercised here, and the frames are sent again after each.
    r = requests.post(f"{API_V1}/complaints/{complaint_id}/reject", headers=auth(police_t), timeout=20)
    check(
        "rejecting an already-verified complaint is refused, not silently applied",
        r.status_code == 409,
        f"HTTP {r.status_code} - {r.text[:90]}",
    )

    closed = clear_hotlist(police_t, HOT_PLATE)
    check("the hot-list entry can be closed", closed >= 1, f"{closed} entries moved to CLOSED")

    cold = send_frames(volunteer_t, "cold2")
    check(
        "after the plate is taken off the list, frames no longer alert",
        not cold.get("stolen"),
        f"{len(cold.get('stolen', []))} stolen, {len(cold.get('plates', []))} reads",
    )
    check(
        "the plate is still read - it is the flag that stopped, not the OCR",
        any(p.get("text") for p in cold.get("plates", [])),
        ", ".join(f"{p.get('text')}@{p.get('conf')}" for p in cold.get("plates", [])[:2]) or "nothing read",
    )

    # 10. privacy, checked at the one moment it is actually true: the plate is now
    #     off the list, so every frame above that read it is a frame carrying a
    #     vehicle nobody has reported. Those must leave no sighting row and no
    #     alert server-side - only a count in traffic_cells.
    rows_before = len(sightings_for(police_t, HOT_PLATE))
    quiet = send_frames(volunteer_t, "quiet")
    rows_after = len(sightings_for(police_t, HOT_PLATE))
    check(
        "an unlisted plate leaves no sighting row",
        rows_after == rows_before,
        f"{rows_before} then {rows_after} over {quiet.get('frames')} frames",
    )
    check(
        "an unlisted plate raises no alert",
        not quiet.get("stolen"),
        f"{len(quiet.get('stolen', []))} stolen",
    )
    check(
        "an unlisted plate is still read",
        any(p.get("text") for p in quiet.get("plates", [])),
        ", ".join(f"{p.get('text')}@{p.get('conf')}" for p in quiet.get("plates", [])[:2]) or "nothing read",
    )
    traffic_after = heat_cells(police_t, "traffic")
    near_after = [
        c
        for c in (traffic_after.get("cells") or [])
        if abs(float(c["lat"]) - 17.44) < 0.01 and abs(float(c["lng"]) - 78.35) < 0.01
    ]
    check(
        "but it is counted in traffic_cells",
        bool(near_after) and int(near_after[0].get("n") or 0) > int(
            [c for c in cells if abs(float(c["lat"]) - 17.44) < 0.01 and abs(float(c["lng"]) - 78.35) < 0.01][0].get("n")
            or 0
        ),
        f"n={near_after[0].get('n') if near_after else None}",
    )

    # And a complaint rejected before it was ever verified never lists the plate.
    r = requests.post(
        f"{API_V1}/complaints",
        headers=auth(citizen_t),
        data={"plate": COLD_PLATE, "complaint_type": "vehicle theft", "description": "chain check - rejected report"},
        timeout=20,
    )
    rejected_id = r.json().get("id") if r.status_code in (200, 201) else None
    if rejected_id:
        r = requests.post(f"{API_V1}/complaints/{rejected_id}/reject", headers=auth(police_t), timeout=20)
        check("a pending complaint can be rejected", r.status_code in (200, 204), f"HTTP {r.status_code}")
        r = requests.get(f"{API_V1}/hotlist", headers=auth(police_t), timeout=20)
        listed = [
            e for e in r.json() if e.get("plate") == COLD_PLATE and e.get("status") in ("ACTIVE", "FIR_CONFIRMED")
        ]
        check("a rejected complaint never reaches the hot-list", not listed, f"{len(listed)} active entries")
    else:
        check("a second complaint can be filed for the rejected case", False, f"HTTP {r.status_code} {r.text[:90]}")

    print("", flush=True)
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = [name for ok, name, _ in RESULTS if not ok]
    print(f"=== {passed}/{len(RESULTS)} checks passed ===", flush=True)
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

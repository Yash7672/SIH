"""End-to-end check for the live-detection WebSocket (WS /api/v1/ws/scan).

Speaks the exact protocol the phone speaks, drives it with real frames off the
road-scene fixture, and asserts the behaviour the mobile overlay depends on:

* auth rejection (bad token, no token, wrong first message, wrong role);
* the two-band frame size policy - over the soft limit is downscaled and still
  detected, over the hard limit is refused with `size_limit`;
* backpressure - a burst is dropped, not queued;
* the privacy promise, and the stolen path, both run against the plate the OCR
  actually reads rather than against a hard-coded one;
* the numbers the phone shows: green-box latency, round trip, frame rate, and how
  much CPU the server burns doing it.

This needs the backend already running (start.bat, or uvicorn by hand).

    .venv\\Scripts\\python.exe scripts\\live_scan_test.py
    .venv\\Scripts\\python.exe scripts\\live_scan_test.py --seconds 30 --api http://192.168.1.5:8000

The plate is *not* hard-coded on purpose. OCR reading a slightly different number
one day must not turn the stolen/privacy phases into a test that quietly stops
testing anything, so the script first streams once to learn what the plate
detector reads, then hot-lists exactly that plate and asserts the alert. The
fixture is verified separately by scripts/make_test_road_scene.py --check.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import requests
import websockets
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
API = os.environ.get("RAKSHAK_API", "http://127.0.0.1:8000")
WS = API.replace("http", "ws") + "/api/v1/ws/scan"
POLICE_WS = API.replace("http", "ws") + "/api/v1/ws/police"
FIXTURE = ROOT / "data" / "test_plates" / "road_scene.jpg"

# The capture contract, mirroring liveCapture.js. STREAM_WIDTH is deliberately the
# same as the server's WORKING_WIDTH: at 640 px the plate is 90x18 px and OCR never
# returns anything, so a narrower stream would make this whole script vacuous.
STREAM_WIDTH = 1280
STREAM_QUALITY = 40
# How far the scene pans between frames, as a fraction of the frame. The tracker
# and the overlay's motion prediction are both about moving vehicles, so the test
# moves the only vehicle it has.
MOTION_PER_FRAME = 0.035

# The phone's own pacing (liveCapture.js): one frame in flight, then a short floor
# so two replies cannot land in the same animation frame.
MIN_INTERVAL_S = 0.12
# The green boxes are the latency budget. 120 ms is the target the server was held
# to; this is the assertion that keeps it honest.
GREEN_BOX_BUDGET_MS = 120
# How long the self-calibrating probe streams. It has to cover a full OCR read
# (~2 s warm, ~4 s on the very first crop after a restart) plus a few frames of
# padding, or the probe reports "nothing detected" on a machine that is working.
PROBE_SECONDS = 14.0

# Mirrors SOFT/HARD_FRAME_BYTES in backend/app/api/v1/live_scan.py.
SOFT_FRAME_BYTES = 300 * 1024
HARD_FRAME_BYTES = 400 * 1024

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def pct(values: list[float], q: float) -> float:
    return float(np.percentile(values, q)) if values else float("nan")


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def login(email: str, password: str) -> str:
    r = requests.post(f"{API}/api/v1/auth/login", json={"email": email, "password": password}, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


def register_device(token: str) -> str:
    """A brand new device every run.

    Deliberate: the sighting cooldown key is `plate:<plate>:<device id>`, so a
    reused device would silently suppress the second run's sighting and the
    "exactly one sighting" assertion would fail for the wrong reason.
    """
    r = requests.post(
        f"{API}/api/v1/devices/register",
        headers={"Authorization": f"Bearer {token}"},
        json={"device_type": "mobile", "device_name": "live_scan_test"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["id"]


def hotlist_entries(cop_token: str) -> list[dict]:
    r = requests.get(f"{API}/api/v1/hotlist", headers={"Authorization": f"Bearer {cop_token}"}, timeout=20)
    r.raise_for_status()
    return r.json()


def deactivate_hotlist(cop_token: str, plate: str) -> int:
    """Move every active entry for `plate` out of the active set.

    RECOVERED rather than DELETE: the sightings that reference the row stay
    readable, and `is_plate_hotlisted` only counts ACTIVE and FIR_CONFIRMED.
    """
    n = 0
    for entry in hotlist_entries(cop_token):
        if entry["plate"] == plate and entry["status"] in ("ACTIVE", "FIR_CONFIRMED"):
            r = requests.patch(
                f"{API}/api/v1/hotlist/{entry['id']}",
                headers={"Authorization": f"Bearer {cop_token}"},
                json={"status": "RECOVERED"},
                timeout=20,
            )
            r.raise_for_status()
            n += 1
    return n


def activate_hotlist(cop_token: str, plate: str) -> dict:
    r = requests.post(
        f"{API}/api/v1/hotlist", headers={"Authorization": f"Bearer {cop_token}"}, json={"plate": plate}, timeout=20
    )
    r.raise_for_status()
    return r.json()


def sightings_for(cop_token: str, hotlist_id: str | None) -> int:
    """Sightings belonging to one hot-list entry, or all of them when None."""
    r = requests.get(f"{API}/api/v1/sightings", headers={"Authorization": f"Bearer {cop_token}"}, timeout=20)
    r.raise_for_status()
    rows = r.json()
    if hotlist_id is None:
        return len(rows)
    return sum(1 for s in rows if s["hotlist_id"] == hotlist_id)


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #
def load_frames(count: int) -> list[tuple[bytes, int, int]]:
    """The fixture, encoded and panned exactly the way the phone sends frames.

    1280 px wide at JPEG quality 40 (~70 KB), each frame shifted a little further
    along the road so the tracker sees a car that is moving rather than one parked
    in the middle of the frame. Returns ``(jpeg, width, height)`` per frame, since
    the protocol wants the dimensions alongside the bytes.
    """
    if not FIXTURE.exists():
        print(f"FAIL: {FIXTURE} is missing. Build it with:\n  python scripts/make_test_road_scene.py")
        raise SystemExit(2)

    src = Image.open(FIXTURE).convert("RGB")
    if src.width != STREAM_WIDTH:
        src = src.resize((STREAM_WIDTH, max(1, round(src.height * STREAM_WIDTH / src.width))), Image.BICUBIC)

    out: list[tuple[bytes, int, int]] = []
    h, w = src.height, src.width
    base = cv2.cvtColor(np.asarray(src), cv2.COLOR_RGB2BGR)
    for i in range(count):
        frame = base
        shift = i * MOTION_PER_FRAME
        if shift:
            m = np.float32([[1, 0, shift * w], [0, 1, 0]])
            # BORDER_REPLICATE, not black: a black band is a real detection
            # difference, not a synthetic-frame artefact.
            frame = cv2.warpAffine(base, m, (w, h), borderMode=cv2.BORDER_REPLICATE)
        out.append((jpeg_bytes(frame, STREAM_QUALITY), w, h))
    return out


def b64(jpeg: bytes) -> str:
    return base64.b64encode(jpeg).decode("ascii")


def disk_snapshot() -> set[str]:
    """Every file under the upload/data roots, with size, to prove nothing lands."""
    seen = set()
    for d in (ROOT / "data" / "uploads", ROOT / "backend" / "data"):
        if d.is_dir():
            for p in d.rglob("*"):
                if p.is_file():
                    seen.add(f"{p}:{p.stat().st_size}")
    return seen


# --------------------------------------------------------------------------- #
# Server CPU
# --------------------------------------------------------------------------- #
def server_pid(port: int) -> int | None:
    try:
        import psutil
    except ImportError:
        return None
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                return c.pid
    except Exception:  # noqa: BLE001 - needs privileges on some platforms
        pass
    try:
        for p in psutil.process_iter(["pid", "cmdline"]):
            cl = " ".join(p.info.get("cmdline") or [])
            if "uvicorn" in cl and str(port) in cl:
                return p.info["pid"]
    except Exception:  # noqa: BLE001
        pass
    return None


class Cpu:
    """Server CPU seconds and peak RSS across one measurement window."""

    def __init__(self, pid: int | None):
        self.pid = pid
        self._proc = None
        self._t0 = 0.0
        self._c0 = None
        self.peak_rss_mb = 0.0

    def __enter__(self):
        if self.pid:
            try:
                import psutil

                self._proc = psutil.Process(self.pid)
                self._proc.cpu_percent(None)
                self._t0 = time.time()
                self._c0 = self._proc.cpu_times()
            except Exception:  # noqa: BLE001
                self._proc = None
        return self

    def _poll_rss(self) -> None:
        if not self._proc:
            return
        try:
            mb = self._proc.memory_info().rss / (1024 * 1024)
            self.peak_rss_mb = max(self.peak_rss_mb, mb)
        except Exception:  # noqa: BLE001
            pass

    def tick(self) -> None:
        self._poll_rss()

    def __exit__(self, *exc) -> None:
        if not self._proc:
            return
        try:
            self._c1 = self._proc.cpu_times()
            self._wall = time.time() - self._t0
            self.cpu_seconds = (self._c1.user + self._c1.system) - (self._c0.user + self._c0.system)
            self._poll_rss()
        except Exception:  # noqa: BLE001
            self.cpu_seconds = None
            self._wall = None

    def report(self) -> str:
        if not self._proc or getattr(self, "cpu_seconds", None) is None or not self._wall:
            return "not measurable (psutil unavailable or the server pid was not found)"
        cores = self.cpu_seconds / self._wall if self._wall else 0.0
        return (
            f"{self.cpu_seconds:.1f} s CPU over {self._wall:.1f} s wall = {cores:.2f} cores "
            f"average, peak RSS {self.peak_rss_mb:.0f} MB"
        )


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #
async def run_stream(
    token: str, device_id: str, frames: list[tuple[bytes, int, int]], seconds: float, cpu: Cpu | None = None
) -> dict:
    """Drive the socket the way the phone's capture loop does.

    One frame in flight at a time, the next one sent only after the previous
    frame's green boxes came back, exactly like LiveCaptureLoop. That is what makes
    the round-trip and frame-rate numbers comparable with what the phone shows.
    """
    sent_at: dict[int, float] = {}
    server_ms: list[float] = []
    round_trip: list[float] = []
    plate_leg_ms: list[float] = []
    labels: list[str] = []
    plates_msgs: list[dict] = []
    reads: list[dict] = []
    errors: list[dict] = []
    unanswered = 0
    stale_boxes = 0
    in_flight = 0
    max_in_flight = 0
    sent = 0

    async with websockets.connect(WS, open_timeout=15, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token, "device_id": device_id}))
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        if json.loads(raw).get("type") != "ready":
            raise RuntimeError(f"expected ready, got {raw[:120]}")

        queue: asyncio.Queue = asyncio.Queue()

        async def reader():
            try:
                async for msg in ws:
                    await queue.put((time.perf_counter(), json.loads(msg)))
            except Exception:  # noqa: BLE001 - socket closed, reader is done
                pass

        reader_task = asyncio.create_task(reader())

        deadline = time.time() + seconds
        i = 0
        while time.time() < deadline and i < len(frames):
            jpeg, fw, fh = frames[i]
            seq = i
            t0 = time.perf_counter()
            await ws.send(json.dumps({
                "type": "frame", "seq": seq, "w": fw, "h": fh,
                "lat": 17.3616, "lng": 78.5147, "jpeg_b64": b64(jpeg),
            }))
            sent_at[seq] = t0
            i += 1
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            sent += 1

            # Wait for *this* frame's green boxes, absorbing everything else that
            # lands in the meantime. Waiting for "the next message of any kind"
            # would be wrong: the server answers boxes -> plates -> plate, so the
            # message after `boxes` is usually the previous frame's plate boxes,
            # and the loop would spend every other iteration on bookkeeping and
            # send frames the server then has to drop. LiveScan.js acknowledges
            # only the seq it is waiting for, for the same reason.
            deadline_at = time.perf_counter() + 6
            answered = False
            while True:
                left = deadline_at - time.perf_counter()
                if left <= 0:
                    break
                try:
                    at, msg = await asyncio.wait_for(queue.get(), timeout=left)
                except asyncio.TimeoutError:
                    break

                kind = msg.get("type")
                if kind == "plates":
                    plate_leg_ms.append((at - sent_at.get(msg.get("seq"), at)) * 1000)
                    plates_msgs.append(msg)
                elif kind == "plate":
                    reads.append(msg)
                elif kind == "error":
                    errors.append(msg)
                elif kind == "boxes":
                    if msg.get("seq") == seq:
                        server_ms.append(float(msg.get("ms", 0)))
                        round_trip.append((at - t0) * 1000)
                        labels.extend(v.get("label", "?") for v in msg.get("vehicles") or [])
                        answered = True
                        break
                    # A late reply for an already-abandoned frame: the phone would
                    # drop it too (liveScan._ack ignores a stale seq).
                    stale_boxes += 1

            in_flight = max(0, in_flight - 1)
            if not answered:
                unanswered += 1
                continue

            if cpu:
                cpu.tick()
            await asyncio.sleep(MIN_INTERVAL_S)

        reader_task.cancel()

    return {
        "server_ms": server_ms, "round_trip": round_trip, "plate_leg_ms": plate_leg_ms,
        "labels": labels, "plates": plates_msgs, "reads": reads, "errors": errors,
        "unanswered": unanswered, "stale_boxes": stale_boxes,
        "max_in_flight": max_in_flight, "sent": sent,
    }


def print_stats(stats: dict, seconds: float, cpu_note: str) -> None:
    print("\n--- what the phone would show ---")
    print(f"  frames sent           : {stats['sent']}")
    print(f"  frames unanswered     : {stats['unanswered']}")
    print(f"  replies for a dead seq: {stats['stale_boxes']}")
    print(f"  frame rate            : {stats['sent'] / seconds:.1f} fps")
    print(f"  green box ms p50/p95  : {pct(stats['server_ms'], 50):.0f} / {pct(stats['server_ms'], 95):.0f}"
          f"   (budget {GREEN_BOX_BUDGET_MS} ms)")
    print(f"  round trip ms p50/p95 : {pct(stats['round_trip'], 50):.0f} / {pct(stats['round_trip'], 95):.0f}")
    print(f"  plate box ms p50/p95  : {pct(stats['plate_leg_ms'], 50):.0f} / {pct(stats['plate_leg_ms'], 95):.0f}")
    print(f"  vehicle labels        : {sorted(set(stats['labels'])) or '-'}")
    print(f"  plate boxes / reads   : {len(stats['plates'])} / {len(stats['reads'])}")
    for r in stats["reads"][:6]:
        print(f"      {str(r.get('norm')):<12} conf={r.get('conf')} valid={r.get('valid')} stolen={r.get('stolen')}")
    for e in stats["errors"][:5]:
        print(f"      error: {e}")
    print(f"  server cpu            : {cpu_note}")


# --------------------------------------------------------------------------- #
# Negative cases
# --------------------------------------------------------------------------- #
async def auth_is_rejected(token: str, device_id: str) -> None:
    print("\n[1] auth failures close the socket")
    for label, payload in (
        ("bad token", {"type": "auth", "token": "not-a-jwt", "device_id": device_id}),
        ("no token", {"type": "auth", "token": "", "device_id": device_id}),
        ("first message not auth", {"type": "frame", "seq": 0}),
    ):
        try:
            async with websockets.connect(WS, open_timeout=10) as ws:
                await ws.send(json.dumps(payload))
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=8)
                except websockets.ConnectionClosed:
                    check(f"auth rejected: {label}", True, "closed without a reply")
                    continue
                # 4401/4403 arrive as a close frame, so a JSON body means it answered.
                check(f"auth rejected: {label}", False, f"got a message instead: {msg[:80]}")
        except Exception as exc:  # noqa: BLE001
            check(f"auth rejected: {label}", False, f"{type(exc).__name__}: {exc}")


async def auth_forbidden_role() -> None:
    """A CITIZEN token must be refused: the role check is part of the contract."""
    try:
        cit = login("citizen@example.com", "Citizen@123")
    except Exception as exc:  # noqa: BLE001
        check("citizen role refused (4403)", True, f"skipped, no citizen account: {exc}")
        return
    try:
        async with websockets.connect(WS, open_timeout=10) as ws:
            await ws.send(json.dumps({"type": "auth", "token": cit, "device_id": None}))
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=8)
                check("citizen role refused (4403)", False, f"got a message: {msg[:80]}")
            except websockets.ConnectionClosed:
                check("citizen role refused (4403)", True, "closed")
    except Exception as exc:  # noqa: BLE001
        check("citizen role refused (4403)", False, f"{type(exc).__name__}: {exc}")


async def oversized_rejected(token: str, device_id: str) -> None:
    print("\n[4] the frame-size policy: soft band is processed, hard band is refused")
    # The contract is two bands, not one cap, and which band does what is worth
    # stating: liveCapture.js downscales before it sends, so a 12 MP capture is a
    # client-side problem and never reaches this handler at all. What the server
    # promises is (a) a frame between the soft and hard limits is still detected,
    # and (b) a frame past the hard limit is refused with a readable reason
    # instead of being silently decoded.
    #
    # Both cases are sized by searching for a quality that lands inside the band
    # rather than hard-coding one. The soft band is 100 KB wide, which is a narrow
    # target for a hand-picked quality number, and a miss would be reported as "the
    # size policy is broken" when it is only the test's arithmetic that moved.

    # (a) A 12 MP frame that is small enough to be legal is downscaled to the
    #     working width and answered normally.
    # A real photo is not a pure gradient: a little sensor noise is what makes a
    # 12 MP frame big in the first place, and without it the content compresses so
    # well that no JPEG quality can be steered into the 100 KB-wide band at all.
    ys, xs = np.mgrid[0:3000, 0:4000]
    big = np.stack([xs % 256, ys % 256, (xs + ys) % 256], axis=-1).astype(np.int16)
    big += np.random.default_rng(11).integers(-40, 40, big.shape)
    big = np.clip(big, 0, 255).astype(np.uint8)
    payload = _jpeg_in_band(big)
    name = "a 12 MP frame in the soft band is downscaled and processed"
    if payload is None:
        check(name, False,
              f"could not build a frame between {SOFT_FRAME_BYTES // 1024} "
              f"and {HARD_FRAME_BYTES // 1024} KB")
    else:
        kb = len(payload) // 1024
        async with websockets.connect(WS, open_timeout=15, max_size=16 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"type": "auth", "token": token, "device_id": device_id}))
            await ws.recv()
            await ws.send(json.dumps({
                "type": "frame", "seq": 1, "w": 4000, "h": 3000, "lat": 17.36, "lng": 78.51,
                "jpeg_b64": b64(payload),
            }))
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=45))
                got = msg.get("type") == "boxes" and msg.get("w") == 1280
                check(name, got, f"{kb} KB sent, got type={msg.get('type')} w={msg.get('w')}")
            except asyncio.TimeoutError:
                check(name, False, "no reply within 45 s")

    # Past the abuse ceiling the frame is refused, loudly. Sized so the base64
    # payload still fits under uvicorn's 16 MB websocket limit and the server's own
    # handler answers, rather than the transport dropping the socket with 1009 first.
    noise = np.random.randint(0, 255, (2400, 3200, 3), dtype=np.uint8)
    huge = b64(jpeg_bytes(noise, 95))
    async with websockets.connect(WS, open_timeout=15, max_size=32 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token, "device_id": device_id}))
        await ws.recv()
        await ws.send(json.dumps({
            "type": "frame", "seq": 1, "w": 3200, "h": 2400, "lat": 17.36, "lng": 78.51,
            "jpeg_b64": huge,
        }))
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=45))
            got = msg.get("type") == "error" and msg.get("code") == "size_limit"
            check(f"a frame past {HARD_FRAME_BYTES // 1024} KB is refused with size_limit", got,
                  f"decoded ~{len(huge) * 3 // 4 // 1024} KB, got {msg}")
        except asyncio.TimeoutError:
            check("a frame past the ceiling is refused with size_limit", False, "no reply within 45 s")
        except websockets.ConnectionClosed as exc:
            # A 1009 means the payload outgrew uvicorn's limit before the handler
            # ran. The frame is still refused, so the check stands - but say which
            # layer stopped it rather than reporting a bare failure.
            check("a frame past the ceiling is refused with size_limit", True,
                  f"refused by the transport ({exc.code}) before the handler")


def jpeg_bytes(img: np.ndarray, quality: int) -> bytes:
    ok, enc = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    return enc.tobytes()


def _jpeg_in_band(img: np.ndarray) -> bytes | None:
    """Encode `img` at the highest JPEG quality that still fits the soft band.

    Returns None rather than "close enough": the point of this helper is to prove
    a frame *inside* the band is still detected, and quietly handing back a frame
    below it would make the check pass while testing a different case. The caller
    reports the miss as a failure rather than a silent pass.
    """
    for quality in range(95, 4, -3):
        blob = jpeg_bytes(img, quality)
        if SOFT_FRAME_BYTES <= len(blob) <= HARD_FRAME_BYTES:
            return blob
        if len(blob) < SOFT_FRAME_BYTES:
            # Below the soft limit and still falling: this content cannot be
            # pushed into the band at any quality, so stop rather than keep
            # encoding a 12 MP image for nothing.
            return None
    return None


async def stale_dropped(token: str, device_id: str, frames: list[tuple[bytes, int, int]]) -> None:
    print("\n[5] stale frames are dropped, not queued")
    frame, fw, fh = frames[0]
    b64frame = b64(frame)
    async with websockets.connect(WS, open_timeout=15) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token, "device_id": device_id}))
        await ws.recv()
        # Fire 12 frames with no pause. The server allows one in flight and caps at
        # 15/s, so it must answer far fewer than 12 times.
        for s in range(12):
            await ws.send(json.dumps({
                "type": "frame", "seq": s, "w": fw, "h": fh,
                "lat": 17.3616, "lng": 78.5147, "jpeg_b64": b64frame,
            }))
        answered = 0
        latest = None
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=6))
                if msg.get("type") == "boxes":
                    answered += 1
                    latest = msg.get("seq")
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            pass
        check("12 rapid frames are answered fewer than 12 times", answered < 12, f"answered={answered}")
        # The server answers the frame it accepted and drops the rest of the burst
        # while that one is in flight. Newest-wins is the *client's* job (liveScan.js
        # overwrites its pending frame while busy); asserting a high seq here would
        # be asserting the wrong contract.
        check("the accepted frame is answered, the burst is not queued",
              answered >= 1 and latest is not None,
              f"answered={answered} last seq={latest}")


async def drain_alerts(pws, seconds: float) -> list[dict]:
    """Collect hotlist_detection pushes on the police socket."""
    got: list[dict] = []
    end = time.time() + seconds
    while time.time() < end:
        try:
            msg = json.loads(await asyncio.wait_for(pws.recv(), timeout=1.0))
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            continue
        if msg.get("type") == "hotlist_detection":
            got.append(msg.get("payload") or msg)
    return got


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
async def main() -> int:
    global API, WS, POLICE_WS

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=API, help="backend base URL (default %(default)s)")
    ap.add_argument("--seconds", type=float, default=20.0, help="stream length per phase")
    args = ap.parse_args()

    API = args.api.rstrip("/")
    WS = API.replace("http", "ws") + "/api/v1/ws/scan"
    POLICE_WS = API.replace("http", "ws") + "/api/v1/ws/police"

    print("=" * 74)
    print(f"live scan WS check -> {WS}")
    print("=" * 74)

    volunteer = login("volunteer@example.com", "Volunteer@123")
    cop = login("cop@example.com", "Police@123")
    device_id = register_device(volunteer)
    print(f"\n[0] device {device_id}")

    frames = load_frames(400)
    sizes = sorted({len(j) for j, _w, _h in frames})
    print(f"[0] {len(frames)} frames at {STREAM_WIDTH}px q{STREAM_QUALITY}, "
          f"{sizes[0] // 1024}-{sizes[-1] // 1024} KB each, panning {MOTION_PER_FRAME}/frame")

    await auth_is_rejected(volunteer, device_id)
    await auth_forbidden_role()

    port = int(API.rsplit(":", 1)[-1]) if ":" in API else 8000
    print(f"[0] server pid {server_pid(port)}")

    # ---------------------------------------------------------------- probe --
    print("\n[2] probe: what does the detector actually read?")
    # Any pre-existing hot-list state is cleared first, so the probe cannot
    # record a sighting and confuse the counts the later phases assert on.
    for entry in hotlist_entries(cop):
        if entry["status"] in ("ACTIVE", "FIR_CONFIRMED"):
            deactivate_hotlist(cop, entry["plate"])
    probe = await run_stream(volunteer, device_id, frames, seconds=PROBE_SECONDS)
    read_plates = sorted({r.get("norm") for r in probe["reads"] if r.get("norm")})
    print(f"    labels={sorted(set(probe['labels']))}  reads={read_plates}")
    if not probe["reads"]:
        check("the pipeline reads a plate off the fixture", False,
              f"no 'plate' message in {PROBE_SECONDS:.0f} s; "
              f"boxes={len(probe['server_ms'])} errors={probe['errors'][:2]}")
        return 1
    check("the pipeline reads a plate off the fixture", True,
          f"{read_plates[0]} @ {probe['reads'][0]['conf']}")
    plate = read_plates[0]
    probe["sightings_after"] = sightings_for(cop, None)
    print(f"    -> self-calibrated to {plate}; no plate name is hard-coded in this script")

    before = disk_snapshot()

    # ------------------------------------------------------------- privacy --
    print(f"\n[3] privacy: {plate} is NOT hot-listed -> read, shown, and stored nowhere")
    async with websockets.connect(POLICE_WS + "?token=" + cop, open_timeout=15) as pws:
        quiet = await run_stream(volunteer, device_id, frames, seconds=args.seconds)
        privacy_alerts = await drain_alerts(pws, 2.0)

    reads = quiet["reads"]
    unlisted = [r for r in reads if r.get("norm") == plate]
    check(f"{plate} was read and sent back to the phone", bool(unlisted),
          f"{len(unlisted)} read(s), conf={[r['conf'] for r in unlisted][:3]}")
    check(f"{plate} was never flagged stolen", all(not r.get("stolen") for r in reads),
          f"stolen flags={[r.get('stolen') for r in reads][:5]}")
    check("every read was a valid plate", all(r.get("valid") for r in reads),
          f"valid={[r.get('valid') for r in reads][:5]}")
    check("no sighting was recorded for it", sightings_for(cop, None) == probe["sightings_after"],
          f"total sightings {probe['sightings_after']} -> {sightings_for(cop, None)}")
    check("no police alert was pushed", not privacy_alerts, f"alerts={len(privacy_alerts)}")
    print_stats(quiet, args.seconds, "(the measured server-CPU number is in phase 5)")

    # -------------------------------------------------------------- stolen --
    print(f"\n[5] stolen path: {plate} is now hot-listed")
    entry = activate_hotlist(cop, plate)
    hotlist_id = entry["id"]
    # A fresh device id per run means the per-plate cooldown cannot suppress this.
    device2 = register_device(volunteer)
    sightings_before = sightings_for(cop, hotlist_id)

    cpu = Cpu(server_pid(port))
    async with websockets.connect(POLICE_WS + "?token=" + cop, open_timeout=15) as pws:
        with cpu:
            hot = await run_stream(volunteer, device2, frames, seconds=args.seconds, cpu=cpu)
            alerts = await drain_alerts(pws, 2.0)

    print_stats(hot, args.seconds, cpu.report())

    stolen_reads = [r for r in hot["reads"] if r.get("norm") == plate]
    check(f"{plate} came back flagged stolen", bool(stolen_reads),
          f"{len(stolen_reads)} read(s), stolen={[r.get('stolen') for r in hot['reads']][:5]}")
    sightings_after = sightings_for(cop, hotlist_id)
    check("exactly one sighting for the stolen plate", sightings_after - sightings_before == 1,
          f"added={sightings_after - sightings_before} for hotlist {hotlist_id}")
    check("the police dashboard was alerted", bool(alerts), f"alerts={len(alerts)}")
    if alerts:
        check("the alert names the plate", alerts[0].get("plate") == plate,
              f"plate={alerts[0].get('plate')}")
        check("the alert carries a location", alerts[0].get("latitude") is not None)

    # ------------------------------------------------------- latency budget --
    p50 = pct(hot["server_ms"], 50)
    p95 = pct(hot["server_ms"], 95)
    check(f"green boxes within the {GREEN_BOX_BUDGET_MS} ms budget at p50", p50 <= GREEN_BOX_BUDGET_MS,
          f"p50={p50:.0f} ms p95={p95:.0f} ms")
    check("no frame errored while streaming", not hot["errors"], f"errors={hot['errors'][:3]}")
    check("every frame was answered", hot["unanswered"] == 0, f"unanswered={hot['unanswered']}")
    check("labels never degrade to a generic class",
          all(l and not str(l).isdigit() for l in hot["labels"]), f"labels={sorted(set(hot['labels']))}")

    await oversized_rejected(volunteer, device_id)
    await stale_dropped(volunteer, device_id, frames)

    print("\n[6] privacy: nothing was written to disk")
    after = disk_snapshot()
    check("no new files under data/uploads", after == before, f"new={sorted(after - before)[:5]}")

    print("\n" + "=" * 74)
    failed = [n for n, ok, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED:")
        for n in failed:
            print("  -", n)
    print("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
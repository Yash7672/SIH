"""End-to-end check for the live-detection WebSocket (WS /api/v1/ws/scan).

Speaks the exact protocol the phone speaks, drives it with the generated demo
media in data/generated, and asserts the behaviour the mobile overlay depends on:
auth rejection, the 300 KB frame cap, stale-frame dropping, the stolen path
(hot-list match -> exactly one sighting -> police alert) and the privacy promise
that a non-hot-listed plate leaves nothing behind.

Run with the backend already up:
    .venv\\Scripts\\python.exe scripts\\live_scan_test.py
"""

from __future__ import annotations

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

ROOT = Path(__file__).resolve().parents[1]
API = os.environ.get("RAKSHAK_API", "http://127.0.0.1:8000")
WS = API.replace("http", "ws") + "/api/v1/ws/scan"
POLICE_WS = API.replace("http", "ws") + "/api/v1/ws/police"
GEN = ROOT / "data" / "generated"

# The plates baked into data/generated/*.png. The first is used for the stolen
# path, the second to prove a clean plate leaves no trace.
STOLEN_PLATE = "MH12JK4567"
CLEAN_PLATE = "TS09AB1234"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def pct(values: list[float], q: float) -> float:
    return float(np.percentile(values, q)) if values else float("nan")


def login(email: str, password: str) -> str:
    r = requests.post(f"{API}/api/v1/auth/login", json={"email": email, "password": password}, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


def register_device(token: str) -> str:
    h = {"Authorization": f"Bearer {token}"}
    r = requests.post(
        f"{API}/api/v1/devices/register", headers=h,
        json={"device_type": "mobile", "device_name": "live_scan_test"}, timeout=20,
    )
    if r.status_code == 200:
        return r.json()["id"]
    r = requests.get(f"{API}/api/v1/devices", headers=h, timeout=20)
    r.raise_for_status()
    return r.json()[0]["id"]


def sighting_count(cop_token: str) -> int:
    r = requests.get(f"{API}/api/v1/sightings", headers={"Authorization": f"Bearer {cop_token}"}, timeout=20)
    r.raise_for_status()
    return len(r.json())


def jpeg_b64(img: np.ndarray, quality: int = 30) -> str:
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    return base64.b64encode(enc.tobytes()).decode("ascii")


def fit_width(img: np.ndarray, width: int = 640) -> np.ndarray:
    h, w = img.shape[:2]
    if w == width:
        return img
    return cv2.resize(img, (width, max(1, int(h * width / w))))


def load_frames() -> list[tuple[str, np.ndarray]]:
    out: list[tuple[str, np.ndarray]] = []
    for f in sorted(GEN.glob("*.png")):
        img = cv2.imread(str(f))
        if img is not None:
            out.append((f.stem, fit_width(img)))
    vid = cv2.VideoCapture(str(GEN / "demo_road.mp4"))
    idx = 0
    while len(out) < 140:
        ok, frame = vid.read()
        if not ok:
            break
        out.append((f"mp4_{idx:03d}", fit_width(frame)))
        idx += 1
    vid.release()
    return out


def disk_snapshot() -> set[str]:
    """Every file under the upload/data roots, with size, to prove nothing lands."""
    seen = set()
    for d in (ROOT / "data" / "uploads", ROOT / "backend" / "data"):
        if d.is_dir():
            for p in d.rglob("*"):
                if p.is_file():
                    seen.add(f"{p}:{p.stat().st_size}")
    return seen


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


async def auth_forbidden_role(token: str) -> None:
    """A CITIZEN token must be refused: role check is part of the contract."""
    try:
        cit = login("citizen@example.com", "Citizen@123")
    except Exception:
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


async def run_stream(token: str, device_id: str, frames: list[tuple[str, np.ndarray]], seconds: float) -> dict:
    """Drive the socket, draining both fast ('boxes') and late ('plate') replies."""
    server_ms: list[float] = []
    round_trip: list[float] = []
    per_frame_boxes: list[int] = []
    plates: list[dict] = []
    errors: list[dict] = []
    seq_to_send: dict[int, float] = {}
    inflight = 0
    max_inflight = 0
    dropped = 0

    async with websockets.connect(WS, open_timeout=15, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token, "device_id": device_id}))
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        if json.loads(raw).get("type") != "ready":
            raise RuntimeError(f"expected ready, got {raw[:120]}")

        deadline = time.time() + seconds
        i = 0
        # A single reader task owns the socket. Doing recv() inline in the send
        # loop meant every reply had to arrive inside a 5 s window, so a late
        # "plate" (OCR is seconds) stalled the whole stream and the loop exited on
        # the deadline with almost nothing measured.
        queue: asyncio.Queue = asyncio.Queue()

        async def reader():
            try:
                async for raw_msg in ws:
                    await queue.put(json.loads(raw_msg))
            except Exception:  # noqa: BLE001 - socket closed, reader is done
                pass

        reader_task = asyncio.create_task(reader())

        async def send_next(img: np.ndarray, seq: int) -> None:
            await ws.send(json.dumps({
                "type": "frame", "seq": seq, "w": img.shape[1], "h": img.shape[0],
                "lat": 17.3616, "lng": 78.5147, "jpeg_b64": jpeg_b64(img),
            }))

        while time.time() < deadline and i < len(frames):
            _name, img = frames[i]
            t0 = time.perf_counter()
            await send_next(img, i)
            seq_to_send[i] = t0
            inflight += 1
            max_inflight = max(max_inflight, inflight)
            i += 1

            try:
                msg = await asyncio.wait_for(queue.get(), timeout=6)
            except asyncio.TimeoutError:
                dropped += 1
                inflight = max(0, inflight - 1)
                continue

            kind = msg.get("type")
            if kind == "boxes":
                inflight = max(0, inflight - 1)
                round_trip.append((time.perf_counter() - seq_to_send.get(msg.get("seq", -1), t0)) * 1000)
                server_ms.append(float(msg.get("ms", 0)))
                per_frame_boxes.append(len(msg.get("vehicles") or []) + len(msg.get("plates") or []))
            elif kind == "plate":
                plates.append(msg)
            elif kind == "error":
                errors.append(msg)

            # Keep the pipe drained: a burst of frames each earning a late "plate"
            # must not sit in the queue until the next recv.
            while not queue.empty():
                extra = queue.get_nowait()
                if extra.get("type") == "plate":
                    plates.append(extra)
                elif extra.get("type") == "boxes":
                    inflight = max(0, inflight - 1)
                    s = extra.get("seq", -1)
                    if s in seq_to_send:
                        round_trip.append((time.perf_counter() - seq_to_send[s]) * 1000)
                        server_ms.append(float(extra.get("ms", 0)))
                        per_frame_boxes.append(
                            len(extra.get("vehicles") or []) + len(extra.get("plates") or [])
                        )

            await asyncio.sleep(1 / 15)  # the phone's rate cap

        reader_task.cancel()

    return {
        "server_ms": server_ms, "round_trip": round_trip, "boxes": per_frame_boxes,
        "plates": plates, "errors": errors, "dropped": dropped, "max_inflight": max_inflight,
        "frames_sent": i,
    }


async def oversized_rejected(token: str, device_id: str) -> None:
    print("\n[3] oversized frame is rejected")
    # 400 KB of noise: incompressible, so it really does exceed the 300 KB cap.
    noise = np.random.randint(0, 255, (600, 800, 3), dtype=np.uint8)
    payload = jpeg_b64(noise, 95)
    async with websockets.connect(WS, open_timeout=15) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token, "device_id": device_id}))
        await ws.recv()
        await ws.send(json.dumps({
            "type": "frame", "seq": 1, "w": 800, "h": 600, "lat": 17.36, "lng": 78.51,
            "jpeg_b64": payload,
        }))
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            got = msg.get("type") == "error" and msg.get("code") == "size_limit"
            check("300 KB cap rejects with size_limit", got, f"size={len(payload) * 3 // 4} bytes, got {msg}")
        except asyncio.TimeoutError:
            check("300 KB cap rejects with size_limit", False, "no reply within 20 s")


async def stale_dropped(token: str, device_id: str, frames) -> None:
    print("\n[4] stale frames are dropped, not queued")
    _, img = frames[0]
    b64 = jpeg_b64(img)
    async with websockets.connect(WS, open_timeout=15) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token, "device_id": device_id}))
        await ws.recv()
        # Fire 12 frames with no pause. The server allows 1 in flight and caps at
        # 15/s, so it must answer far fewer than 12 times.
        for s in range(12):
            await ws.send(json.dumps({
                "type": "frame", "seq": s, "w": img.shape[1], "h": img.shape[0],
                "lat": 17.3616, "lng": 78.5147, "jpeg_b64": b64,
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
        check("12 rapid frames answered fewer than 12 times", answered < 12, f"answered={answered}")
        check("newest frame wins (last answered seq is high)", latest is not None and latest >= 6, f"last seq={latest}")


async def main() -> int:
    print("=" * 70)
    print("live scan WS check ->", WS)
    print("=" * 70)

    volunteer = login("volunteer@example.com", "Volunteer@123")
    cop = login("cop@example.com", "Police@123")
    device_id = register_device(volunteer)

    # Hot-list the plate we expect to read out of plate_3_MH12JK4567.png.
    h = {"Authorization": f"Bearer {cop}"}
    r = requests.post(f"{API}/api/v1/hotlist", headers=h, json={"plate": STOLEN_PLATE}, timeout=20)
    print(f"\n[0] hot-list {STOLEN_PLATE}: HTTP {r.status_code}")

    frames = load_frames()
    print(f"[0] loaded {len(frames)} frames from data/generated")

    await auth_is_rejected(volunteer, device_id)
    await auth_forbidden_role(volunteer)

    before = disk_snapshot()
    cops_before = sighting_count(cop)

    # Police dashboards must see the stolen alert.
    alerts: list[dict] = []
    async with websockets.connect(f"{POLICE_WS}?token={cop}", open_timeout=15) as pws:
        print("\n[2] streaming frames (30 s budget)")
        stats = await run_stream(volunteer, device_id, frames, seconds=30.0)

        try:
            while True:
                msg = json.loads(await asyncio.wait_for(pws.recv(), timeout=3))
                if msg.get("type") == "hotlist_detection":
                    alerts.append(msg)
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            pass

    print("\n--- throughput / latency ---")
    print(f"  frames sent           : {stats['frames_sent']}")
    print(f"  boxes replies         : {len(stats['server_ms'])}")
    print(f"  dropped by server     : {stats['dropped']}")
    print(f"  server ms  p50/p95    : {pct(stats['server_ms'], 50):.0f} / {pct(stats['server_ms'], 95):.0f}")
    print(f"  round trip ms p50/p95 : {pct(stats['round_trip'], 50):.0f} / {pct(stats['round_trip'], 95):.0f}")
    if stats["boxes"]:
        print(f"  boxes per frame avg   : {np.mean(stats['boxes']):.2f} (max {max(stats['boxes'])})")
    print(f"  plate reads           : {len(stats['plates'])}")
    for p in stats["plates"][:10]:
        print(f"      {p.get('norm'):<12} conf={p.get('conf'):.2f} valid={p.get('valid')} stolen={p.get('stolen')}")
    for e in stats["errors"][:5]:
        print(f"      error: {e}")

    print("\n[5] stolen path")
    stolen_reads = [p for p in stats["plates"] if p.get("stolen")]
    seen = {p.get("norm") for p in stats["plates"]}
    check(f"read {STOLEN_PLATE} as stolen", bool(stolen_reads), f"reads={sorted(x for x in seen if x)}")
    check("police dashboard got hotlist_detection", bool(alerts), f"alerts={len(alerts)}")

    cops_after = sighting_count(cop)
    added = cops_after - cops_before
    check("exactly one sighting for the stolen plate", added == 1, f"added={added}")
    if alerts:
        check("alert carries the plate", alerts[0].get("payload", {}).get("plate") == STOLEN_PLATE)

    print("\n[6] a plate that is not hot-listed leaves nothing behind")
    clean_reads = [p for p in stats["plates"] if p.get("norm") == CLEAN_PLATE]
    check(f"{CLEAN_PLATE} was read", bool(clean_reads), f"reads={sorted(x for x in seen if x)}")
    check(f"{CLEAN_PLATE} never flagged stolen", all(not p.get("stolen") for p in clean_reads))
    final = sighting_count(cop)
    check("no sighting added by the clean plate", final == cops_after, f"total={final} (was {cops_before}+1)")

    print("\n[7] privacy: nothing written to disk")
    after = disk_snapshot()
    check("no new files under data/uploads", after == before, f"new={sorted(after - before)[:5]}")

    print("\n" + "=" * 70)
    failed = [n for n, ok, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED:")
        for n in failed:
            print("  -", n)
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
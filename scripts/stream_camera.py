#!/usr/bin/env python3
"""Stream a fixed camera (MJPEG / RTSP / USB / video file) into WS /ws/scan.

Run from the repo root with the backend up (start.bat):

    python scripts/stream_camera.py --source demo_road
    python scripts/stream_camera.py --source phone_ip_camera ^
        --username volunteer@example.com --password "Volunteer@123"
    python scripts/stream_camera.py --spec "{\"kind\":\"video\",\"path\":\"data/generated/demo_road.mp4\",\"loop\":true}"

What it does, in order
----------------------
1. Resolves the source (registered name in config/stream_sources.json, or an
   inline JSON spec) and opens it -> prints SOURCE CONFIRMED with resolution.
2. Logs in (or takes --token) and opens ws://<api>/api/v1/ws/scan, sending the
   same {type: auth} first message the phone sends -> prints
   WS /ws/scan CONFIRMED only after the server's {type: ready} reply.
3. Pumps frames: latest-frame-wins. While the server owes an answer for the
   frame in flight, newer source frames are dropped rather than queued, so the
   detector always works on the freshest view of the road. The slot is released
   by the server's `boxes` reply (or after UNANSWERED_MS of silence).
4. Reconnects both ends: a dropped WS is reopened with backoff and re-auth; a
   dropped network source auto-reconnects inside StreamSource.
5. Prints STOLEN / POSSIBLE / CLEAR verdicts as they arrive and a stats line
   on exit/stats tick.

Privacy: frames and plates never touch disk here; the script prints text only.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "backend"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import httpx  # noqa: E402
import websockets  # noqa: E402

from backend.stream_source import (  # noqa: E402
    SourceError,
    StreamSource,
    find_source,
    load_sources,
)

WS_PATH = "/api/v1/ws/scan"
LOGIN_PATH = "/api/v1/auth/login"
# The phone releases a frame slot 1.5 s after sending if no reply arrived; a
# fixed camera should behave the same so a lost reply cannot wedge the stream.
UNANSWERED_MS = 1.5
AUTH_TIMEOUT_S = 10.0
WS_BACKOFF_S = [1.0, 2.0, 5.0, 10.0]
PRINTABLE_STATES = {"STOLEN", "POSSIBLE", "CLEAR", "UNREAD"}


def ws_url(api_base: str) -> str:
    api = api_base.rstrip("/")
    return re.sub(r"^https", "wss", re.sub(r"^http", "ws", api)) + WS_PATH


def fetch_token(api_base: str, username: str, password: str) -> str:
    """Minimal login: the same POST the phone uses for its access token."""
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            api_base.rstrip("/") + LOGIN_PATH,
            json={"email": username, "password": password},
        )
        if resp.status_code != 200:
            detail = resp.json().get("detail", "unknown") if resp.headers.get("content-type", "").startswith("application/json") else resp.text[:200]
            raise SystemExit(f"login failed ({resp.status_code}): {detail}")
        token = resp.json().get("access_token")
        if not token:
            raise SystemExit("login returned no access_token")
        return token


class ScanPump:
    """One WS session: receives verdicts and pumps source frames, latest wins."""

    def __init__(self, ws, source: StreamSource, lat: float, lng: float, stats: dict) -> None:
        self.ws = ws
        self.source = source
        self.lat = lat
        self.lng = lng
        self.stats = stats
        self.busy = False
        self.busy_deadline = 0.0
        self.seq = 0
        self._loop = asyncio.get_running_loop()

    # ------------------------------------------------------------- outgoing
    async def send_frame(self, jpeg: bytes) -> None:
        # Frames the server downscales to WORKING_WIDTH anyway; tell it the real
        # capture size so its boxes map onto the right coordinates.
        h, w = self.source.height, self.source.width
        self.seq += 1
        self.busy = True
        self.busy_deadline = self._loop.time() + UNANSWERED_MS
        payload = {
            "type": "frame",
            "seq": self.seq,
            "w": w,
            "h": h,
            "lat": self.lat,
            "lng": self.lng,
            "jpeg_b64": base64.b64encode(jpeg).decode("ascii"),
        }
        await self.ws.send(json.dumps(payload))
        self.stats["sent"] += 1

    def maybe_release(self) -> None:
        """Release the slot if the server answered (`boxes`) or gave up."""
        if self.busy and self._loop.time() >= self.busy_deadline:
            self.busy = False
            self.stats["dropped"] += 1  # the frame the timeout abandoned

    # ------------------------------------------------------------- incoming
    async def receive_loop(self) -> None:
        async for raw in self.ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = data.get("type")
            if kind == "ready":
                continue  # handled by the caller's confirmation wait
            if kind == "ping":
                try:
                    await self.ws.send(json.dumps({"type": "pong", "t": data.get("t")}))
                except Exception:  # noqa: BLE001
                    return
                continue
            if kind == "boxes":
                self.busy = False
                continue
            if kind == "error":
                self.stats["errors"] += 1
                print(f"server error: [{data.get('code')}] {data.get('message')}")
                continue
            if kind == "need_hires":
                # The phone answers hi-res requests; a fixed camera cannot change
                # its own resolution. Logged only - the rate limiter keeps an
                # unanswered request cheap.
                print(f"need_hires for track {data.get('track')}: fixed camera, skipped")
                continue
            if kind == "plate":
                self.stats["verdicts"] += 1
                vd = data.get("verdict") or {}
                state = str(vd.get("state") or "")
                self.stats["states"][state] = self.stats["states"].get(state, 0) + 1
                if state in PRINTABLE_STATES:
                    print(
                        f"[{state:<8}] track {data.get('track')} "
                        f"plate={vd.get('plate') or '-'} "
                        f"conf={float(vd.get('confidence') or 0):.3f} "
                        f"reads={vd.get('reads') or 1} "
                        f"reason={vd.get('reason') or ''}"
                        + ("  POLICE-ALERTED" if data.get("police_alerted") else "")
                    )
                continue

    # ---------------------------------------------------------------- pump
    async def pump(self, max_frames: int = 0) -> None:
        while max_frames <= 0 or self.stats["sent"] < max_frames:
            self.maybe_release()
            jpeg = await asyncio.to_thread(self.source.read_jpeg)
            if jpeg is None:
                if self.source.kind in ("mjpeg", "rtsp"):
                    print("source dropped; waiting on auto-reconnect ...")
                    await asyncio.sleep(1.0)
                    continue
                if self.source.kind == "video" and self.source.loop:
                    await asyncio.to_thread(self.source.open)
                    continue
                return  # finite video, no loop
            if self.busy:
                # Latest frame wins: the server still owes an answer for the one
                # in flight; drop this one rather than queue stale views.
                self.stats["dropped"] += 1
                await asyncio.sleep(0.005)
                continue
            await self.send_frame(jpeg)


async def run_session(
    api_base: str,
    token: str,
    source: StreamSource,
    lat: float,
    lng: float,
    max_frames: int,
    stats: dict,
) -> None:
    url = ws_url(api_base)
    attempt = 0
    while True:
        try:
            async with websockets.connect(url, open_timeout=AUTH_TIMEOUT_S) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "auth",
                            "token": token,
                            # A fixed camera is not a registered volunteer
                            # device; sighting attribution is simply skipped.
                            "device_id": None,
                        }
                    )
                )
                ready = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT_S)
                msg = json.loads(ready)
                if msg.get("type") != "ready":
                    raise SourceError(f"server refused the session: {msg.get('reason') or msg}")
                print(f"WS /ws/scan CONFIRMED via {url} (session ready)")
                attempt = 0
                pump = ScanPump(ws, source, lat, lng, stats)
                receiver = asyncio.create_task(pump.receive_loop())
                try:
                    await pump.pump(max_frames=max_frames)
                finally:
                    receiver.cancel()
                    try:
                        await receiver
                    except asyncio.CancelledError:
                        pass
                return  # finite video without loop, or max-frames reached
        except (websockets.exceptions.ConnectionClosed, OSError, SourceError, asyncio.TimeoutError) as exc:
            stats["ws_reconnects"] += 1
            wait = WS_BACKOFF_S[min(attempt, len(WS_BACKOFF_S) - 1)]
            attempt += 1
            print(f"WS dropped ({type(exc).__name__}): reconnecting in {wait:.0f}s ...")
            await asyncio.sleep(wait)


async def main_async(args) -> int:
    # ------------------------------------------------------------- resolve
    if args.list_sources:
        for name in sorted(load_sources()):
            print(name)
        return 0

    try:
        name, spec = find_source(args.source)
    except SourceError as exc:
        print(f"source error: {exc}")
        return 2

    source = StreamSource(name, spec)
    ok = await asyncio.to_thread(source.open)
    if not ok:
        print(f"SOURCE FAILED to open ({source.last_error}); auto-reconnecting ...")
        if not await asyncio.to_thread(source.reconnect_now):
            print("source cannot be reached; giving up")
            source.close()
            return 2
    jpeg = await asyncio.to_thread(source.read_jpeg)
    if jpeg is None:
        print("SOURCE FAILED: no frame within the read timeout")
        source.close()
        return 2
    print(f"SOURCE CONFIRMED: {source.describe()} (first frame {len(jpeg)} bytes)")

    token = args.token or fetch_token(args.api, args.username, args.password)
    stats: dict = {
        "sent": 0,
        "dropped": 0,
        "errors": 0,
        "verdicts": 0,
        "states": {},
        "ws_reconnects": 0,
    }
    try:
        await run_session(
            args.api, token, source, args.lat, args.lng, args.max_frames, stats
        )
    finally:
        source.close()
    print(
        f"done: sent={stats['sent']} frames, dropped={stats['dropped']} (busy), "
        f"reconnects(ws)={stats['ws_reconnects']}, reconnects(source)={source.reconnects}, "
        f"verdicts={stats['verdicts']} {stats['states'] or ''}"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stream a fixed camera into WS /ws/scan (see module docstring)."
    )
    ap.add_argument("--source", help="registered source name or inline JSON spec")
    ap.add_argument("--list-sources", action="store_true", help="print registered sources")
    ap.add_argument("--api", default=os.getenv("API_BASE_URL", "http://localhost:8000"))
    ap.add_argument("--username", default=os.getenv("STREAM_USERNAME", "volunteer@example.com"))
    ap.add_argument("--password", default=os.getenv("STREAM_PASSWORD", "Volunteer@123"))
    ap.add_argument("--token", default=os.getenv("STREAM_TOKEN", ""))
    ap.add_argument("--lat", type=float, default=0.0)
    ap.add_argument("--lng", type=float, default=0.0)
    ap.add_argument("--max-frames", type=int, default=0, help="stop after N sent frames (0 = run)")
    args = ap.parse_args()

    if not args.list_sources and not args.source:
        print("use --source <name> (see --list-sources) or --spec JSON")
        return 2

    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\ninterrupted; stream closed")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
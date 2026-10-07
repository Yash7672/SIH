"""Stream sources for the live ANPR path: MJPEG / RTSP / USB / video file.

One ``StreamSource`` is one named camera from ``config/stream_sources.json``
(there is no cameras table in this schema, so fixed cameras are registered in
config and shared by ``scripts/stream_camera.py`` and the stream probe). A
source is a reader of pixels with two hard-won behaviours:

* **latest-frame-wins** - the consumer (the WS scan loop feeding the detector)
  is often slower than the source. ``StreamSource.skip_buffered()`` throws away
  frames queued inside the capture backend so the next ``read_jpeg()`` returns
  only the freshest view of the road, never a backlog of stale ones.
* **auto-reconnect** - a network source (MJPEG over HTTP, RTSP) that drops is
  reopened with bounded backoff, so a Wi-Fi blip or a camera reboot shows up as
  a pause, not as a dead stream that needs a human to restart it.

Privacy contract: this module never writes a frame anywhere; every read stays
in memory and is handed onward to the caller (compare the WS scan path, which
is intentionally memory-only as well).
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import cv2
import numpy as np

try:  # the backend can resolve settings; the standalone script does not need it
    from app.core.config import REPO_ROOT, settings

    _stream_path = Path(getattr(settings, "STREAM_SOURCES_PATH", "") or "config/stream_sources.json")
    if not _stream_path.is_absolute():
        # The .env default is "config/stream_sources.json" which is meaningless
        # resolved against the process CWD (uvicorn starts in backend/). Always
        # anchor relative paths to the repo root.
        _stream_path = REPO_ROOT / _stream_path
    DEFAULT_SOURCES_PATH = _stream_path
    _ROOT = REPO_ROOT
except Exception:  # noqa: BLE001 - running without backend on sys.path
    _ROOT = Path(__file__).resolve().parents[1]
    DEFAULT_SOURCES_PATH = _ROOT / "config" / "stream_sources.json"

NETWORK_KINDS = ("mjpeg", "rtsp")
# Never wait forever for a dead camera; the WS scan loop has its own budget.
READ_TIMEOUT_S = 5.0
BACKOFF_CAP_S = 10.0


class SourceError(RuntimeError):
    """A stream source that cannot be opened or has permanently failed."""


def load_sources(path: Optional[Path] = None) -> Dict[str, dict]:
    """Registered sources from the JSON config, ``{name: spec}``."""
    p = Path(path) if path is not None else DEFAULT_SOURCES_PATH
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceError(f"cannot read stream sources {p}: {exc}")
    return {k: v for k, v in data.items() if isinstance(v, dict) and not k.startswith("_")}


def find_source(name_or_spec: str) -> Tuple[str, dict]:
    """Resolve ``name`` (registered) or a raw JSON spec ``{"kind": ...}``."""
    sources = load_sources()
    if name_or_spec in sources:
        return name_or_spec, sources[name_or_spec]
    stripped = name_or_spec.strip()
    if stripped.startswith("{"):
        try:
            spec = json.loads(stripped)
        except ValueError as exc:
            raise SourceError(f"stream spec is not valid JSON: {exc}")
        if not isinstance(spec, dict) or "kind" not in spec:
            raise SourceError("stream spec must be an object with a 'kind'")
        return "<inline-spec>", spec
    raise SourceError(
        f"stream source {name_or_spec!r} is not registered "
        f"(registered: {sorted(sources) or 'none'})"
    )


@dataclass
class _MjpegHttpReader:
    """Bare multipart/x-mixed-replace parser for camera MJPEG endpoints.

    ``cv2.VideoCapture`` opens many of these, but plenty of low-cost IP cameras
    serve headers that FFmpeg refuses; parsing the boundary framing directly is
    the robust route and it costs little. Frames are read serially, so yielding
    each decoded body *is* the newest frame available at that instant.
    """

    url: str
    timeout: float = READ_TIMEOUT_S
    _response: Optional[object] = field(default=None, init=False)
    _buf: bytes = field(default=b"", init=False)
    _boundary: Optional[bytes] = field(default=None, init=False)

    def open(self) -> None:
        req = urllib.request.Request(
            self.url, headers={"User-Agent": "RAKSHAK-stream/1.0"}
        )
        self._response = urllib.request.urlopen(req, timeout=self.timeout)
        ctype = self._response.headers.get("Content-Type", "")
        m = re.search(r"boundary=(?:\"([^\"]+)\"|([^;\s]+))", ctype)
        if m:
            self._boundary = ("--" + (m.group(1) or m.group(2))).encode()
            self._buf = b""
            return
        # No boundary in headers: sniff the first chunk for one.
        chunk = self._response.read(4096)
        self._buf = chunk
        m = re.search(rb"--[A-Za-z0-9_.-]{4,}", chunk)
        if m:
            self._boundary = m.group(0)
            # Rewind to include everything after the *last* boundary in the chunk.
            cut = chunk.rfind(self._boundary)
            if cut >= 0:
                self._buf = chunk[cut:]
            return
        raise SourceError(f"MJPEG endpoint at {self.url} has no multipart boundary")

    def _more(self, n: int = 4096) -> bytes:
        data = self._response.read(n)
        if not data:
            raise EOFError("MJPEG stream closed")
        return data

    def next_jpeg(self) -> bytes:
        """One JPEG body, or None at clean EOF. Raises on transport errors."""
        if self._boundary is None:
            raise SourceError("MjpegHttpReader not opened")
        while True:
            # Skip to the next boundary marker.
            pos = self._buf.find(self._boundary)
            while pos < 0:
                self._buf += self._more()
                pos = self._buf.find(self._boundary)
            # Consume up to and including the boundary + CRLF (or LF).
            after = self._buf[pos + len(self._boundary):]
            if after.startswith(b"\r\n"):
                after = after[2:]
            elif after.startswith(b"\n"):
                after = after[1:]
            elif after.startswith(b"\r"):
                after = after[1:]
            self._buf = after
            # Optional per-part headers until a blank line.
            while True:
                nl = self._buf.find(b"\n")
                while nl < 0:
                    self._buf += self._more()
                    nl = self._buf.find(b"\n")
                line = self._buf[:nl].rstrip(b"\r")
                self._buf = self._buf[nl + 1:]
                if not line:
                    break
            # The image body runs to the next boundary.
            end = self._buf.find(self._boundary)
            while end < 0:
                self._buf += self._more()
                end = self._buf.find(self._boundary)
            body = self._buf[:end]
            self._buf = self._buf[end:]
            if body:
                return body

    def close(self) -> None:
        if self._response is not None:
            try:
                self._response.close()
            except Exception:  # noqa: BLE001
                pass
            self._response = None


class StreamSource:
    """One named camera: opens the transport, reads freshest frames, heals.

    ``kind`` is one of ``mjpeg``, ``rtsp``, ``usb``, ``video``. Spec keys that
    matter: ``url`` (mjpeg/rtsp), ``device`` (usb, int index), ``path``
    (video), ``fps`` (optional throttle), ``quality`` (JPEG encode 0-100,
    default 85), ``loop`` (video: reopen at EOF), ``reconnect_backoff_s``
    (network kinds, default 2.0 -> capped at BACKOFF_CAP_S).
    """

    def __init__(self, name: str, spec: dict) -> None:
        kind = str(spec.get("kind") or "").strip().lower()
        if kind not in ("mjpeg", "rtsp", "usb", "video"):
            raise SourceError(f"unsupported stream kind {kind!r} (mjpeg/rtsp/usb/video)")
        self.kind = kind
        self.name = name
        self.spec = spec
        self.quality = int(spec.get("quality") or 85)
        self.fps = float(spec.get("fps") or 0.0)
        self.loop = bool(spec.get("loop"))
        self.reconnect_backoff_s = float(spec.get("reconnect_backoff_s") or 2.0)

        self._cap: Optional[cv2.VideoCapture] = None
        self._http: Optional[_MjpegHttpReader] = None
        self._lock = threading.Lock()
        self._closed = False
        self.last_error: Optional[str] = None
        self.reconnects = 0
        self.frames_read = 0
        self.frames_dropped = 0
        self.fps_host: float = 0.0
        self.width = 0
        self.height = 0
        self._last_paced: Optional[float] = None

    # ------------------------------------------------------------------ open
    def open(self) -> bool:
        """Open the transport. True once frames are actually readable."""
        if self._closed:
            return False
        try:
            self._close_transport()
            if self.kind == "mjpeg":
                if self.spec.get("url", "").startswith(("http://", "https://")):
                    reader = _MjpegHttpReader(self.spec["url"])
                    reader.open()
                    self._http = reader
                else:
                    self._cap = cv2.VideoCapture(self.spec.get("url", ""))
                    if not self._cap.isOpened():
                        self._cap = None
                        return False
            elif self.kind == "rtsp":
                self._cap = cv2.VideoCapture(
                    self.spec.get("url", ""), cv2.CAP_FFMPEG
                )
                if not self._cap.isOpened():
                    self._cap = None
                    return False
            elif self.kind == "usb":
                device = int(self.spec.get("device", 0))
                backend = (
                    cv2.CAP_DSHOW if hasattr(cv2, "CAP_DSHOW") else cv2.CAP_ANY
                )
                self._cap = cv2.VideoCapture(device, backend)
                if not self._cap.isOpened():
                    self._cap = None
                    return False
            else:  # video file
                path = Path(self.spec.get("path", ""))
                if not path.is_absolute():
                    path = _ROOT / path
                if not path.is_file():
                    self.last_error = f"video file missing: {path}"
                    return False
                self._cap = cv2.VideoCapture(str(path))
                if not self._cap.isOpened():
                    self._cap = None
                    return False
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001 - any transport failure
            self.last_error = str(exc)
            return False

        if self._cap is not None:
            self.width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            self.height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            self.fps_host = float(self._cap.get(cv2.CAP_PROP_FPS) or 0.0)
        self.last_error = None
        return True

    def _close_transport(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:  # noqa: BLE001
                pass
            self._cap = None
        if self._http is not None:
            self._http.close()
            self._http = None

    # --------------------------------------------------------------- helpers
    @property
    def is_open(self) -> bool:
        if self._http is not None:
            return True
        return self._cap is not None and self._cap.isOpened()

    def _ensure_open(self) -> bool:
        if self.is_open:
            return True
        if self._closed:
            return False
        return self.open()

    def _reconnect(self) -> bool:
        """Auto-reconnect a dropped *network* source with bounded backoff."""
        if self.kind not in NETWORK_KINDS:
            return False
        self._close_transport()
        wait = self.reconnect_backoff_s
        while not self._closed:
            if self.open():
                self.reconnects += 1
                return True
            time.sleep(min(wait, BACKOFF_CAP_S))
            wait = min(wait * 2.0, BACKOFF_CAP_S)
        return False

    def reconnect_now(self) -> bool:
        """Public reconnect (network kinds): a script can force a fresh session."""
        return self._reconnect()

    def skip_buffered(self, budget: int = 8) -> None:
        """Latest-frame-wins: throw away frames queued in the capture backend.

        The OCR/detection loop is slower than most sources; leaving the backlog
        to drain normally would feed the detector stale views of the road. Grab
        and discard up to ``budget`` frames so the next ``read_jpeg`` gets the
        freshest one. A no-op for MJPEG-over-HTTP (frames are consumed serially).
        """
        if self._cap is None or self.kind in NETWORK_KINDS:
            return
        for _ in range(budget):
            if not self._cap.grab():
                break
            self.frames_dropped += 1

    def read_frame(self) -> Optional[np.ndarray]:
        """The freshest BGR frame, or None (EOF for video, reconnect pending)."""
        with self._lock:
            if not self._ensure_open():
                return None
            if self._http is not None:
                try:
                    body = self._http.next_jpeg()
                except EOFError:
                    return None
                except Exception as exc:  # noqa: BLE001 - transport error
                    self.last_error = str(exc)
                    self._close_transport()
                    return None
                arr = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)
                if arr is None:
                    return None
                self.width, self.height = arr.shape[1], arr.shape[0]
                return arr
            ret, frame = self._cap.read()
            if not ret:
                return None
            return frame

    def read_jpeg(self) -> Optional[bytes]:
        """The freshest frame, JPEG-encoded (memory only, quality per spec)."""
        # Video files pace reads at the source (or configured) frame rate so a
        # 7 s clip is not replayed in 0.5 s. Network/USB sources are not paced
        # here - `skip_buffered` keeps those fresh instead.
        if self.kind == "video":
            pace = self.fps or self.fps_host
            if pace > 0:
                now = time.monotonic()
                wait = 1.0 / pace
                if self._last_paced is not None:
                    remaining = wait - (now - self._last_paced)
                    if remaining > 0:
                        time.sleep(remaining)
                self._last_paced = time.monotonic()
        frame = self.read_frame()
        if frame is None:
            return None
        ok, buf = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality]
        )
        if not ok:
            return None
        self.frames_read += 1
        return buf.tobytes()

    def iter_frames(self, max_frames: int = 0) -> Iterator[Tuple[bytes, int, int]]:
        """Yield (jpeg, width, height). Reconnects network drops; rewinds video.
        """
        n = 0
        while max_frames <= 0 or n < max_frames:
            if self._closed:
                return
            jpeg = self.read_jpeg()
            if jpeg is None:
                if self.kind in NETWORK_KINDS and self._reconnect():
                    continue
                if self.kind == "video" and self.loop:
                    self.open()
                    continue
                return
            n += 1
            yield jpeg, self.width, self.height

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._close_transport()

    def describe(self) -> str:
        where: str
        if self.kind == "usb":
            where = f"device {self.spec.get('device', 0)}"
        elif self.kind == "video":
            where = str(self.spec.get("path", ""))
        else:
            where = self.spec.get("url", "")
        return f"{self.name} [{self.kind}] {where} @ {self.width}x{self.height}"


async def list_sources() -> List[str]:
    """Names of every registered source (handy for the probe / CLI)."""
    return sorted(load_sources())


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="probe a registered stream source")
    ap.add_argument("--source", default=None, help="name or inline JSON spec")
    ap.add_argument("--frames", type=int, default=5)
    args = ap.parse_args()

    if args.source:
        name, spec = find_source(args.source)
    else:
        sources = load_sources()
        name, spec = (list(sources.items()) or [("", {})])[0]
    src = StreamSource(name, spec)
    ok = src.open()
    print(f"open={ok} {src.describe() if ok else ''}  err={src.last_error}")
    if ok:
        got = 0
        try:
            for jpeg, w, h in src.iter_frames(max_frames=args.frames):
                got += 1
                print(f"frame {got}: {w}x{h} jpeg={len(jpeg)} bytes")
        finally:
            src.close()
        print(f"frames read {got} (dropped {src.frames_dropped}, reconnects {src.reconnects})")
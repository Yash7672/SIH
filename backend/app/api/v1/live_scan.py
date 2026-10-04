"""Live vehicle + plate detection over a WebSocket (WS /api/v1/ws/scan).

Shape of the stream
-------------------
The phone streams JPEG frames and gets three different kinds of reply, on purpose
separated by when they are actually ready:

* ``boxes``  - vehicle boxes, the instant detection finishes (~90 ms).
* ``plates`` - the plate boxes inside those vehicles (~70 ms later).
* ``plate``  - the OCR text for one track, seconds later, because OCR costs
  seconds on CPU.

Three replies rather than one because the measured cost of both detector passes
plus decode is ~190 ms on this CPU: folding them into a single message would put
every green box behind the plate pass and make the overlay stutter. Boxes never
wait for OCR, OCR never blocks the next frame, and the two detector stages run
back to back on the same connection's single in-flight slot.

Privacy
-------
A frame lives in one local variable for the length of one inference and is never
written to disk or logged. A plate that is read is shown back to the one phone
that sent the frame; it is only ever persisted when it is on the active
hot-list, and then only as ``plate + lat + lng + time + confidence``.
"""

import asyncio
import base64
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from PIL import Image, ImageOps

from app.core.config import settings
from app.core.security import decode_token
from app.db.session import SessionLocal
from app.models.models import Device, Role
from app.services.hotlist_service import HotlistService
from app.services.sighting_service import SightingService
from app.services.traffic_service import traffic_accumulator
from app.ws.manager import alert_manager
from app.ws.scan_manager import COCO_CLASS_IDS, Detection, ScanConnection, Track, VehicleTracker

# AI dependencies
import torch

torch.set_num_threads(2)
from ultralytics import YOLO  # noqa: E402

# The OCR pipeline is reused exactly as it is everywhere else in the project.
# Detection/thresholding/normalisation are deliberately NOT touched here: the
# plate reading is correct today and this module is only responsible for finding
# the plate and scheduling the read.
from ai.detector import _load_plate_detector, _pad_box, preprocess_crop  # noqa: E402
from ai.ocr_engine import best_plate, candidates, read_text  # noqa: E402
from app.services.plate import normalize_plate  # noqa: E402

logger = logging.getLogger(__name__)

router = APIRouter()

# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #
ACTIVE_CONNECTIONS = 0
MAX_CONNECTIONS = 3
PING_INTERVAL_SECONDS = 20
# A socket that never sends its auth message releases its slot after this long,
# so a client that connects and says nothing cannot hold a slot (and the CPU
# time that goes with it) indefinitely.
AUTH_TIMEOUT_SECONDS = 10

# Frame size policy.
#
# The phone asks CameraView for a 640 px-wide picture and JPEG quality 0.4, which
# lands around 40-60 KB. That is the contract, so anything much larger is either
# a bug or abuse.
#
# Between SOFT and HARD the frame is kept and downscaled rather than refused,
# because a dropped frame is a silently missing detection. Past HARD it is
# refused loudly with `size_limit`, which the app surfaces as "frame too large".
SOFT_FRAME_BYTES = 300 * 1024
HARD_FRAME_BYTES = 400 * 1024

# Inference width for every model - the width every frame is normalised to before
# it reaches a detector or the OCR.
#
# 1280 px, and that number is measured rather than guessed. With the phone
# streaming 1280 px wide frames (what the manual snap path always sent, and what
# `choosePictureSize` asks for), a 960 px working width throws the resolution away
# with a bilinear downscale, the plate detector then sees a 640 px letterbox of an
# already-softened image, and the OCR read degrades from 0.99 to "no read at all"
# depending on JPEG quality:
#
#     input 960  px -> plate 139x28 px -> read MH12JK4567 @ 0.954 at q40, nothing at q60+
#     input 1280 px -> plate 185x39 px -> read MH12JK4567 @ 0.991 (q40) .. 0.996 (q80)
#
# Working at 1280 is also *faster* here: for a 1280 px frame there is no resize to
# do, and a 1280->960 PIL bilinear costs more than the JPEG decode it saves
# (24.9 ms vs 10.7 ms). It only binds on frames larger than 1280, and on those the
# two are within noise of each other (125 ms vs 134 ms on a 12 MP capture).
WORKING_WIDTH = 1280
FRAME_RATE_LIMIT = 15.0

VEHICLE_CONF = 0.25
PLATE_CONF = 0.25
# Measured on this 8-core CPU box, with torch pinned to 2 threads:
#
#   JPEG decode         ~26 ms
#   vehicle pass  @384   ~60 ms   -> green boxes at ~86 ms p50
#   plate pass    @640  ~107 ms   -> red boxes at ~193 ms p50
#
# The vehicle inference size is the only one that was free to move: 384 keeps the
# green boxes inside the 120 ms budget and detects the same vehicles as 416 here.
# PLATE_IMGSZ stays at 640 because it is what the plate detector already ran at,
# and it finds a tighter box on the same plate (conf 0.70 at 640 vs 0.66 at 416).
# A looser box means a differently padded crop, which means a different OCR input -
# and the plate reading is correct today and must stay byte-for-byte the same.
#
# ultralytics costs ~50 ms per call on this CPU whatever the input size, which is
# why the plate detector runs ONCE per frame rather than once per vehicle: 4
# vehicles cost 66 ms as a single whole-frame call versus 178 ms as four crops.
VEHICLE_IMGSZ = 384
PLATE_IMGSZ = 640

# A plate narrower than this in the working image cannot carry enough characters
# to be worth the seconds of OCR it costs.
MIN_PLATE_PX = 45
# Plates read per frame. OCR is off the hot path, so this is a recall/coverage
# choice, not a latency one.
MAX_READS_PER_FRAME = 3
# Per track, never more than this many OCR attempts, and stop as soon as a read
# is this confident. This is what keeps a car that sits in view for a minute from
# occupying the OCR worker for a minute.
MAX_READ_ATTEMPTS = 3
PLATE_CONF_DONE = 0.8
MIN_PLATE_CONFIDENCE = 0.6

inference_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="LiveInference")
ocr_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="LiveOCR")

# OCR is by far the slowest thing in the system here: ~2 s per crop on this CPU,
# measured warm on a 56x221 plate crop. The capture loop offers a crop for every
# unsettled track on every frame, so without a gate the single OCR worker falls
# behind within seconds and its queue grows for as long as the screen is open.
# That is not just wasted memory: every queued crop keeps burning cores that the
# green-box pass needs, and the green boxes - the thing the phone actually renders
# at 4 fps - visibly slow down.
#
# So at most OCR_MAX_IN_FLIGHT crops may be waiting at any moment, process-wide.
# Everything else waits for a later frame, by which time the vehicle has very
# often left view anyway, and a track that has already been read is not queued
# twice (see ScanConnection.ocr_tracks).
OCR_MAX_IN_FLIGHT = 2
_ocr_inflight = 0
_ocr_lock = threading.Lock()


def _ocr_reserve(want: int) -> int:
    """Claim up to `want` OCR slots; returns how many were actually free."""
    global _ocr_inflight
    with _ocr_lock:
        take = max(0, min(OCR_MAX_IN_FLIGHT - _ocr_inflight, want))
        _ocr_inflight += take
        return take


def _ocr_release(n: int) -> None:
    global _ocr_inflight
    with _ocr_lock:
        _ocr_inflight = max(0, _ocr_inflight - n)


class OcrClaim:
    """OCR capacity that is given back exactly once, however the holder exits.

    A naive `finally: _ocr_release(n)` is not enough on its own: the connection's
    cleanup path also has to cover tasks that were created but never got to run,
    and `_ocr_release` clamps at zero, so a double release would silently take
    capacity away from a *different* volunteer mid-read. One-shot makes the two
    paths safe to both run.
    """

    __slots__ = ("count", "_open")

    def __init__(self, count: int) -> None:
        self.count = count
        self._open = True

    def release(self) -> None:
        if not self._open:
            return
        self._open = False
        _ocr_release(self.count)

    @property
    def open(self) -> bool:
        return self._open


def ocr_inflight() -> int:
    """Crops currently waiting on the OCR worker. For tests and logging."""
    with _ocr_lock:
        return _ocr_inflight

# live_scan.py -> app/api/v1 -> api -> app -> backend/. Uvicorn runs with its
# working directory set to backend\, so resolve the checkpoint from the file
# itself rather than from the cwd.
_MODELS_DIR = Path(__file__).resolve().parents[3] / "models"
VEHICLE_MODEL_PATH = _MODELS_DIR / "yolov8n.pt"

_vehicle_model = None


def get_vehicle_model():
    """Load the vehicle detector once, process-wide.

    Raises ``RuntimeError("vehicle model missing")`` when the checkpoint is not on
    disk; the WebSocket turns that into a `missing_model` error for that
    connection rather than taking the whole API down at import time.
    """
    global _vehicle_model
    if _vehicle_model is None:
        if not VEHICLE_MODEL_PATH.exists():
            raise RuntimeError("vehicle model missing")
        _vehicle_model = YOLO(str(VEHICLE_MODEL_PATH))
    return _vehicle_model


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def decode_frame(jpeg_bytes: bytes) -> np.ndarray:
    """Decode a frame to BGR, EXIF-corrected and capped at the working width.

    The EXIF transpose is not optional: a portrait phone frame arrives tagged
    ROTATE_90, and without this the boxes are computed against a sideways image
    while the preview shows an upright one, so every box lands in the wrong place
    on screen.
    """
    img = Image.open(BytesIO(jpeg_bytes))
    img = ImageOps.exif_transpose(img)
    if img.width > WORKING_WIDTH:
        new_h = max(1, round(img.height * (WORKING_WIDTH / img.width)))
        img = img.resize((WORKING_WIDTH, new_h), Image.BILINEAR)
    # np.array gives an RGB view; this is the one conversion on the hot path, so it
    # is kept here rather than repeated by callers.
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


def detect_vehicles(bgr: np.ndarray) -> List[Detection]:
    """Every COCO vehicle class in the frame, as normalised boxes."""
    model = get_vehicle_model()
    results = model(
        bgr,
        conf=VEHICLE_CONF,
        classes=list(COCO_CLASS_IDS),
        verbose=False,
        imgsz=VEHICLE_IMGSZ,
    )
    out: List[Detection] = []
    if not results or results[0].boxes is None:
        return out
    boxes = results[0].boxes
    h, w = bgr.shape[:2]
    for i in range(len(boxes)):
        coords = boxes.xyxy[i].detach().cpu().numpy()
        out.append(
            Detection(
                box=[coords[0] / w, coords[1] / h, coords[2] / w, coords[3] / h],
                cls_id=int(boxes.cls[i].item()),
                conf=float(boxes.conf[i].item()),
            )
        )
    return out


def detect_plates(bgr: np.ndarray) -> List[Tuple[List[float], float]]:
    """Locate every plate in the frame with a single detector call.

    One call for the whole frame, not one per vehicle. ultralytics has a large
    fixed per-call cost on a CPU (~50 ms here) independent of the input size, so
    a whole-frame pass costs the same whether the road holds one car or ten,
    while a per-vehicle pass costs that per vehicle. `assign_plates` then decides
    which vehicle each plate belongs to.

    Returns ``(normalised_box, confidence)`` pairs, largest first.
    """
    results = _load_plate_detector()(bgr, conf=PLATE_CONF, verbose=False, imgsz=PLATE_IMGSZ)
    if not results or results[0].boxes is None:
        return []

    h, w = bgr.shape[:2]
    out: List[Tuple[List[float], float]] = []
    boxes = results[0].boxes
    for i in range(len(boxes)):
        coords = boxes.xyxy[i].detach().cpu().numpy()
        x1, y1, x2, y2 = coords[:4]
        if x2 <= x1 or y2 <= y1:
            continue
        out.append(([float(x1) / w, float(y1) / h, float(x2) / w, float(y2) / h], float(boxes.conf[i].item())))
    # Detector confidence first, so the tightest-supported plate wins for a vehicle
    # rather than the biggest loose rectangle the model also offered.
    out.sort(key=lambda item: -item[1])
    return out


def _plate_centre(box: Sequence[float]) -> Tuple[float, float]:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def assign_plates(
    tracks: List[Track],
    plate_dets: List[Tuple[List[float], float]],
    width: int,
    height: int,
) -> Tuple[List[dict], Dict[int, Tuple[List[float], float]]]:
    """Attach each detected plate to the vehicle that contains it.

    A plate belongs to the vehicle whose box contains the plate's centre. That
    test is what makes the red box sit inside the green one on screen, and it is
    why the client can key the red box by track id instead of guessing.

    Returns the wire list plus ``{track_id: (box, conf)}`` for the tracks that
    are worth reading.
    """
    plates: List[dict] = []
    crops: Dict[int, Tuple[List[float], float]] = {}
    for box, conf in plate_dets:
        cx, cy = _plate_centre(box)
        host = None
        for track in tracks:
            tx1, ty1, tx2, ty2 = track.box
            if tx1 <= cx <= tx2 and ty1 <= cy <= ty2:
                # Smallest containing vehicle, so a plate on a car in front of a
                # bus is attributed to the car and not to the bus behind it.
                area = (tx2 - tx1) * (ty2 - ty1)
                if host is None or area < host[0]:
                    host = (area, track)
        if host is None:
            # A plate with no vehicle around it has no green box to sit in, and
            # nothing to hang a track id off. Drawing it anyway is what produced
            # stray red rectangles floating over empty road.
            continue
        track = host[1]
        if track.tid in crops:
            continue
        box_px = (box[2] - box[0]) * width
        box_py = (box[3] - box[1]) * height
        if box_px < MIN_PLATE_PX or box_py < 12:
            continue
        plates.append(
            {"track": track.tid, "conf": round(float(conf), 4), "box": [round(float(v), 5) for v in box]}
        )
        crops[track.tid] = (box, conf)
    return plates, crops


def _crop_for_ocr(bgr: np.ndarray, box: Sequence[float]) -> Optional[np.ndarray]:
    """The padded plate crop to hand to OCR, in full-frame pixels."""
    h, w = bgr.shape[:2]
    raw = (int(round(box[0] * w)), int(round(box[1] * h)), int(round(box[2] * w)), int(round(box[3] * h)))
    padded = _pad_box(raw, w, h)
    crop = bgr[padded[1] : padded[3], padded[0] : padded[2]]
    return crop if crop.size else None


def infer_vehicles(jpeg_bytes: bytes, tracker: VehicleTracker):
    """Stage 1: decode, detect vehicles, update the tracker's tracks.

    Returns ``(bgr, w, h, ms, vehicles, tracks)``. `ms` covers decode + the
    vehicle pass only, which is the latency the green boxes are held to. The
    decoded frame is returned because stage 2 needs the pixels; it stays in this
    local variable and is never written anywhere.
    """
    t0 = time.perf_counter()
    bgr = decode_frame(jpeg_bytes)
    h, w = bgr.shape[:2]
    tracks = tracker.update(detect_vehicles(bgr), now_ms=t0 * 1000.0)
    ms = int((time.perf_counter() - t0) * 1000)
    return bgr, w, h, ms, [t.as_dict() for t in tracks], tracks


def infer_plates(
    bgr: np.ndarray,
    w: int,
    h: int,
    tracks: List[Track],
    tracker: VehicleTracker,
    ocr_busy: Optional[set] = None,
):
    """Stage 2: one plate-detector call for the frame, assigned to vehicles.

    Returns ``(plates, crops)`` where `plates` is the wire list keyed by track id
    and `crops` maps track id to the padded crop OCR should read. Tracks that have
    already been read well enough, or run out of attempts, are skipped here so a
    car parked in view cannot occupy the OCR worker for ever.

    `ocr_busy` is the set of track ids the caller already has in the OCR pipeline.
    Those are skipped without costing an attempt: a crop that is dropped here is
    not a read, and charging it against the per-track budget would settle a car
    that was never actually looked at, so its chip would read "reading..." for
    ever.
    """
    plate_dets = detect_plates(bgr)
    plates, found = assign_plates(tracks, plate_dets, w, h)

    busy = ocr_busy or set()
    widths = {tid: (box[2] - box[0]) * w for tid, (box, _c) in found.items()}
    crops: Dict[int, np.ndarray] = {}
    for track, _px in tracker.plates_to_read(widths, MAX_READS_PER_FRAME, PLATE_CONF_DONE):
        if track.tid in busy:
            continue
        crop = _crop_for_ocr(bgr, found[track.tid][0])
        if crop is not None:
            track.attempts += 1
            crops[track.tid] = crop
    return plates, crops


def decode_and_infer(jpeg_bytes: bytes, tracker: VehicleTracker):
    """Both stages in one call, for tests and offline tooling.

    Returns ``(w, h, vehicle_ms, total_ms, vehicles, plates, crops)``. The
    WebSocket path calls the two stages separately so it can answer with the
    green boxes before spending anything on the plate pass.
    """
    bgr, w, h, vehicle_ms, vehicles, tracks = infer_vehicles(jpeg_bytes, tracker)
    t1 = time.perf_counter()
    plates, crops = infer_plates(bgr, w, h, tracks, tracker)
    total_ms = int((time.perf_counter() - t1) * 1000)
    return w, h, vehicle_ms, total_ms, vehicles, plates, crops


# --------------------------------------------------------------------------- #
# OCR (off the hot path, unchanged pipeline)
# --------------------------------------------------------------------------- #
def run_ocr(crop_bgr: np.ndarray) -> Optional[dict]:
    """Read one plate crop.

    Ranking goes through ``best_plate`` rather than raw score: RapidOCR returns
    per-character fragments that each score 1.00 ("4890", "JK", "1234"), so
    picking the highest score returned a fragment instead of the merged plate.
    """
    preprocessed = preprocess_crop(crop_bgr)
    hits = candidates(read_text(preprocessed))
    if not hits:
        return None
    best = best_plate(hits)
    if best is None:
        return None

    norm = normalize_plate(best.normalized)
    if norm.valid and best.score >= MIN_PLATE_CONFIDENCE:
        return {"text": best.text, "norm": norm.normalized, "conf": best.score, "valid": True}
    return None


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #
def _device_allowed(db, device_id: Optional[str], user_id, role) -> Tuple[bool, str]:
    """The device must exist, belong to this user and not be revoked."""
    if not device_id:
        # No device claimed: allowed, but sightings simply cannot be attributed.
        return True, ""
    device = db.get(Device, device_id)
    if device is None:
        return False, "Device not registered"
    if device.revoked:
        return False, "Device has been revoked"
    if str(device.user_id) != str(user_id) and role != Role.ADMIN.value:
        return False, "Device does not belong to you"
    return True, ""


async def _check_hotlist_and_record(
    db,
    device,
    plate: str,
    lat: Optional[float],
    lng: Optional[float],
    conf: float,
) -> bool:
    """Return True when the plate is hot-listed, recording and alerting if so.

    `stolen` is a statement about the hot-list, so it comes from the hot-list
    lookup itself. `record_detection` enforces the 60 s cooldown and returns None
    both for "not hotlisted" and "cooldown active"; only a real sighting is worth
    waking the dashboards for.
    """
    hotlist = HotlistService(db)
    stolen = hotlist.is_plate_hotlisted(plate)
    if not stolen or device is None or lat is None or lng is None:
        return stolen

    try:
        sighting = SightingService(db).record_detection(
            plate=plate,
            latitude=lat,
            longitude=lng,
            timestamp=datetime.now(timezone.utc),
            confidence=conf,
            device=device,
        )
    except ValueError:
        # record_detection rejects impossible coordinates; drop the frame.
        return False
    if sighting is None:
        return True

    entry = hotlist.get_active_by_plate(plate)
    await alert_manager.broadcast(
        "hotlist_detection",
        {
            "sighting_id": str(sighting.id),
            "plate": plate,
            "latitude": sighting.latitude,
            "longitude": sighting.longitude,
            "timestamp": sighting.detected_at.isoformat(),
            "confidence": sighting.confidence,
            "hotlist_id": str(sighting.hotlist_id),
            "last_seen_at": entry.last_seen_at.isoformat() if entry and entry.last_seen_at else None,
        },
    )
    return True


@router.websocket("/scan")
async def ws_live_scan(websocket: WebSocket):
    global ACTIVE_CONNECTIONS

    if not settings.DEMO_MODE:
        await websocket.accept()
        await websocket.close(code=1000, reason="Demo mode disabled")
        return

    if ACTIVE_CONNECTIONS >= MAX_CONNECTIONS:
        await websocket.accept()
        await websocket.close(code=1013, reason="Server busy")
        return

    await websocket.accept()
    ACTIVE_CONNECTIONS += 1

    # Per connection. Two phones must never share tracks.
    conn = ScanConnection()
    ping_task: Optional[asyncio.Task] = None

    try:
        # ---- auth ------------------------------------------------------- #
        try:
            first = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await websocket.close(code=4401, reason="Auth timeout")
            return
        try:
            data = json.loads(first)
        except json.JSONDecodeError:
            await websocket.close(code=4401, reason="Malformed auth")
            return
        if data.get("type") != "auth":
            await websocket.close(code=4401)
            return

        token = data.get("token")
        if not token:
            await websocket.close(code=4401)
            return
        try:
            payload = decode_token(token)
            role = payload.get("role")
            user_id = payload.get("sub")
            if role not in (Role.VOLUNTEER.value, Role.COP.value, Role.ADMIN.value):
                await websocket.close(code=4403, reason="Forbidden")
                return
        except Exception:  # noqa: BLE001 - any decode failure is an auth failure
            await websocket.close(code=4401, reason="Invalid token")
            return

        device_id = data.get("device_id")
        db = SessionLocal()
        try:
            allowed, reason = _device_allowed(db, device_id, user_id, role)
            if not allowed:
                await websocket.close(code=4403, reason=reason)
                return
            if device_id:
                conn.device = db.get(Device, device_id)
        finally:
            db.close()

        await websocket.send_text(json.dumps({"type": "ready"}))

        async def keepalive() -> None:
            """Server-side keepalive.

            Without it a phone that backgrounds the app loses the socket to a
            NAT/proxy timeout, and the next frame lands on a dead connection,
            which then looks like "live detection offline".
            """
            try:
                while True:
                    await asyncio.sleep(PING_INTERVAL_SECONDS)
                    await websocket.send_text(json.dumps({"type": "ping", "t": time.time()}))
            except Exception:  # noqa: BLE001 - socket closing
                return

        ping_task = asyncio.create_task(keepalive())

        async def send(payload: dict) -> None:
            await websocket.send_text(json.dumps(payload))

        async def report_error(code: str, message: str) -> None:
            try:
                await send({"type": "error", "code": code, "message": message})
            except Exception:  # noqa: BLE001 - client already gone
                pass

        async def process_frame(jpeg: bytes, seq, lat, lng) -> None:
            """Infer one frame, answering with the green boxes first.

            Two replies, in order: `boxes` as soon as vehicle detection is done
            (~50 ms), then `plates` a while later, then `plate` with the OCR text
            seconds after that from a separate task. Nothing on the box path waits
            for OCR, and the vehicle slot is handed back the moment the green boxes
            are on the wire - so the next frame's boxes are never queued behind
            this frame's plate pass.
            """
            loop = asyncio.get_running_loop()
            try:
                try:
                    bgr, w, h, ms, vehicles, tracks = await loop.run_in_executor(
                        inference_pool, infer_vehicles, jpeg, conn.tracker
                    )
                except Exception as exc:  # noqa: BLE001
                    if "vehicle model missing" in str(exc):
                        await report_error("missing_model", "Vehicle model missing on server")
                    else:
                        logger.error("Live scan inference error: %s", exc)
                    return

                # Density hook for the Maps heatmap. A coordinate and a per-class
                # tally only - no plate, no image, no device id. `vehicles` is the
                # wire list, whose dicts carry the COCO class id under "cls"; the
                # counter reads dicts like these and silently ignores anything it
                # does not recognise, so passing the internal Detection objects
                # would tally nothing and quietly empty the Maps heatmap.
                traffic_accumulator.add(lat, lng, vehicles)

                try:
                    await send({"type": "boxes", "seq": seq, "w": w, "h": h, "ms": ms, "vehicles": vehicles})
                except Exception:  # noqa: BLE001 - client gone mid-inference
                    return

                # The vehicle slot is free from here on, so the phone's next frame
                # is accepted while this frame's plate pass is still running.
                conn.end_frame()
                if not conn.begin_plates():
                    # The previous frame's plate pass is still going. Reporting no
                    # plates for this frame is the honest answer: the boxes are the
                    # part that has to keep up, and a plate box drawn against an
                    # older frame would point at the wrong car.
                    return
                try:
                    try:
                        plates, crops = await loop.run_in_executor(
                            inference_pool,
                            lambda: infer_plates(bgr, w, h, tracks, conn.tracker, conn.ocr_tracks),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Live scan plate pass error: %s", exc)
                        return

                    try:
                        await send({"type": "plates", "seq": seq, "plates": plates})
                    except Exception:  # noqa: BLE001
                        return

                    if crops:
                        conn.reclaim_ocr()
                        # infer_plates already ordered the candidates biggest plate
                        # first and skipped the ones in the pipeline, so a plain
                        # prefix here takes the crops worth paying OCR for.
                        room = _ocr_reserve(len(crops))
                        if room:
                            batch = dict(list(crops.items())[:room])
                            claim = OcrClaim(room)
                            # Claim the tracks here, not inside the task: between
                            # this reservation and the task's first step the event
                            # loop can run another frame, and that frame must not
                            # queue the same car a second time.
                            conn.ocr_tracks.update(batch)
                            conn.ocr_claims.add(claim)
                            conn.ocr_tasks.add(
                                asyncio.create_task(read_plates(seq, batch, claim, lat, lng))
                            )
                finally:
                    conn.end_plates()
            finally:
                # Belt and braces: every early return above is covered by this too,
                # so no error path can leave the slot claimed and make this
                # connection's frames all look stale.
                conn.end_frame()

        async def read_plates(seq, crops, claim, lat, lng) -> None:
            """OCR each crop off the hot path and answer with its text.

            The reserved OCR capacity is handed back in the `finally` however this
            task ends - a cancelled task on a closing socket must not leak
            capacity, or the connection stops reading plates for good.
            """
            loop = asyncio.get_running_loop()
            try:
                for tid, crop in crops.items():
                    try:
                        track = conn.tracker.get(tid)
                        if track is None:
                            # The vehicle left view while OCR was queued.
                            continue
                        result = await loop.run_in_executor(ocr_pool, run_ocr, crop)
                        if result is None:
                            # Keep the chip honest: no read this attempt. The track
                            # stays eligible until it runs out of attempts.
                            if track.attempts >= MAX_READ_ATTEMPTS:
                                track.plate_settled = True
                            continue

                        track.plate_text = result["text"]
                        track.plate_conf = result["conf"]
                        track.plate_valid = result["valid"]
                        if result["conf"] >= PLATE_CONF_DONE or track.attempts >= MAX_READ_ATTEMPTS:
                            # Good enough, or out of budget: stop paying for this car.
                            track.plate_settled = True

                        stolen = False
                        if result["valid"]:
                            db = SessionLocal()
                            try:
                                stolen = await _check_hotlist_and_record(
                                    db, conn.device, result["norm"], lat, lng, result["conf"]
                                )
                            finally:
                                db.close()
                        track.plate_stolen = bool(stolen and result["valid"])

                        try:
                            await send({
                                "type": "plate",
                                "track": tid,
                                "label": track.label,
                                "seq": seq,
                                "text": result["text"],
                                "norm": result["norm"],
                                "conf": result["conf"],
                                "valid": result["valid"],
                                "stolen": track.plate_stolen,
                            })
                        except Exception:  # noqa: BLE001 - client gone
                            return
                    finally:
                        conn.ocr_tracks.discard(tid)
            finally:
                conn.ocr_claims.discard(claim)
                claim.release()

        # ---- frame loop -------------------------------------------------- #
        while True:
            msg = await websocket.receive_text()
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue

            kind = data.get("type")
            if kind == "ping":
                await send({"type": "pong", "t": data.get("t")})
                continue
            if kind != "frame":
                continue

            # Backpressure: latest frame wins. A frame arriving while one is in
            # flight is dropped rather than queued, so the detector never works
            # through a backlog of stale views of the road.
            now = time.time()
            if now - conn.last_frame_time < (1.0 / FRAME_RATE_LIMIT):
                continue
            if not conn.begin_frame(now):
                continue

            try:
                jpeg = base64.b64decode(data.get("jpeg_b64", ""), validate=False)
            except Exception:  # noqa: BLE001
                conn.end_frame()
                await report_error("bad_frame", "Frame was not valid base64")
                continue

            if len(jpeg) > HARD_FRAME_BYTES:
                conn.end_frame()
                await report_error(
                    "size_limit",
                    f"Frame rejected: {len(jpeg) // 1024} KB exceeds the "
                    f"{HARD_FRAME_BYTES // 1024} KB limit",
                )
                continue
            if len(jpeg) > SOFT_FRAME_BYTES:
                logger.info("Live frame %s KB over soft limit; downscaling to %spx", len(jpeg) // 1024, WORKING_WIDTH)

            asyncio.create_task(process_frame(jpeg, data.get("seq"), data.get("lat"), data.get("lng")))

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.error("Live scan ws error: %s", exc)
    finally:
        # Released here too: process_frame can still be running when the socket
        # dies, and leaving the slot claimed would make this connection's frames
        # all look stale.
        conn.end_frame()
        conn.end_plates()
        if ping_task is not None:
            ping_task.cancel()
        # A task cancelled before its first step never reaches its own `finally`,
        # so every claim still open at this point is released here. OcrClaim makes
        # that safe to overlap with the tasks' own releases.
        for claim in conn.ocr_claims:
            claim.release()
        conn.ocr_tracks.clear()
        for t in conn.ocr_tasks:
            t.cancel()
        # Flush on disconnect too: the periodic worker may be up to 5 s behind and
        # the cells counted in that window are real observations.
        if traffic_accumulator.pending_cells:
            db = None
            try:
                db = SessionLocal()
                written = traffic_accumulator.flush(db)
                if written:
                    logger.info("Traffic flush on disconnect wrote %d cells", written)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Traffic flush on disconnect failed: %s", exc)
            finally:
                if db is not None:
                    db.close()
        ACTIVE_CONNECTIONS -= 1
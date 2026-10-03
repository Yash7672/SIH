import asyncio
import base64
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO
from typing import Dict, List, Tuple

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

# AI dependencies
import torch
torch.set_num_threads(2)
from ultralytics import YOLO

from ai.detector import _load_plate_detector, _pad_box, preprocess_crop
from ai.ocr_engine import best_plate, candidates, read_text
from app.services.plate import normalize_plate

logger = logging.getLogger(__name__)

router = APIRouter()

ACTIVE_CONNECTIONS = 0
MAX_CONNECTIONS = 3
PING_INTERVAL_SECONDS = 20
# A socket that never sends its auth message releases its slot after this long.
AUTH_TIMEOUT_SECONDS = 10
MAX_FRAME_BYTES = 300 * 1024
FRAME_RATE_LIMIT = 15.0
MIN_PLATE_PX = 60
MAX_READS_PER_FRAME = 2
CACHE_TTL_SECONDS = 3.0
CACHE_IOU = 0.3
MIN_PLATE_CONFIDENCE = 0.6

inference_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="LiveInference")
ocr_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="LiveOCR")

vehicle_model = None

def get_vehicle_model():
    global vehicle_model
    if vehicle_model is None:
        from pathlib import Path
        model_path = Path(__file__).resolve().parents[3] / "models" / "yolov8n.pt"
        if not model_path.exists():
            raise RuntimeError("vehicle model missing")
        vehicle_model = YOLO(str(model_path))
    return vehicle_model

def compute_iou(boxA, boxB):
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])
    interArea = max(0, xB - xA) * max(0, yB - yA)
    if interArea == 0:
        return 0.0
    boxAArea = (boxA[2] - boxA[0]) * (boxA[3] - boxA[1])
    boxBArea = (boxB[2] - boxB[0]) * (boxB[3] - boxB[1])
    return interArea / float(boxAArea + boxBArea - interArea)

def decode_and_infer(jpeg_bytes: bytes):
    t0 = time.perf_counter()
    img = Image.open(BytesIO(jpeg_bytes))
    img = ImageOps.exif_transpose(img)
    bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    
    h, w = bgr.shape[:2]
    
    v_model = get_vehicle_model()
    p_model = _load_plate_detector()
    
    v_results = v_model(bgr, conf=0.25, classes=[2, 3, 5, 7], verbose=False, imgsz=416)
    p_results = p_model(bgr, conf=0.25, verbose=False, imgsz=640)
    
    vehicles = []
    if v_results and len(v_results) > 0:
        boxes = getattr(v_results[0], "boxes", None)
        if boxes is not None:
            for i, box in enumerate(boxes):
                try:
                    coords = box.xyxy[0].detach().cpu().numpy()
                    conf = float(box.conf[0].detach().cpu().item())
                    cls_id = int(box.cls[0].detach().cpu().item())
                except:
                    coords = box.xyxy[0].cpu().numpy()
                    conf = float(box.conf[0].item())
                    cls_id = int(box.cls[0].item())
                
                # normalized box
                nx1, ny1, nx2, ny2 = coords[0]/w, coords[1]/h, coords[2]/w, coords[3]/h
                vehicles.append({
                    "id": f"v_{i}",
                    "cls": cls_id,
                    "conf": conf,
                    "box": [nx1, ny1, nx2, ny2]
                })

    plates = []
    crops = []
    if p_results and len(p_results) > 0:
        boxes = getattr(p_results[0], "boxes", None)
        if boxes is not None:
            for i, box in enumerate(boxes):
                try:
                    coords = box.xyxy[0].detach().cpu().numpy().astype(int)
                    conf = float(box.conf[0].detach().cpu().item())
                except:
                    coords = box.xyxy[0].cpu().numpy().astype(int)
                    conf = float(box.conf[0].item())
                
                x1, y1, x2, y2 = coords[:4]
                width = x2 - x1
                # Read a plate only if its box is at least 60 px wide in the source image
                if width < 60:
                    continue
                
                nx1, ny1, nx2, ny2 = x1/w, y1/h, x2/w, y2/h
                p_id = f"p_{i}"
                plates.append({
                    "id": p_id,
                    "conf": conf,
                    "box": [nx1, ny1, nx2, ny2]
                })
                
                padded = _pad_box((x1, y1, x2, y2), w, h)
                crop_bgr = bgr[padded[1]:padded[3], padded[0]:padded[2]]
                crops.append((p_id, crop_bgr, [nx1, ny1, nx2, ny2]))
                
                if len(crops) >= 2: # max 2 reads per frame
                    break
                    
    ms = int((time.perf_counter() - t0) * 1000)
    return w, h, ms, vehicles, plates, crops
def run_ocr(crop_bgr: np.ndarray):
    """Read one plate crop. Runs off the frame path on ocr_pool, never inline.

    Ranking goes through ``best_plate`` rather than raw score: RapidOCR returns
    per-character fragments that each score 1.00 ("4890", "JK", "1234"), so
    picking the highest score returned a fragment instead of the merged plate.
    ``best_plate`` prefers a candidate that is actually a valid plate and only
    then falls back to confidence.
    """
    preprocessed = preprocess_crop(crop_bgr)

    hits = candidates(read_text(preprocessed))
    if not hits:
        return None

    best = best_plate(hits)
    if best is None:
        return None

    norm = normalize_plate(best.normalized)
    # A plate is valid only when normalization passes and confidence >= 0.6
    if norm.valid and best.score >= MIN_PLATE_CONFIDENCE:
        return {
            "text": best.text,
            "norm": norm.normalized,
            "conf": best.score,
            "valid": True
        }
    return None

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
    
    user_id = None
    device_id = None
    role = None
    
    recent_plates = [] # list of (box, timestamp, result_dict)
    
    # limits & backpressure
    last_frame_time = 0
    frame_processing = False
    # OCR tasks outlive the frame that produced them, so they are tracked here and
    # cancelled on disconnect - otherwise a closed socket leaves them writing into it.
    ocr_tasks: set = set()
    
    try:
        # Auth
        # Bounded: the endpoint accepts only a handful of connections, so a
        # client that connects and then says nothing would hold a slot (and the
        # GPU-less CPU time that goes with it) indefinitely.
        try:
            first = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await websocket.close(code=4401, reason="Auth timeout")
            return
        data = json.loads(first)
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
        except Exception:
            await websocket.close(code=4401, reason="Invalid token")
            return
            
        device_id = data.get("device_id")

        # The device must exist, belong to this user and not be revoked. Without
        # this check any VOLUNTEER token could push sightings through someone
        # else's device, and a revoked scanner would keep reporting forever.
        device = None
        if device_id:
            db = SessionLocal()
            try:
                device = db.get(Device, device_id)
                if device is None:
                    await websocket.close(code=4403, reason="Device not registered")
                    return
                if device.revoked:
                    await websocket.close(code=4403, reason="Device has been revoked")
                    return
                if str(device.user_id) != str(user_id) and role != Role.ADMIN.value:
                    await websocket.close(code=4403, reason="Device does not belong to you")
                    return
            finally:
                db.close()

        await websocket.send_text(json.dumps({"type": "ready"}))

        # Keepalive. Without it a phone that backgrounds the app silently loses the
        # socket to a NAT/proxy timeout and the next frame lands on a dead
        # connection, which then looks like "live detection offline".
        async def keepalive():
            try:
                while True:
                    await asyncio.sleep(PING_INTERVAL_SECONDS)
                    await websocket.send_text(json.dumps({"type": "ping", "t": time.time()}))
            except Exception:
                return

        ping_task = asyncio.create_task(keepalive())

        while True:
            msg = await websocket.receive_text()
            data = json.loads(msg)

            if data.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong", "t": time.time()}))
                continue

            if data.get("type") == "frame":
                if frame_processing:
                    # drop stale
                    continue
                    
                now = time.time()
                if now - last_frame_time < (1.0 / 15.0):
                    # rate limit 15 fps
                    continue
                    
                b64 = data.get("jpeg_b64", "")
                jpeg_bytes = base64.b64decode(b64)
                
                if len(jpeg_bytes) > 300 * 1024:
                    await websocket.send_text(json.dumps({
                        "type": "error", "code": "size_limit", "message": "Frame rejected: too large"
                    }))
                    continue
                
                seq = data.get("seq")
                lat = data.get("lat")
                lng = data.get("lng")
                
                frame_processing = True
                last_frame_time = now
                
                async def process_task(jpeg, seq, lat, lng):
                    # Without `nonlocal` this assignment creates a local, so the
                    # outer flag stayed True forever and the server silently
                    # dropped every frame after the first as "stale".
                    nonlocal frame_processing
                    try:
                        loop = asyncio.get_running_loop()
                        w, h, ms, vehicles, plates, crops = await loop.run_in_executor(
                            inference_pool, decode_and_infer, jpeg
                        )

                        # Density hook for the Maps heatmap. Counts per class in
                        # every processed frame - including frames that saw
                        # nothing, so an empty road still registers as low
                        # density. Lat/lng come from the phone; a missing, 0/0 or
                        # non-Indian fix is counted but not credited to a cell.
                        # No plate, image or device id is passed here by design.
                        traffic_accumulator.add(lat, lng, vehicles)

                        await websocket.send_text(json.dumps({
                            "type": "boxes",
                            "seq": seq,
                            "w": w,
                            "h": h,
                            "ms": ms,
                            "vehicles": vehicles,
                            "plates": plates
                        }))

                        # OCR costs seconds on this CPU, so it must never sit
                        # between the phone and its boxes. Hand the crops to a
                        # separate task and let the "plate" message arrive late.
                        if crops:
                            # add()/discard() only - rebinding the name here
                            # would make it local to this function and raise
                            # UnboundLocalError, silently killing every OCR read.
                            for _done in [t for t in ocr_tasks if t.done()]:
                                ocr_tasks.discard(_done)
                            ocr_tasks.add(asyncio.create_task(ocr_task(crops, lat, lng)))

                    except Exception as e:
                        logger.error(f"Live scan inference error: {e}")
                        try:
                            if "vehicle model missing" in str(e):
                                await websocket.send_text(json.dumps({
                                    "type": "error", "code": "missing_model", "message": "Vehicle model missing on server"
                                }))
                        except Exception:
                            pass
                    finally:
                        frame_processing = False

                async def ocr_task(crops, lat, lng):
                    """Read the frame's plates off the hot path, reusing the cache."""
                    nonlocal recent_plates
                    loop = asyncio.get_running_loop()
                    now_t = time.time()
                    recent_plates = [r for r in recent_plates if now_t - r[1] < CACHE_TTL_SECONDS]

                    for p_id, crop_bgr, box in crops:
                        # A plate already read from an overlapping box moments ago
                        # reuses that answer instead of paying OCR again.
                        cached_res = None
                        for c_box, _c_t, c_res in recent_plates:
                            if compute_iou(box, c_box) > CACHE_IOU:
                                cached_res = c_res
                                break

                        if cached_res:
                            res = cached_res
                        else:
                            res = await loop.run_in_executor(ocr_pool, run_ocr, crop_bgr)
                            if res:
                                recent_plates.append((box, now_t, res))

                        if not res:
                            continue

                        # `stolen` is a statement about the hot-list, so it has to
                        # come from the hot-list lookup itself. Redis answers it
                        # without touching PostgreSQL for the common "not stolen" case.
                        stolen = False
                        db = SessionLocal()
                        try:
                            stolen = HotlistService(db).is_plate_hotlisted(res["norm"])
                            if stolen and device is not None and lat is not None and lng is not None:
                                try:
                                    sighting = SightingService(db).record_detection(
                                        plate=res["norm"],
                                        latitude=lat,
                                        longitude=lng,
                                        timestamp=datetime.now(timezone.utc),
                                        confidence=res["conf"],
                                        device=device,
                                    )
                                    # record_detection returns None both for "not
                                    # hotlisted" and "cooldown active"; only a real
                                    # sighting is worth waking the dashboards for.
                                    if sighting is not None:
                                        entry = HotlistService(db).get_active_by_plate(res["norm"])
                                        await alert_manager.broadcast("hotlist_detection", {
                                            "sighting_id": str(sighting.id),
                                            "plate": res["norm"],
                                            "latitude": sighting.latitude,
                                            "longitude": sighting.longitude,
                                            "timestamp": sighting.detected_at.isoformat(),
                                            "confidence": sighting.confidence,
                                            "hotlist_id": str(sighting.hotlist_id),
                                            "last_seen_at": entry.last_seen_at.isoformat()
                                            if entry and entry.last_seen_at
                                            else None,
                                        })
                                except ValueError:
                                    # record_detection rejects impossible
                                    # coordinates; drop the frame silently.
                                    stolen = False
                        finally:
                            db.close()

                        await websocket.send_text(json.dumps({
                            "type": "plate",
                            "plate_id": p_id,
                            "seq": seq,
                            "text": res["text"],
                            "norm": res["norm"],
                            "conf": res["conf"],
                            "valid": res["valid"],
                            "stolen": stolen
                        }))

                asyncio.create_task(process_task(jpeg_bytes, seq, lat, lng))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Live scan ws error: {e}")
    finally:
        for _t in ocr_tasks:
            _t.cancel()
        # Flush on disconnect too: the periodic worker may be up to 5 s behind,
        # and the cells counted in that window are real observations that should
        # not sit in memory waiting for the next flush.
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


"""DEMO-ONLY remote scanner endpoint.

Lets the Expo phone app send camera frames for on-host ANPR OCR. The host runs
the CPU-friendly RapidOCR pipeline and returns only the detected plate +
confidence — no raw frame is stored and nothing about the frame (other than the
plate string) reaches the backend.

Two request encodings are accepted, both returning the identical response shape:

* ``multipart/form-data`` — ``image`` (single) and/or ``images`` (up to three,
  repeated field) as ``UploadFile`` parts. Kept for the existing tests, tools
  and any non-Expo client.
* ``application/json`` — ``{"image_b64": "..."}`` or
  ``{"images_b64": ["...", "..."]}``, each entry an optionally
  ``data:image/...;base64,``-prefixed base64 JPEG.

The JSON form exists because Expo SDK 58's ambient ``fetch`` (expo/fetch, pulled
in by the winter runtime) implements its own FormData whose parts reject React
Native's legacy ``{ uri, name, type }`` file object with
``Unsupported FormDataPart implementation`` — the request is never sent. Base64
JSON sidesteps FormData entirely and behaves the same on every SDK.

Disabled unless DEMO_MODE=true. Production mobile apps run the same pipeline
on-device and never upload frames.
"""

import base64
import binascii
import logging
import os
import re
import sys
import time

import cv2
import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.api.deps import require_volunteer
from app.core.config import settings
from app.db.session import get_db
from app.models import User
from app.services.audit_service import log_action
from app.services.plate import normalize_plate

router = APIRouter()

ALLOWED_CONTENT_TYPES = (None, "image/jpeg", "image/jpg", "image/png", "image/webp")
MAX_FRAMES = 3
# Decoded bytes per frame. Base64 inflates by 4/3, so the wire body is ~8 MB for
# a full-size frame; MAX_UPLOAD_MB still governs the multipart path.
MAX_JSON_IMAGE_MB = 6
MAX_JSON_IMAGE_BYTES = MAX_JSON_IMAGE_MB * 1024 * 1024
_DATA_URL_PREFIX = re.compile(r"^data:image/[A-Za-z0-9.+-]+;base64,", re.IGNORECASE)
_log = logging.getLogger("rakshak.scanner")


def _read_upload(upload: UploadFile) -> bytes:
    if upload.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="Only still images allowed")
    content = upload.file.read(max(settings.MAX_UPLOAD_MB * 1024 * 1024, 1024 * 1024) + 1)
    if len(content) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large")
    return content


def _decode_b64_image(value, index: int) -> bytes:
    """Validate and decode one base64 image entry from a JSON scan request."""
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"image_b64[{index}] is empty")
    raw = _DATA_URL_PREFIX.sub("", value.strip())

    # Refuse before decoding: base64 expands 3 bytes into 4 characters, so this
    # bounds the work without materialising an arbitrarily large buffer.
    b64_ceiling = ((MAX_JSON_IMAGE_BYTES + 2) // 3) * 4 + 4
    if len(raw) > b64_ceiling:
        raise HTTPException(
            status_code=413,
            detail=f"Image {index + 1} too large - limit is {MAX_JSON_IMAGE_MB} MB per image",
        )

    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"image_b64[{index}] is not valid base64") from exc
    if not blob:
        raise HTTPException(status_code=422, detail=f"image_b64[{index}] decoded to zero bytes")
    if len(blob) > MAX_JSON_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Image {index + 1} too large - limit is {MAX_JSON_IMAGE_MB} MB per image",
        )

    # Confirm the bytes really are an image before paying for OCR.
    array = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_COLOR)
    if array is None:
        raise HTTPException(status_code=422, detail=f"image_b64[{index}] is not a decodable image")
    return blob


async def _collect_contents(request: Request) -> list[bytes]:
    """Read the request body as frame bytes, whichever encoding was used."""
    content_type = (request.headers.get("content-type") or "").lower()

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        single: StarletteUploadFile | None = None
        repeated: list[StarletteUploadFile] = []
        for key, value in form.multi_items():
            # request.form() yields Starlette's UploadFile. fastapi.UploadFile
            # *subclasses* it, so testing against the FastAPI class here never
            # matched and every multipart request looked empty.
            if not isinstance(value, StarletteUploadFile):
                continue
            if key == "image" and single is None:
                single = value
            elif key == "images":
                repeated.append(value)
        # Original precedence: a single `image` comes first, then repeated `images`.
        uploads = ([single] if single is not None else []) + repeated
        if not uploads:
            raise HTTPException(status_code=422, detail="No image supplied")
        if len(uploads) > MAX_FRAMES:
            raise HTTPException(status_code=422, detail=f"At most {MAX_FRAMES} frames per scan")
        return [_read_upload(upload) for upload in uploads]

    if content_type.startswith("application/json"):
        try:
            payload = await request.json()
        except Exception as exc:  # malformed JSON body
            raise HTTPException(status_code=422, detail="Body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="Body must be a JSON object")

        entries = payload.get("images_b64")
        if entries is None:
            single = payload.get("image_b64")
            if single is None:
                raise HTTPException(status_code=422, detail="No image supplied (image_b64 or images_b64)")
            entries = [single]
        if not isinstance(entries, list) or not entries:
            raise HTTPException(status_code=422, detail="images_b64 must be a non-empty list")
        if len(entries) > MAX_FRAMES:
            raise HTTPException(status_code=422, detail=f"At most {MAX_FRAMES} frames per scan")
        return [_decode_b64_image(entry, i) for i, entry in enumerate(entries)]

    raise HTTPException(
        status_code=415,
        detail="Send multipart/form-data or application/json with base64 images",
    )


def _import_pipeline():
    def _ai_package_root() -> str | None:
        node = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        while node and os.path.dirname(node) != node:
            if os.path.isfile(os.path.join(node, "ai", "pipeline.py")):
                return node
            node = os.path.dirname(node)
        return None

    root = _ai_package_root()
    if root is not None and root not in sys.path:
        sys.path.append(root)
    from ai.pipeline import detect_and_read_many

    return detect_and_read_many


def _run_pipeline(contents: list[bytes]):
    """OCR the frames and log one concise line per frame plus the vote.

    Kept synchronous and pushed onto the threadpool by the route so the CPU
    work never blocks the event loop.
    """
    detect_and_read_many = _import_pipeline()
    started = time.perf_counter()
    dets = detect_and_read_many(contents)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    sizes = ",".join(str(len(c)) for c in contents)
    _log.info("scan frames=%d bytes=%s", len(contents), sizes)
    # detect_and_read_many stops reading once a frame is confident (early_exit),
    # so it can return fewer detections than frames were sent. Say so, otherwise
    # a single line for a 3-frame scan looks like the other frames went missing.
    for i, det in enumerate(dets, start=1):
        norm = normalize_plate(det.plate)
        _log.info(
            "scan frame %d/%d detector=%s raw=%r norm=%s conf=%.3f valid=%s",
            i,
            len(contents),
            det.source,
            det.plate,
            norm.normalized or "-",
            float(det.confidence),
            bool(det.valid),
        )
    if len(dets) < len(contents):
        _log.info(
            "scan read %d/%d frames - pipeline early-exit on a confident read",
            len(dets),
            len(contents),
        )
    return dets, elapsed_ms


@router.post("/scan")
async def scan_frame(
    request: Request,
    user: User = Depends(require_volunteer),
    db=Depends(get_db),
):
    if not settings.DEMO_MODE:
        raise HTTPException(status_code=404, detail="Not available")

    contents = await _collect_contents(request)
    dets, elapsed_ms = await run_in_threadpool(_run_pipeline, contents)

    if not dets:
        log_action(db, user.id, "scan.no_match", "scan", meta={"frames": len(contents)})
        return {
            "plate": None,
            "confidence": None,
            "valid": False,
            "uncertain": True,
            "note": "No plate found",
            "frames": len(contents),
        }

    det = max(dets, key=lambda d: d.confidence)
    norm = normalize_plate(det.plate)
    # An uncertain read is returned with its best guess but must not look like a
    # confirmed plate: a false hotlist match is worse than asking for a re-scan.
    log_action(
        db,
        user.id,
        "scan.uncertain" if det.uncertain else "scan.hit",
        "scan",
        meta={"plate": det.plate, "confidence": det.confidence, "frames": len(contents)},
    )
    _log.info(
        "scan vote plate=%s norm=%s conf=%.3f valid=%s uncertain=%s frames=%d elapsed_ms=%d",
        det.plate,
        norm.normalized or "-",
        float(det.confidence),
        bool(det.valid and not det.uncertain),
        bool(det.uncertain),
        len(contents),
        elapsed_ms,
    )
    return {
        "plate": det.plate,
        "confidence": det.confidence,
        "valid": det.valid and not det.uncertain,
        "uncertain": det.uncertain,
        "reason": det.reason,
        "frames": len(contents),
        "votes": det.votes,
    }
"""DEMO-ONLY remote scanner endpoint.

Lets the Expo phone app send camera frames for on-host ANPR OCR. The host runs
the CPU-friendly RapidOCR pipeline and returns only the detected plate +
confidence — no raw frame is stored and nothing about the frame (other than the
plate string) reaches the backend.

Accepts either a single ``image`` (the original contract) or up to three
``images`` for a multi-read vote, which averages out blur and glare.

Disabled unless DEMO_MODE=true. Production mobile apps run the same pipeline
on-device and never upload frames.
"""

import logging
import os
import sys

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.api.deps import require_volunteer
from app.core.config import settings
from app.db.session import get_db
from app.models import User
from app.services.audit_service import log_action

router = APIRouter()

ALLOWED_CONTENT_TYPES = (None, "image/jpeg", "image/jpg", "image/png", "image/webp")
MAX_FRAMES = 3
_log = logging.getLogger("rakshak.scanner")


def _read_upload(upload: UploadFile) -> bytes:
    if upload.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="Only still images allowed")
    content = upload.file.read(max(settings.MAX_UPLOAD_MB * 1024 * 1024, 1024 * 1024) + 1)
    if len(content) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large")
    return content


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


@router.post("/scan")
def scan_frame(
    image: UploadFile | None = File(default=None),
    images: list[UploadFile] | None = File(default=None),
    user: User = Depends(require_volunteer),
    db=Depends(get_db),
):
    if not settings.DEMO_MODE:
        raise HTTPException(status_code=404, detail="Not available")

    uploads = list(images or [])
    if image is not None:
        uploads.insert(0, image)
    if not uploads:
        raise HTTPException(status_code=422, detail="No image supplied")
    if len(uploads) > MAX_FRAMES:
        raise HTTPException(status_code=422, detail=f"At most {MAX_FRAMES} frames per scan")

    contents = [_read_upload(upload) for upload in uploads]
    detect_and_read_many = _import_pipeline()

    dets = detect_and_read_many(contents)
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
    # An uncertain read is returned with its best guess but must not look like a
    # confirmed plate: a false hotlist match is worse than asking for a re-scan.
    log_action(
        db,
        user.id,
        "scan.uncertain" if det.uncertain else "scan.hit",
        "scan",
        meta={"plate": det.plate, "confidence": det.confidence, "frames": len(contents)},
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
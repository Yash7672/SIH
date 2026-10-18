"""GET /analytics/ocr - measured OCR accuracy from the offline evaluation.

Read-only: this endpoint serves ``data/eval/ocr_report.json``, which is produced
by ``python scripts/eval_ocr.py``. It NEVER runs the evaluation itself - a
request must not load YOLO/OCR models - and it reads nothing else: no database
queries beyond the role check, no images, no live detections. COP and ADMIN
only, like the rest of /analytics.
"""

from pathlib import Path
from typing import Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError

from app.api.deps import require_cop
from app.core.config import REPO_ROOT
from app.models import User
from app.services.ocr_metrics import OcrNoReport, OcrReport, load_report

router = APIRouter()

# Repo-root anchored (same reasoning as STREAM_SOURCES_PATH): uvicorn and pytest
# start in backend/, so a relative path would look in the wrong tree.
REPORT_PATH: Path = REPO_ROOT / "data" / "eval" / "ocr_report.json"


@router.get(
    "",
    response_model=Union[OcrReport, OcrNoReport],
    summary="Latest measured OCR model accuracy",
    response_description="The parsed eval report, or a no_report hint",
)
def ocr_model_report(_: User = Depends(require_cop)) -> Union[OcrReport, OcrNoReport]:
    """Serve the latest offline OCR evaluation, cached by the file's mtime.

    Returns 200 with ``{"status": "no_report", ...}`` when the evaluation has
    never been run, so the dashboard can show the command to produce it.
    """
    try:
        report = load_report(Path(REPORT_PATH))
    except (OSError, ValueError, ValidationError) as exc:
        raise HTTPException(status_code=500, detail=f"ocr report unreadable: {exc}") from exc
    if report is None:
        return OcrNoReport(status="no_report", hint="Run: python scripts/eval_ocr.py")
    return report

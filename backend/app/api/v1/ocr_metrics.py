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
from app.core.config import BACKEND_DIR, REPO_ROOT
from app.models import User
from app.services.ocr_metrics import OcrEmptyReport, OcrNoReport, OcrReport, load_report

router = APIRouter()


def _default_report_path() -> Path:
    """The eval report, in whichever tree layout is running.

    Locally ``REPO_ROOT`` is the git root and the writer's output lands there.
    Inside the Docker image the package is copied to ``/app/app``, so
    ``REPO_ROOT`` becomes ``/`` while the mounted data directory is ``/app/data``
    - the repo-root guess then pointed at ``/data/eval/...`` and the tab always
    read "no report". Anchor on the first candidate whose *directory* exists, so
    the report is picked up whether or not it has been generated yet.
    """
    candidates = (
        REPO_ROOT / "data" / "eval" / "ocr_report.json",
        BACKEND_DIR / "data" / "eval" / "ocr_report.json",
        Path("/app/data/eval/ocr_report.json"),
    )
    for candidate in candidates:
        if candidate.parent.is_dir():
            return candidate
    return candidates[0]


REPORT_PATH: Path = _default_report_path()


@router.get(
    "",
    response_model=Union[OcrReport, OcrEmptyReport, OcrNoReport],
    summary="Latest measured OCR model accuracy",
    response_description="The parsed eval report, an empty-report notice, or a no_report hint",
)
def ocr_model_report(_: User = Depends(require_cop)) -> Union[OcrReport, OcrEmptyReport, OcrNoReport]:
    """Serve the latest offline OCR evaluation, cached by the file's mtime.

    Returns 200 with ``{"status": "no_report", ...}`` when the evaluation has
    never been run, so the dashboard can show the command to produce it.

    Returns ``{"status": "empty", ...}`` when the evaluation ran but found no
    labelled plates. That is deliberately distinct from ``"ok"``: a report with
    no scored samples has no accuracy, and serving it as a normal report let the
    dashboard render a zero error rate and a green "no bad reads" tick, which is
    a safety claim an empty set cannot support.
    """
    try:
        report = load_report(Path(REPORT_PATH))
    except (OSError, ValueError, ValidationError) as exc:
        raise HTTPException(status_code=500, detail=f"ocr report unreadable: {exc}") from exc
    if report is None:
        return OcrNoReport(status="no_report", hint="Run: python scripts/eval_ocr.py")
    if report.status == "empty":
        return OcrEmptyReport(status="empty", metadata=report.metadata)
    return report

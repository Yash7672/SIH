"""MIT ONNX YOLOv9 "end2end" plate *detector* (open-image-models / fast-alpr).

What this is
------------
A drop-in alternative to the YOLOv8n ``.pt`` plate detector in
:mod:`ai.detector`. It is the detector half of **fast-alpr** by ankandrew, which
sources its detector from the **open-image-models** package. Both are MIT.

Why it exists
-------------
The plate detector is the one stage RAKSHAK can still move cheaply. Measured on
this box on ``data/test_plates/road_scene.jpg`` (1280 px):

    YOLOv8n .pt @640 (ultralytics)   ~98 ms/frame
    this, yolo-v9-t @256 (ONNX)      ~20-28 ms/frame   (~4x faster)

NMS is baked into the graph ("end2end"), so there is no second pass and no
torch - just one small ONNX session, the same runtime the OCR path already uses.

How it returns
--------------
Faithful to open-image-models/fast-alpr: :meth:`PlateDetector.predict_batch`
takes N frames and returns a **list of lists of :class:`PlateBox`**, one inner
list per input frame. Scores are already filtered at ``conf``.

Honest limitation
-----------------
Swapping the detector changes the crop that reaches OCR, and RAKSHAK's
documented invariant is that the plate reading stays byte-for-byte identical, so
this was validated against the plate-accuracy eval before being made the
default. It is on out of the box (``RAKSHAK_PLATE_DETECTOR=auto``) but always
falls back to the YOLOv8n detector and then contours whenever it is absent or
finds nothing, and ``legacy`` restores the old detector exactly.

Provenance and licence
----------------------
Model and the letterbox/decode below are adapted from **open-image-models** by
Andrés (ankandrew), MIT:

    https://github.com/ankandrew/open-image-models
    https://github.com/ankandrew/fast-alpr
    Weights: https://github.com/ankandrew/open-image-models/releases/tag/assets

Privacy
-------
Operates on an in-memory frame and returns boxes only. It never writes an image
and never sends anything anywhere.
"""

from __future__ import annotations

import logging
import os
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

logger = logging.getLogger("rakshak.plate_detector")

# --------------------------------------------------------------------------- #
# Model catalogue
# --------------------------------------------------------------------------- #
_RELEASE_BASE = "https://github.com/ankandrew/open-image-models/releases/download/assets"
# name (input size in px) -> published filename
MODELS: Dict[str, str] = {
    "256": "yolo-v9-t-256-license-plates-end2end.onnx",
    "384": "yolo-v9-t-384-license-plates-end2end.onnx",
    "416": "yolo-v9-t-416-license-plates-end2end.onnx",
    "512": "yolo-v9-t-512-license-plates-end2end.onnx",
    "640": "yolo-v9-t-640-license-plates-end2end.onnx",
}
# Measured choice: 256 was the fastest AND the only size that detected the plate
# at every scale of road_scene.jpg (1280/960/800/640) plus the car photo. 512
# missed road_scene entirely here, 384/416 were erratic. See docs/PLATE_DETECTOR.md.
DEFAULT_SIZE = "256"
DEFAULT_CONF = 0.25

# Weight dir. backend/models/ already holds the other RAKSHAK checkpoints and is
# tracked in git, so this follows the existing convention. Two layouts must both
# work: a repo checkout keeps `backend/models/`, but the Docker image copies
# `backend/models` to `/app/models` while `ai/` lands at `/app/ai` (see
# backend/Dockerfile). Resolved dynamically, mirroring
# `ai.detector.resolve_model_path()`.
MODEL_DIR = Path(__file__).resolve().parents[1] / "backend" / "models" / "open-image-models"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def mode() -> str:
    """``auto`` (default) | ``oim`` | ``legacy`` from ``RAKSHAK_PLATE_DETECTOR``.

    ``auto``   - use the ONNX detector whenever the weights are on disk (default;
                 the weights are vendored, so this is on out of the box, and it
                 silently falls back to legacy on a checkout without them).
    ``oim``    - use it, downloading the weights if they are missing.
    ``legacy`` - never use it; the YOLOv8n ``.pt`` detector, unchanged.
    """
    value = _env("RAKSHAK_PLATE_DETECTOR", "auto").lower()
    return value if value in {"legacy", "oim", "auto"} else "auto"


def size() -> str:
    value = _env("RAKSHAK_PLATE_DETECTOR_SIZE", DEFAULT_SIZE)
    return value if value in MODELS else DEFAULT_SIZE


def _model_dirs() -> List[Path]:
    root = Path(__file__).resolve().parents[1]
    return [
        root / "backend" / "models" / "open-image-models",  # repo checkout
        root / "models" / "open-image-models",              # Docker image layout
    ]


def model_dir() -> Path:
    """The weights directory, preferring whichever layout exists on disk.

    Never raises and never downloads: the caller treats a missing directory as
    "detector unavailable" and falls back to the YOLOv8n detector.
    """
    override = _env("RAKSHAK_PLATE_DETECTOR_DIR")
    if override:
        return Path(override)
    for candidate in _model_dirs():
        if candidate.is_dir():
            return candidate
    return MODEL_DIR


def conf_threshold() -> float:
    try:
        return float(_env("RAKSHAK_PLATE_DETECTOR_CONF", str(DEFAULT_CONF)))
    except ValueError:
        return DEFAULT_CONF


@dataclass(frozen=True)
class PlateBox:
    """One detected plate: pixel box in the *source* frame plus its score."""

    x1: int
    y1: int
    x2: int
    y2: int
    score: float

    @property
    def box(self) -> Tuple[int, int, int, int]:
        return (self.x1, self.y1, self.x2, self.y2)

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1


# --------------------------------------------------------------------------- #
# Preprocessing (letterbox, BGR->RGB, /255, NCHW)
# --------------------------------------------------------------------------- #
def letterbox(
    im: np.ndarray,
    new_shape: Tuple[int, int],
    color: Tuple[int, int, int] = (114, 114, 114),
    scaleup: bool = True,
) -> Tuple[np.ndarray, Tuple[float, float], Tuple[float, float]]:
    """Resize keeping aspect ratio, pad to a square. Mirrors open-image-models."""
    shape = im.shape[:2]
    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
    if not scaleup:
        r = min(r, 1.0)
    new_unpad = int(round(shape[1] * r)), int(round(shape[0] * r))
    dw = (new_shape[1] - new_unpad[0]) / 2
    dh = (new_shape[0] - new_unpad[1]) / 2
    if shape[::-1] != new_unpad:
        im = cv2.resize(im, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    im = cv2.copyMakeBorder(im, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)
    return im, (r, r), (dw, dh)


def prepare_batch(
    images: Sequence[np.ndarray], img_size: int
) -> Tuple[Optional[np.ndarray], List[Tuple[float, float]]]:
    """Stack N BGR frames into one ``(N, 3, s, s)`` float32 tensor.

    Returns the tensor (or ``None`` when there is nothing to run) and the
    per-image ``(ratio, pad_x, pad_y)`` needed to map boxes back.
    """
    tensors: List[np.ndarray] = []
    metas: List[Tuple[float, float]] = []
    for img in images:
        frame, ratio, (dw, dh) = letterbox(img, (img_size, img_size))
        frame = frame.transpose((2, 0, 1))[::-1]  # HWC BGR -> CHW RGB
        frame = (frame / 255.0).astype(np.float32)
        tensors.append(frame)
        metas.append((ratio[0], dw, dh))
    if not tensors:
        return None, []
    return np.ascontiguousarray(np.stack(tensors, axis=0)), metas


def decode_output(
    predictions: np.ndarray,
    metas: Sequence[Tuple[float, float, float]],
    conf: float,
) -> List[List[PlateBox]]:
    """Split a batch of end2end rows and map each box back to source pixels.

    The graph emits ``(N, k, 7)`` = ``[batch_idx, x1, y1, x2, y2, class_id,
    score]`` with NMS already applied, so there is nothing to threshold but the
    score and nothing to undo but the letterbox.
    """
    preds = np.asarray(predictions)
    if preds.ndim == 2:
        preds = preds[None, ...]
    out: List[List[PlateBox]] = []
    for index, (ratio, pad_x, pad_y) in enumerate(metas):
        rows = preds[index] if preds.shape[0] > index else preds[0]
        boxes: List[PlateBox] = []
        for row in rows:
            score = float(row[6])
            if score < conf:
                continue
            x1 = int((row[1] - pad_x) / ratio)
            y1 = int((row[2] - pad_y) / ratio)
            x2 = int((row[3] - pad_x) / ratio)
            y2 = int((row[4] - pad_y) / ratio)
            if x2 <= x1 or y2 <= y1:
                continue
            boxes.append(PlateBox(x1, y1, x2, y2, score))
        boxes.sort(key=lambda b: b.score, reverse=True)
        out.append(boxes)
    return out


# --------------------------------------------------------------------------- #
# Detector
# --------------------------------------------------------------------------- #
class PlateDetector:
    """Thin ONNX wrapper. Batch-first, like fast-alpr's detector."""

    def __init__(self, onnx_path: Path, img_size: int, conf: float = DEFAULT_CONF) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        try:
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        except AttributeError:  # pragma: no cover - very old ORT
            pass
        threads = _env("RAKSHAK_ORT_THREADS")
        if threads.isdigit():
            options.intra_op_num_threads = int(threads)
        self.session = ort.InferenceSession(
            str(onnx_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.img_size = img_size
        self.conf = conf
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name

    def predict_batch(self, images: Sequence[np.ndarray]) -> List[List[PlateBox]]:
        """Detect in N frames in one ONNX call. Always one list per input frame."""
        batch, metas = prepare_batch(images, self.img_size)
        if batch is None:
            return [[] for _ in images]
        try:
            outputs = self.session.run([self.output_name], {self.input_name: batch})
        except Exception as exc:  # noqa: BLE001 - a detector fault must not kill the caller
            logger.warning("plate detector inference failed: %s", exc)
            return [[] for _ in images]
        if not outputs:
            return [[] for _ in images]
        return decode_output(outputs[0], metas, self.conf)

    def predict(self, image: np.ndarray) -> List[PlateBox]:
        return self.predict_batch([image])[0]


# --------------------------------------------------------------------------- #
# Model resolution / loading
# --------------------------------------------------------------------------- #
_load_lock = threading.Lock()
_detector: Optional[PlateDetector] = None
_unavailable_logged = False


def _path(name: Optional[str] = None) -> Path:
    return model_dir() / MODELS[name or size()]


def model_present(name: Optional[str] = None) -> bool:
    return _path(name).is_file()


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "rakshak-anpr"})
    with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310 - fixed https host
        data = response.read()
    tmp.write_bytes(data)
    tmp.replace(dest)


def ensure_model(name: Optional[str] = None, *, allow_download: bool = False) -> bool:
    """Make the weights available locally. Downloads only if asked to."""
    dest = _path(name)
    if dest.is_file():
        return True
    if not allow_download:
        return False
    filename = MODELS[name or size()]
    try:
        logger.info("Downloading plate detector %s to %s", filename, dest.parent)
        _download(f"{_RELEASE_BASE}/{filename}", dest)
        return True
    except Exception as exc:  # noqa: BLE001 - a failed fetch must not break a frame
        logger.warning("Could not download plate detector %s: %s", filename, exc)
        return False


def is_enabled() -> bool:
    """Is the ONNX detector selected *and* usable right now (no load, no network)?

    ``legacy`` -> always False (the caller uses the existing YOLOv8n detector).
    ``oim``    -> True; :func:`get_detector` may then fetch the weights.
    ``auto``   -> True only when the weights are on disk, so an ``auto`` run
                  never touches the network during a request.
    """
    state = mode()
    if state == "legacy":
        return False
    if state == "oim":
        return True
    return model_present()


def available() -> bool:
    return get_detector() is not None


def get_detector() -> Optional[PlateDetector]:
    """Load the detector once, process-wide. ``None`` when unavailable.

    Never raises: a missing model, missing ONNX Runtime or a corrupt file all
    degrade to "use the legacy detector", which is what the caller does with an
    empty result.
    """
    global _detector, _unavailable_logged
    if not is_enabled():
        return None
    if _detector is not None:
        return _detector
    with _load_lock:
        if _detector is not None:
            return _detector
        name = size()
        if not model_present(name) and not ensure_model(name, allow_download=True):
            if not _unavailable_logged:
                logger.warning(
                    "RAKSHAK_PLATE_DETECTOR=%s but the %s weights are absent; "
                    "falling back to the legacy plate detector.",
                    mode(),
                    name,
                )
                _unavailable_logged = True
            return None
        try:
            _detector = PlateDetector(_path(name), int(name), conf_threshold())
            logger.info("open-image-models plate detector ready: yolo-v9-t @%s", name)
        except Exception as exc:  # noqa: BLE001
            if not _unavailable_logged:
                logger.warning("plate detector unavailable: %s", exc)
                _unavailable_logged = True
            return None
    return _detector


def warm() -> bool:
    """Eagerly load the detector. Returns True when it is ready."""
    return get_detector() is not None


# --------------------------------------------------------------------------- #
# Public detect API
# --------------------------------------------------------------------------- #
def detect_batch(images: Sequence[np.ndarray]) -> List[List[PlateBox]]:
    """Detect in a batch of frames. Empty lists when the detector is off."""
    detector = get_detector()
    if detector is None:
        return [[] for _ in images]
    return detector.predict_batch(images)


def detect(image: np.ndarray) -> List[PlateBox]:
    """Detect in one frame. ``[]`` when the detector is off or found nothing."""
    return detect_batch([image])[0]


if __name__ == "__main__":  # pragma: no cover - manual helper
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--download" in sys.argv:
        ok = ensure_model(allow_download=True)
        print("model ready" if ok else "download failed")
        raise SystemExit(0 if ok else 1)
    if not warm():
        print("detector not available (set RAKSHAK_PLATE_DETECTOR=oim to use it)")
        raise SystemExit(1)
    paths = [a for a in sys.argv[1:] if not a.startswith("-")]
    frames = [cv2.imread(p) for p in paths]
    for path, boxes in zip(paths, detect_batch(frames)):
        top = boxes[0] if boxes else None
        print(f"{path}: {len(boxes)} box(es)" + (f" top={top.box} conf={top.score:.2f}" if top else ""))

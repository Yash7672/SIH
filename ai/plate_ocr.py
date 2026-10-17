"""A fast, tiny ONNX plate *recogniser* (crop -> text) for RAKSHAK.

What this is
------------
A second, independent plate reader that runs beside RapidOCR in the
:mod:`app.services.plate_reader` vote and in :mod:`ai.pipeline`. It is
**recognition only**: it expects a crop that already contains a plate and returns
the characters on it. It does not find plates and it does not replace
:mod:`ai.detector` or the RapidOCR engine.

Why it is fast (borrowed wholesale from fast-plate-ocr)
-------------------------------------------------------
1. **One small graph, one pass.** A 3-5 MB CCT model, forward pass only - no
   text-detection and no text-classification stage the way RapidOCR has. The
   model normalises pixels internally, so preprocessing is a single
   ``cv2.resize`` plus a colour conversion, nothing more (:func:`prepare_input`).
2. **Batching.** The graph has a dynamic batch axis, so N crops are stacked into
   one ``(N, H, W, C)`` tensor and read in **one** ``session.run`` call. Call
   overhead is paid once for a whole crop's worth of variants rather than once
   per variant - that is the difference between ~160 ms and ~60 ms for the four
   live variants on this CPU box. See :func:`predict_batch`.
3. **A speed-friendly ORT session** - full graph optimisation and the same thread
   pin the RapidOCR engine uses.

The code returns
----------------
Faithful to fast-plate-ocr's API: a read is a frozen :class:`PlatePrediction`
(``plate`` + per-character ``char_probs``), and the batch entry point returns a
**list of them, one per input image** (:func:`decode_output`). RAKSHAK's own
:class:`~ai.ocr_engine.OcrHit` shape is produced by :func:`read_plate` on top.

Provenance and licence
----------------------
Model and the preprocessing/decoding below are adapted from **fast-plate-ocr** by
Andrés (ankandrew), MIT licensed:

    https://github.com/ankandrew/fast-plate-ocr
    Released weights: https://github.com/ankandrew/cnn-ocr-lp/releases/tag/arg-plates

Only the pieces we actually use are vendored here, on purpose. Installing
``fast-plate-ocr[onnx]`` would pull ``opencv-python-headless`` (which shadows the
``opencv-python`` RAKSHAK already ships) and ``rich`` (which RAKSHAK does not
use). This module reuses the ONNX Runtime, OpenCV, NumPy and PyYAML already in
``backend/requirements.txt``.

Honest limitation
-----------------
The public ``cct-*-v2-global`` weights are trained on 65 countries and **India is
not one of them** (see ``plate_regions`` in the shipped ``*_plate_config.yaml``).
They read Indian plates reasonably but not perfectly, which is exactly why they
are wired in as *supporting* evidence (weighted below RapidOCR by default) rather
than as a replacement. The model is small enough to fine-tune on Indian plates
later; that is the real accuracy lever.

Privacy
-------
Operates on in-memory crops and returns only ``(text, confidence)``. It never
writes an image and never sends anything anywhere.
"""

from __future__ import annotations

import logging
import os
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

if TYPE_CHECKING:  # the OcrHit shape lives in ocr_engine; import only for typing
    from ai.ocr_engine import OcrHit

import cv2
import numpy as np

try:  # allow `ai/` to run standalone without the backend package on sys.path
    from app.services.plate import normalize_plate
except ImportError:  # pragma: no cover
    import sys

    sys.path.append(str(Path(__file__).resolve().parents[1] / "backend"))
    from app.services.plate import normalize_plate

logger = logging.getLogger("rakshak.plate_ocr")

# --------------------------------------------------------------------------- #
# Model catalogue
# --------------------------------------------------------------------------- #
# Release base for the ONNX exports. Each entry maps a model name to the
# (weights, config) filenames published under it.
_MODEL_BASE = "https://github.com/ankandrew/cnn-ocr-lp/releases/download/arg-plates"
MODELS: Dict[str, Tuple[str, str]] = {
    "cct-s-v2-global": ("cct_s_v2_global.onnx", "cct_s_v2_global_plate_config.yaml"),
    "cct-xs-v2-global": ("cct_xs_v2_global.onnx", "cct_xs_v2_global_plate_config.yaml"),
}
# Recommended default: the small CCT model, ~5 MB, best accuracy/size trade-off.
DEFAULT_MODEL = "cct-s-v2-global"

# Where the weights live. Two layouts must both work: a repo checkout keeps them
# in `backend/models/`, but the Docker image copies `backend/models` to
# `/app/models` while `ai/` lands at `/app/ai` (see backend/Dockerfile), so the
# directory is resolved rather than hard-coded. Mirrors
# `ai.detector.resolve_model_path()`, which has the same two candidates.
MODEL_DIR = Path(__file__).resolve().parents[1] / "backend" / "models" / "fast-plate-ocr"

# How much this reader's vote counts against one RapidOCR variant. Below 1.0
# because the shipped weights are not India-trained: it should be able to tip a
# close vote and corroborate a read, but not outvote a confident RapidOCR read on
# its own. Tunable without a code change for A/B runs.
DEFAULT_VOTE_WEIGHT = 0.6

_INTERPOLATION = {
    "nearest": cv2.INTER_NEAREST,
    "linear": cv2.INTER_LINEAR,
    "cubic": cv2.INTER_CUBIC,
    "area": cv2.INTER_AREA,
    "lanczos4": cv2.INTER_LANCZOS4,
}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def mode() -> str:
    """``auto`` (default) | ``on`` | ``off`` from ``RAKSHAK_PLATE_OCR``."""
    value = _env("RAKSHAK_PLATE_OCR", "auto").lower()
    return value if value in {"auto", "on", "off"} else "auto"


def model_name() -> str:
    value = _env("RAKSHAK_PLATE_OCR_MODEL", DEFAULT_MODEL)
    return value if value in MODELS else DEFAULT_MODEL


def _model_dirs() -> List[Path]:
    root = Path(__file__).resolve().parents[1]
    return [
        root / "backend" / "models" / "fast-plate-ocr",  # repo checkout
        root / "models" / "fast-plate-ocr",              # Docker image layout
    ]


def model_dir() -> Path:
    """The weights directory, preferring whichever layout exists on disk.

    Never raises and never downloads: the caller treats a missing directory as
    "reader unavailable" and falls back to RapidOCR.
    """
    override = _env("RAKSHAK_PLATE_OCR_DIR")
    if override:
        return Path(override)
    for candidate in _model_dirs():
        if candidate.is_dir():
            return candidate
    return MODEL_DIR


def vote_weight() -> float:
    try:
        return float(_env("RAKSHAK_PLATE_OCR_WEIGHT", str(DEFAULT_VOTE_WEIGHT)))
    except ValueError:
        return DEFAULT_VOTE_WEIGHT


@dataclass(frozen=True)
class PlateConfig:
    """The subset of a fast-plate-ocr ``*_plate_config.yaml`` we need."""

    img_height: int
    img_width: int
    alphabet: str
    max_plate_slots: int
    pad_char: str
    image_color_mode: str
    keep_aspect_ratio: bool
    interpolation: str

    @property
    def vocabulary_size(self) -> int:
        return len(self.alphabet)

    @property
    def num_channels(self) -> int:
        return 3 if self.image_color_mode == "rgb" else 1

    @classmethod
    def from_yaml(cls, path: Path) -> "PlateConfig":
        import yaml

        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(
            img_height=int(raw.get("img_height", 64)),
            img_width=int(raw.get("img_width", 128)),
            alphabet=str(raw.get("alphabet", "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_")),
            max_plate_slots=int(raw.get("max_plate_slots", 10)),
            pad_char=str(raw.get("pad_char", "_")),
            image_color_mode=str(raw.get("image_color_mode", "rgb")).lower(),
            keep_aspect_ratio=bool(raw.get("keep_aspect_ratio", False)),
            interpolation=str(raw.get("interpolation", "linear")).lower(),
        )


@dataclass(frozen=True)
class PlatePrediction:
    """One reading: the plate string and its per-character confidence.

    Mirrors fast-plate-ocr's ``PlatePrediction`` (a frozen dataclass returned one
    per image), trimmed to the fields RAKSHAK actually consumes.
    """

    plate: str
    char_probs: Tuple[float, ...] = ()

    @property
    def has_confidence(self) -> bool:
        return bool(self.char_probs)

    @property
    def mean_conf(self) -> float:
        return float(np.mean(self.char_probs)) if self.char_probs else 0.0


# --------------------------------------------------------------------------- #
# Preprocessing
# --------------------------------------------------------------------------- #
def _resize_for_model(img: np.ndarray, config: PlateConfig) -> np.ndarray:
    interp = _INTERPOLATION.get(config.interpolation, cv2.INTER_LINEAR)
    if not config.keep_aspect_ratio:
        return cv2.resize(img, (config.img_width, config.img_height), interpolation=interp)
    # Letterbox, matching fast-plate-ocr's padding behaviour.
    oh, ow = img.shape[:2]
    if oh <= 0 or ow <= 0:
        return cv2.resize(img, (config.img_width, config.img_height), interpolation=interp)
    r = min(config.img_height / oh, config.img_width / ow)
    nw, nh = max(1, round(ow * r)), max(1, round(oh * r))
    resized = cv2.resize(img, (nw, nh), interpolation=interp)
    dw, dh = (config.img_width - nw) / 2, (config.img_height - nh) / 2
    top, bottom = round(dh - 0.1), round(dh + 0.1)
    left, right = round(dw - 0.1), round(dw + 0.1)
    border = (114, 114, 114) if img.ndim == 3 else 114
    return cv2.copyMakeBorder(
        resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=border
    )


def _one_frame(img: np.ndarray, config: PlateConfig) -> np.ndarray:
    """BGR/gray crop -> ``(H, W, C)`` uint8 in the model's colour mode."""
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if config.image_color_mode == "rgb":
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    return _resize_for_model(img, config)


def prepare_input(img: np.ndarray, config: PlateConfig) -> Optional[np.ndarray]:
    """Turn one BGR crop into the model's ``(1, H, W, C)`` uint8 input."""
    if img is None or img.size == 0:
        return None
    return np.ascontiguousarray(_one_frame(img, config)[np.newaxis, ...].astype(np.uint8))


def prepare_batch(images: Sequence[np.ndarray], config: PlateConfig) -> Optional[np.ndarray]:
    """Stack N crops into one ``(N, H, W, C)`` uint8 tensor - the batching win.

    ``None`` when there is nothing to read. Empty or degenerate crops in the list
    are replaced by a blank frame rather than dropped, so the returned rows always
    line up with the input rows and the caller does not have to re-index.
    """
    frames = []
    for img in images:
        if img is None or getattr(img, "size", 0) == 0:
            frame = np.zeros((config.img_height, config.img_width, config.num_channels), np.uint8)
        else:
            frame = _one_frame(img, config)
        frames.append(frame)
    if not frames:
        return None
    return np.ascontiguousarray(np.stack(frames, axis=0, dtype=np.uint8))


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #
def decode_output(output: np.ndarray, config: PlateConfig) -> List[PlatePrediction]:
    """Decode a batch of plate-head logits into one prediction per image.

    Mirrors fast-plate-ocr's ``postprocess_output``: reshape to
    ``(N, slots, vocab)``, argmax over the vocabulary, index into the alphabet
    string, then strip the trailing pad character. Per-character probabilities
    are trimmed to the same length so they line up with the plate string.
    """
    vocab = config.vocabulary_size
    if vocab == 0 or config.max_plate_slots == 0:
        return []
    preds = np.asarray(output).reshape(-1, config.max_plate_slots, vocab)
    indices = np.argmax(preds, axis=-1)
    probs = np.max(preds, axis=-1)
    alphabet = np.array(list(config.alphabet))

    out: List[PlatePrediction] = []
    for row in range(indices.shape[0]):
        text = "".join(str(c) for c in alphabet[indices[row]])
        char_probs = probs[row]
        if config.pad_char:
            keep = len(text.rstrip(config.pad_char))
            text = text[:keep]
            char_probs = char_probs[:keep]
        if not text:
            out.append(PlatePrediction(plate=""))
        else:
            out.append(PlatePrediction(plate=text, char_probs=tuple(float(p) for p in char_probs)))
    return out


# --------------------------------------------------------------------------- #
# Recogniser
# --------------------------------------------------------------------------- #
class Recognizer:
    """Thin ONNX wrapper. Batch-first, matching fast-plate-ocr's call shape."""

    def __init__(self, onnx_path: Path, config: PlateConfig) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        # The model is small and fully static after export; let ORT fold it as far
        # as it can, and pin threads the same way the RapidOCR engine is pinned so
        # -Light bounds the whole OCR path.
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
        self.config = config
        self.input_name = self.session.get_inputs()[0].name
        names = [output.name for output in self.session.get_outputs()]
        # The plate head is called "plate" in every released graph; fall back to
        # the first output so a re-export with a different name still runs.
        self.plate_output = "plate" if "plate" in names else names[0]

    def predict_batch(self, images: Sequence[np.ndarray]) -> List[PlatePrediction]:
        """Read N crops in a single ONNX call. Always returns len(images) rows."""
        batch = prepare_batch(images, self.config)
        if batch is None:
            return []
        outputs = self.session.run([self.plate_output], {self.input_name: batch})
        if not outputs:
            return [PlatePrediction(plate="") for _ in images]
        return decode_output(outputs[0], self.config)

    def predict(self, img: np.ndarray) -> Optional[PlatePrediction]:
        results = self.predict_batch([img])
        return results[0] if results else None


# --------------------------------------------------------------------------- #
# Model resolution / loading
# --------------------------------------------------------------------------- #
_load_lock = threading.Lock()
_recognizer: Optional[Recognizer] = None
_unavailable_logged = False


def _paths(name: Optional[str] = None) -> Tuple[Path, Path]:
    onnx, config = MODELS[name or model_name()]
    base = model_dir()
    return base / onnx, base / config


def model_present(name: Optional[str] = None) -> bool:
    onnx, config = _paths(name)
    return onnx.is_file() and config.is_file()


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "rakshak-anpr"})
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - fixed https host
        data = response.read()
    tmp.write_bytes(data)
    tmp.replace(dest)


def ensure_model(name: Optional[str] = None, *, allow_download: bool = False) -> bool:
    """Make the weights available locally. Downloads only if asked to."""
    onnx, config = _paths(name)
    if onnx.is_file() and config.is_file():
        return True
    if not allow_download:
        return False
    onnx_name, config_name = MODELS[name or model_name()]
    try:
        logger.info("Downloading fast-plate-ocr model %s to %s", onnx_name, onnx.parent)
        _download(f"{_MODEL_BASE}/{onnx_name}", onnx)
        _download(f"{_MODEL_BASE}/{config_name}", config)
        return True
    except Exception as exc:  # noqa: BLE001 - a failed fetch must not break a read
        logger.warning("Could not download fast-plate-ocr model %s: %s", onnx_name, exc)
        return False


def is_enabled() -> bool:
    """Is this reader switched on right now (without loading anything)?

    ``auto`` means "on, but only once the weights are on disk" - so nothing ever
    reaches out to the network during a request. ``on`` allows the download.
    """
    state = mode()
    if state == "off":
        return False
    if state == "on":
        return True
    return model_present()


def available() -> bool:
    return get_recognizer() is not None


def get_recognizer() -> Optional[Recognizer]:
    """Load the recogniser once, process-wide. ``None`` when unavailable.

    Never raises: a missing model, a missing ONNX Runtime or a corrupt file all
    degrade to "no second reader", which the callers treat as an empty vote.
    """
    global _recognizer, _unavailable_logged
    if not is_enabled():
        return None
    if _recognizer is not None:
        return _recognizer
    with _load_lock:
        if _recognizer is not None:
            return _recognizer
        name = model_name()
        if not model_present(name):
            if mode() == "on" and not ensure_model(name, allow_download=True):
                if not _unavailable_logged:
                    logger.warning(
                        "RAKSHAK_PLATE_OCR=on but the %s weights could not be fetched; "
                        "falling back to RapidOCR only.",
                        name,
                    )
                    _unavailable_logged = True
                return None
        onnx, config = _paths(name)
        try:
            cfg = PlateConfig.from_yaml(config)
            _recognizer = Recognizer(onnx, cfg)
            logger.info("fast-plate-ocr recogniser ready: %s (%s)", name, onnx.name)
        except Exception as exc:  # noqa: BLE001
            if not _unavailable_logged:
                logger.warning("fast-plate-ocr recogniser unavailable: %s", exc)
                _unavailable_logged = True
            return None
    return _recognizer


def warm() -> bool:
    """Eagerly load the recogniser. Returns True when it is ready."""
    return get_recognizer() is not None


# --------------------------------------------------------------------------- #
# Public read API
# --------------------------------------------------------------------------- #
def predict_batch(images: Sequence[np.ndarray]) -> List[PlatePrediction]:
    """Read a batch of crops in one call. Empty list when the reader is off."""
    recognizer = get_recognizer()
    if recognizer is None:
        return []
    try:
        return recognizer.predict_batch(images)
    except Exception as exc:  # noqa: BLE001 - never let a read fault kill the worker
        logger.warning("fast-plate-ocr batch inference failed: %s", exc)
        return []


def predict(image: np.ndarray) -> Optional[PlatePrediction]:
    """Read one crop. ``None`` when the reader is off or read nothing."""
    results = predict_batch([image])
    if not results or not results[0].plate:
        return None
    return results[0]


def read_plate(image: np.ndarray) -> List["OcrHit"]:
    """Read one crop into RAKSHAK's ``OcrHit`` shape (0 or 1 hits).

    Imported lazily to keep the module importable without the OCR engine.
    """
    results = predict_batch([image])
    return _to_hits(results)


def read_plates(images: Sequence[np.ndarray]) -> List[List["OcrHit"]]:
    """Read many crops in one call, one OcrHit list per input crop."""
    results = predict_batch(images)
    if not results:
        return [[] for _ in images]
    return [[hit] if hit is not None else [] for hit in _as_hits(results)]


def _to_hits(results: Sequence[PlatePrediction]) -> List["OcrHit"]:
    return [hit for hit in _as_hits(results) if hit is not None]


def _as_hits(results: Sequence[PlatePrediction]) -> List[Optional["OcrHit"]]:
    from ai.ocr_engine import OcrHit

    out: List[Optional[OcrHit]] = []
    for pred in results:
        if not pred.plate:
            out.append(None)
            continue
        norm = normalize_plate(pred.plate, ocr_score=pred.mean_conf)
        if not norm.normalized:
            out.append(None)
            continue
        out.append(
            OcrHit(
                text=pred.plate,
                normalized=norm.normalized,
                valid_plate=norm.valid,
                score=float(pred.mean_conf),
                box=None,
                reason=norm.reason,
                state_code_valid=norm.state_code_valid,
                repaired=norm.repaired,
                stage="plate_ocr",
            )
        )
    return out


if __name__ == "__main__":  # pragma: no cover - manual helper
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--download" in sys.argv:
        ok = ensure_model(allow_download=True)
        print("model ready" if ok else "download failed")
        raise SystemExit(0 if ok else 1)
    if not warm():
        print("recogniser not available (set RAKSHAK_PLATE_OCR=on to download)")
        raise SystemExit(1)
    paths = [a for a in sys.argv[1:] if not a.startswith("-")]
    imgs = [cv2.imread(p) for p in paths]
    for path, pred in zip(paths, predict_batch(imgs)):
        print(f"{path}: {pred.plate!r} conf={pred.mean_conf:.3f}")

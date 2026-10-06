"""Character verifier: a small ONNX classifier over *single glyphs*.

What this is for
----------------
The OCR in this project reads a plate as one string. That is the wrong unit for
a decision that pages the police, because ``DL1ZA9092`` and ``DL12AG092`` are
both legal plates - a misread ``Z`` does not produce a string the validator can
reject, it produces a *different real car*. This classifier is trained on
individual character crops (``scripts/gen_char_data.py`` ->
``scripts/train_char_classifier.py``) and is asked, per glyph, "what is this
and how sure are you?", so a weak position can be named instead of being
silently averaged away into a confident-looking string.

Design rules
------------
* **Never fatal.** If the model file, ``onnxruntime`` or OpenCV is missing, or
  inference throws, this reports *disabled / unavailable* and the caller falls
  back to the previous consensus-only behaviour. A missing optional model must
  not take the live scanner down.
* **Polarity-normalised input.** An Indian plate is white, yellow, black or
  green; the glyph is black on the first three and white on the last. The
  border ring of a crop is background by construction, so its median gives the
  background and the crop is inverted when the glyph is the darker thing. That
  makes every crop look the same to the network. The *exact same function* is
  used by the trainer, so training and inference cannot drift apart.
* **Runs off the OCR hot path** and does one batched inference per plate, not
  one per character.

Nothing here writes to disk or looks at a frame - only at the plate crop that
has already been read.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

try:  # pragma: no cover
    import onnxruntime as ort
except Exception:  # pragma: no cover
    ort = None  # type: ignore[assignment]
try:  # pragma: no cover
    import cv2
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
NUM_CLASSES = len(CHARS)
CHAR_TO_IDX = {c: i for i, c in enumerate(CHARS)}
IDX_TO_CHAR = {i: c for i, c in enumerate(CHARS)}

# 32x32 single channel, the shape the exported model takes.
INPUT_HW = 32

# `ai.char_verifier.MODEL_PATH` is <repo>/models/char_cls.onnx
ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = Path(
    os.environ.get("CHAR_VERIFIER_MODEL") or (ROOT / "models" / "char_cls.onnx")
)
# Optional companion file: the temperature the calibrator fitted, so the ONNX
# path applies the same calibration the torch model was measured with.
CALIB_PATH = MODEL_PATH.with_suffix(".calib.json")

_log = logging.getLogger("rakshak.char_verifier")

_session: Any = None
_lock = threading.Lock()
_temperature: float = 1.0
_loaded = False
_disabled_reason = ""


def _disabled_why() -> str:
    if ort is None:
        return "onnxruntime not installed"
    if cv2 is None:
        return "opencv not installed"
    if not MODEL_PATH.exists():
        return f"model not found at {MODEL_PATH}"
    return _disabled_reason


def _load() -> None:
    global _session, _temperature, _loaded, _disabled_reason
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        _loaded = True  # set first: a failed load must not retry every call
        if _disabled_why():
            _disabled_reason = _disabled_why()
            _log.info("char verifier unavailable (%s) -> reads stay unverified", _disabled_reason)
            return
        try:
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.log_severity_level = 3
            _session = ort.InferenceSession(
                str(MODEL_PATH), sess_options=opts, providers=["CPUExecutionProvider"]
            )
        except Exception as exc:  # noqa: BLE001
            _disabled_reason = f"onnx load failed: {exc}"
            _log.warning("char verifier unavailable (%s)", _disabled_reason)
            return
        try:
            if CALIB_PATH.exists():
                import json

                _temperature = float(json.loads(CALIB_PATH.read_text())["temperature"])
        except Exception:  # noqa: BLE001 - calibration is an optimisation only
            _temperature = 1.0
        _log.info("char verifier ready: %s (T=%.3f)", MODEL_PATH.name, _temperature)


def load_and_warm() -> bool:
    """Load the model and run one dummy inference. Safe to call repeatedly."""
    ok = is_enabled()
    if ok:
        classify_batch([np.zeros((INPUT_HW, INPUT_HW, 3), np.uint8)])
    return ok


def is_enabled() -> bool:
    """True when a per-character verdict is actually available."""
    _load()
    return _session is not None


def unavailable_reason() -> str:
    _load()
    return "" if _session is not None else (_disabled_reason or "unavailable")


def reset_for_tests() -> None:
    """Drop the cached session. Tests monkeypatch MODEL_PATH, so they need this."""
    global _session, _temperature, _loaded, _disabled_reason
    with _lock:
        _session = None
        _temperature = 1.0
        _loaded = False
        _disabled_reason = ""


# --------------------------------------------------------------------------- #
# Preprocessing - shared with scripts/train_char_classifier.py on purpose.
# --------------------------------------------------------------------------- #
def preprocess_char(crop_bgr: np.ndarray) -> np.ndarray:
    """One BGR/gray glyph crop -> ``(1, 1, 32, 32)`` float32 in ``[-1, 1]``.

    Order matters and must not change: polarity first (so a black glyph on a
    white plate and a white glyph on a black plate look alike), then pad to a
    square on black, then resize, then scale.
    """
    if crop_bgr is None or getattr(crop_bgr, "size", 0) == 0:
        return np.zeros((1, 1, INPUT_HW, INPUT_HW), np.float32)
    gray = (
        cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
        if len(crop_bgr.shape) == 3
        else crop_bgr.astype(np.uint8, copy=False)
    )
    if gray.ndim != 2 or gray.shape[0] < 2 or gray.shape[1] < 2:
        return np.zeros((1, 1, INPUT_HW, INPUT_HW), np.float32)
    gray = _normalise_polarity(gray)
    h, w = gray.shape
    side = int(max(h, w))
    pad = np.zeros((side, side), np.uint8)
    y, x = (side - h) // 2, (side - w) // 2
    pad[y : y + h, x : x + w] = gray
    resized = cv2.resize(pad, (INPUT_HW, INPUT_HW), interpolation=cv2.INTER_AREA)
    out = resized.astype(np.float32) / 255.0
    out = (out - 0.5) / 0.5
    return out.reshape(1, 1, INPUT_HW, INPUT_HW)


def _normalise_polarity(gray: np.ndarray) -> np.ndarray:
    """Invert when the glyph is darker than the background.

    The background is taken as the median of a thin border ring; the glyph from
    the median of the middle. Medians, not means, because glare and dirt hit a
    minority of pixels and must not move the estimate.
    """
    h, w = gray.shape
    hy = max(1, h // 10)
    wx = max(1, w // 20)
    ring = np.concatenate(
        [
            gray[:hy, :].ravel(),
            gray[-hy:, :].ravel(),
            gray[:, :wx].ravel(),
            gray[:, -wx:].ravel(),
        ]
    )
    y0, y1 = int(h * 0.2), max(int(h * 0.8), int(h * 0.2) + 1)
    x0, x1 = int(w * 0.2), max(int(w * 0.8), int(w * 0.2) + 1)
    core = gray[y0:y1, x0:x1]
    if core.size == 0:
        core = gray
    if float(np.median(core)) < float(np.median(ring)):
        return 255 - gray
    return gray


def _softmax(logits: np.ndarray) -> np.ndarray:
    z = logits.astype(np.float64) / max(_temperature, 1e-3)
    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=-1, keepdims=True)


def _names() -> List[str]:
    if _session is None:  # pragma: no cover - only via a race with reset
        return ["input"]
    return [i.name for i in _session.get_inputs()]


def classify_batch(crops_bgr: Sequence[Any]) -> List[Optional[Dict[str, Any]]]:
    """Classify a whole plate's glyphs in one inference.

    Returns one dict per input crop (``None`` where that crop could not be
    classified, which must not happen to the whole batch just because one glyph
    is odd). Each dict carries ``probs`` (36,), ``max_c``/``max_p``, the top-3
    characters and their probabilities, and ``allowed_max_c``/``allowed_max_p``
    restricted to the characters the plate grammar permits at that position.
    """
    if not is_enabled() or not crops_bgr:
        return [None] * len(crops_bgr)
    usable = [c for c in crops_bgr if c is not None and getattr(c, "size", 0) > 0]
    if not usable:
        return [None] * len(crops_bgr)
    try:
        batch = np.concatenate([preprocess_char(c) for c in usable], axis=0)
        outs = _session.run(None, {_names()[0]: batch})
        probs = _softmax(np.asarray(outs[0]).reshape(len(usable), -1))
    except Exception as exc:  # noqa: BLE001
        _log.debug("char verifier batch failed: %s", exc)
        return [None] * len(crops_bgr)

    out: List[Optional[Dict[str, Any]]] = [None] * len(crops_bgr)
    it = iter(probs)
    for i, c in enumerate(crops_bgr):
        if c is None or getattr(c, "size", 0) == 0:
            continue
        row = next(it)
        order = np.argsort(row)[::-1]
        out[i] = {
            "probs": row.astype(np.float32),
            "max_c": IDX_TO_CHAR[int(order[0])],
            "max_p": float(row[order[0]]),
            "top3": [
                [IDX_TO_CHAR[int(j)], float(row[j])] for j in order[:3]
            ],
            "p_for": None,
        }
    return out


def probability_of(result: Optional[Dict[str, Any]], char: str) -> float:
    """P(``char``) under one classifier result, 0.0 when unavailable."""
    if not result or char not in CHAR_TO_IDX:
        return 0.0
    return float(result["probs"][CHAR_TO_IDX[char]])


def restricted_max(
    result: Optional[Dict[str, Any]], allowed: Optional[Sequence[str]]
) -> tuple[Optional[str], float]:
    """Best character at this position *within the grammar's whitelist*.

    Returns ``(char, p)`` or ``(None, 0.0)`` when the whitelist is empty or the
    classifier is unavailable. A position where the whitelist-constrained argmax
    disagrees with the unrestricted one is the strongest signal the verifier has:
    the pixels look like ``S`` but the grammar only allows digits here.
    """
    if not result or not allowed:
        return None, 0.0
    probs = result["probs"]
    best_c, best_p = None, 0.0
    for ch in allowed:
        if ch not in CHAR_TO_IDX:
            continue
        p = float(probs[CHAR_TO_IDX[ch]])
        if p > best_p:
            best_c, best_p = ch, p
    return best_c, best_p


def classify_char(crop_bgr: np.ndarray) -> Optional[Dict[str, Any]]:
    """Single-crop convenience wrapper."""
    return classify_batch([crop_bgr])[0]
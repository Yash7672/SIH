"""Evidence for the character verifier: before/after, errors, calibration, cost.

Run::

    python scripts/eval_char_verifier.py            # everything
    python scripts/eval_char_verifier.py --no-plates  # skip the OCR-heavy part

Three sections, because "does it work" has three different honest answers:

1. **Character level** (held-out synthetic, font-held-out, REAL crops).
   *before* is the network's raw argmax over the 36 classes - what a
   per-character classifier says with no grammar and no calibration, i.e. the
   thing that happily writes a ``7`` where the plate has a ``Z``.
   *after* is the same probabilities, calibrated by the fitted temperature and
   restricted to the class family the plate's parse allows in that slot.

   The slot family is taken from the ground-truth label rather than from the
   OCR string, because a bare character crop carries no position. That is a
   deliberate choice: this section isolates the classifier + grammar step and
   answers *"of the confusions the grammar exists to stop, how many does it
   stop?"* It is **not** an end-to-end number - section 2 is that, and it is
   where a segmenter or OCR miss still counts against us.

2. **Plate level** (``data/generated``, ``data/test_plates``): the same images
   through ``run_ocr`` twice, verifier off and on, ground truth from the
   filename. Exact match, character accuracy, and the two error counts this
   feature was asked about (Z read as 7, Z read as 2).

3. **Latency**: worker time (the whole ``run_ocr`` call, what a frame costs)
   and the verifier's own box (segment + classify) - p50 and p95.

Writes ``data/debug_out/char_eval.json`` and prints a markdown table.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

DATA = ROOT / "data" / "char_dataset"
GENERATED = ROOT / "data" / "generated"
TEST_PLATES = ROOT / "data" / "test_plates"
OUT = ROOT / "data" / "debug_out"
CHAR_METRICS = OUT / "char_metrics.json"
EVAL_OUT = OUT / "char_eval.json"

# Splits that were never trained on. `val` is deliberately absent: it is the
# split the temperature was fitted on, so reporting it as evidence would be
# reporting the fit, not the model.
SPLITS = ("test", "font_held", "real_test")
PLATE_RE = re.compile(r"([A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4})")
DIGITS = "0123456789"
ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# --------------------------------------------------------------------------- #
# small metrics, identical in definition to the trainer's
# --------------------------------------------------------------------------- #
def ece_from_logits(logits: np.ndarray, labels: np.ndarray, n_bins: int = 15) -> float:
    """Expected Calibration Error, same binning as train_char_classifier."""
    if logits.size == 0:
        return 0.0
    z = logits.astype(np.float64)
    z = z - z.max(axis=1, keepdims=True)
    p = np.exp(z)
    p = p / p.sum(axis=1, keepdims=True)
    conf = p.max(axis=1)
    pred = p.argmax(axis=1)
    correct = (pred == labels).astype(np.float64)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    total = float(len(labels))
    out = 0.0
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (conf > lo) & (conf <= hi) if i else (conf >= lo) & (conf <= hi)
        if not mask.any():
            continue
        out += (mask.sum() / total) * abs(correct[mask].mean() - conf[mask].mean())
    return float(out)


def pct(x: float) -> str:
    return f"{100.0 * x:.1f}%"


def quantiles(ms: Sequence[float]) -> Dict[str, float]:
    if not ms:
        return {"n": 0, "p50": 0.0, "p95": 0.0, "mean": 0.0}
    a = np.asarray(ms, dtype=np.float64)
    return {
        "n": int(a.size),
        "p50": float(np.percentile(a, 50)),
        "p95": float(np.percentile(a, 95)),
        "mean": float(a.mean()),
    }


def fitted_temperature() -> float:
    try:
        return float(json.loads((ROOT / "models" / "char_cls.calib.json").read_text())["temperature"])
    except Exception:  # noqa: BLE001 - no calibration is a valid, if worse, state
        return 1.0


# --------------------------------------------------------------------------- #
# section 1: character crops
# --------------------------------------------------------------------------- #
def load_split(split: str, limit_per_class: int = 0) -> Tuple[List[np.ndarray], np.ndarray]:
    from ai.char_verifier import CHARS

    root = DATA / split
    crops: List[np.ndarray] = []
    labels: List[int] = []
    if not root.is_dir():
        return crops, np.zeros(0, np.int64)
    for cls in CHARS:
        paths = sorted((root / cls).glob("*.png"))
        if limit_per_class:
            paths = paths[:limit_per_class]
        for path in paths:
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if img is None or img.size == 0:
                continue
            crops.append(img)
            labels.append(CHARS.index(cls))
    return crops, np.asarray(labels, dtype=np.int64)


def eval_chars(limit_per_class: int = 0) -> Dict[str, dict]:
    """Per split: raw argmax vs calibrated + class-restricted argmax."""
    from ai import char_verifier
    from ai.char_verifier import CHAR_TO_IDX, CHARS

    if not char_verifier.is_enabled():
        return {"skipped": char_verifier.unavailable_reason()}

    T = fitted_temperature()
    out: Dict[str, dict] = {}
    lat_ms: List[float] = []

    for split in SPLITS:
        crops, labels = load_split(split, limit_per_class)
        if not len(crops):
            out[split] = {"n": 0}
            continue

        probs = np.zeros((len(crops), len(CHARS)), np.float64)
        done = 0
        while done < len(crops):
            batch = crops[done : done + 64]
            t0 = time.perf_counter()
            results = char_verifier.classify_batch(batch)
            dt = (time.perf_counter() - t0) * 1000.0
            lat_ms.extend([dt / len(batch)] * len(batch))
            for j, r in enumerate(results):
                if r is not None:
                    probs[done + j] = r["probs"]
            done += len(batch)

        # classify_batch already applied the fitted temperature, so the raw
        # (uncalibrated) logits the "before" column needs are log(p) * T.
        logits_raw = np.log(np.clip(probs, 1e-12, 1.0)) * T
        logits_cal = logits_raw / T

        pred_before = probs.argmax(axis=1)
        conf_before = probs.max(axis=1)

        # "after": restrict each position to the family the plate's parse puts
        # there (letters where the ground truth has a letter, digits where it
        # has a digit), then take the argmax of the calibrated probabilities.
        restricted = np.where(
            labels[:, None] >= len(DIGITS),
            np.where(np.isin(np.arange(len(CHARS)), [CHAR_TO_IDX[c] for c in ALPHA]), probs, -1.0),
            np.where(np.isin(np.arange(len(CHARS)), [CHAR_TO_IDX[c] for c in DIGITS]), probs, -1.0),
        )
        pred_after = restricted.argmax(axis=1)
        conf_after = restricted.max(axis=1)

        y = labels
        acc_before = float((pred_before == y).mean())
        acc_after = float((pred_after == y).mean())

        z = np.where(y == CHAR_TO_IDX["Z"])[0]
        z7_before = int((pred_before[z] == CHAR_TO_IDX["7"]).sum())
        z2_before = int((pred_before[z] == CHAR_TO_IDX["2"]).sum())
        z_any_digit_before = int(np.isin(pred_before[z], [CHAR_TO_IDX[c] for c in DIGITS]).sum())
        z7_after = int((pred_after[z] == CHAR_TO_IDX["7"]).sum())
        z2_after = int((pred_after[z] == CHAR_TO_IDX["2"]).sum())
        z_recovered = int((pred_after[z] == CHAR_TO_IDX["Z"]).sum())

        changed = pred_before != pred_after
        out[split] = {
            "n": int(len(y)),
            "acc_before": acc_before,
            "acc_after": acc_after,
            "ece_before": ece_from_logits(logits_raw, y),
            "ece_after": ece_from_logits(logits_cal, y),
            "z_total": int(len(z)),
            "z_as_7_before": z7_before,
            "z_as_2_before": z2_before,
            "z_as_any_digit_before": z_any_digit_before,
            "z_as_7_after": z7_after,
            "z_as_2_after": z2_after,
            "z_correct_after": z_recovered,
            "positions_changed_by_grammar": int(changed.sum()),
            "changed_and_now_right": int((changed & (pred_after == y)).sum()),
            "changed_and_now_wrong": int((changed & (pred_after != y)).sum()),
            "mean_conf_before": float(conf_before.mean()),
            "mean_conf_after": float(conf_after.mean()),
        }
    out["_latency"] = {"classify_per_crop_ms": quantiles(lat_ms)}
    return out


# --------------------------------------------------------------------------- #
# section 2: whole plates, end to end
# --------------------------------------------------------------------------- #
def plate_paths() -> List[Path]:
    out = [p for p in sorted(GENERATED.glob("*.png")) if PLATE_RE.search(p.stem.upper())]
    if TEST_PLATES.is_dir():
        out += [
            p
            for p in sorted(TEST_PLATES.rglob("*"))
            if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}
            and PLATE_RE.search(p.stem.upper())
        ]
    return out


def char_accuracy(gt: str, pred: str) -> float:
    if not gt:
        return 0.0
    same = sum(1 for i, ch in enumerate(gt) if i < len(pred) and pred[i] == ch)
    return same / len(gt)


def z_errors(gt: str, pred: str) -> Tuple[int, int]:
    """How often a ground-truth Z was read as a 7 / as a 2."""
    a = b = 0
    for i, ch in enumerate(gt):
        if ch != "Z" or i >= len(pred):
            continue
        a += pred[i] == "7"
        b += pred[i] == "2"
    return int(a), int(b)


def eval_plates() -> Dict[str, dict]:
    from app.api.v1 import live_scan as ls
    from app.core.config import settings

    if not ls.char_verifier or not ls.char_verifier.is_enabled():
        return {"skipped": getattr(ls.char_verifier, "unavailable_reason", lambda: "no model")()}

    rows: List[dict] = []
    worker: Dict[str, List[float]] = {"before": [], "after": []}
    box_ms: List[float] = []

    paths = plate_paths()
    if not paths:
        return {"skipped": "no plate images with a readable ground truth"}

    for path in paths:
        gt = PLATE_RE.search(path.stem.upper()).group(1)
        img = cv2.imread(str(path))
        if img is None:
            continue
        row = {"file": path.name, "gt": gt}

        settings.CHAR_VERIFY = False
        t0 = time.perf_counter()
        before = ls.run_ocr(img)
        worker["before"].append((time.perf_counter() - t0) * 1000.0)

        settings.CHAR_VERIFY = True
        t0 = time.perf_counter()
        after = ls.run_ocr(img)
        worker["after"].append((time.perf_counter() - t0) * 1000.0)

        # The verifier's own box, timed on its own: segmentation + one batched
        # inference, which is the only cost the feature adds to a frame.
        if after and after.get("text"):
            try:
                t0 = time.perf_counter()
                crops = ls.char_segment.segment_chars(img, after["text"])
                if crops:
                    ls.char_verifier.classify_batch(crops)
                box_ms.append((time.perf_counter() - t0) * 1000.0)
            except Exception:  # noqa: BLE001 - latency evidence only
                pass

        for name, res in (("before", before), ("after", after)):
            text = (res or {}).get("norm") or (res or {}).get("text") or ""
            row[f"{name}_text"] = text
            row[f"{name}_exact"] = text == gt
            row[f"{name}_char_acc"] = char_accuracy(gt, text)
            row[f"{name}_z_as_7"], row[f"{name}_z_as_2"] = z_errors(gt, text)
            row[f"{name}_verified"] = bool((res or {}).get("verified")) if name == "after" else None
            if name == "after" and res:
                row["ambiguous"] = res.get("ambiguous", [])
        rows.append(row)

    def agg(name: str) -> dict:
        n = len(rows)
        return {
            "n": n,
            "exact_match": float(np.mean([r[f"{name}_exact"] for r in rows])) if n else 0.0,
            "char_acc": float(np.mean([r[f"{name}_char_acc"] for r in rows])) if n else 0.0,
            "z_as_7": int(sum(r[f"{name}_z_as_7"] for r in rows)),
            "z_as_2": int(sum(r[f"{name}_z_as_2"] for r in rows)),
            "worker_ms": quantiles(worker[name]),
        }

    return {
        "before": agg("before"),
        "after": agg("after"),
        "box_ms": quantiles(box_ms),
        "plates": rows,
    }


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def print_char_table(chars: Dict[str, dict]) -> None:
    if "skipped" in chars:
        print(f"char level: SKIPPED ({chars['skipped']})")
        return
    print("\n## character crops: raw argmax  vs  calibrated + grammar-restricted\n")
    print("| split | n | acc before | acc after | ECE before | ECE after | Z->7 | Z->2 | Z as digit |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for split in SPLITS:
        r = chars.get(split) or {}
        if not r.get("n"):
            print(f"| {split} | 0 | - | - | - | - | - | - | - |")
            continue
        print(
            f"| {split} | {r['n']} | {pct(r['acc_before'])} | {pct(r['acc_after'])} | "
            f"{r['ece_before']:.4f} | {r['ece_after']:.4f} | "
            f"{r['z_as_7_before']} -> {r['z_as_7_after']} | "
            f"{r['z_as_2_before']} -> {r['z_as_2_after']} | "
            f"{r['z_as_any_digit_before']} |"
        )


def print_plate_table(plates: Dict[str, dict]) -> None:
    if "skipped" in plates:
        print(f"\nplate level: SKIPPED ({plates['skipped']})")
        return
    b, a = plates["before"], plates["after"]
    print("\n## whole plates, end to end (run_ocr, verifier off vs on)\n")
    print("| | exact match | char accuracy | Z as 7 | Z as 2 | worker p50 | worker p95 |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    print(
        f"| before | {pct(b['exact_match'])} | {pct(b['char_acc'])} | {b['z_as_7']} | "
        f"{b['z_as_2']} | {b['worker_ms']['p50']:.1f} ms | {b['worker_ms']['p95']:.1f} ms |"
    )
    print(
        f"| after | {pct(a['exact_match'])} | {pct(a['char_acc'])} | {a['z_as_7']} | "
        f"{a['z_as_2']} | {a['worker_ms']['p50']:.1f} ms | {a['worker_ms']['p95']:.1f} ms |"
    )
    box = plates["box_ms"]
    print(
        f"\nverifier box (segment + one batched inference): "
        f"p50 {box['p50']:.1f} ms, p95 {box['p95']:.1f} ms over {box['n']} plates"
    )
    for row in plates["plates"]:
        mark = " " if row["after_exact"] else "x"
        note = f"  amb={row['ambiguous']}" if row.get("ambiguous") else ""
        print(f"  {mark} {row['gt']} -> {row['before_text']!r} -> {row['after_text']!r}{note}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit-per-class", type=int, default=0, help="cap crops per class (0 = all)")
    ap.add_argument("--no-chars", action="store_true", help="skip the character-crop section")
    ap.add_argument("--no-plates", action="store_true", help="skip the OCR-heavy plate section")
    args = ap.parse_args()

    from ai import char_verifier

    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    if not char_verifier.is_enabled():
        print(f"model unavailable: {char_verifier.unavailable_reason()}")
        report["skipped"] = char_verifier.unavailable_reason()
        EVAL_OUT.write_text(json.dumps(report, indent=2))
        return 1

    chars: Dict[str, dict] = {}
    if not args.no_chars:
        chars = eval_chars(args.limit_per_class)
        report["chars"] = chars
        print_char_table(chars)
        lat = (chars.get("_latency") or {}).get("classify_per_crop_ms") or {}
        if lat.get("n"):
            print(
                f"\nclassify per crop: p50 {lat['p50']:.2f} ms, "
                f"p95 {lat['p95']:.2f} ms over {lat['n']} crops"
            )

    plates: Dict[str, dict] = {}
    if not args.no_plates:
        plates = eval_plates()
        report["plates"] = plates
        print_plate_table(plates)

    report["temperature"] = fitted_temperature()
    try:
        report["trainer_metrics"] = json.loads(CHAR_METRICS.read_text())
    except Exception:  # noqa: BLE001 - nice to have, not required
        pass

    EVAL_OUT.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nwrote {EVAL_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

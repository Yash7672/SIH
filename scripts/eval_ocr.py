"""Offline, read-only OCR evaluation with real measurements.

Runs the SAME function the live system uses - ``ai.pipeline.detect_and_read``
(plate detection, crop, preprocessing, RapidOCR, character verifier when
``CHAR_VERIFY`` is on, normalisation) - over two labelled sets on disk:

  REAL      ``data/test_plates/*``       expected plate comes from the file name
  SYNTHETIC ``data/generated/*.png``     fixed-seed generated test plates

Files whose name is not a valid plate pattern (e.g. ``road_scene.jpg``) are
listed as skipped, never scored. ``data/char_dataset`` is NOT used: it holds
single-character glyph renders (train/val/test splits), not plates, so plate
detection cannot run on it; it is stated in the report metadata instead. If a
training manifest is ever found on disk, the files it lists are excluded and
counted.

Writes ``data/eval/ocr_report.json`` (atomic) and ``data/eval/ocr_samples.csv``.
Never writes an image, never touches the database, never tunes anything: the
model, the thresholds and the file list are exactly what the live path uses.
A low accuracy is a RESULT - this script exits 0 whenever it completed.

Usage:
    python scripts/eval_ocr.py
"""

from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from ai.pipeline import detect_and_read  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.services.plate_verdict import STOLEN, PlateRead, TrackVerdict  # noqa: E402
from app.services.ocr_metrics import (  # noqa: E402
    CONFIDENT_THRESHOLD,
    bootstrap_accuracy,
    character_totals,
    confidence_histogram,
    confusion_from_pairs,
    decision_matrix,
    kde_curve,
    levenshtein_distance,
    align_chars,
    percentile,
    wilson_interval,
)

REAL_DIR = ROOT / "data" / "test_plates"
SYNTHETIC_DIR = ROOT / "data" / "generated"
EVAL_DIR = ROOT / "data" / "eval"
REPORT_PATH = EVAL_DIR / "ocr_report.json"
SAMPLES_PATH = EVAL_DIR / "ocr_samples.csv"

TARGET = 0.90
# The headline is the REAL set only once it has enough plates to say anything;
# below that the combined number is shown with a flag instead of quietly
# standing in for real-world accuracy.
REAL_MIN_SAMPLES = 30

# Fixed seeds and sizes for the two distribution plots and the decision matrix,
# so the same inputs always produce the same report.
DECISION_SEED = 20261018
KDE_POINTS = 100
BOOTSTRAP_ITERATIONS = 2000

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
# "DL1ZA9092.jpg" and "plate_3_MH12JK4567.png" both encode the plate in the name.
FILENAME_PLATE = re.compile(r"([A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4})")

# Conventional places a training manifest could live. None exists today; when
# one appears, the files it lists stop being evaluation data.
MANIFEST_CANDIDATES = [
    ROOT / "data" / "train_manifest.txt",
    ROOT / "data" / "train_manifest.json",
    ROOT / "data" / "char_dataset" / "train_manifest.txt",
    ROOT / "data" / "char_dataset" / "manifest.json",
]

CHAR_DATASET_NOTE = (
    "data/char_dataset holds single-character glyph renders (train/val/test "
    "splits), not plates, so its held-out test split cannot be evaluated with "
    "plate detection and was NOT used. The synthetic set is the fixed-seed "
    "generated plates in data/generated/ (never used to train anything)."
)


def expected_plate(path: Path) -> Optional[str]:
    match = FILENAME_PLATE.search(path.stem.upper())
    return re.sub(r"[^A-Z0-9]", "", match.group(1)) if match else None


def iter_images(directory: Path) -> List[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )


def training_manifest_exclusions() -> tuple[int, List[str]]:
    """Files listed by an on-disk training manifest, if one exists."""
    for manifest in MANIFEST_CANDIDATES:
        if not manifest.is_file():
            continue
        text = manifest.read_text(encoding="utf-8")
        if manifest.suffix == ".json":
            try:
                data = json.loads(text)
                entries = data if isinstance(data, list) else data.get("files", [])
                return len(entries), [str(e) for e in entries]
            except json.JSONDecodeError:
                pass
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        return len(lines), lines
    return 0, []


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def model_files() -> List[dict]:
    found: List[Path] = []
    for pattern in ("backend/models/*.pt", "backend/models/*.onnx", "models/*.pt", "models/*.onnx"):
        found.extend(sorted(ROOT.glob(pattern)))
    try:
        spec = importlib.util.find_spec("rapidocr_onnxruntime")
        if spec and spec.submodule_search_locations:
            pkg = Path(list(spec.submodule_search_locations)[0])
            found.extend(sorted(pkg.rglob("*.onnx"))[:6])
    except Exception:
        pass
    seen = set()
    out = []
    for p in found:
        key = str(p)
        if key in seen or not p.is_file():
            continue
        seen.add(key)
        out.append({"name": p.name, "path": str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else key,
                    "size_bytes": p.stat().st_size})
    return out


def package_versions() -> dict:
    def ver(dist: str) -> str:
        try:
            return importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            return ""

    return {
        "python": sys.version.split()[0],
        "ultralytics": ver("ultralytics"),
        "onnxruntime": ver("onnxruntime"),
        "rapidocr": ver("rapidocr-onnxruntime"),
        "rapidfuzz": ver("RapidFuzz"),
    }


def run_sample(path: Path, set_name: str, expected: str) -> dict:
    started = time.perf_counter()
    reads = detect_and_read(str(path))
    ms = (time.perf_counter() - started) * 1000.0
    det = reads[0] if reads else None
    predicted = det.plate if det else ""
    confidence = float(det.confidence) if det else 0.0
    valid = bool(det.valid) if det else False
    uncertain = bool(det.uncertain) if det else True
    verdict = "no_read" if det is None else ("uncertain" if uncertain else "read")

    pairs = align_chars(expected, predicted)
    edits = len(pairs)
    true_chars = sum(1 for t, _ in pairs if t != "\u2205")
    return {
        "set": set_name,
        "file": path.name,
        "expected": expected,
        "predicted": predicted,
        "exact": predicted == expected,
        "confidence": confidence,
        "valid": valid,
        "uncertain": uncertain,
        "verdict": verdict,
        "stage": det.stage if det else "",
        "time_ms": ms,
        "pairs": pairs,
        "char_edits": edits,
        "char_total": true_chars,
        "wrong_confident": bool(
            predicted and predicted != expected and confidence >= CONFIDENT_THRESHOLD
        ),
        "right_but_uncertain": bool(predicted == expected and uncertain),
    }


def summarize(samples: List[dict]) -> dict:
    """Full metric bundle for one set of samples."""
    n = len(samples)
    if n == 0:
        return {
            "n": 0,
            "exact_matches": 0,
            "plate_accuracy": None,
            "ci_low": None,
            "ci_high": None,
            "character_accuracy": None,
            "cer": None,
            "no_read_rate": 0.0,
            "invalid_format_rate": 0.0,
            "wrong_confident_rate": 0.0,
            "wrong_confident_count": 0,
            "right_but_uncertain_count": 0,
            "latency_p50_ms": None,
            "latency_p95_ms": None,
            "accuracy_at_conf_08": None,
            "coverage_at_conf_08": 0.0,
        }
    exact = sum(1 for s in samples if s["exact"])
    accuracy = exact / n
    ci_low, ci_high = wilson_interval(exact, n)
    chars = character_totals([s["pairs"] for s in samples])
    confident = [s for s in samples if s["confidence"] >= CONFIDENT_THRESHOLD]
    confident_exact = sum(1 for s in confident if s["exact"])
    latencies = [s["time_ms"] for s in samples]
    return {
        "n": n,
        "exact_matches": exact,
        "plate_accuracy": accuracy,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "character_accuracy": chars["character_accuracy"],
        "cer": chars["cer"],
        "no_read_rate": sum(1 for s in samples if not s["predicted"]) / n,
        "invalid_format_rate": sum(1 for s in samples if s["predicted"] and not s["valid"]) / n,
        "wrong_confident_count": sum(1 for s in samples if s["wrong_confident"]),
        "wrong_confident_rate": sum(1 for s in samples if s["wrong_confident"]) / n,
        "right_but_uncertain_count": sum(1 for s in samples if s["right_but_uncertain"]),
        "latency_p50_ms": percentile(latencies, 0.50),
        "latency_p95_ms": percentile(latencies, 0.95),
        "accuracy_at_conf_08": (confident_exact / len(confident)) if confident else None,
        "coverage_at_conf_08": len(confident) / n,
    }


def in_memory_hotlist(expected_plates: List[str]) -> set:
    """Half of the expected plates, picked with a fixed seed.

    Deterministic (so the report is reproducible) and it leaves the other half
    out, so a positive sample really is a listed plate and a negative sample
    really is one that is not listed.
    """
    unique = sorted(set(expected_plates))
    if not unique:
        return set()
    take = max(1, len(unique) // 2)
    return set(random.Random(DECISION_SEED).sample(unique, take))


def build_decision(samples: List[dict], hotlist: set) -> dict:
    """Stolen vs Not-stolen, decided by the real verdict function, in memory.

    The live ``TrackVerdict.evaluate`` decides each sample from one PlateRead
    built from the same read the scanner feeds it. No verdict rule is changed
    and nothing here touches the database or Redis: the hot-list is a plain
    Python set. The offline read path exposes no per-character confidences, so
    ``verified`` mirrors ``settings.CHAR_VERIFY`` (what the deployed scanner
    does) and ``char_confs`` is left empty.
    """
    actual: List[bool] = []
    predicted: List[bool] = []
    for s in samples:
        verdict = TrackVerdict(tid=0, now=0.0)
        verdict.add(
            PlateRead(
                text=s["predicted"],
                norm=s["predicted"],
                conf=float(s["confidence"]),
                at=0.0,
                valid=bool(s["valid"]),
                verified=bool(settings.CHAR_VERIFY),
            )
        )
        actual.append(s["expected"] in hotlist)
        predicted.append(verdict.evaluate(hotlist).state == STOLEN)
    metrics = decision_matrix(actual, predicted)
    metrics["hotlist_size"] = len(hotlist)
    metrics["method"] = (
        "TrackVerdict.evaluate, one PlateRead per sample, in-memory half-hotlist "
        f"(fixed seed {DECISION_SEED}); glyph verifier "
        f"{'on' if settings.CHAR_VERIFY else 'off'}; char_confs unavailable offline"
    )
    return metrics


def build_distributions(samples: List[dict]) -> dict:
    """Correct/wrong confidence densities and the bootstrap accuracy curve."""
    correct = [float(s["confidence"]) for s in samples if s["exact"]]
    wrong = [float(s["confidence"]) for s in samples if not s["exact"]]
    grid, correct_curve = kde_curve(correct, points=KDE_POINTS)
    _, wrong_curve = kde_curve(wrong, points=KDE_POINTS)
    return {
        "grid": grid,
        "correct": correct_curve,
        "wrong": wrong_curve,
        "correct_mean": (sum(correct) / len(correct)) if correct else None,
        "wrong_mean": (sum(wrong) / len(wrong)) if wrong else None,
        "correct_n": len(correct),
        "wrong_n": len(wrong),
        "bootstrap": bootstrap_accuracy(
            [s["exact"] for s in samples], iterations=BOOTSTRAP_ITERATIONS, seed=DECISION_SEED
        ),
    }


def atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    real_images = iter_images(REAL_DIR)
    synthetic_images = iter_images(SYNTHETIC_DIR)

    excluded_count, excluded_files = training_manifest_exclusions()
    excluded_names = {Path(f).name for f in excluded_files}

    skipped: List[dict] = []
    kept: List[tuple[str, Path, str]] = []  # (set, path, expected)
    for set_name, images in (("real", real_images), ("synthetic", synthetic_images)):
        for path in images:
            if path.name in excluded_names:
                skipped.append({"file": path.name, "reason": "listed in a training manifest"})
                continue
            expected = expected_plate(path)
            if expected is None:
                skipped.append({"file": path.name, "reason": "no plate pattern in the file name"})
                continue
            kept.append((set_name, path, expected))

    print(f"OCR eval: {len(kept)} scored files ({len(skipped)} skipped), "
          f"char verifier {'ON' if settings.CHAR_VERIFY else 'OFF'}")
    if not kept:
        print("nothing to evaluate - add labelled plates (e.g. DL1ZA9092.jpg) to data/test_plates/")
        atomic_write_json(
            REPORT_PATH,
            {
                "status": "ok",
                "metadata": {"generated_at": datetime.now(timezone.utc).isoformat(),
                             "commit": git_commit(),
                             "dataset": {"real_files": 0, "synthetic_files": 0, "skipped": skipped,
                                         "char_dataset_note": CHAR_DATASET_NOTE}},
            },
        )
        return 0

    samples: List[dict] = []
    for set_name, path, expected in kept:
        row = run_sample(path, set_name, expected)
        samples.append(row)
        mark = "ok  " if row["exact"] else "MISS"
        shown = row["predicted"] or "(no read)"
        print(f"  [{mark}] {set_name:<9} {path.name:<32} expected {expected:<11} read {shown:<11} "
              f"conf {row['confidence']:.2f} {row['time_ms']:7.0f} ms")

    by_set = {
        "real": [s for s in samples if s["set"] == "real"],
        "synthetic": [s for s in samples if s["set"] == "synthetic"],
    }
    stats = {name: summarize(rows) for name, rows in by_set.items()}
    stats["combined"] = summarize(samples)

    # Headline: the real set once it is big enough, otherwise the combined
    # number with a flag - never a silent stand-in.
    if stats["real"]["n"] >= REAL_MIN_SAMPLES:
        headline = "real"
        flag = None
    else:
        headline = "combined"
        flag = (f"real sample too small: {stats['real']['n']} real plates "
                f"(< {REAL_MIN_SAMPLES}); headline is the combined set")

    head = stats[headline]
    head_rows = samples if headline == "combined" else by_set[headline]
    accuracy = head["plate_accuracy"] or 0.0
    target_met_point = accuracy >= TARGET
    target_met_proven = (head["ci_low"] or 0.0) >= TARGET

    # Decision matrix over every scored sample, and the two distribution curves
    # over the headline set (same rows as the confusion matrix and histogram).
    hotlist = in_memory_hotlist([s["expected"] for s in samples])
    decision = build_decision(samples, hotlist)
    distributions = build_distributions(head_rows)

    file_list_hash = hashlib.sha256(
        "\n".join(sorted(f"{s}:{p.name}" for s, p, _ in kept)).encode("utf-8")
    ).hexdigest()

    report = {
        "status": "ok",
        "target": {
            "value": TARGET,
            "met_point": target_met_point,
            "met_proven": target_met_proven,
            "headline_set": headline,
            "flag": flag,
            "accuracy": head["plate_accuracy"],
            "ci_low": head["ci_low"],
            "ci_high": head["ci_high"],
        },
        "kpis": {
            **{k: v for k, v in head.items() if k not in {"n", "exact_matches", "ci_low", "ci_high"}},
        },
        "sets": [
            {"name": name, **{k: stats[name][k] for k in (
                "n", "exact_matches", "plate_accuracy", "ci_low", "ci_high",
                "character_accuracy", "cer")}}
            for name in ("real", "synthetic", "combined")
        ],
        "confusion": confusion_from_pairs([s["pairs"] for s in head_rows]),
        "confidence_histogram": confidence_histogram(
            [s["confidence"] for s in head_rows], [s["exact"] for s in head_rows]
        ),
        "decision": decision,
        "distributions": distributions,
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "commit": git_commit(),
            "dataset": {
                "real_files": stats["real"]["n"],
                "synthetic_files": stats["synthetic"]["n"],
                "skipped": skipped,
                "training_manifest_excluded": excluded_count,
                "file_list_hash": file_list_hash,
                "char_dataset_note": CHAR_DATASET_NOTE,
            },
            "char_verifier": bool(settings.CHAR_VERIFY),
            "models": model_files(),
            "versions": package_versions(),
            # The live read path (ai.pipeline.DetectedPlate) does not expose
            # per-character confidences; recorded as unavailable rather than faked.
            "char_confidences_available": False,
            "notes": [
                f"confidence histogram / confusion / KPIs are computed on the headline set ({headline})",
                f"wrong-confident threshold: confidence >= {CONFIDENT_THRESHOLD}",
                decision["method"],
                "distributions: Gaussian KDE (Silverman bandwidth) on "
                f"{KDE_POINTS} grid points; bootstrap {BOOTSTRAP_ITERATIONS} resamples, seed {DECISION_SEED}",
            ],
        },
    }

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(REPORT_PATH, report)

    tmp_csv = SAMPLES_PATH.with_suffix(".csv.tmp")
    with tmp_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "set", "file", "expected", "predicted", "exact", "confidence", "valid",
            "uncertain", "verdict", "stage", "time_ms", "char_edits", "char_total",
            "wrong_confident",
        ])
        for s in samples:
            writer.writerow([
                s["set"], s["file"], s["expected"], s["predicted"], int(s["exact"]),
                f"{s['confidence']:.3f}", int(s["valid"]), int(s["uncertain"]),
                s["verdict"], s["stage"], f"{s['time_ms']:.1f}", s["char_edits"],
                s["char_total"], int(s["wrong_confident"]),
            ])
    os.replace(tmp_csv, SAMPLES_PATH)

    def pct(v: Optional[float]) -> str:
        return f"{v * 100:5.1f}%" if v is not None else "    -"

    print()
    header = f"{'set':<10} {'n':>3} {'exact':>5} {'accuracy':>8} {'95% CI':>18} {'char acc':>8} {'CER':>7}"
    print(header)
    print("-" * len(header))
    for name in ("real", "synthetic", "combined"):
        st = stats[name]
        ci = (f"[{st['ci_low'] * 100:.1f}, {st['ci_high'] * 100:.1f}]"
              if st["ci_low"] is not None else "-")
        print(f"{name:<10} {st['n']:>3} {st['exact_matches']:>5} {pct(st['plate_accuracy']):>8} "
              f"{ci:>18} {pct(st['character_accuracy']):>8} {pct(st['cer']):>7}")
    print("-" * len(header))
    print(f"headline: {headline}  (accuracy {pct(head['plate_accuracy']).strip()}, "
          f"95% CI {head['ci_low'] * 100:.1f} to {head['ci_high'] * 100:.1f})")
    if flag:
        print(f"flag: {flag}")
    proven = "PROVEN" if target_met_proven else "not proven"
    print(f"target {TARGET:.0%}: {'met' if target_met_point else 'NOT met'} by point estimate, "
          f"{proven} (Wilson lower bound {head['ci_low'] * 100:.1f}%)")
    print(f"wrong-but-confident (conf >= {CONFIDENT_THRESHOLD}): {head['wrong_confident_count']}")
    print(f"decision (stolen vs not, {decision['n']} samples, hot-list {decision['hotlist_size']}): "
          f"TP {decision['tp']} FP {decision['fp']} FN {decision['fn']} TN {decision['tn']} | "
          f"precision {pct(decision['precision']).strip()} recall {pct(decision['recall']).strip()} "
          f"F1 {pct(decision['f1']).strip()} false-alert {pct(decision['false_alert_rate']).strip()}")
    boot = distributions["bootstrap"]
    if boot["mean"] is not None:
        print(f"bootstrap accuracy mean {pct(boot['mean']).strip()} "
              f"95% [{pct(boot['ci_low']).strip()}, {pct(boot['ci_high']).strip()}] "
              f"over {boot['iterations']} resamples")
    top5 = report["confusion"]["top"][:5]
    if top5:
        print("top confusions: " + ", ".join(
            f"{c['true']}->{c['pred']} x{c['count']}" for c in top5))
    else:
        print("top confusions: none (no character errors in the headline set)")
    print(f"report  -> {REPORT_PATH.relative_to(ROOT)}")
    print(f"samples -> {SAMPLES_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

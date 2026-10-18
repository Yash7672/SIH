"""Pure metric maths + report schema for the offline OCR evaluation.

Consumed by ``scripts/eval_ocr.py`` (which writes ``data/eval/ocr_report.json``)
and by ``GET /api/v1/analytics/ocr`` (which only reads that file). Nothing here
touches the database, the models, or any live plate path - it is metric maths
and one cached file read, so it can be unit-tested with hand-computed values.

Standard-library only apart from pydantic (already a FastAPI dependency); the
evaluation script imports it with ``backend/`` on ``sys.path``, exactly like
``scripts/benchmark_ocr.py`` imports ``app.services.plate``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict

try:  # rapidfuzz is installed; fall back to the DP below if it ever is not.
    from rapidfuzz.distance import Levenshtein as _RfLevenshtein
except Exception:  # pragma: no cover - exercised only without rapidfuzz
    _RfLevenshtein = None

# The 36 plate characters plus the "missing or extra" bucket. Rows are true
# characters, columns are what the model read.
PLATE_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
MISSING = "\u2205"  # ∅ - a deletion (true char not read) or an insertion
LABELS: List[str] = list(PLATE_CHARS) + [MISSING]
LABEL_INDEX: Dict[str, int] = {c: i for i, c in enumerate(LABELS)}

# Confidence at or above which a wrong read is "dangerous": the live path acts
# on reads at this level, so a wrong one here is an operational risk.
CONFIDENT_THRESHOLD = 0.8
# Wilson interval z for a 95% interval.
WILSON_Z = 1.96


def normalize_label(ch: str) -> str:
    """Map any character to a matrix label; unknown glyphs fold into ∅."""
    up = ch.upper()
    return up if up in LABEL_INDEX else MISSING


def levenshtein_distance(a: str, b: str) -> int:
    if _RfLevenshtein is not None:
        return int(_RfLevenshtein.distance(a, b))
    return _dp_distance(a, b)


def _dp_distance(a: str, b: str) -> int:
    m, n = len(a), len(b)
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        cur = [i] + [0] * n
        ai = a[i - 1]
        for j in range(1, n + 1):
            cost = 0 if ai == b[j - 1] else 1
            cur[j] = min(prev[j - 1] + cost, prev[j] + 1, cur[j - 1] + 1)
        prev = cur
    return prev[n]


def align_chars(true: str, pred: str) -> List[Tuple[str, str]]:
    """Levenshtein alignment as (true char, predicted char) pairs.

    A substitution is (T, P); a deletion - a character on the plate the model
    missed - is (T, ∅); an insertion - a character the model hallucinated - is
    (∅, P). On ties the backtrace prefers the diagonal, so the alignment is
    deterministic. Misaligned pairs (substitutions + deletions + insertions)
    number exactly levenshtein_distance(true, pred).
    """
    m, n = len(true), len(pred)
    d = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        d[i][0] = i
    for j in range(1, n + 1):
        d[0][j] = j
    for i in range(1, m + 1):
        ti = true[i - 1]
        row, prev_row = d[i], d[i - 1]
        for j in range(1, n + 1):
            cost = 0 if ti == pred[j - 1] else 1
            row[j] = min(prev_row[j - 1] + cost, prev_row[j] + 1, row[j - 1] + 1)

    pairs: List[Tuple[str, str]] = []
    i, j = m, n
    while i > 0 or j > 0:
        if i > 0 and j > 0 and d[i][j] == d[i - 1][j - 1] + (0 if true[i - 1] == pred[j - 1] else 1):
            pairs.append((true[i - 1], pred[j - 1]))
            i -= 1
            j -= 1
        elif i > 0 and d[i][j] == d[i - 1][j] + 1:
            pairs.append((true[i - 1], MISSING))
            i -= 1
        else:
            pairs.append((MISSING, pred[j - 1]))
            j -= 1
    pairs.reverse()
    return pairs


def wilson_interval(successes: int, total: int, z: float = WILSON_Z) -> Tuple[float, float]:
    """Wilson score 95% confidence interval for a binomial proportion."""
    if total <= 0:
        return (0.0, 1.0)
    z2 = z * z
    denom = 1.0 + z2 / total
    p = successes / total
    center = (p + z2 / (2 * total)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total))
    return (max(0.0, center - margin), min(1.0, center + margin))


def percentile(values: List[float], q: float) -> Optional[float]:
    """Nearest-rank percentile (q in 0..1); None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def character_totals(pairs_per_sample: List[List[Tuple[str, str]]]) -> Dict[str, float]:
    """Micro-averaged CER and character accuracy over aligned samples."""
    total_chars = 0
    total_edits = 0
    for pairs in pairs_per_sample:
        total_chars += sum(1 for t, _ in pairs if t != MISSING)
        # Each misaligned pair (substitution / deletion / insertion) is one
        # edit; an aligned equal pair is free.
        total_edits += sum(1 for t, p in pairs if t != p)
    cer = (total_edits / total_chars) if total_chars else 0.0
    return {
        "total_true_chars": total_chars,
        "total_edits": total_edits,
        "cer": cer,
        "character_accuracy": max(0.0, 1.0 - cer),
    }


def confusion_from_pairs(pairs_per_sample: List[List[Tuple[str, str]]]) -> Dict:
    """Character confusion matrix + per-character stats + top confusions."""
    size = len(LABELS)
    counts = [[0] * size for _ in range(size)]
    for pairs in pairs_per_sample:
        for t, p in pairs:
            counts[LABEL_INDEX[normalize_label(t)]][LABEL_INDEX[normalize_label(p)]] += 1

    row_totals = [sum(row) for row in counts]
    col_totals = [sum(counts[i][j] for i in range(size)) for j in range(size)]

    normalized = [
        [(counts[i][j] / row_totals[i] if row_totals[i] else 0.0) for j in range(size)]
        for i in range(size)
    ]

    per_char: List[Dict] = []
    for label in PLATE_CHARS:
        idx = LABEL_INDEX[label]
        tp = counts[idx][idx]
        support = row_totals[idx]
        precision = tp / col_totals[idx] if col_totals[idx] else 0.0
        recall = tp / support if support else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        per_char.append(
            {
                "char": label,
                "precision": precision,
                "recall": recall,
                "f1": f1,
                "support": support,
            }
        )

    top: List[Dict] = []
    for i in range(size):
        row_errors = row_totals[i] - counts[i][i]
        if row_errors <= 0:
            continue
        for j in range(size):
            if i == j or counts[i][j] == 0:
                continue
            top.append(
                {
                    "true": LABELS[i],
                    "pred": LABELS[j],
                    "count": counts[i][j],
                    "share": counts[i][j] / row_errors,
                }
            )
    top.sort(key=lambda item: (-item["count"], item["true"], item["pred"]))

    return {
        "labels": list(LABELS),
        "counts": counts,
        "normalized": normalized,
        "per_char": per_char,
        "top": top[:15],
    }


def confidence_histogram(
    confidences: List[float], correct: List[bool], bins: int = 10
) -> Dict[str, List]:
    """Histogram of read confidence split by correct vs wrong (incl. no-reads)."""
    edges = [i / bins for i in range(bins + 1)]
    labels = [f"{edges[i]:.1f}-{edges[i + 1]:.1f}" for i in range(bins)]
    correct_counts = [0] * bins
    wrong_counts = [0] * bins
    for conf, ok in zip(confidences, correct):
        idx = min(int(conf * bins), bins - 1)  # conf == 1.0 lands in the last bin
        idx = max(idx, 0)
        if ok:
            correct_counts[idx] += 1
        else:
            wrong_counts[idx] += 1
    return {"bins": labels, "correct": correct_counts, "wrong": wrong_counts}


# --------------------------------------------------------------------------------------
# Report schema - one shape shared by the writer (scripts/eval_ocr.py), the endpoint and
# the dashboard. Extra fields are tolerated so an older backend can serve a newer report.
# --------------------------------------------------------------------------------------


class SkippedFile(BaseModel):
    model_config = ConfigDict(extra="allow")

    file: str
    reason: str


class DatasetInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    real_files: int = 0
    synthetic_files: int = 0
    skipped: List[SkippedFile] = []
    training_manifest_excluded: int = 0
    file_list_hash: str = ""
    char_dataset_note: str = ""


class ModelFile(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    path: str
    size_bytes: int


class PackageVersions(BaseModel):
    model_config = ConfigDict(extra="allow")

    python: str = ""
    ultralytics: str = ""
    onnxruntime: str = ""
    rapidocr: str = ""
    rapidfuzz: str = ""


class Metadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    generated_at: str = ""
    commit: str = ""
    dataset: DatasetInfo = DatasetInfo()
    char_verifier: bool = False
    models: List[ModelFile] = []
    versions: PackageVersions = PackageVersions()
    char_confidences_available: bool = False
    notes: List[str] = []


class SetMetrics(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    n: int = 0
    exact_matches: int = 0
    plate_accuracy: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    character_accuracy: Optional[float] = None
    cer: Optional[float] = None


class Kpis(BaseModel):
    model_config = ConfigDict(extra="allow")

    plate_accuracy: Optional[float] = None
    error_rate: Optional[float] = None
    character_accuracy: Optional[float] = None
    cer: Optional[float] = None
    no_read_rate: float = 0.0
    invalid_format_rate: float = 0.0
    wrong_confident_rate: float = 0.0
    wrong_confident_count: int = 0
    right_but_uncertain_count: int = 0
    latency_p50_ms: Optional[float] = None
    latency_p95_ms: Optional[float] = None
    accuracy_at_conf_08: Optional[float] = None
    coverage_at_conf_08: float = 0.0


class TargetBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    value: float = 0.90
    met_point: bool = False
    met_proven: bool = False
    headline_set: str = "combined"
    flag: Optional[str] = None
    accuracy: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None


class CharStat(BaseModel):
    model_config = ConfigDict(extra="allow")

    char: str
    precision: float
    recall: float
    f1: float
    support: int


class TopConfusion(BaseModel):
    model_config = ConfigDict(extra="allow")

    true: str
    pred: str
    count: int
    share: float


class Confusion(BaseModel):
    model_config = ConfigDict(extra="allow")

    labels: List[str] = []
    counts: List[List[int]] = []
    normalized: List[List[float]] = []
    per_char: List[CharStat] = []
    top: List[TopConfusion] = []


class Histogram(BaseModel):
    model_config = ConfigDict(extra="allow")

    bins: List[str] = []
    correct: List[int] = []
    wrong: List[int] = []


class OcrReport(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str = "ok"
    target: TargetBlock = TargetBlock()
    kpis: Kpis = Kpis()
    sets: List[SetMetrics] = []
    confusion: Confusion = Confusion()
    confidence_histogram: Histogram = Histogram()
    metadata: Metadata = Metadata()


class OcrNoReport(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str = "no_report"
    hint: str


_REPORT_CACHE: Dict[int, OcrReport] = {}


def load_report(path: Path) -> Optional[OcrReport]:
    """The latest report from ``path``, cached by the file's mtime.

    Returns None when the file does not exist (the endpoint answers
    ``no_report``). The evaluation itself is NEVER run from here - a request
    must not load YOLO/OCR models; the file is produced by
    ``python scripts/eval_ocr.py``.
    """
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    cached = _REPORT_CACHE.get(mtime)
    if cached is not None:
        return cached
    data = json.loads(path.read_text(encoding="utf-8"))
    report = OcrReport.model_validate(data)
    if len(_REPORT_CACHE) > 8:
        _REPORT_CACHE.clear()
    _REPORT_CACHE[mtime] = report
    return report

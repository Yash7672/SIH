#!/usr/bin/env python3
"""Train the 36-class glyph classifier, calibrate it, and export it to ONNX.

What this produces
------------------
``models/char_cls.onnx``        the model ``ai.char_verifier`` loads at runtime
``models/char_cls.calib.json``  the temperature fitted on the validation split,
                                so the ONNX path is as calibrated as the torch
                                model that was measured
``models/char_cls.pt``          the raw weights, for re-export
``data/debug_out/char_metrics.json``  accuracy / ECE / per-class / worst
                                confusions, one section per split
``data/debug_out/confusion_<split>.png|.csv``  the confusion matrix, image and
                                machine-readable, for every reported split

The parts that make the numbers mean something
----------------------------------------------
* **One preprocessing function.** Every crop is loaded through
  ``ai.char_verifier.preprocess_char`` - the same function the live path uses.
  Training on one resize and inferring on another is the classic way to ship a
  model that works in the notebook and fails in production.
* **Documented, fixed seeds.** ``SEED`` below seeds Python, NumPy and torch
  (including the CPU kernels' determinism flags). Two runs on the same data
  give the same weights, so a number quoted in the report can be reproduced.
* **Early stopping on validation loss**, not on training loss and not on the
  test split - the test split is only ever read once, at the end.
* **Temperature scaling** fitted on validation logits. Averaging probabilities
  across frames (which ``plate_verdict`` does) only means anything if those
  probabilities are honest, and a raw softmax at 36 classes is not.
* **Reporting on four splits separately**: synthetic ``test``, ``font_held``
  (typefaces never seen in training) and ``real_test`` (crops cut from actual
  plate photographs). Reporting one blended number would let the easy synthetic
  half hide a real half that does not work.

Usage
-----
    python scripts/train_char_classifier.py
    python scripts/train_char_classifier.py --quick      # smoke test
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai.char_verifier import CHARS, preprocess_char  # noqa: E402

MODEL_DIR = ROOT / "models"
DATA_DIR = ROOT / "data" / "char_dataset"
DEBUG_DIR = ROOT / "data" / "debug_out"
CHAR_TO_IDX = {c: i for i, c in enumerate(CHARS)}

# Fixed seeds. Documented in the module docstring because "we got 97.1%" is a
# claim someone has to be able to reproduce.
SEED = 42

# Splits that are reported. `real_train` is training data and `train`/`val` are
# accounted for during training, so they are not re-reported as held-out rows.
REPORT_SPLITS = ("val", "test", "font_held", "real_test")
ECE_BINS = 15


def set_seed(seed: int = SEED) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(False)  # CNN ops here are deterministic anyway
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


class Net(nn.Module):
    """A small CNN.32x32x1 in, 36 logits out - small enough to run on the OCR
    worker next to the detector without moving the frame budget."""

    def __init__(self) -> None:
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(1, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(), nn.MaxPool2d(2),
        )
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(128 * 4 * 4, 256), nn.ReLU(), nn.Dropout(0.2),
            nn.Linear(256, len(CHARS)),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.classifier(self.features(x))


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def load_split(split: str) -> Optional[Tuple[torch.Tensor, torch.Tensor]]:
    """All crops of one split, already through ``preprocess_char``.

    Returns ``None`` when the split does not exist or is empty, so a machine
    that has only run the generator partially still trains on what it has.
    """
    root = DATA_DIR / split
    if not root.is_dir():
        return None
    xs: List[np.ndarray] = []
    ys: List[int] = []
    for cls in CHARS:
        for path in sorted((root / cls).glob("*.png")):
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if img is None or img.size == 0:
                continue
            xs.append(preprocess_char(img))
            ys.append(CHAR_TO_IDX[cls])
    if not xs:
        return None
    x = torch.from_numpy(np.concatenate(xs, axis=0)).float()
    y = torch.tensor(ys, dtype=torch.long)
    return x, y


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def ece(logits: np.ndarray, labels: np.ndarray, n_bins: int = ECE_BINS) -> float:
    """Expected Calibration Error: the gap between confidence and accuracy,
    bucketed. The number that says "the model says 0.9 and is right 90% of the
    time" - or, when it is large, that it is not."""
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


def softmax(logits: np.ndarray, temperature: float = 1.0) -> np.ndarray:
    z = logits.astype(np.float64) / max(temperature, 1e-4)
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def fit_temperature(logits: np.ndarray, labels: np.ndarray) -> float:
    """Scalar temperature minimising NLL on validation logits.

    A coarse grid then a fine grid around the winner - deliberately not a
    gradient method: the objective in one dimension is smooth and unimodal
    enough, and a grid cannot diverge on a small validation set.
    """
    if logits.size == 0:
        return 1.0

    def nll(t: float) -> float:
        p = softmax(logits, t)
        return float(-np.log(np.clip(p[np.arange(len(labels)), labels], 1e-12, 1.0)).mean())

    best_t, best = 1.0, nll(1.0)
    for t in np.exp(np.linspace(-3.0, 3.0, 61)):
        v = nll(float(t))
        if v < best:
            best_t, best = float(t), v
    lo, hi = best_t / 1.6, best_t * 1.6
    for t in np.exp(np.linspace(np.log(lo), np.log(hi), 61)):
        v = nll(float(t))
        if v < best:
            best_t, best = float(t), v
    return float(best_t)


def confusion(logits: np.ndarray, labels: np.ndarray) -> np.ndarray:
    n = len(CHARS)
    cm = np.zeros((n, n), dtype=np.int64)
    if logits.size:
        pred = logits.argmax(axis=1)
        for t, p in zip(labels, pred):
            cm[int(t), int(p)] += 1
    return cm


def write_confusion(cm: np.ndarray, split: str) -> None:
    """Confusion matrix as CSV (machine-readable) and PNG (human-readable)."""
    import csv

    csv_path = DEBUG_DIR / f"confusion_{split}.csv"
    with csv_path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["true\\pred"] + list(CHARS))
        for i, ch in enumerate(CHARS):
            writer.writerow([ch] + [int(v) for v in cm[i]])
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(9, 8))
        show = np.log1p(cm)  # log scale, or the head of the matrix is all you see
        im = ax.imshow(show, cmap="Blues")
        ax.set_xticks(range(len(CHARS)), list(CHARS), fontsize=7)
        ax.set_yticks(range(len(CHARS)), list(CHARS), fontsize=7)
        ax.set_xlabel("predicted")
        ax.set_ylabel("true")
        ax.set_title(f"char classifier confusion - {split} (log1p counts)")
        fig.colorbar(im, ax=ax, fraction=0.046)
        fig.tight_layout()
        fig.savefig(DEBUG_DIR / f"confusion_{split}.png", dpi=110)
        plt.close(fig)
    except Exception as exc:  # noqa: BLE001 - a missing matplotlib loses a picture, not a model
        print(f"  (confusion image skipped: {exc})")


def per_class_accuracy(cm: np.ndarray) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for i, ch in enumerate(CHARS):
        row = cm[i].sum()
        out[ch] = float(cm[i, i] / row) if row else 0.0
    return out


def worst_confusions(cm: np.ndarray, k: int = 10) -> List[dict]:
    pairs: List[dict] = []
    for i in range(len(CHARS)):
        for j in range(len(CHARS)):
            if i == j or cm[i, j] == 0:
                continue
            pairs.append({"true": CHARS[i], "pred": CHARS[j], "count": int(cm[i, j])})
    pairs.sort(key=lambda d: -d["count"])
    return pairs[:k]


# --------------------------------------------------------------------------- #
# Training
# --------------------------------------------------------------------------- #
@torch.no_grad()
def collect_logits(net: nn.Module, loader: DataLoader) -> Tuple[np.ndarray, np.ndarray]:
    net.eval()
    logits: List[np.ndarray] = []
    labels: List[np.ndarray] = []
    for x, y in loader:
        logits.append(net(x).numpy())
        labels.append(y.numpy())
    if not logits:
        return np.zeros((0, len(CHARS))), np.zeros(0, dtype=np.int64)
    return np.concatenate(logits), np.concatenate(labels)


def train(
    net: nn.Module,
    tr: DataLoader,
    va: DataLoader,
    epochs: int,
    patience: int,
    device: torch.device,
) -> Dict[str, object]:
    opt = optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-4)
    sch = optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    crit = nn.CrossEntropyLoss(label_smoothing=0.05)
    best_val = float("inf")
    best_state = {k: v.detach().clone() for k, v in net.state_dict().items()}
    bad = 0
    history: List[dict] = []
    for ep in range(epochs):
        net.train()
        running = 0.0
        seen = 0
        for x, y in tr:
            x, y = x.to(device), y.to(device)
            opt.zero_grad()
            loss = crit(net(x), y)
            loss.backward()
            opt.step()
            running += float(loss.item()) * x.size(0)
            seen += x.size(0)
        sch.step()
        val_logits, val_labels = collect_logits(net, va)
        val_loss = float(
            -np.log(np.clip(softmax(val_logits)[np.arange(len(val_labels)), val_labels], 1e-12, 1.0)).mean()
        ) if len(val_labels) else 0.0
        val_acc = float((val_logits.argmax(1) == val_labels).mean()) if len(val_labels) else 0.0
        history.append(
            {
                "epoch": ep + 1,
                "train_loss": round(running / max(1, seen), 4),
                "val_loss": round(val_loss, 4),
                "val_acc": round(val_acc, 4),
            }
        )
        print(f"  {ep + 1:2d} train={running / max(1, seen):.4f} val_loss={val_loss:.4f} val_acc={val_acc:.4f}")
        if val_loss < best_val - 1e-4:
            best_val = val_loss
            best_state = {k: v.detach().clone() for k, v in net.state_dict().items()}
            bad = 0
        else:
            bad += 1
            if bad >= patience:
                print(f"  early stop at epoch {ep + 1} (no val improvement for {patience})")
                break
    net.load_state_dict(best_state)
    return {"history": history, "best_val_loss": round(best_val, 4), "stopped_early": bad >= patience}


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def export_onnx(net: nn.Module, sample: torch.Tensor, path: Path) -> None:
    """Export for onnxruntime.

    ``dynamo=False`` selects the legacy TorchScript exporter, which is what the
    installed onnx/onnxscript pair actually supports here, and opset 12 is new
    enough for everything this graph uses and old enough for every onnxruntime
    the deployment might have.

    The batch axis is dynamic because the live path feeds a whole plate's glyphs
    in one call (8-10 rows), while the warmup and the parity check feed 1 and
    256. A static batch-1 graph loads fine and then rejects every real request
    with ``Got invalid dimensions for input ... Got: 256 Expected: 1``.
    """
    torch.onnx.export(
        net.cpu().eval(),
        sample,
        str(path),
        export_params=True,
        opset_version=12,
        do_constant_folding=True,
        input_names=["input"],
        output_names=["logits"],
        dynamic_axes={"input": {0: "batch"}, "logits": {0: "batch"}},
        dynamo=False,
    )


def onnx_parity(net: nn.Module, x: torch.Tensor, path: Path, n: int = 256, tol: float = 1e-3) -> dict:
    """Max |torch - onnxruntime| over logits and probabilities.

    Export bugs are silent by nature: the file loads, inference runs, and the
    numbers are subtly different. This is the check that says they are not.
    """
    try:
        import onnxruntime as ort
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why": f"onnxruntime unavailable: {exc}"}
    try:
        sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        name = sess.get_inputs()[0].name
        batch = x[: min(n, len(x))].numpy()
        ort_logits = sess.run(None, {name: batch})[0]
        with torch.no_grad():
            torch_logits = net(x[: len(batch)]).numpy()
        d_logit = float(np.abs(ort_logits - torch_logits).max())
        d_prob = float(np.abs(softmax(ort_logits) - softmax(torch_logits)).max())
        return {
            "ok": d_logit <= tol and d_prob <= tol,
            "max_abs_logit_diff": round(d_logit, 6),
            "max_abs_prob_diff": round(d_prob, 6),
            "tolerance": tol,
            "samples": int(len(batch)),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why": str(exc)}


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--patience", type=int, default=6)
    ap.add_argument("--batch-size", type=int, default=128)
    ap.add_argument("--quick", action="store_true", help="3 epochs, for wiring checks only")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument(
        "--export-only",
        action="store_true",
        help="re-export models/char_cls.onnx from the saved weights, without retraining",
    )
    args = ap.parse_args()
    if args.quick:
        args.epochs, args.patience = 3, 3

    set_seed(args.seed)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device={device} seed={args.seed} epochs={args.epochs} patience={args.patience}")

    # ---- data ------------------------------------------------------------- #
    splits: Dict[str, Tuple[torch.Tensor, torch.Tensor]] = {}
    for name in ("train", "val", "test", "font_held", "real_train", "real_test"):
        got = load_split(name)
        if got is not None:
            splits[name] = got
    for name, (x, _y) in splits.items():
        print(f"  {name}: {len(x)}")
    if "train" not in splits:
        raise SystemExit("no training data - run `python scripts/gen_char_data.py` first")
    if "val" not in splits:
        # No validation split means no early stopping and no calibration set,
        # which would quietly make every calibration number below meaningless.
        raise SystemExit("no validation split - run `python scripts/gen_char_data.py` first")

    if args.export_only:
        # Re-export only: the weights from the last run are still on disk, and a
        # fixed export graph should not cost another six minutes of training.
        weights = MODEL_DIR / "char_cls.pt"
        if not weights.exists():
            raise SystemExit(f"no weights at {weights} - train first")
        net = Net().cpu().eval()
        net.load_state_dict(torch.load(weights, map_location="cpu"))
        x, _y = splits["train"]
        onnx_path = MODEL_DIR / "char_cls.onnx"
        export_onnx(net, x[:1], onnx_path)
        parity = onnx_parity(net, x, onnx_path)
        metrics = DEBUG_DIR / "char_metrics.json"
        if metrics.exists():
            report = json.loads(metrics.read_text())
            report["onnx_parity"] = parity
            metrics.write_text(json.dumps(report, indent=2))
        print(f"re-exported {onnx_path}  parity={parity}")
        if not parity.get("ok", False):
            raise SystemExit("ONNX parity check did not pass")
        return

    # real_train is tiny and precious: it is the only signal from photographs,
    # so it is mixed into training rather than left to rot in a split.
    xtr, ytr = splits["train"]
    if "real_train" in splits:
        xr, yr = splits["real_train"]
        xtr = torch.cat([xtr, xr], 0)
        ytr = torch.cat([ytr, yr], 0)
    order = torch.randperm(len(xtr))
    xtr, ytr = xtr[order], ytr[order]

    xva, yva = splits["val"]
    g = torch.Generator().manual_seed(args.seed)
    dl_tr = DataLoader(TensorDataset(xtr, ytr), batch_size=args.batch_size, shuffle=True, generator=g)
    dl_va = DataLoader(TensorDataset(xva, yva), batch_size=256)

    # ---- train ------------------------------------------------------------ #
    net = Net().to(device)
    t0 = time.perf_counter()
    train_report = train(net, dl_tr, dl_va, args.epochs, args.patience, device)
    print(f"trained in {time.perf_counter() - t0:.1f}s")
    torch.save(net.state_dict(), MODEL_DIR / "char_cls.pt")

    # ---- calibration ------------------------------------------------------ #
    val_logits, val_labels = collect_logits(net, dl_va)
    temperature = fit_temperature(val_logits, val_labels)
    print(f"temperature fitted on val: T={temperature:.4f}")

    # ---- report ----------------------------------------------------------- #
    report: dict = {
        "seed": args.seed,
        "epochs": args.epochs,
        "patience": args.patience,
        "batch_size": args.batch_size,
        "device": str(device),
        "train": {k: v for k, v in train_report.items() if k != "history"},
        "history": train_report["history"],
        "temperature": round(temperature, 4),
        "splits": {},
        "ece_bins": ECE_BINS,
    }

    def evaluate_split(name: str) -> None:
        if name not in splits:
            report["splits"][name] = {"present": False}
            print(f"{name}: absent")
            return
        x, y = splits[name]
        loader = DataLoader(TensorDataset(x, y), batch_size=256)
        logits, labels = collect_logits(net, loader)
        if not len(labels):
            report["splits"][name] = {"present": False, "count": 0}
            return
        acc = float((logits.argmax(1) == labels).mean())
        e_before = ece(logits, labels)
        e_after = ece(logits / temperature, labels)
        cm = confusion(logits, labels)
        write_confusion(cm, name)
        pca = per_class_accuracy(cm)
        report["splits"][name] = {
            "present": True,
            "count": int(len(labels)),
            "accuracy": round(acc, 4),
            "ece_before_calibration": round(e_before, 4),
            "ece_after_calibration": round(e_after, 4),
            "per_class_accuracy": {k: round(v, 4) for k, v in pca.items()},
            "worst_per_class": sorted(pca, key=lambda k: pca[k])[:5],
            "worst_confusions": worst_confusions(cm),
        }
        print(
            f"{name}: acc={acc:.4f} ece {e_before:.4f} -> {e_after:.4f}  "
            f"(worst classes: {', '.join(sorted(pca, key=lambda k: pca[k])[:5])})"
        )

    for split_name in REPORT_SPLITS:
        evaluate_split(split_name)
    # The confusion matrix for the training distribution is not a quality claim,
    # but it is what tells you the model learned the classes at all.
    evaluate_split("train")

    # ---- export ----------------------------------------------------------- #
    onnx_path = MODEL_DIR / "char_cls.onnx"
    export_onnx(net, xtr[:1], onnx_path)
    parity = onnx_parity(net, xtr, onnx_path)
    report["onnx_parity"] = parity
    print(f"exported {onnx_path}  parity={parity}")

    calib = {
        "temperature": round(float(temperature), 4),
        "fitted_on": "val",
        "ece_before": report["splits"].get("val", {}).get("ece_before_calibration"),
        "seed": args.seed,
    }
    (MODEL_DIR / "char_cls.calib.json").write_text(json.dumps(calib, indent=2))
    print(f"wrote {MODEL_DIR / 'char_cls.calib.json'}")

    out = DEBUG_DIR / "char_metrics.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"wrote {out}")

    if not parity.get("ok", False):
        print("WARNING: ONNX parity check did not pass - see report above")


if __name__ == "__main__":
    main()

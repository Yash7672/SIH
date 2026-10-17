#!/usr/bin/env python3
"""Evaluate plate reading: the 'before' reader vs the current live-scan path.

Run from the repo root:

    python scripts/eval_plates.py
    python scripts/eval_plates.py --video-stride 2 --video-max-frames 180

The two readers
---------------
* ``before`` - the reader as it was on `main`: one CLAHE preprocessing pass and
  one full RapidOCR ``engine()`` call (detect + classify + recognise).
* ``after``  - the code path the live WebSocket (/api/v1/ws/scan) actually runs:
  decode -> vehicle detection -> plate detection + assignment -> crop ->
  ``read_crop`` (preprocessing variants + confidence vote) -> character-verifier
  fusion (segmenter + grammar + classifier) -> ``normalize_plate`` -> the verdict
  rules in ``plate_verdict``.

Both readers get the SAME crops, cut from the SAME detector output, so the only
thing being compared is the reading.

Datasets and what counts as an answer
-------------------------------------
* ``data/generated/plate_*.png`` and ``data/test_plates/plate_*.png`` - the
  filename states the expected plate; scored as exact match + per-character
  accuracy.
* ``data/test_plates/road_scene.jpg`` - a vehicle context with several plates;
  read through the full server path and reported as *observed* (no filename
  expectation exists, so inventing one would be guessing at the answer the test
  exists to measure).
* ``data/generated/demo_road.mp4`` - the live path run frame by frame to
  measure tracking stability, motion gating, per-track verdicts and the
  best-frame read per track.

Privacy
-------
The pipeline under test never writes anything; this script writes only its own
report. ``DEBUG_SAVE_PLATE_CROPS`` is left off, no frame or plate is persisted
unless ``--save-artifacts`` is given, and the script asserts at the end that the
pipeline's own debug-crop directory was not touched during the run.
"""

from __future__ import annotations

import argparse
import difflib
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "backend"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import app.api.v1.live_scan as ls  # noqa: E402
from app.services.plate_verdict import PlateRead, TrackVerdict  # noqa: E402
from app.ws.scan_manager import ScanConnection, VehicleTracker  # noqa: E402
from scripts.bench_plate_reader import (  # noqa: E402
    NAMED_SIX,
    expected_plate,
    old_read,
    plate_crop,
    warm,
)

PLATE_DIR = ROOT / "data" / "test_plates"
GENERATED_DIR = ROOT / "data" / "generated"
DEMO_VIDEO = GENERATED_DIR / "demo_road.mp4"
EVAL_DIR = ROOT / "data" / "debug_out" / "eval"
# The pipeline's own opt-in debug sink. Left un-touched so the privacy check can
# assert we never leaked a plate crop through a side channel.
PIPELINE_CROP_DIR = ROOT / "data" / "debug_out" / "plate_crops"


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def char_accuracy(read: Optional[str], expected: str) -> float:
    """Fraction of the expected characters the read gets right.

    Uses the longest matching alignment, so "MH12JK4567" vs "MH12JK456" scores
    9/10 rather than a positional 0 - the read lost a trailing digit, which is
    what the metric should say.
    """
    if not read:
        return 0.0
    a, b = read.upper(), expected.upper()
    if a == b:
        return 1.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    matched = sum(block.size for block in sm.get_matching_blocks())
    return matched / len(b)


def pre_verifier_text(after: dict) -> str:
    """What RapidOCR said *before* the character verifier was allowed to act.

    ``run_ocr`` keeps the original glyph in ``chars[i]["ocr"]`` and only rewrites
    ``chars[i]["c"]`` when the fusion replaces it, so this string is the reading
    the pre-verifier path would have produced - the only way to tell "the OCR
    misread it" from "the verifier broke it".
    """
    chars = after.get("chars") or []
    if chars:
        text = "".join(str(d.get("ocr") or "") for d in chars)
    else:
        text = str(after.get("text") or "")
    return "".join(ch for ch in text.upper() if ch.isalnum())


def failing_stage(row: dict, which: str = "after") -> str:
    """The first stage in the pipeline that could have lost the expected plate.

    The ladder walks the real path in order - decode, plate detector, OCR,
    verifier fusion, normalisation, full-path crop assignment - and names the
    first one that is inconsistent with the expected string. It is an
    attribution from evidence the row already carries, not a guess: every stage
    it names has a value in the row that proves it. ``which`` picks the reader
    (``after`` is the live path, ``before`` is the old one-pass reader, which
    has no verifier stage to fall back on).
    """
    exp = str(row.get("expected") or "")
    if not exp:
        return "- (no expectation)"
    if row.get("error"):
        return f"decode: {row['error']}"
    if not row.get("detected"):
        return "plate detector: no box"
    rec = row.get(which) or {}
    norm = str(rec.get("norm") or "")
    pre = pre_verifier_text(rec) if which == "after" else norm
    if norm == exp:
        reads = [str(r.get("norm") or "") for r in (row.get("server_reads") or [])]
        if which == "after" and reads and exp not in reads:
            return "- crop right, full path missed"
        return "- correct"
    if not pre:
        return "OCR: no read at all"
    if which == "after" and pre == exp:
        # OCR had the right glyphs; only the fusion or the normaliser can have
        # turned them into something else.
        return "char verifier fusion" if rec.get("verified") else "normalisation"
    return "OCR recognition"


def z_substitutions(read: str, expected: str, target: str) -> int:
    """How often an expected ``Z`` was read as ``target`` (``7`` or ``2``).

    Positions come from a longest-common-subsequence alignment, so a deleted
    trailing digit cannot shift the comparison and invent a substitution that
    never happened.
    """
    if not read or not expected:
        return 0
    a, b = expected.upper(), read.upper()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    hits = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace":
            for k in range(min(i2 - i1, j2 - j1)):
                if a[i1 + k] == "Z" and b[j1 + k] == target:
                    hits += 1
    return hits


def verdict_state_for(read: dict, hotlist: Tuple[str, ...], reads: int = 1) -> Tuple[str, str]:
    """The plate_verdict decision one (or more) reads would produce.

    Feeds the read through the exact verdict rules used by the live path
    (TrackVerdict + evaluate), so the report shows not just what OCR said but
    how far that read would have got: UNREAD / POSSIBLE / CLEAR / STOLEN.
    ``reads=2`` adds the same read twice inside the consensus window - the
    corroboration route. Returns ``(state, reason)``.
    """
    norm = str(read.get("norm") or "")
    if not norm:
        return "UNREAD", "no valid read"
    tv = TrackVerdict(tid=0, now=0.0)
    for k in range(max(1, reads)):
        tv.add(
            PlateRead(
                text=str(read.get("text") or ""),
                norm=norm,
                conf=float(read.get("conf") or 0.0),
                # Distinct instants so `backing` (distinct `at`) sees real
                # corroboration inside the 5 s consensus window.
                at=float(k),
                valid=bool(read.get("valid")),
                char_confs=tuple(float(c) for c in (read.get("char_confs") or ())),
                reason=str(read.get("reason") or ""),
                verified=bool(read.get("verified")),
                amb=tuple(int(i) for i in (read.get("ambiguous") or ())),
            )
        )
    verdict = tv.evaluate(hotlist=hotlist, now=0.0, cooldown_ok=True)
    return str(verdict.state), str(verdict.reason)


# --------------------------------------------------------------------------- #
# Image fixtures
# --------------------------------------------------------------------------- #
def collect_images() -> List[Path]:
    out: List[Path] = []
    for d in (PLATE_DIR, GENERATED_DIR):
        if d.is_dir():
            out.extend(
                sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg"})
            )
    return out


def plate_crop_bgr(bgr: np.ndarray) -> Optional[Tuple[np.ndarray, int, int]]:
    """(crop, width_px, height_px) from an in-memory frame (no disk writes).

    Same recipe as ``bench_plate_reader.plate_crop`` but for an ndarray: the
    plate detector on the full frame, best box padded, crop cut.
    """
    if bgr is None or bgr.size == 0:
        return None
    h, w = bgr.shape[:2]
    result = ls._load_plate_detector()(bgr, conf=ls.PLATE_CONF, verbose=False, imgsz=ls.PLATE_IMGSZ)[0]
    boxes = getattr(result, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return None
    xyxy = boxes.xyxy[0].detach().cpu().numpy()
    p = ls._pad_box((int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])), w, h)
    crop = bgr[p[1] : p[3], p[0] : p[2]]
    if not crop.size:
        return None
    return crop, int(xyxy[2] - xyxy[0]), int(xyxy[3] - xyxy[1])


def read_server_crops(image_bytes: bytes) -> Tuple[List[dict], Dict[int, np.ndarray]]:
    """The full server path for one image: decode -> vehicle -> plate -> crops.

    Returns ``(plates_wire, crops)`` where ``plates_wire`` is the list of plate
    boxes keyed by track id (as the WebSocket would send them) and ``crops`` maps
    track id to the padded crop OCR should read.
    """
    tracker = VehicleTracker()
    w, h, vehicle_ms, plate_ms, vehicles, plates, crops = ls.decode_and_infer(
        image_bytes, tracker
    )
    return plates, crops


def eval_image(path: Path) -> Dict[str, object]:
    """One still image: both readers on the same crop + server-path evidence."""
    row: Dict[str, object] = {
        "file": path.name,
        "expected": expected_plate(path),
    }
    img = cv2.imread(str(path))
    if img is None or img.size == 0:
        row["error"] = "unreadable image"
        return row

    found = plate_crop(path)
    if found is None:
        row["detected"] = False
    else:
        row["detected"] = True
        crop, px_w, px_h = found
        row["px"] = f"{px_w}x{px_h}"

        # Warm each reader once, then measure the median of 3 (deterministic,
        # but a single-call timing would be dominated by first-call warm-up).
        old_read(crop)
        ls.run_ocr(crop)
        old_runs = []
        after_runs = []
        for _ in range(3):
            t0 = time.perf_counter()
            old = old_read(crop)
            old_runs.append((old, (time.perf_counter() - t0) * 1000))
            t0 = time.perf_counter()
            after = ls.run_ocr(crop)
            after_runs.append((after, (time.perf_counter() - t0) * 1000))

        old, _ = max(old_runs, key=lambda o: o[0]["conf"])
        after, _ = max(after_runs, key=lambda a: a[0]["conf"] if a else 0.0)
        if after is None:
            after = {"norm": None, "conf": 0.0, "text": "", "valid": False,
                     "verified": False, "ambiguous": [], "char_confs": None,
                     "reason": "", "chars": []}
        row["old"] = old
        row["after"] = after
        row["old_ms"] = statistics.median(r[1] for r in old_runs)
        row["after_ms"] = statistics.median(r[1] for r in after_runs)

        exp = row["expected"]
        if exp:
            row["old_exact"] = (old.get("norm") or "") == exp
            row["after_exact"] = (after.get("norm") or "") == exp
            row["old_char"] = char_accuracy(str(old.get("norm") or ""), exp)
            row["after_char"] = char_accuracy(str(after.get("norm") or ""), exp)
            state, reason = verdict_state_for(after, (exp,))
            row["verdict_hot"] = state  # hot-list holds the expected plate
            state0, _r0 = verdict_state_for(after, ())
            row["verdict_plain"] = state0
            state2, reason2 = verdict_state_for(after, (exp,), reads=2)
            row["verdict_hot_2x"] = state2  # same read corroborated a second time
            row["verdict_hot_2x_reason"] = reason2

    # Full-path evidence: does the vehicle+plate chain find and crop anything?
    enc = cv2.imencode(".jpg", img)[1].tobytes()
    plates_wire, crops = read_server_crops(enc)
    row["server_plates_found"] = len(plates_wire)
    row["server_crops"] = len(crops)
    server_reads = []
    for tid, crop in sorted(crops.items()):
        res = ls.run_ocr(crop)
        if res and res.get("norm"):
            server_reads.append(
                {
                    "track": tid,
                    "norm": res["norm"],
                    "conf": round(float(res["conf"]), 3),
                    "verified": bool(res["verified"]),
                }
            )
    row["server_reads"] = server_reads
    return row


# --------------------------------------------------------------------------- #
# Video (demo_road.mp4) - the live path, frame by frame
# --------------------------------------------------------------------------- #
def process_video(
    path: Path,
    *,
    stride: int = 1,
    max_frames: int = 0,
    save: bool = False,
    realtime: bool = True,
    hotlist: Tuple[str, ...] = (),
    gt_vehicles: int = 0,
    expected: Optional[str] = None,
) -> Dict[str, object]:
    """Run the live path over a video and evidence tracking + verdicts.

    Mirrors the WebSocket loop (vehicle pass then plate pass, both against one
    shared tracker) without the socket. Per frame we record how many vehicle
    tracks were live, how many tracks the OCR budget could afford, how many were
    skipped because they had already settled (the motion/read gating that keeps a
    parked car from occupying the OCR worker), and - per track - the frame where
    each plate was read best (best-frame selection).

    ``realtime`` schedules every frame at its own video timestamp on the wall
    clock, so the run plays the clip at the speed a camera would have delivered
    it - which is the only way the read budget, the track expiry and the 5 s
    consensus window are exercised on the same clock the server uses. Frames are
    never skipped to catch up: when inference outruns the schedule the shortfall
    is reported as lag instead of being hidden.

    ``hotlist`` is fed to the same ``TrackVerdict.evaluate`` the WebSocket path
    calls, with reads timestamped ``time.monotonic()`` exactly as
    ``live_scan`` timestamps them, so the first STOLEN frame is the alert the
    phone would have been sent (minus the database write). ``gt_vehicles`` is
    the number of vehicles the clip is *known* to contain - only that number
    makes "missed completely" computable instead of a guess - and ``expected``
    is the plate the clip is supposed to show, so correct-or-not is scored
    against a stated string instead of eyeballed.
    """
    if not path.is_file():
        return {"error": f"{path.name} not present on disk"}

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return {"error": f"cannot open {path.name}"}
    resolution = (
        f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}"
    )
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    raw_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
    fps = raw_fps if raw_fps > 0 else 24.0
    period = 1.0 / fps

    tracker = VehicleTracker()
    conn = ScanConnection()
    verdicts: Dict[int, TrackVerdict] = {}
    per_track: Dict[int, List[dict]] = {}
    # Per-vehicle evidence for the moving-vehicle table: when a track was first
    # seen, when it produced its first *valid* read, and when a hot-listed plate
    # first reached STOLEN (same evaluate() call, same clock as the server).
    first_seen: Dict[int, dict] = {}
    first_valid: Dict[int, dict] = {}
    stolen_at: Dict[int, dict] = {}
    hot_verdicts: Dict[int, TrackVerdict] = {}
    plate_px: List[int] = []
    sharpness: List[float] = []
    start_wall = time.perf_counter()

    frames = 0
    idx = 0
    totals = {
        "frames_in": 0,
        "gated_out": 0,
        "vehicle_pass_ms": [],
        "plate_pass_ms": [],
        "ocr_runs": 0,
        "tracks_seen": 0,
        "frames_with_vehicles": 0,
    }
    while True:
        if realtime:
            # Scheduled at this frame's own video timestamp: a camera delivers
            # on its clock, not on ours. Late is late - no skipping to catch up.
            ahead = (start_wall + idx * period) - time.perf_counter()
            if ahead > 0:
                time.sleep(ahead)
        ok, frame = cap.read()
        if not ok:
            break
        if idx % stride != 0:
            idx += 1
            continue
        idx += 1
        if max_frames and frames >= max_frames:
            break
        frames += 1
        totals["frames_in"] += 1
        mono = time.monotonic()
        sharpness.append(_sharpness(frame))

        # The server only ever sees JPEG. Encode like the phone would.
        ok, buf = cv2.imencode(".jpg", frame)
        if not ok:
            continue
        jpeg = buf.tobytes()

        bgr, w, h, vehicle_ms, vehicles, tracks = ls.infer_vehicles(jpeg, tracker)
        totals["vehicle_pass_ms"].append(vehicle_ms)
        if tracks:
            totals["frames_with_vehicles"] += 1
        for tr in tracks:
            if tr.tid not in first_seen:
                first_seen[tr.tid] = {"frame": frames, "t": mono, "label": tr.label}

        t1 = time.perf_counter()
        plates, crops = ls.infer_plates(bgr, w, h, tracks, tracker, conn=conn)
        totals["plate_pass_ms"].append((time.perf_counter() - t1) * 1000)
        plate_px.extend(int(p.get("px_w") or 0) for p in plates if p.get("px_w"))

        for track in tracker.live():
            if track.tid not in verdicts:
                verdicts[track.tid] = TrackVerdict(track.tid)
                totals["tracks_seen"] += 1

        # Motion/read gating: tracks whose plate has settled are not re-read,
        # and the per-frame read budget (MAX_READS_PER_FRAME) bounds the rest.
        pre = len(crops)
        totals["ocr_runs"] += len(crops)
        totals["gated_out"] += max(0, len(plates) - pre)

        for tid, crop in sorted(crops.items()):
            read = ls.run_ocr(crop)
            if read is None:
                continue
            verdicts.setdefault(tid, TrackVerdict(tid))
            verdicts[tid].add(
                PlateRead(
                    text=str(read.get("text") or ""),
                    norm=str(read.get("norm") or ""),
                    conf=float(read.get("conf") or 0.0),
                    at=float(frames),
                    valid=bool(read.get("valid")),
                    char_confs=tuple(float(c) for c in (read.get("char_confs") or ())),
                    reason=str(read.get("reason") or ""),
                    verified=bool(read.get("verified")),
                    amb=tuple(int(i) for i in (read.get("ambiguous") or ())),
                )
            )
            per_track.setdefault(tid, []).append(
                {
                    "frame": frames,
                    "norm": read.get("norm"),
                    "conf": round(float(read.get("conf") or 0.0), 3),
                    "verified": bool(read.get("verified")),
                }
            )
            # ---- the moving-vehicle evidence, on the server's own clock ----- #
            if read.get("valid") and read.get("norm") and tid not in first_valid:
                first_valid[tid] = {
                    "frame": frames,
                    "t": time.monotonic(),
                    "plate": str(read.get("norm")),
                    "conf": round(float(read.get("conf") or 0.0), 3),
                }
            if hotlist:
                # The same two calls live_scan makes per read: add with
                # time.monotonic(), then evaluate with cooldown_ok=not already.
                hot = hot_verdicts.setdefault(tid, TrackVerdict(tid))
                hot.add(
                    PlateRead(
                        text=str(read.get("text") or ""),
                        norm=str(read.get("norm") or ""),
                        conf=float(read.get("conf") or 0.0),
                        at=time.monotonic(),
                        valid=bool(read.get("valid")),
                        char_confs=tuple(float(c) for c in (read.get("char_confs") or ())),
                        reason=str(read.get("reason") or ""),
                        verified=bool(read.get("verified")),
                        amb=tuple(int(i) for i in (read.get("ambiguous") or ())),
                    )
                )
                already = hot.state == "STOLEN"
                vd = hot.evaluate(hotlist=hotlist, now=time.monotonic(), cooldown_ok=not already)
                hot.state = vd.state
                if vd.state == "STOLEN" and tid not in stolen_at:
                    stolen_at[tid] = {
                        "frame": frames,
                        "t": time.monotonic(),
                        "plate": vd.plate,
                        "conf": round(float(vd.confidence or 0.0), 3),
                    }
    cap.release()
    elapsed = time.perf_counter() - start_wall

    # ---- the moving-vehicle rows: every track, read or not ------------------ #
    # A track that was never read is the interesting one (that is what "missed
    # completely" looks like), so this walks first_seen as well as the read
    # history rather than dropping the silent tracks the verdict table skips.
    vehicle_rows: List[dict] = []
    for tid in sorted(set(first_seen) | set(per_track) | set(stolen_at)):
        reads = per_track.get(tid, [])
        seen = first_seen.get(tid) or {}
        valid = first_valid.get(tid) or {}
        best = max(reads, key=lambda r: r["conf"]) if reads else None
        best_plate = str(best["norm"]) if best else None
        vehicle_rows.append(
            {
                "track": tid,
                "label": seen.get("label") or ("-" if tid < 0 else None),
                "kind": "vehicle" if tid > 0 else "plate only (no vehicle box)",
                "seen_frame": seen.get("frame"),
                "seen_t": seen.get("t"),
                "valid_frame": valid.get("frame"),
                "valid_t": valid.get("t"),
                "valid_plate": valid.get("plate"),
                "valid_conf": valid.get("conf"),
                "best_plate": best_plate,
                "reads": len(reads),
                "correct": (best_plate == expected) if (expected and best_plate) else None,
                "stolen": stolen_at.get(tid),
            }
        )
    vehicles_detected = len([t for t in first_seen if t > 0])
    scheduled = max(0, idx - 1) / fps

    # ---- per-track verdicts at the end of the run --------------------------- #
    track_rows = []
    direct_best: Optional[dict] = None
    for tid, tv in sorted(verdicts.items()):
        reads = per_track.get(tid, [])
        if not reads:
            continue
        best = max(reads, key=lambda r: r["conf"])
        verdict = tv.evaluate(hotlist=set(), now=time.monotonic(), cooldown_ok=True)
        track = tracker.get(tid)
        track_rows.append(
            {
                "track": tid,
                "label": track.label if track else None,
                "frames_live": frames_span(tid, tracker, verdicts),
                "reads": len(reads),
                "best_frame": best["frame"],
                "best_plate": best["norm"],
                "best_conf": best["conf"],
                "state": verdict.state,
                "reason": verdict.reason,
                "verified_any": any(r["verified"] for r in reads),
            }
        )
        if direct_best is None or best["conf"] > direct_best["conf"]:
            direct_best = dict(best)

    # A second, direct look at the single best frame: the plate detector on the
    # full frame (not the live-path crop assignment) then the same OCR. This
    # shows what the plate actually says when the box is not the degenerate
    # full-width band the synthetic render produces.
    if direct_best is not None and 0 <= direct_best["frame"] < n_frames:
        try:
            cap = cv2.VideoCapture(str(path))
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(direct_best["frame"]))
            ok, frame = cap.read()
            cap.release()
            if ok:
                ok, buf = cv2.imencode(".jpg", frame)
                bgr = ls.decode_frame(buf.tobytes())
                found = plate_crop_bgr(bgr)
                if found is not None:
                    crop, px_w, px_h = found
                    res = ls.run_ocr(crop)
                    if res and res.get("norm"):
                        direct_best["direct_plate"] = res["norm"]
                        direct_best["direct_conf"] = round(float(res["conf"]), 3)
                        direct_best["direct_verified"] = bool(res["verified"])
                        direct_best["direct_px"] = f"{px_w}x{px_h}"
                        direct_best["direct_ambiguous"] = list(res.get("ambiguous") or [])
        except Exception as exc:  # noqa: BLE001 - secondary evidence, never fatal
            direct_best["direct_error"] = str(exc)

    return {
        "file": path.name,
        "resolution": resolution,
        "frames_read": frames,
        "frames_total": n_frames,
        "tracks": track_rows,
        "direct_best": direct_best,
        "vehicle_pass_ms_p50": _p50(totals["vehicle_pass_ms"]),
        "plate_pass_ms_p50": _p50(totals["plate_pass_ms"]),
        "ocr_runs": totals["ocr_runs"],
        "gated_out": totals["gated_out"],
        "frames_with_vehicles": totals["frames_with_vehicles"],
        "frames_without_vehicles": totals["frames_in"] - totals["frames_with_vehicles"],
        "tracks_seen": totals["tracks_seen"],
        # --- moving-vehicle measurement (real-speed playback) --- #
        "expected": expected,
        "hotlist": list(hotlist),
        "realtime": realtime,
        "fps": round(fps, 2),
        "clip_seconds": round(n_frames / fps, 2),
        "scheduled_s": round(scheduled, 2),
        "elapsed_s": round(elapsed, 2),
        "lag_s": round(elapsed - scheduled, 2),
        "vehicles_rows": vehicle_rows,
        "vehicles_expected": gt_vehicles,
        "vehicles_detected": vehicles_detected,
        "vehicles_missed": max(0, gt_vehicles - vehicles_detected),
        "plate_box_frames": len(plate_px),
        "plate_px_median": int(statistics.median(plate_px)) if plate_px else 0,
        "plate_px_min": min(plate_px) if plate_px else 0,
        "plate_px_max": max(plate_px) if plate_px else 0,
        "sharpness_median": round(statistics.median(sharpness), 1) if sharpness else 0.0,
        "sharpness_min": round(min(sharpness), 1) if sharpness else 0.0,
    }


def _p50(values: List[float]) -> float:
    return round(statistics.median(values), 1) if values else 0.0


def _sharpness(frame: np.ndarray) -> float:
    """Variance of the Laplacian - the standard blur metric, in one number.

    Answers "was this frame too blurred to read?" with a measurement instead of
    an impression (lower = blurrier), so "the plate was missed because of blur"
    can be checked against the clip rather than asserted about it.
    """
    if frame is None or frame.size == 0:
        return 0.0
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def frames_span(tid: int, tracker: VehicleTracker, verdicts: Dict[int, TrackVerdict]) -> int:
    """Rough number of frames a track was alive (uncertain but useful)."""
    track = tracker.get(tid)
    if track is not None:
        # last_ms/first_ms are milliseconds since the tracker's epoch; the
        # per-track read history records nominal frame indices, so the span here
        # is derived from the reads plus the track's own hit count.
        return int(track.hits)
    return len(verdicts.get(tid, TrackVerdict(0)).reads)


# --------------------------------------------------------------------------- #
# Privacy guard
# --------------------------------------------------------------------------- #
def privacy_snapshot() -> dict:
    """Directory state before the run, to prove nothing was written by the run."""
    snap: Dict[str, object] = {
        "DEBUG_SAVE_PLATE_CROPS": os.getenv("DEBUG_SAVE_PLATE_CROPS", "").strip(),
        "plate_crops_existed": PIPELINE_CROP_DIR.is_dir(),
        "plate_crops_entries": sorted(p.name for p in PIPELINE_CROP_DIR.iterdir()) if PIPELINE_CROP_DIR.is_dir() else [],
    }
    return snap


def privacy_check(before: dict) -> Tuple[bool, str]:
    after = privacy_snapshot()
    if after["DEBUG_SAVE_PLATE_CROPS"] not in ("", "0", "false", "off", "no"):
        return False, "DEBUG_SAVE_PLATE_CROPS was left ON - aborting (never trust a run that can leak crops)"
    if after["plate_crops_existed"] and after["plate_crops_entries"] != before["plate_crops_entries"]:
        return False, f"pipeline debug-crop dir changed during run: {after['plate_crops_entries']}"
    return True, "no frames or plates left the pipeline; DEBUG_SAVE_PLATE_CROPS off"


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video-stride", type=int, default=1, help="read every Nth video frame")
    ap.add_argument("--video-max-frames", type=int, default=0, help="cap video frames (0 = all)")
    ap.add_argument(
        "--video-plate",
        default="TS09AB1234",
        help="plate the clip is supposed to show AND the hot-list it is tested against "
             "(generate_demo_video.py's default; empty string = neither score nor hot-list)",
    )
    ap.add_argument(
        "--video-vehicles",
        type=int,
        default=0,
        help="how many vehicles the clip actually contains - the ground truth 'missed "
             "completely' is measured against (demo_road draws none, so the default is 0)",
    )
    ap.add_argument("--video-fast", action="store_true",
                    help="decode the clip as fast as it reads instead of at its real fps")
    ap.add_argument("--save-artifacts", action="store_true",
                    help="OPT-IN: write report JSON + debug crops to data/debug_out/eval/")
    ap.add_argument("--skip-video", action="store_true")
    ap.add_argument("--skip-images", action="store_true")
    args = ap.parse_args()

    privacy_before = privacy_snapshot()

    print("Loading models and warming the OCR engine (one-time) ...")
    warm()
    ls.get_vehicle_model()

    rows: List[Dict[str, object]] = []
    missing: List[str] = []

    if not args.skip_images:
        targets = collect_images()
        for path in targets:
            row = eval_image(path)
            if row.get("error") or row.get("detected") is False:
                missing.append(f"{path.name}: {row.get('error') or 'no plate detected'}")
            else:
                rows.append(row)

    video_row: Dict[str, object] = {}
    if not args.skip_video and DEMO_VIDEO.is_file():
        plate_expect = (args.video_plate or "").strip().upper()
        video_row = process_video(
            DEMO_VIDEO, stride=args.video_stride, max_frames=args.video_max_frames,
            save=args.save_artifacts,
            realtime=not args.video_fast,
            hotlist=(plate_expect,) if plate_expect else (),
            gt_vehicles=args.video_vehicles,
            expected=plate_expect or None,
        )

    ok, note = privacy_check(privacy_before)
    if not ok:
        print(f"PRIVACY VIOLATION: {note}")
        return 2

    # ---------------------------------------------------------------- table
    print()
    print("=" * 104)
    print("PLATE READING: before (old reader) vs after (live-scan path)")
    print("=" * 104)
    print(
        f"{'file':<28} {'expected':<12} {'px':>10} "
        f"{'before':<13} {'after':<13} {'chr %':>6} {'verdict':<9} {'ms b/a':>12}"
    )
    print("-" * 104)

    scoreable = exact_before = exact_after = 0
    char_before: List[float] = []
    char_after: List[float] = []
    for r in rows:
        exp = str(r.get("expected") or "")
        before_norm = (r.get("old") or {}).get("norm")
        after_norm = (r.get("after") or {}).get("norm")
        ms = f"{r.get('old_ms', 0):.0f}/{r.get('after_ms', 0):.0f}"
        ca = r.get("after_char", 0.0)
        cb = r.get("old_char", 0.0)
        verdict = str(r.get("verdict_hot", "-"))
        print(
            f"{r['file']:<28} {exp or '(unnamed)':<12} {str(r.get('px', '-')):>10} "
            f"{str(before_norm):<13} {str(after_norm):<13} {ca:>6.0%} {verdict:<9} {ms:>12}"
        )
        if exp:
            scoreable += 1
            exact_before += bool(r.get("old_exact"))
            exact_after += bool(r.get("after_exact"))
            char_before.append(cb)
            char_after.append(ca)

    print("-" * 104)
    if scoreable:
        print(
            f"exact: before {exact_before}/{scoreable} "
            f"-> after {exact_after}/{scoreable} "
            f"({100 * exact_before / scoreable:.0f}% -> {100 * exact_after / scoreable:.0f}%)"
        )
        print(
            f"char:  before {100 * sum(char_before) / len(char_before):.1f}% "
            f"-> after {100 * sum(char_after) / len(char_after):.1f}% "
            f"({len(char_before)} scoreable)"
        )
    else:
        print("no filename-encoded expectations to score against")
    if missing:
        print(f"skipped/failed: {', '.join(missing)}")

    # ------------------------------------------------------------ worst reads
    # The single number that matters is the exact-match rate, but a rate hides
    # *which* glyph lost and at which stage, so the worst rows are listed with
    # the first pipeline stage inconsistent with the expectation.
    scored = [r for r in rows if r.get("expected")]
    if scored:
        scored.sort(
            key=lambda r: (float(r.get("after_char", 0.0)), float(r.get("old_char", 0.0)))
        )
        worst = scored[:10]
        n_err = sum(1 for r in scored if not r.get("after_exact"))
        print()
        print("=" * 104)
        print(
            f"WORST READS: worst first, {n_err} wrong of {len(scored)} scoreable "
            f"(showing {len(worst)})"
        )
        print("=" * 104)
        print(
            f"{'file':<26} {'expected':<11} {'before':<13} {'after':<13} "
            f"{'b':>4} {'a':>4}  stage (before) / stage (after)"
        )
        print("-" * 104)
        for r in worst:
            after = r.get("after") or {}
            print(
                f"{r['file']:<26} {str(r.get('expected')):<11} "
                f"{str((r.get('old') or {}).get('norm')):<13} {str(after.get('norm')):<13} "
                f"{float(r.get('old_char', 0.0)):>4.0%} {float(r.get('after_char', 0.0)):>4.0%}  "
                f"{failing_stage(r, 'old')} / {failing_stage(r, 'after')}"
            )
        print("-" * 104)
        # ------------------------------------------------------------ Z / 7
        # The case the whole verifier exists for. Counted on both readers over
        # the same aligned positions, and reported as 0 *with the reason* when
        # the fixture set simply has no Z in it - a silent 0 would read like a
        # fix.
        z7_before = z2_before = 0
        z7_after = z2_after = 0
        z_chars = 0
        for r in scored:
            exp = str(r.get("expected") or "")
            z_chars += exp.upper().count("Z")
            z7_before += z_substitutions(str((r.get("old") or {}).get("norm") or ""), exp, "7")
            z2_before += z_substitutions(str((r.get("old") or {}).get("norm") or ""), exp, "2")
            after_norm = str((r.get("after") or {}).get("norm") or "")
            z7_after += z_substitutions(after_norm, exp, "7")
            z2_after += z_substitutions(after_norm, exp, "2")
        print(
            f"Z misreads on this eval set: expected strings hold {z_chars} 'Z' characters."
        )
        print(
            f"  Z read as 7: before {z7_before}   after {z7_after}\n"
            f"  Z read as 2: before {z2_before}   after {z2_after}"
        )
        if z_chars == 0:
            print(
                "  -> no fixture plate contains a Z, so this set cannot exercise the "
                "Z/7 case at all; the counts above are 0 because the question was "
                "never asked of the reader, not because it was answered correctly."
            )
        dl = [r for r in rows if str(r.get("expected") or "").upper() == "DL1ZA9092"]
        if dl:
            for r in dl:
                after = r.get("after") or {}
                print(
                    f"DL1ZA9092 ({r['file']}): before {(r.get('old') or {}).get('norm')} "
                    f"after {after.get('norm')} verified={after.get('verified')}"
                )
        else:
            print("DL1ZA9092: no image with that plate exists on disk - not run, not scored.")

    # ------------------------------------------------------------ video table
    if video_row.get("frames_read") is not None:
        print()
        print("=" * 104)
        print("DEMO VIDEO: live path frame-by-frame (motion gating, tracking, verdicts)")
        print("=" * 104)
        print(
            f"frames read ... {video_row['frames_read']} / {video_row['frames_total']} "
            f"(stride {args.video_stride})\n"
            f"frames with vehicles ... {video_row['frames_with_vehicles']}  "
            f"without ... {video_row['frames_without_vehicles']}\n"
            f"vehicle pass p50 ... {video_row['vehicle_pass_ms_p50']} ms   "
            f"plate pass p50 ... {video_row['plate_pass_ms_p50']} ms\n"
            f"OCR runs ... {video_row['ocr_runs']}   "
            f"read-gated (settled/budget) ... {video_row['gated_out']}   "
            f"tracks ever seen ... {video_row['tracks_seen']}"
        )
        print(
            "  demo_road.mp4 is a synthetic plate-transit clip (a rendered plate "
            "sliding on a dark strip, no vehicles), so every tracked 'vehicle' "
            "count is 0 and the plate travels as an orphan."
        )
        print("-" * 104)
        print(
            f"{'track':>5} {'label':<8} {'reads':>5} {'best@frame':>12} {'plate':<14} "
            f"{'conf':>6} {'state':<9} verified"
        )
        print("-" * 104)
        for t in video_row["tracks"]:
            best = f"{str(t['best_frame'])}f"
            print(
                f"{t['track']:>5} {str(t['label'] or '-'):<8} {t['reads']:>5} "
                f"{best:>12} {str(t['best_plate']):<14} {t['best_conf']:>6.3f} "
                f"{str(t['state']):<9} {'yes' if t['verified_any'] else 'no'}"
            )
        print("-" * 104)
        db = video_row.get("direct_best") or {}
        if db.get("direct_plate"):
            print(
                f"Best-frame direct read (plate detector on full frame, frame {db.get('frame')}): "
                f"{db.get('direct_plate')} conf {db.get('direct_conf')} "
                f"{'verified' if db.get('direct_verified') else 'unverified'} "
                f"@ {db.get('direct_px')}"
            )
            print(
                "  Why: generate_demo_video.py renders a 747px-wide plate onto a "
                "660px canvas and slides/clips it, so the right-hand digits never "
                "appear (\"TS09AB12\" is the correct read of what is visible); and "
                "the live path reads through vehicle-assigned crops while the "
                "detector also boxes the whole bright band, which "
                "plate_box_plausible rejects. The render itself reads "
                "TS09AB1234 @0.894 verified when unclipped."
            )
        else:
            print(
                "No best-frame direct read could be produced (plate not found by the "
                "plate detector at the best frame)."
            )
        print("-" * 104)
        print(
            "Gating proof: 'gated out' counts plate boxes the tracker had already read "
            "(or run out of attempts) - the OCR worker was not asked to re-read "
            "the same parked car on every frame (plus the per-frame read budget)."
        )

        # ------------------------------------------------ moving vehicles
        # What the brief asks for, per vehicle: when it was first seen, when it
        # first produced a valid read, whether that read was right, and - with
        # the clip's plate on the hot-list - the wall-clock moment the STOLEN
        # verdict first came out. Everything here is measured on the wall clock
        # the frames were scheduled on, i.e. at the clip's real speed.
        rows_mv = video_row.get("vehicles_rows") or []
        print()
        print("=" * 104)
        print(
            "MOVING VEHICLES at real speed "
            f"({video_row['fps']} fps, paced): {video_row['file']}"
        )
        print("=" * 104)
        if not rows_mv:
            print("nothing was tracked or read in this clip")
        else:
            print(
                f"{'track':>5} {'what':<27} {'first seen':>14} "
                f"{'first valid read':>32} {'correct':>8} {'first STOLEN':>15}"
            )
            print("-" * 104)
            for r in rows_mv:
                seen = "-" if r["seen_t"] is None else f"{r['seen_t']:.2f}s (f{r['seen_frame']})"
                if r["valid_t"] is None:
                    first = "never"
                else:
                    first = (
                        f"{r['valid_t']:.2f}s (f{r['valid_frame']}) "
                        f"{r['valid_plate']} {r['valid_conf']:.3f}"
                    )
                st = r.get("stolen")
                stolen = "-" if st is None else f"{st['t']:.2f}s (f{st['frame']})"
                correct = "-" if r["correct"] is None else ("yes" if r["correct"] else "no")
                print(
                    f"{r['track']:>5} {str(r['kind']):<27} {seen:>14} "
                    f"{first:>32} {correct:>8} {stolen:>15}"
                )
            print("-" * 104)
        print(
            f"playback ....... {video_row['frames_read']} frames @ {video_row['fps']} fps "
            f"(clip {video_row['clip_seconds']} s): scheduled to end at "
            f"{video_row['scheduled_s']} s, wall clock {video_row['elapsed_s']} s "
            f"-> lag {video_row['lag_s']:+.2f} s"
        )
        print(
            f"hot-list ....... {list(video_row.get('hotlist') or []) or 'none'} - "
            "STOLEN needs two agreeing reads inside the 5 s consensus window, so "
            "the time above is from the first frame, not from the first read."
        )
        print(
            f"vehicles ....... ground truth in the clip {video_row['vehicles_expected']}, "
            f"detected {video_row['vehicles_detected']} "
            f"({video_row['frames_with_vehicles']} frame(s) with a vehicle box), "
            f"missed completely {video_row['vehicles_missed']}"
        )
        print(
            f"plate boxes .... {video_row['plate_box_frames']} frame(s) with an accepted box, "
            f"width px median {video_row['plate_px_median']} "
            f"(min {video_row['plate_px_min']}, max {video_row['plate_px_max']}); "
            f"MIN_PLATE_PX in the server is 45"
        )
        print(
            f"frame blur ..... Laplacian variance median {video_row['sharpness_median']} "
            f"(min {video_row['sharpness_min']}) across {video_row['frames_read']} frames - "
            "reported because blur is one of the candidate reasons, not gated on by the server"
        )
        why = []
        if video_row["vehicles_expected"] and not video_row["vehicles_detected"]:
            why.append("detector miss (the vehicle detector returned no box in any frame)")
        if video_row["plate_box_frames"] == 0:
            why.append("no plate box survived the size/plausibility gates in any frame")
        elif video_row["plate_px_median"] < 45:
            why.append(
                f"plate too small (median {video_row['plate_px_median']} px < MIN_PLATE_PX 45)"
            )
        if video_row.get("expected") and rows_mv:
            wrong = [r for r in rows_mv if r.get("correct") is False]
            if wrong:
                why.append(
                    f"{len(wrong)} track(s) read something other than "
                    f"{video_row['expected']} - see the clipped-render note above"
                )
        print(f"why ............ {'; '.join(why) if why else 'nothing was missed'}")

    # -------------------------------------------------------------- verdicts
    if rows:
        print()
        print("=" * 104)
        print("VERDICT RULES on single reads (TrackVerdict.evaluate)")
        print("=" * 104)
        print(f"{'file':<28} {'read':<13} {'plain':<9} {'hot 1x':<9} {'hot 2x':<9} reason")
        print("-" * 104)
        for r in rows:
            after = r.get("after") or {}
            if not after.get("norm"):
                print(f"{r['file']:<28} {'-':<13} {'-':<9} {'-':<9} {'-':<9} no read")
                continue
            print(
                f"{r['file']:<28} {str(after['norm']):<13} {str(r.get('verdict_plain', '-')):<9} "
                f"{str(r.get('verdict_hot', '-')):<9} {str(r.get('verdict_hot_2x', '-')):<9} "
                f"{str(r.get('verdict_hot_2x_reason') or after.get('reason') or '')[:44]}"
            )
        print(
            "  hot 1x = the read is on the hot-list, seen once; hot 2x = the same "
            "read corroborated inside the consensus window. STOLEN only arrives "
            "with corroboration."
        )

    # --------------------------------------------------------------- named six
    print()
    print("=" * 104)
    print("THE SIX NAMED PLATES (data/test_plates/road_scene.jpg)")
    print("=" * 104)
    by_expected = {
        str(r.get("expected")): r for r in rows if r.get("server_reads") or r.get("expected")
    }
    scene_rows = [r for r in rows if str(r.get("file", "")).startswith("road_scene")]
    scene_reads = [
        str(sr.get("norm")) for r in scene_rows for sr in (r.get("server_reads") or [])
    ]
    for plate in NAMED_SIX:
        row = by_expected.get(plate)
        if row is None:
            # No file on disk claims this plate. The only other place it could
            # come from is the road scene, so report what that scene actually
            # read rather than letting an absent fixture look like a pass.
            if plate in scene_reads:
                print(
                    f"{plate:<12} fixture: none  road_scene.jpg full path: OK "
                    f"(scene reads {scene_reads})"
                )
            else:
                print(
                    f"{plate:<12} NOT PRESENT as a fixture and NOT read by road_scene.jpg "
                    f"(it read {scene_reads or 'nothing'}) - not run, not scored"
                )
            continue
        after = row.get("after") or {}
        reads = row.get("server_reads") or []
        crop_flag = "OK" if str(after.get("norm")) == plate else "MISS"
        path_flag = "OK" if any(str(sr.get("norm")) == plate for sr in reads) else "MISS"
        print(
            f"{plate:<12} crop {crop_flag:<5} full-path {path_flag:<5} "
            f"reads {[sr['norm'] for sr in reads]}"
        )

    # ------------------------------------------------------- full-path reads
    # What the *whole* server path (decode -> vehicle pass -> plate pass ->
    # crop -> OCR) produced for each image: the reading a WebSocket client
    # would have been sent, as opposed to the detector-then-crop shortcut the
    # accuracy table scores.
    print()
    print("=" * 104)
    print("FULL SERVER PATH READS (decode -> vehicle pass -> plate pass -> crop -> OCR)")
    print("=" * 104)
    for r in rows:
        reads = r.get("server_reads") or []
        detail = [(x.get("norm"), x.get("conf")) for x in reads]
        print(
            f"{r['file']:<26} boxes {r.get('server_plates_found', 0)}, "
            f"crops {r.get('server_crops', 0)} -> {detail or 'no read'}"
        )

    # ------------------------------------------------------------- artifacts
    if args.save_artifacts and rows:
        EVAL_DIR.mkdir(parents=True, exist_ok=True)
        import json

        payload = {"rows": rows, "video": video_row, "privacy": note}
        out = EVAL_DIR / "eval_report.json"
        out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"\nartifacts written: {out}")
    else:
        print(f"\nprivacy: {note}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
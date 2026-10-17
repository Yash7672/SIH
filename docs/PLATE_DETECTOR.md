# Plate detector: legacy YOLOv8n vs the opt-in ONNX YOLOv9 detector

RAKSHAK's default plate detector is the trained **YOLOv8n `.pt`**
(`backend/models/license-plate-finetune-v1n.pt`, ultralytics, `imgsz=640`) in
`ai/detector.py`. Nothing about that changes unless an operator opts in.

`ai/plate_detector.py` adds a second, **ONNX-only** detector taken from
**open-image-models** (the detector half of **fast-alpr** by ankandrew), MIT
licensed. It runs on the same `onnxruntime` the OCR path already uses — no torch,
no NMS code (the NMS is baked into the graph, hence "end2end"). **It is the
default** (`auto`), with the YOLOv8n `.pt` detector as the automatic fallback.

## Turning it on / off

```dotenv
RAKSHAK_PLATE_DETECTOR=auto         # auto (default) | oim | legacy
RAKSHAK_PLATE_DETECTOR_SIZE=256     # 256 | 384 | 416 | 512 | 640
RAKSHAK_PLATE_DETECTOR_CONF=0.25
```

`auto` (default) uses the ONNX detector whenever the vendored weights are on
disk and falls back to the YOLOv8n detector then contours otherwise. `oim` uses
it and downloads the weights if they are missing. `legacy` restores today's old
behaviour exactly.

Weights: `backend/models/open-image-models/yolo-v9-t-256-license-plates-end2end.onnx`
(~7.4 MB, vendored, git-tracked). Re-fetch with `python -m ai.plate_detector --download`.

## Measured

Through the real code path (`ai.detector.detect_plate_region`) on
`data/test_plates/road_scene.jpg` (1280×842), same box, same session:

| detector | source | box | conf | crop | time |
| --- | --- | --- | --- | --- | --- |
| legacy YOLOv8n `.pt` @640 | `yolo` | 390,512,577,549 | 0.49 | 223×57 | **180.5 ms** |
| **oim yolo-v9-t @256** | `oim` | 435,515,555,551 | 0.54 | 144×56 | **21.8 ms** |

≈**8× faster**, box IoU 0.57 (the ONNX box is tighter around the plate), and both
crops read the **same** plate through RapidOCR. Isolated detector throughput
(no crop/OCR): ~98 ms → ~28 ms, ~4×.

### Why 256

Tested all sizes on the plate at several scales (`road@1280/960/800/640`) plus
the car photo. 256 was both the fastest **and** the only size that found the
plate at every scale. 384/416 were erratic on the tight synthetic crops; 512
missed `road_scene.jpg` entirely at `conf 0.25`.

```
scene         legacy   oim256  oim384  oim416  oim512
road@1280       0.49     0.54    0.46    0.32      --
road@960        0.52     0.56    0.57    0.64      --
road@800        0.51     0.56    0.62    0.37      --
road@640        0.49     0.59    0.49    0.37      --
car             0.70     0.80    0.81    0.83    0.86
```

## Caveats

- The ONNX model is a general "License Plate" single-class detector; it was not
  trained specifically on Indian plates and has not been validated on Indian
  footage the way the fine-tuned YOLOv8n has. Treat `oim` as an experiment to
  A/B on real frames, not a drop-in replacement.
- The ONNX box is tighter, so the padded crop is smaller. That is usually good
  for OCR, but it is exactly the kind of change that can move a read — so it was
  checked against `scripts/eval_plates.py` (6/6 exact reads, unchanged) before
  being made the default, and `RAKSHAK_PLATE_DETECTOR=legacy` restores the old
  detector if a future change ever regresses.
- Every detection appends a line to `ai.detector.DETECTOR_EVENTS` recording the
  `source` (`yolo` / `oim` / `contour`), so a debug run can prove which detector
  produced a crop.

## Repos evaluated and rejected

| repo | why not |
| --- | --- |
| DoubangoTelecom/ultimateALPR-SDK | licence "For non commercial use only" (proprietary); C++/binary SDK, 2.1 GB; cannot be bundled. |
| parkpow/deep-license-plate-recognition | MIT, but it is a **cloud API client** (Plate Recognizer); sends frames off-device — breaks RAKSHAK's privacy contract. |
| apoorva-dave/LicensePlateDetector | **no licence** → not legally reusable; 2018 OpenCV+SVM, far weaker. |
| ShreyAmbesh/Traffic-Rule-Violation-Detection-System | MIT but unmaintained (2021), TF-SSD + OpenALPR cloud API. Ideas only (centroid tracking, red-light, speed). |

# open-image-models plate-detector weights (vendored)

These files are the ONNX **plate detection** model used by
**fast-alpr** (detector half), sourced from **open-image-models**:

- Model: `yolo-v9-t-256-license-plate-end2end` (YOLOv9-T, 256 px input)
- Weights: `yolo-v9-t-256-license-plates-end2end.onnx` (~7.4 MB)
- Source:  https://github.com/ankandrew/open-image-models/releases/tag/assets
- Projects: https://github.com/ankandrew/open-image-models
            https://github.com/ankandrew/fast-alpr
- Licence: MIT (see `LICENSE` in this directory)

"end2end" means NMS is baked into the ONNX graph: the model emits
`(N, k, 7)` = `[batch_idx, x1, y1, x2, y2, class_id, score]` with duplicates
already removed, so RAKSHAK runs it directly on ONNX Runtime with no torch and
no second pass. Used by `ai/plate_detector.py`.

## Why 256

Measured on this box against `data/test_plates/road_scene.jpg` (1280 px) and the
car photo, the 256 model was both the fastest and the only size that found the
plate at every scale (1280 / 960 / 800 / 640). 384 and 416 were erratic on the
tight synthetic crops; 512 missed `road_scene.jpg` entirely at `conf 0.25`.

| detector | speed | notes |
| --- | --- | --- |
| YOLOv8n `.pt` @640 (current default) | ~98 ms/frame | ultralytics |
| **yolo-v9-t @256 (this)** | **~20–28 ms/frame** | ~4x faster, ONNX only |

## Opt-in no longer — on by default

`RAKSHAK_PLATE_DETECTOR=auto` (default) uses this model whenever the vendored
weights are on disk, and `ai/detector.py` falls back to the YOLOv8n `.pt`
detector (then contours) when it is absent or finds nothing. Set
`RAKSHAK_PLATE_DETECTOR=legacy` to restore the old detector exactly, or `=oim`
to use this one and download the weights if they are missing.

## Re-fetching

```powershell
python -m ai.plate_detector --download
```

Fetched into this directory by default; override with `RAKSHAK_PLATE_DETECTOR_DIR`.
Set the size with `RAKSHAK_PLATE_DETECTOR_SIZE` (256 | 384 | 416 | 512 | 640).

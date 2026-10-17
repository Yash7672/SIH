# fast-plate-ocr weights (vendored)

These files are the ONNX plate **recognition** model and its config, downloaded
from the `arg-plates` release of **cnn-ocr-lp**:

- Model: `cct-s-v2-global` (Compact Convolutional Transformer, "S" size)
- Weights: `cct_s_v2_global.onnx` (~5 MB)
- Config:  `cct_s_v2_global_plate_config.yaml`
- Source:  https://github.com/ankandrew/cnn-ocr-lp/releases/tag/arg-plates
- Project: https://github.com/ankandrew/fast-plate-ocr
- Licence: MIT (see `LICENSE` in this directory)

Used by `ai/plate_ocr.py` as a second, plate-specialised reader. It is
**recognition only** — it reads a crop that already contains a plate; it does
not detect plates.

## Not India-trained

The `*_v2_global` models are trained on 65 countries and **India is not one of
them** (see the `plate_regions` key in the config above; the list stops short of
India). They read Indian plates well but not perfectly, so RAKSHAK uses them as
*supporting* evidence at `RAKSHAK_PLATE_OCR_WEIGHT` (default 0.6) — enough to
corroborate or tip a close vote, not enough to outvote a confident RapidOCR read.

## Re-fetching

```powershell
python -m ai.plate_ocr --download
```

Fetched into this directory by default; override with `RAKSHAK_PLATE_OCR_DIR`.

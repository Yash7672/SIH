"""RAKSHAK on-device ANPR (Automatic Number Plate Recognition) package.

Modules
-------
detector     : plate region detection (YOLOv8n .pt; opt-in ONNX YOLOv9 via
               plate_detector, plus a classical-CV fallback)
ocr_engine   : RapidOCR text reader with Indian-plate normalization
plate_ocr    : optional plate-specialised ONNX recogniser (fast-plate-ocr, MIT)
plate_detector: optional ONNX YOLOv9 plate detector (open-image-models, MIT)
pipeline     : detect -> crop -> OCR -> normalized plate result

Privacy
-------
This pipeline consumes an image in memory and emits ONLY
(plate, confidence). No frames, crops or video are persisted here and
none are ever sent to the RAKSHAK backend.
"""

from ai.pipeline import DetectedPlate, detect_and_read

__all__ = ["DetectedPlate", "detect_and_read"]
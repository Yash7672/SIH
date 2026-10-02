"""RAKSHAK on-device ANPR (Automatic Number Plate Recognition) package.

Modules
-------
detector     : classical-CV plate region detection (no GPU/trained model)
ocr_engine   : RapidOCR text reader with Indian-plate normalization
pipeline     : detect -> crop -> OCR -> normalized plate result

Privacy
-------
This pipeline consumes an image in memory and emits ONLY
(plate, confidence). No frames, crops or video are persisted here and
none are ever sent to the RAKSHAK backend.
"""

from ai.pipeline import DetectedPlate, detect_and_read

__all__ = ["DetectedPlate", "detect_and_read"]
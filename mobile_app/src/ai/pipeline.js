// RAKSHAK on-device ANPR adapter for React Native.
//
// Privacy contract: the phone never uploads video. In demo mode this app sends
// ONE still frame to the host laptop's `/scanner/scan` endpoint, which runs the
// CPU-friendly RapidOCR pipeline and returns only (plate, confidence). The raw
// frame is discarded immediately after OCR and never persisted server-side.
// A production build would run an on-device ONNX OCR model and never leave the
// handset.

import { scanImage } from "../services/api";

export async function detectPlate(uri) {
  const result = await scanImage(uri);
  if (!result || !result.valid) {
    return null;
  }
  return {
    plate: result.plate,
    confidence: result.confidence,
    source: "host-ocr",
  };
}
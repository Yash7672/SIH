/**
 * Projection maths for the live-detection overlay.
 *
 * The server returns boxes normalised to 0..1 against the *captured image*.
 * The camera preview is a different rectangle that usually does not share the
 * image's aspect ratio, so the boxes cannot simply be multiplied by the preview
 * size - they have to be mapped through the same cover/contain transform the
 * preview uses, or every box drifts away from the car it belongs to.
 *
 * Kept free of React and of any react-native import so it can be unit tested
 * directly.
 */

export const VEHICLE_COLOR = "#FF8A00";
export const PLATE_COLOR = "#FF1F1F";
export const VEHICLE_WIDTH = 2.5;
export const PLATE_WIDTH = 3;
export const MAX_BOXES = 20;
// A box that vanishes after this long would otherwise flicker for ever on a
// road where detection is intermittent.
export const BOX_FADE_MS = 700;

const clamp01 = (n) => (n < 0 ? 0 : n > 1 ? 1 : n);

/**
 * Work out how the image is laid out inside the preview rectangle.
 *
 * "cover" is what expo-camera's native preview does: the image fills the view
 * and the overflow is cropped, evenly on both sides. "contain" letterboxes
 * instead.
 */
export function computeTransform(preview, image, mode = "cover") {
  const { width: pw, height: ph } = preview;
  const { width: iw, height: ih } = image;
  if (!(pw > 0) || !(ph > 0) || !(iw > 0) || !(ih > 0)) return null;

  const scale =
    mode === "contain"
      ? Math.min(pw / iw, ph / ih)
      : Math.max(pw / iw, ph / ih);

  return {
    scale,
    // Rendered size of the image inside the preview.
    drawW: iw * scale,
    drawH: ih * scale,
    // Distance from the preview's top-left to the image's top-left. Negative
    // values are the crop on a cover.
    offX: (pw - iw * scale) / 2,
    offY: (ph - ih * scale) / 2,
    previewW: pw,
    previewH: ph,
  };
}

/**
 * Map one normalised box through a transform into preview pixels.
 * Returns null for anything that cannot be drawn safely.
 */
export function projectBox(box, t) {
  if (!t || !Array.isArray(box) || box.length < 4) return null;
  const raw = box.map((n) => (typeof n === "number" && isFinite(n) ? clamp01(n) : NaN));
  if (raw.some((n) => Number.isNaN(n))) return null;

  const x0 = Math.min(raw[0], raw[2]);
  const x1 = Math.max(raw[0], raw[2]);
  const y0 = Math.min(raw[1], raw[3]);
  const y1 = Math.max(raw[1], raw[3]);

  const left = t.offX + x0 * t.drawW;
  const top = t.offY + y0 * t.drawH;
  const width = (x1 - x0) * t.drawW;
  const height = (y1 - y0) * t.drawH;

  // Fully outside the visible area.
  if (width <= 0 || height <= 0) return null;
  if (left + width <= 0 || top + height <= 0) return null;
  if (left >= t.previewW || top >= t.previewH) return null;

  // Partly cropped on a cover: clip to the preview so no border is drawn
  // through the bezel.
  const clippedLeft = Math.max(0, left);
  const clippedTop = Math.max(0, top);
  const clippedRight = Math.min(t.previewW, left + width);
  const clippedBottom = Math.min(t.previewH, top + height);

  return {
    left: clippedLeft,
    top: clippedTop,
    width: clippedRight - clippedLeft,
    height: clippedBottom - clippedTop,
  };
}

const area = (b) => Math.max(0, b[2] - b[0]) * Math.max(0, b[3] - b[1]);

/**
 * Turn one server frame into the flat list the overlay renders.
 * Caps the count and keeps the largest boxes: when a busy junction produces
 * more than MAX_BOXES detections, the nearest cars are the useful ones.
 */
export function buildOverlayItems(message, preview, mode = "cover") {
  if (!message) return [];
  const t = computeTransform(preview, { width: message.w, height: message.h }, mode);
  if (!t) return [];

  const raw = [];
  (message.vehicles || []).forEach((v, i) => {
    raw.push({
      key: `v${message.seq ?? 0}_${i}`,
      kind: "vehicle",
      color: VEHICLE_COLOR,
      borderWidth: VEHICLE_WIDTH,
      box: v.box || v,
      label: v.cls || v.class || null,
      score: v.conf != null ? v.conf : null,
    });
  });
  (message.plates || []).forEach((p, i) => {
    raw.push({
      key: `p${message.seq ?? 0}_${i}`,
      kind: "plate",
      color: PLATE_COLOR,
      borderWidth: PLATE_WIDTH,
      box: p.box || p,
      label: null,
      score: p.conf != null ? p.conf : null,
    });
  });

  const projected = [];
  for (const item of raw) {
    const rect = projectBox(item.box, t);
    if (rect) projected.push({ ...item, rect, area: area(item.box) });
  }
  projected.sort((a, b) => b.area - a.area);
  return projected.slice(0, MAX_BOXES);
}

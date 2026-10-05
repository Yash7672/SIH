/**
 * Projection and tracking maths for the live-detection overlay.
 *
 * The server returns boxes normalised to 0..1 against the *captured image*. The
 * camera preview is a different rectangle that usually does not share the
 * image's aspect ratio, so the boxes cannot simply be multiplied by the preview
 * size - they have to be mapped through the same cover/contain transform the
 * preview uses, or every box drifts away from the car it belongs to.
 *
 * On top of the projection this module keeps the short-lived *track* state that
 * makes the overlay look like a live CCTV feed rather than a slideshow:
 *
 *   - every rectangle is keyed by the server's track id, so a box follows its
 *     car instead of flickering under a per-message key;
 *   - a refreshed box is drawn where the car will be by the time the 150 ms
 *     slide finishes (constant velocity from the last two positions, capped);
 *   - a box that stops being reported fades out after 700 ms instead of
 *     sticking to a car that has already driven off.
 *
 * Kept free of React and of any react-native import so it can be unit tested
 * directly with `node --test`.
 */

export const VEHICLE_COLOR = "#22C55E";
export const PLATE_COLOR = "#38BDF8";
// Border colour per verdict state.
//
// A plate box used to be red in every state, which painted "STOLEN" next to every
// ordinary car on the road - the same mistake as the banner, in the same feature.
// Colour now follows the server's verdict and nothing else. `PLATE_COLOR` is the
// ordinary, not-listed case, so the default and the CLEAR case are identical.
export const STATE_COLORS = {
  STOLEN: "#FF1F1F",
  POSSIBLE: "#F59E0B",
  CLEAR: PLATE_COLOR,
  UNREAD: "#9CA3AF",
};
export const VEHICLE_WIDTH = 3;
export const PLATE_WIDTH = 3;
// Box cap. A crowded junction can produce far more vehicles than are useful on
// a phone screen; the nearest (largest) ones win. Plates are capped separately
// and always kept, because a read plate is the whole point of the screen.
export const MAX_BOXES = 20;
export const MAX_PLATE_BOXES = 8;
// How long a slide between two reported positions takes, and how far ahead of
// the newest report the box is drawn. They are the same number on purpose: the
// animation then arrives exactly as the predicted box does.
export const MOVE_MS = 150;
export const LEAD_MS = MOVE_MS;
export const FADE_MS = 220;
export const BOX_EXPIRY_MS = 700;
// A prediction is extrapolated from two samples. Samples closer together than
// this carry no usable velocity (sub-pixel quantisation), so nothing is
// extrapolated from them.
export const MIN_PREDICT_DT = 40;
// The extrapolation is clamped to this fraction of the box's own diagonal, so a
// single jumpy detection can never fling a rectangle across the screen.
export const PREDICTION_CAP = 0.75;
// Height reserved for a chip, and the gap between a box and its chip.
export const CHIP_H = 13;
export const CHIP_GAP = 2;

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

/** Fresh, empty overlay state. */
export function createOverlayState() {
  return {
    // Track id -> record. Two independent maps on purpose: the `plates` and
    // `plate` messages can arrive a moment after the `boxes` message for the
    // same frame, and one missed box must not cost the plate chip.
    vehicles: new Map(),
    plates: new Map(),
    image: { width: 0, height: 0 },
  };
}

function upsert(map, id, box, now, patch) {
  if (id == null) return null;
  const key = String(id);
  const prev = map.get(key);
  if (prev) {
    if (Array.isArray(box)) {
      prev.prev = prev.box;
      prev.prevAt = prev.at;
      prev.box = box;
    }
    prev.at = now;
    prev.fading = false;
    prev.fadeAt = 0;
    if (patch) Object.assign(prev, patch);
    return prev;
  }
  const rec = {
    id: key,
    box: Array.isArray(box) ? box : null,
    prev: Array.isArray(box) ? box : null,
    at: now,
    prevAt: now,
    fading: false,
    fadeAt: 0,
    ...(patch || {}),
  };
  map.set(key, rec);
  return rec;
}

/** Fold a `boxes` message into the state. `now` is injected so tests are deterministic. */
export function applyBoxes(state, message, now) {
  if (!message) return state;
  if (message.w > 0 && message.h > 0) state.image = { width: message.w, height: message.h };
  (message.vehicles || []).forEach((v) => {
    upsert(state.vehicles, v.track, v.box, now, {
      label: v.label || v.cls || null,
      cls: v.cls != null ? v.cls : null,
      score: typeof v.conf === "number" ? v.conf : null,
    });
  });
  return state;
}

/** Fold a `plates` message (box only) into the state. */
export function applyPlates(state, message, now) {
  if (!message) return state;
  (message.plates || []).forEach((p) => {
    const rec = upsert(state.plates, p.track, p.box, now, {
      // Detector confidence. The OCR confidence replaces it once the text is in.
      score: typeof p.conf === "number" ? p.conf : null,
      read: null,
      stolen: false,
    });
    // A fresh box means the previous text is stale: the car moved and the new
    // pixels have not been read yet.
    if (rec) rec.read = null;
  });
  return state;
}

/** Fold a `plate` message (the OCR result) into the state. */
export function applyPlateText(state, message, now) {
  if (!message) return null;
  const rec = upsert(state.plates, message.track, null, now, {});
  if (!rec) return null;
  const verdict = message.verdict || null;
  rec.read = verdict?.plate || message.norm || message.text || null;
  rec.raw = message.text || null;
  rec.score = typeof (verdict?.confidence ?? message.conf) === "number"
    ? (verdict?.confidence ?? message.conf)
    : rec.score;
  rec.valid = Boolean(message.valid);
  // The verdict is the only authority on the box's colour. It used to be
  // recomputed here from `stolen && valid`, which is the same class of bug the
  // banner had: a second implementation of the decision, free to disagree.
  //
  // The fallback is for a server that predates the verdict, and it keeps the
  // original rule exactly: an invalid read can never be STOLEN, however it was
  // flagged. It degrades to UNREAD rather than to CLEAR so an unreadable plate is
  // never shown as a clean identification.
  rec.state =
    verdict?.state ||
    (message.stolen ? (message.valid ? "STOLEN" : "UNREAD") : "CLEAR");
  rec.stolen = rec.state === "STOLEN";
  rec.reads = verdict?.reads ?? 0;
  rec.weak = Array.isArray(verdict?.weak) ? verdict.weak : [];
  rec.reason = verdict?.reason || "";
  rec.label = message.label || rec.label || null;
  return rec;
}

/**
 * Extrapolate a box forward along its own velocity.
 *
 * `leadMs` of travel is applied from the two most recent positions. The result
 * is clamped to PREDICTION_CAP of the box's own diagonal, and nothing is
 * extrapolated when the last two samples are too close together to carry a
 * velocity.
 */
export function predictBox(rec, leadMs = LEAD_MS) {
  const box = rec.box;
  if (!Array.isArray(box) || !Array.isArray(rec.prev)) return box;
  const dt = rec.at - rec.prevAt;
  if (!(dt >= MIN_PREDICT_DT)) return box;

  const vx = (box[0] - rec.prev[0]) / dt;
  const vy = (box[1] - rec.prev[1]) / dt;
  let dx = vx * leadMs;
  let dy = vy * leadMs;

  const cap = PREDICTION_CAP * Math.hypot(box[2] - box[0], box[3] - box[1]);
  const mag = Math.hypot(dx, dy);
  if (mag > cap && cap > 0) {
    dx = (dx / mag) * cap;
    dy = (dy / mag) * cap;
  }
  return [box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy];
}

function chipBelowBox(rect, previewH) {
  return rect.top + rect.height + CHIP_GAP + CHIP_H <= previewH;
}

/**
 * Put a "?" on every character the server was unsure about.
 *
 * The chip is a plain string, so this is the only place the doubt can be shown.
 * It is deliberately not a colour change: the chip's background already carries
 * the verdict, and a volunteer glancing at a plate needs to see *which* glyph is
 * in doubt, not a differently-coloured box.
 *
 * Indices come from the server's per-character confidence list, which is aligned
 * to the *stripped* plate string - so an index that did not survive the read is
 * simply out of range and ignored rather than marking the wrong character.
 */
function markWeak(read, weak) {
  const weakSet = new Set(weak || []);
  if (!weakSet.size) return read;
  const glyphs = String(read).split("");
  return glyphs
    .map((g, i) => (weakSet.has(i) ? `${g}?` : g))
    .join("");
}

/**
 * Everything the overlay draws, in draw order.
 *
 * Pure: it reads the state and never writes to it. Expiry and fading are driven
 * separately by `tick`.
 */
export function selectItems(state, preview, mode = "cover", now = 0) {
  if (!state) return [];
  const t = computeTransform(preview, state.image, mode);
  if (!t) return [];

  const vehicles = [];
  state.vehicles.forEach((rec, id) => {
    if (!Array.isArray(rec.box)) return;
    // A fading box holds its last position: sliding it while it dissolves looks
    // like a bug, not like a car driving away.
    const rect = projectBox(rec.fading ? rec.box : predictBox(rec), t);
    if (!rect) return;
    vehicles.push({
      key: `v:${id}`,
      id,
      kind: "vehicle",
      color: VEHICLE_COLOR,
      borderWidth: VEHICLE_WIDTH,
      rect,
      area: area(rec.box),
      label: rec.label,
      chip: rec.label,
      chipBelow: false,
      score: rec.score,
      fading: Boolean(rec.fading),
      stolen: false,
    });
  });
  // Largest first, then capped: on a busy road the nearest cars are the useful
  // ones and the far ones are three pixels tall.
  vehicles.sort((a, b) => b.area - a.area);
  const keptVehicles = vehicles.slice(0, MAX_BOXES);

  const plates = [];
  state.plates.forEach((rec, id) => {
    if (!Array.isArray(rec.box)) return;
    const rect = projectBox(rec.fading ? rec.box : predictBox(rec), t);
    if (!rect) return;
    const pct = typeof rec.score === "number" ? Math.round(rec.score * 100) : null;
    const state = rec.state || "CLEAR";
    plates.push({
      key: `p:${id}`,
      id,
      kind: "plate",
      color: STATE_COLORS[state] || PLATE_COLOR,
      borderWidth: PLATE_WIDTH,
      rect,
      area: area(rec.box),
      // No text yet means the OCR is still running for this track.
      chip: rec.read
        ? `${markWeak(rec.read, rec.weak)}${pct != null ? ` ${pct}%` : ""}`
        : "reading...",
      reading: !rec.read,
      chipBelow: chipBelowBox(rect, t.previewH),
      score: rec.score,
      valid: rec.valid,
      fading: Boolean(rec.fading),
      stolen: Boolean(rec.stolen),
      state: rec.state || "CLEAR",
      reads: rec.reads || 0,
      weak: rec.weak || [],
    });
  });
  plates.sort((a, b) => b.area - a.area);

  return [...keptVehicles, ...plates.slice(0, MAX_PLATE_BOXES)];
}

/**
 * Advance expiry. A box stops being drawn once it has faded out.
 *
 * Returns true when something changed, so the caller can skip a re-render for
 * the (common) tick that changed nothing.
 */
export function tick(state, now) {
  let changed = false;
  const sweep = (map) => {
    map.forEach((rec, id) => {
      if (!rec.fading && now - rec.at >= BOX_EXPIRY_MS) {
        rec.fading = true;
        rec.fadeAt = now;
        changed = true;
      } else if (rec.fading && now - rec.fadeAt >= FADE_MS) {
        map.delete(id);
        changed = true;
      }
    });
  };
  sweep(state.vehicles);
  sweep(state.plates);
  return changed;
}

/** Drop every track. Used when the socket drops, so nothing survives a reconnect. */
export function clearState(state) {
  state.vehicles.clear();
  state.plates.clear();
  state.image = { width: 0, height: 0 };
  return state;
}
/**
 * Unit tests for the overlay projection and tracking maths.
 *
 *   node --test src/components/__tests__/liveOverlayMath.test.js
 *
 * Two failures are guarded against here, and neither is visible from the code:
 *
 *   1. The preview is a different shape from the captured frame, so multiplying
 *      a normalised box by the preview size drifts the rectangle off the car.
 *   2. The boxes must be keyed by track id. Keyed per message, every box is a
 *      brand new component each frame, so React unmounts and remounts it: no
 *      animation ever runs, and the feed flickers instead of moving.
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  BOX_EXPIRY_MS,
  FADE_MS,
  LEAD_MS,
  MAX_BOXES,
  MAX_PLATE_BOXES,
  MIN_PREDICT_DT,
  PLATE_COLOR,
  PLATE_WIDTH,
  VEHICLE_COLOR,
  VEHICLE_WIDTH,
  applyBoxes,
  applyPlateText,
  applyPlates,
  clearState,
  computeTransform,
  createOverlayState,
  predictBox,
  projectBox,
  selectItems,
  tick,
} from "../liveOverlayMath.js";

const PREVIEW = { width: 400, height: 300 };
// The server works at 960 px wide, so that is the frame aspect to project from.
const FRAME = { w: 960, h: 720 };

function seed(state, vehicles, plates, now = 1000) {
  applyBoxes(state, { seq: 1, ...FRAME, vehicles }, now);
  if (plates) applyPlates(state, { seq: 1, plates }, now);
}

// ---- projection ---------------------------------------------------------- //

test("cover crops evenly and centres the image", () => {
  // 640x480 image (4:3) inside a tall 300x600 preview.
  const t = computeTransform({ width: 300, height: 600 }, { width: 640, height: 480 }, "cover");
  assert.equal(t.scale, 600 / 480);
  assert.equal(t.drawH, 600);
  assert.ok(Math.abs(t.offY) < 1e-9, "vertical overflow is zero");
  assert.ok(t.offX < 0, "horizontal overflow is cropped");
  assert.ok(
    Math.abs(t.offX - -(t.drawW - 300) / 2) < 1e-9,
    "the crop is split evenly between left and right"
  );
});

test("contain letterboxes and centres the image", () => {
  const t = computeTransform({ width: 300, height: 600 }, { width: 640, height: 480 }, "contain");
  assert.equal(t.scale, 300 / 640);
  assert.ok(Math.abs(t.offY - (600 - t.drawH) / 2) < 1e-9);
});

test("degenerate sizes produce no transform instead of NaN boxes", () => {
  assert.equal(computeTransform({ width: 0, height: 100 }, { width: 64, height: 64 }), null);
  assert.equal(computeTransform({ width: 100, height: 100 }, { width: 0, height: 0 }), null);
});

test("a matching aspect ratio maps 1:1 with no offset", () => {
  const t = computeTransform({ width: 400, height: 300 }, { width: 800, height: 600 }, "cover");
  assert.equal(t.scale, 0.5);
  assert.equal(t.offX, 0);
  assert.equal(t.offY, 0);
  const r = projectBox([0.25, 0.5, 0.75, 1.0], t);
  assert.deepEqual(r, { left: 100, top: 150, width: 200, height: 150 });
});

// The headline case: a 4:3 camera frame shown in a taller preview.
test("a 4:3 frame in a taller preview crops the sides, not the top or bottom", () => {
  const preview = { width: 390, height: 640 }; // a modern phone, portrait
  const frame = { width: 960, height: 720 }; // 4:3 landscape sensor
  const t = computeTransform(preview, frame, "cover");

  // The frame fills the height; the width overflows and is split evenly.
  assert.equal(t.scale, preview.height / frame.height);
  assert.equal(t.drawH, preview.height);
  assert.ok(t.drawW > preview.width, "the frame is wider than the preview");
  assert.ok(Math.abs(t.offY) < 1e-9, "no vertical crop: a car cannot fall off the top");
  assert.ok(Math.abs(t.offX - -(t.drawW - preview.width) / 2) < 1e-9, "crop split evenly left/right");

  // The visible horizontal band of the frame, in normalised coordinates.
  const leftEdge = (0 - t.offX) / t.drawW;
  const rightEdge = (preview.width - t.offX) / t.drawW;
  assert.ok(leftEdge > 0 && rightEdge < 1, "the preview sees a crop of the middle of the frame");

  // A box in the cropped-away left band must NOT be drawn: that part of the road
  // is not on the phone.
  assert.equal(projectBox([0, 0, leftEdge * 0.5, 0.2], t), null);

  // A box in the middle band lands where the maths says it should.
  const box = [leftEdge + 0.05, 0.4, leftEdge + 0.45, 0.8];
  const r = projectBox(box, t);
  assert.ok(Math.abs(r.left - (t.offX + box[0] * t.drawW)) < 1e-6);
  assert.ok(Math.abs(r.top - (t.offY + box[1] * t.drawH)) < 1e-6);
  assert.ok(Math.abs(r.width - (box[2] - box[0]) * t.drawW) < 1e-6);
  assert.ok(Math.abs(r.height - (box[3] - box[1]) * t.drawH) < 1e-6);

  // And it must land inside the preview, or the box is off the bezel.
  assert.ok(r.left >= 0 && r.left + r.width <= preview.width + 1e-6);
  assert.ok(r.top >= 0 && r.top + r.height <= preview.height + 1e-6);
});

test("normalised corners project to the expected pixels under cover", () => {
  const t = computeTransform({ width: 300, height: 600 }, { width: 640, height: 480 }, "cover");
  const full = projectBox([0, 0, 1, 1], t);
  assert.ok(Math.abs(full.width - 300) < 1e-6);
  assert.ok(Math.abs(full.height - 600) < 1e-6);
  assert.ok(Math.abs(full.left) < 1e-6);
  assert.ok(Math.abs(full.top) < 1e-6);
});

test("a box cropped by cover is clipped to the preview, not drawn off-screen", () => {
  const t = computeTransform({ width: 300, height: 600 }, { width: 640, height: 480 }, "cover");
  const r = projectBox([0.2, 0.2, 0.4, 0.6], t);
  assert.equal(r.left, 0, "clipped to the preview's left edge");
  assert.ok(r.left + r.width <= 300 + 1e-6);
  assert.ok(r.width > 0 && r.height > 0, "the visible part is still drawn");
});

test("boxes fully outside the visible area are rejected", () => {
  const t = computeTransform({ width: 300, height: 600 }, { width: 640, height: 480 }, "cover");
  assert.equal(projectBox([0, 0, 0.01, 0.1], t), null);
});

test("malformed boxes are rejected rather than rendered as garbage", () => {
  const t = computeTransform({ width: 300, height: 600 }, { width: 640, height: 480 }, "cover");
  assert.equal(projectBox([0.1, 0.2], t), null);
  assert.equal(projectBox([0.1, 0.2, NaN, 0.9], t), null);
  assert.equal(projectBox([0.1, 0.2, "0.5", 0.9], t), null);
  assert.equal(projectBox(null, t), null);
  assert.equal(projectBox([0.5, 0.5, 0.5, 0.5], t), null, "zero area");
});

test("inverted corners are normalised instead of producing a negative size", () => {
  const t = computeTransform({ width: 400, height: 400 }, { width: 400, height: 400 }, "cover");
  const fwd = projectBox([0.2, 0.2, 0.6, 0.8], t);
  const rev = projectBox([0.6, 0.8, 0.2, 0.2], t);
  assert.deepEqual(rev, fwd);
  assert.ok(rev.width > 0 && rev.height > 0);
});

test("out-of-range coordinates are clamped, not trusted", () => {
  const t = computeTransform({ width: 400, height: 400 }, { width: 400, height: 400 }, "cover");
  const r = projectBox([-0.5, -0.5, 1.5, 1.5], t);
  assert.deepEqual(r, { left: 0, top: 0, width: 400, height: 400 });
});

// ---- drawing rules ------------------------------------------------------- //

test("vehicles are green and plates are red, with the spec widths", () => {
  const state = createOverlayState();
  seed(state, [{ track: 1, label: "CAR 1", cls: "car", conf: 0.8, box: [0.1, 0.1, 0.4, 0.6] }], [
    { track: 1, conf: 0.4, box: [0.2, 0.3, 0.3, 0.38] },
  ]);
  const items = selectItems(state, PREVIEW, "cover", 1000);
  const v = items.find((i) => i.kind === "vehicle");
  const p = items.find((i) => i.kind === "plate");
  assert.equal(v.color, "#22C55E");
  assert.equal(v.color, VEHICLE_COLOR);
  assert.equal(v.borderWidth, 3);
  assert.equal(v.borderWidth, VEHICLE_WIDTH);
  assert.equal(p.color, "#FF1F1F");
  assert.equal(p.color, PLATE_COLOR);
  assert.equal(p.borderWidth, 3);
  assert.equal(p.borderWidth, PLATE_WIDTH);
  assert.equal(v.chip, "CAR 1", "the chip names the class and the track number");
});

test("a plate with no OCR text yet says so instead of showing an empty chip", () => {
  const state = createOverlayState();
  seed(state, [{ track: 3, label: "BIKE 1", box: [0.2, 0.2, 0.5, 0.6] }], [
    { track: 3, conf: 0.4, box: [0.3, 0.3, 0.4, 0.36] },
  ]);
  const plate = selectItems(state, PREVIEW, "cover", 1000).find((i) => i.kind === "plate");
  assert.equal(plate.chip, "reading...");
  assert.equal(plate.reading, true);

  applyPlateText(state, { track: 3, text: "MH12JK4567", norm: "MH12JK4567", conf: 0.91, valid: true }, 1100);
  const read = selectItems(state, PREVIEW, "cover", 1100).find((i) => i.kind === "plate");
  assert.equal(read.chip, "MH12JK4567 91%", "text and confidence both on the chip");
  assert.equal(read.reading, false);
});

test("a plate chip goes below its box, and flips above near the bottom edge", () => {
  const state = createOverlayState();
  seed(state, [{ track: 1, label: "CAR 1", box: [0.1, 0.55, 0.5, 0.8] }], [
    { track: 1, conf: 0.5, box: [0.2, 0.6, 0.3, 0.66] },
  ]);
  const low = selectItems(state, PREVIEW, "cover", 1000).find((i) => i.kind === "plate");
  assert.equal(low.chipBelow, true, "room under the box, so the chip goes below");

  seed(state, [{ track: 2, label: "CAR 2", box: [0.1, 0.85, 0.5, 0.99] }], [
    { track: 2, conf: 0.5, box: [0.2, 0.9, 0.3, 0.96] },
  ]);
  const edge = selectItems(state, PREVIEW, "cover", 1000).find((i) => i.id === "2" && i.kind === "plate");
  assert.equal(edge.chipBelow, false, "no room under the box, so the chip flips above it");
});

test("a busy junction is capped, keeping the largest boxes", () => {
  const state = createOverlayState();
  const many = Array.from({ length: 60 }, (_, i) => ({
    track: i,
    label: `CAR ${i}`,
    box: [0, 0, 0.1 + i / 200, 0.1 + i / 200],
  }));
  seed(state, many);
  const items = selectItems(state, PREVIEW, "cover", 1000);
  assert.equal(items.length, MAX_BOXES);
  assert.ok(items[0].area >= items[items.length - 1].area);
  assert.equal(items[0].id, "59", "the largest box survives");
});

test("plates are capped separately and always survive the cap", () => {
  const state = createOverlayState();
  const vehicles = Array.from({ length: 40 }, (_, i) => ({
    track: i,
    label: `CAR ${i}`,
    box: [0, 0, 0.3 + i / 500, 0.3 + i / 500],
  }));
  seed(state, vehicles);
  // Plates ride on the vehicles that are kept.
  applyPlates(
    state,
    {
      seq: 2,
      plates: Array.from({ length: 12 }, (_, i) => ({ track: i, conf: 0.3, box: [0.05, 0.05 + i / 100, 0.1, 0.07 + i / 100] })),
    },
    1000
  );
  const items = selectItems(state, PREVIEW, "cover", 1000);
  const plates = items.filter((i) => i.kind === "plate");
  const vehiclesShown = items.filter((i) => i.kind === "vehicle");
  assert.equal(plates.length, MAX_PLATE_BOXES);
  assert.equal(vehiclesShown.length, MAX_BOXES);
});

test("keys are keyed by track id, so React reuses the same rectangle", () => {
  const state = createOverlayState();
  const v = [{ track: 7, label: "CAR 1", box: [0.1, 0.1, 0.2, 0.2] }];
  seed(state, v);
  const first = selectItems(state, PREVIEW, "cover", 1000);
  seed(state, v, [{ track: 7, conf: 0.5, box: [0.11, 0.11, 0.19, 0.19] }]);
  const second = selectItems(state, PREVIEW, "cover", 1100);
  assert.equal(first[0].key, second[0].key, "the same track keeps its key across frames");
  assert.ok(first.every((it, i) => it.key !== second[(i + 1) % second.length].key), "keys stay unique");
});

test("two tracks never share a key even in the same frame", () => {
  const state = createOverlayState();
  seed(state, [
    { track: 1, label: "CAR 1", box: [0.1, 0.1, 0.2, 0.2] },
    { track: 2, label: "CAR 2", box: [0.3, 0.3, 0.4, 0.4] },
  ]);
  const items = selectItems(state, PREVIEW, "cover", 1000);
  assert.equal(new Set(items.map((i) => i.key)).size, items.length);
});

test("an unmeasured preview yields no items instead of NaN positions", () => {
  const state = createOverlayState();
  seed(state, [{ track: 1, label: "CAR 1", box: [0, 0, 1, 1] }]);
  assert.deepEqual(selectItems(state, { width: 0, height: 300 }, "cover", 1000), []);
});

test("a frame with no detections produces an empty list", () => {
  const state = createOverlayState();
  seed(state, []);
  assert.deepEqual(selectItems(state, PREVIEW, "cover", 1000), []);
});

test("a box fully cropped out of a tall preview is not drawn", () => {
  const state = createOverlayState();
  seed(state, [{ track: 1, label: "CAR 1", box: [0, 0.2, 0.05, 0.4] }]);
  const items = selectItems(state, { width: 390, height: 640 }, "cover", 1000);
  assert.deepEqual(items, [], "the visible band starts well to the right of x=0.05");
});

// ---- tracking ------------------------------------------------------------ //

test("a refreshed track keeps its identity and its key", () => {
  const state = createOverlayState();
  seed(state, [{ track: 4, label: "TRUCK 1", box: [0.2, 0.2, 0.6, 0.6] }]);
  seed(state, [{ track: 4, label: "TRUCK 1", box: [0.21, 0.21, 0.61, 0.61] }]);
  assert.equal(state.vehicles.size, 1);
  assert.equal(selectItems(state, PREVIEW, "cover", 1200).length, 1);
});

test("a track that changes class keeps its id and takes the new label", () => {
  const state = createOverlayState();
  seed(state, [{ track: 4, label: "CAR 1", box: [0.2, 0.2, 0.6, 0.6] }]);
  seed(state, [{ track: 4, label: "BUS 1", box: [0.2, 0.2, 0.6, 0.6] }]);
  assert.equal(state.vehicles.size, 1, "same car, not a new one");
  assert.equal(selectItems(state, PREVIEW, "cover", 1200)[0].chip, "BUS 1");
});

test("the box is drawn where the car will be when the slide finishes", () => {
  const state = createOverlayState();
  seed(state, [{ track: 1, label: "CAR 1", box: [0.10, 0.10, 0.30, 0.30] }], null, 1000);
  // 100 ms later the car has moved right by 0.05.
  applyBoxes(state, { seq: 2, ...FRAME, vehicles: [{ track: 1, label: "CAR 1", box: [0.15, 0.10, 0.35, 0.30] }] }, 1100);
  const rec = state.vehicles.get("1");
  const predicted = predictBox(rec, LEAD_MS);
  // Velocity is 0.05 over 100 ms; LEAD_MS is 150, so half again as far.
  assert.ok(Math.abs(predicted[0] - 0.225) < 1e-9, `predicted x0 was ${predicted[0]}`);
  assert.ok(Math.abs(predicted[2] - 0.425) < 1e-9);
});

test("a prediction is capped to the box's own size, so a jump cannot fling it", () => {
  const state = createOverlayState();
  seed(state, [{ track: 1, label: "CAR 1", box: [0.10, 0.10, 0.30, 0.30] }], null, 1000);
  // The detector teleports the box across the frame between two frames.
  applyBoxes(state, { seq: 2, ...FRAME, vehicles: [{ track: 1, label: "CAR 1", box: [0.70, 0.70, 0.90, 0.90] }] }, 1100);
  const predicted = predictBox(state.vehicles.get("1"), LEAD_MS);
  const shift = Math.hypot(predicted[0] - 0.70, predicted[1] - 0.70);
  const diag = Math.hypot(0.2, 0.2);
  assert.ok(shift > 0, "it still leads, so the box is not late");
  assert.ok(shift <= diag + 1e-9, `shift ${shift} must not exceed the box diagonal ${diag}`);
});

test("nothing is extrapolated from two samples too close to carry a velocity", () => {
  const state = createOverlayState();
  seed(state, [{ track: 1, label: "CAR 1", box: [0.10, 0.10, 0.30, 0.30] }], null, 1000);
  applyBoxes(state, { seq: 2, ...FRAME, vehicles: [{ track: 1, label: "CAR 1", box: [0.40, 0.10, 0.60, 0.30] }] }, 1000 + MIN_PREDICT_DT - 1);
  assert.deepEqual(predictBox(state.vehicles.get("1"), LEAD_MS), [0.40, 0.10, 0.60, 0.30]);
});

test("a stationary car is not extrapolated into a drift", () => {
  const state = createOverlayState();
  const box = [0.2, 0.2, 0.4, 0.4];
  seed(state, [{ track: 1, label: "CAR 1", box }], null, 1000);
  applyBoxes(state, { seq: 2, ...FRAME, vehicles: [{ track: 1, label: "CAR 1", box }] }, 1200);
  assert.deepEqual(predictBox(state.vehicles.get("1"), LEAD_MS), box);
});

test("a box that stops being reported fades, then disappears", () => {
  const state = createOverlayState();
  seed(state, [{ track: 1, label: "CAR 1", box: [0.1, 0.1, 0.3, 0.3] }], null, 1000);

  // Still drawn just before the expiry.
  assert.equal(selectItems(state, PREVIEW, "cover", 1000 + BOX_EXPIRY_MS - 1).length, 1);

  // At the expiry it starts fading: still on screen, so it dissolves.
  assert.equal(tick(state, 1000 + BOX_EXPIRY_MS), true);
  const fading = selectItems(state, PREVIEW, "cover", 1000 + BOX_EXPIRY_MS);
  assert.equal(fading.length, 1, "the box stays during the fade");
  assert.equal(fading[0].fading, true);
  assert.deepEqual(fading[0].rect, selectItems(state, PREVIEW, "cover", 1000)[0].rect, "and it holds still");

  // After the fade it is gone.
  assert.equal(tick(state, 1000 + BOX_EXPIRY_MS + FADE_MS), true);
  assert.equal(state.vehicles.size, 0);
  assert.deepEqual(selectItems(state, PREVIEW, "cover", 1000 + BOX_EXPIRY_MS + FADE_MS + 1), []);
});

test("a still-reported box never expires", () => {
  const state = createOverlayState();
  const v = [{ track: 1, label: "CAR 1", box: [0.1, 0.1, 0.3, 0.3] }];
  let now = 1000;
  seed(state, v, null, now);
  for (let i = 0; i < 40; i += 1) {
    now += 100;
    seed(state, v, null, now);
    assert.equal(tick(state, now), false, `tick ${i} must not expire a live track`);
  }
  assert.equal(selectItems(state, PREVIEW, "cover", now).length, 1);
});

test("expiry is reported only when it actually changes something", () => {
  const state = createOverlayState();
  assert.equal(tick(state, 5000), false, "nothing tracked, nothing to do");
});

test("plates survive a frame where the vehicle box is missing", () => {
  const state = createOverlayState();
  seed(state, [{ track: 1, label: "CAR 1", box: [0.1, 0.1, 0.3, 0.3] }], [
    { track: 1, conf: 0.5, box: [0.15, 0.15, 0.2, 0.19] },
  ], 1000);
  applyPlateText(state, { track: 1, text: "MH12JK4567", norm: "MH12JK4567", conf: 0.93, valid: true }, 1050);

  // The next frame drops the vehicle detection but still sees the plate.
  applyPlates(state, { seq: 2, plates: [{ track: 1, conf: 0.5, box: [0.16, 0.16, 0.21, 0.2] }] }, 1200);
  const items = selectItems(state, PREVIEW, "cover", 1200);
  assert.equal(items.filter((i) => i.kind === "plate").length, 1);
});

test("a fresh plate box clears the previous read", () => {
  const state = createOverlayState();
  seed(state, [{ track: 1, label: "CAR 1", box: [0.1, 0.1, 0.3, 0.3] }], [
    { track: 1, conf: 0.5, box: [0.15, 0.15, 0.2, 0.19] },
  ], 1000);
  applyPlateText(state, { track: 1, text: "MH12JK4567", norm: "MH12JK4567", conf: 0.93, valid: true }, 1050);
  // New pixels: the old text is stale and must not be shown over them.
  applyPlates(state, { seq: 2, plates: [{ track: 1, conf: 0.5, box: [0.16, 0.16, 0.21, 0.2] }] }, 1200);
  const plate = selectItems(state, PREVIEW, "cover", 1200).find((i) => i.kind === "plate");
  assert.equal(plate.chip, "reading...");
});

test("an invalid read never raises an alert", () => {
  const state = createOverlayState();
  seed(state, [{ track: 1, label: "CAR 1", box: [0.1, 0.1, 0.3, 0.3] }], [
    { track: 1, conf: 0.5, box: [0.15, 0.15, 0.2, 0.19] },
  ], 1000);
  applyPlateText(state, { track: 1, text: "illegible", norm: "illegible", conf: 0.4, valid: false, stolen: true }, 1050);
  const plate = selectItems(state, PREVIEW, "cover", 1200).find((i) => i.kind === "plate");
  assert.equal(plate.stolen, false, "an invalid read must not pulse as a stolen car");
});

test("a valid stolen read pulses", () => {
  const state = createOverlayState();
  seed(state, [{ track: 1, label: "CAR 1", box: [0.1, 0.1, 0.3, 0.3] }], [
    { track: 1, conf: 0.5, box: [0.15, 0.15, 0.2, 0.19] },
  ], 1000);
  applyPlateText(state, { track: 1, text: "KL01QR4455", norm: "KL01QR4455", conf: 0.97, valid: true, stolen: true }, 1050);
  const plate = selectItems(state, PREVIEW, "cover", 1200).find((i) => i.kind === "plate");
  assert.equal(plate.stolen, true);
});

test("clearState drops everything, so a reconnect starts from nothing", () => {
  const state = createOverlayState();
  seed(state, [{ track: 1, label: "CAR 1", box: [0.1, 0.1, 0.3, 0.3] }], [
    { track: 1, conf: 0.5, box: [0.15, 0.15, 0.2, 0.19] },
  ], 1000);
  clearState(state);
  assert.equal(state.vehicles.size, 0);
  assert.equal(state.plates.size, 0);
  assert.deepEqual(selectItems(state, PREVIEW, "cover", 1000), []);
});

test("a track with no id is ignored rather than collapsing every box into one", () => {
  const state = createOverlayState();
  seed(state, [{ label: "CAR 1", box: [0.1, 0.1, 0.3, 0.3] }], null, 1000);
  assert.deepEqual(selectItems(state, PREVIEW, "cover", 1000), []);
});

test("a moving box stays inside the preview across a whole pass", () => {
  // A car crossing the frame left to right: every drawn box must be on screen.
  const state = createOverlayState();
  const preview = { width: 390, height: 640 };
  let now = 1000;
  for (let i = 0; i <= 20; i += 1) {
    const x = -0.1 + i * 0.06;
    now += 100;
    applyBoxes(state, { seq: i, ...FRAME, vehicles: [{ track: 1, label: "CAR 1", box: [x, 0.4, x + 0.25, 0.7] }] }, now);
    const items = selectItems(state, preview, "cover", now);
    for (const it of items) {
      assert.ok(it.rect.left >= -0.001 && it.rect.left < preview.width, `step ${i}: left ${it.rect.left}`);
      assert.ok(it.rect.top >= -0.001 && it.rect.top < preview.height, `step ${i}: top ${it.rect.top}`);
      assert.ok(it.rect.width > 0 && it.rect.height > 0, `step ${i}: positive size`);
    }
  }
});
/**
 * Unit tests for the overlay projection maths.
 *
 *   node --test src/components/__tests__/liveOverlayMath.test.js
 *
 * The failure this guards against is invisible in a unit test but obvious on a
 * phone: the preview is a different shape from the captured frame, so multiplying
 * a normalised box by the preview size drifts the rectangle off the car.
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  MAX_BOXES,
  PLATE_COLOR,
  VEHICLE_COLOR,
  buildOverlayItems,
  computeTransform,
  projectBox
} from "../liveOverlayMath.js";

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

test("normalised corners project to the expected pixels under cover", () => {
  const t = computeTransform({ width: 300, height: 600 }, { width: 640, height: 480 }, "cover");
  // Full-frame box: the whole preview, since cover fills it.
  const full = projectBox([0, 0, 1, 1], t);
  assert.ok(Math.abs(full.width - 300) < 1e-6);
  assert.ok(Math.abs(full.height - 600) < 1e-6);
  assert.ok(Math.abs(full.left) < 1e-6);
  assert.ok(Math.abs(full.top) < 1e-6);
});

test("a box cropped by cover is clipped to the preview, not drawn off-screen", () => {
  const t = computeTransform({ width: 300, height: 600 }, { width: 640, height: 480 }, "cover");
  // Left edge of the image sits at offX = -250, so x in 0.1125..0.3125 straddles
  // the preview's left border. 0.2..0.4 is squarely in that band.
  const r = projectBox([0.2, 0.2, 0.4, 0.6], t);
  assert.equal(r.left, 0, "clipped to the preview's left edge");
  assert.ok(r.left + r.width <= 300 + 1e-6);
  assert.ok(r.width > 0 && r.height > 0, "the visible part is still drawn");
});

test("boxes fully outside the visible area are rejected", () => {
  const t = computeTransform({ width: 300, height: 600 }, { width: 640, height: 480 }, "cover");
  // A sliver at the far left that cover crops away entirely.
  const off = projectBox([0, 0, 0.01, 0.1], t);
  assert.equal(off, null);
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

test("vehicles are orange and plates are red, with the spec widths", () => {
  const items = buildOverlayItems(
    { seq: 1, w: 640, h: 480, vehicles: [{ box: [0.1, 0.1, 0.4, 0.6] }], plates: [{ box: [0.5, 0.5, 0.7, 0.7] }] },
    { width: 400, height: 300 },
    "cover"
  );
  const v = items.find((i) => i.kind === "vehicle");
  const p = items.find((i) => i.kind === "plate");
  assert.equal(v.color, VEHICLE_COLOR);
  assert.equal(v.borderWidth, 2.5);
  assert.equal(p.color, PLATE_COLOR);
  assert.equal(p.borderWidth, 3);
});

test("a busy junction is capped, keeping the largest boxes", () => {
  const many = Array.from({ length: 60 }, (_, i) => ({
    // Bigger box = larger normalised area.
    box: [0, 0, 0.1 + i / 200, 0.1 + i / 200]
  }));
  const items = buildOverlayItems({ seq: 2, w: 640, h: 480, vehicles: many, plates: [] }, { width: 400, height: 300 });
  assert.equal(items.length, MAX_BOXES);
  // Sorted largest first, so the first item must be one of the biggest.
  assert.ok(items[0].area >= items[items.length - 1].area);
  assert.equal(items[0].box[2], many[59].box[2]);
});

test("keys are unique per box so React does not reuse a rectangle", () => {
  const items = buildOverlayItems(
    { seq: 7, w: 640, h: 480, vehicles: [{ box: [0.1, 0.1, 0.2, 0.2] }, { box: [0.3, 0.3, 0.4, 0.4] }], plates: [] },
    { width: 400, height: 300 }
  );
  assert.equal(new Set(items.map((i) => i.key)).size, items.length);
});

test("an unmeasured preview yields no items instead of NaN positions", () => {
  assert.deepEqual(buildOverlayItems({ seq: 1, w: 640, h: 480, vehicles: [{ box: [0, 0, 1, 1] }] }, { width: 0, height: 300 }), []);
  assert.deepEqual(buildOverlayItems(null, { width: 400, height: 300 }), []);
});

test("a frame with no detections produces an empty list", () => {
  assert.deepEqual(buildOverlayItems({ seq: 1, w: 640, h: 480, vehicles: [], plates: [] }, { width: 400, height: 300 }), []);
});

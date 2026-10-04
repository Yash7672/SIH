import { File } from "expo-file-system";
import { getLocation } from "./location";

/**
 * The live capture loop.
 *
 * One loop, one capture at a time, always. The server can only usefully look at
 * one frame at a time anyway, so a second capture in flight is pure cost: it
 * heats the phone, starves the JS thread that also has to decode base64 and
 * draw the boxes, and it makes the frame rate number on screen meaningless.
 *
 * Capture options, all verified against the installed expo-camera 58.0.7:
 *
 *   - `base64: true` reads the JPEG the native side already compressed, so the
 *     bytes never cross into JS as a file and back.
 *   - `quality: 0.4` is the working point. The server works at 1280 px wide and
 *     a 1280 px frame at quality 0.4 is ~70 KB - comfortably under the 300 KB
 *     soft limit - and the plate still reads at 0.99 confidence. Only higher
 *     quality was measured to be marginally better (0.996 at 0.8) for twice the
 *     upload, so quality buys nothing here.
 *   - `shutterSound: false` matters more than it looks: it defaults to TRUE on
 *     Android, and a shutter click eight times a second is unusable outdoors.
 *   - `skipProcessing` stays FALSE. It is the obvious "fast" flag, but it skips
 *     the whole pipeline: `quality` is discarded, so every frame comes back at
 *     full sensor size, and `width`/`height` come from EXIF tags that Android
 *     frequently leaves at -1 - which broke the box projection completely.
 *   - `exif: false` - nothing reads it and it costs time to attach.
 */
const CAPTURE_OPTIONS = {
  quality: 0.4,
  base64: true,
  exif: false,
  shutterSound: false,
  skipProcessing: false,
};

// Smallest picture we ask the camera for.
//
// 1280 px, measured on the road-scene fixture end to end. This is the single
// number that decides whether the plate reads at all:
//
//     640 px  -> plate 90x18 px  -> OCR returns nothing, ever
//     960 px  -> plate 139x28 px -> reads at q40 only; a cleaner JPEG reads worse
//     1280 px -> plate 185x39 px -> reads at 0.991 (q40) .. 0.996 (q80)
//
// 1280 px is also the width the pre-existing manual snap path uploaded, so the
// live stream feeds the detector the same pixels the already-correct one-frame
// scan fed it. Going below it would quietly trade the plate read for upload
// bandwidth, which is the one thing this screen exists to avoid.
const MIN_PICTURE_WIDTH = 1280;

// Above this the frame is over the server's soft limit and gets downscaled
// anyway, so there is no point capturing larger pixels. Used only to decide when
// to fall back to the slower file-based downscale.
const MAX_PICTURE_WIDTH = 1280;

// The server's SOFT_FRAME_BYTES. Below it a frame is kept as sent; above it the
// server downscales and logs. Kept here so the client can avoid the situation
// rather than merely survive it. Mirrors backend/app/api/v1/live_scan.py.
const SOFT_FRAME_BYTES = 300 * 1024;

/** Decoded JPEG size of a bare base64 payload, without decoding it. */
export function base64Bytes(b64) {
  const padding = b64.endsWith("==") ? 2 : b64.endsWith("=") ? 1 : 0;
  return Math.floor((b64.length * 3) / 4) - padding;
}

// Frame pacing. The loop is driven by the socket, not by a fixed timer: it
// sends a frame, waits for the answer, and only then captures again. The floor
// exists so two replies cannot arrive inside the same animation frame and make
// the overlay look like it is skipping.
const MIN_INTERVAL_MS = 120;
// Once a round trip takes longer than this, the detector is the bottleneck and
// capturing faster only queues frames the server will throw away.
const SLOW_ROUND_TRIP_MS = 800;
const SLOW_INTERVAL_MS = 600;
// Give up on a frame rather than letting the loop wedge.
const IDLE_TIMEOUT_MS = 2000;
const SLEEP_SLICE_MS = 120;

/** Parse expo-camera's pictureSize strings ("640x480"). */
function parsePictureSize(size) {
  const m = /^(\d+)x(\d+)$/.exec(String(size || "").trim());
  if (!m) return null;
  return { width: Number(m[1]), height: Number(m[2]) };
}

/**
 * The smallest available picture at least MIN_PICTURE_WIDTH wide.
 *
 * A 12 MP sensor would otherwise hand the loop a ~4 MB frame to compress down
 * to 70 KB, which is where the frame rate goes to die - and, past the server's
 * 400 KB ceiling, straight into a "frame too large" error.
 *
 * Note on Android (ExpoCameraView.kt): `pictureSize` is fed to a single
 * ResolutionSelector shared by the Preview *and* the ImageCapture use cases, so
 * this also sets the preview resolution. That is a second reason the floor is
 * 1280 rather than something small: a 640 px preview would be visibly soft, and
 * a soft preview is exactly what this screen must not be.
 */
export function choosePictureSize(sizes) {
  const parsed = (sizes || [])
    .map(parsePictureSize)
    .filter((s) => s && s.width >= MIN_PICTURE_WIDTH && s.height > 0);
  if (!parsed.length) return null;
  parsed.sort((a, b) => a.width * a.height - b.width * b.height);
  // Portrait framing: the sensor reports landscape, and the preview is taller
  // than it is wide, so an exactly-4:3 size keeps the plate axis-aligned.
  return `${parsed[0].width}x${parsed[0].height}`;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms)));

/**
 * Run the loop until `stop()`.
 *
 * Everything the loop needs is injected, so the same code drives a real camera
 * and a test double with no `if (process.env.NODE_ENV)` in sight.
 */
export class LiveCaptureLoop {
  constructor({ cameraRef, client, onStats, onError, onPause }) {
    this.cameraRef = cameraRef;
    this.client = client;
    this.onStats = onStats || (() => {});
    this.onError = onError || (() => {});
    this.onPause = onPause || (() => {});

    this._running = false;
    this._loop = null;
    this._frames = 0;
    this._lastStatsAt = 0;
    this._intervalMs = MIN_INTERVAL_MS;
  }

  start() {
    if (this._running) return;
    this._running = true;
    this._loop = this._run();
  }

  async stop() {
    this._running = false;
    if (this._loop) await this._loop.catch(() => {});
    this._loop = null;
  }

  get running() {
    return this._running;
  }

  async _run() {
    while (this._running) {
      const cam = this.cameraRef.current;
      const client = this.client;

      if (!cam || !client || !client.ready || !client.connected) {
        // Paused on purpose: a backgrounded app, a refused permission or a dead
        // socket must not keep the camera sensor powered up.
        this.onPause();
        await sleep(SLEEP_SLICE_MS);
        continue;
      }

      try {
        await this._tick(cam, client);
      } catch (e) {
        // One bad capture - a ref released mid-shot, the camera still warming
        // up - must never kill the loop.
        this.onError(e?.message || String(e));
      }
    }
  }

  async _tick(cam, client) {
    const startedAt = Date.now();

    // One capture, awaited. Nothing else may take a picture until it resolves.
    const photo = await cam.takePictureAsync(CAPTURE_OPTIONS);
    if (!this._running) return;

    if (!photo?.base64) {
      this.onError("Camera returned no image");
      return;
    }
    // base64 gives us the bytes without a file, but expo-camera still writes the
    // temp JPEG to its cache directory. Over a long shift those add up, so the
    // file is removed as soon as the frame is on the wire.
    const tempFile = photo?.uri ? new File(photo.uri) : null;
    let extraFile = null;

    try {
      let width = photo.width > 0 ? photo.width : 0;
      let height = photo.height > 0 ? photo.height : 0;
      if (!width || !height) {
        // The native side reported no usable dimensions. Sending them anyway
        // would make every box on screen project against an unknown frame.
        this.onError("Camera returned an unknown frame size");
        return;
      }

      let base64 = photo.base64;

      // Safety net for the devices where `pictureSize` is not honoured (it is a
      // shared ResolutionSelector on Android, and iOS rounds to whatever the
      // sensor supports). A 12 MP capture at quality 0.4 is still ~2 MB, which is
      // past the server's 400 KB ceiling and would come back as "frame too large"
      // eight times a second. Downscale it the slow, obvious way - only when it is
      // actually needed, because the fast path stays file-free.
      if (width > MAX_PICTURE_WIDTH && base64Bytes(base64) > SOFT_FRAME_BYTES) {
        const shrunk = await this._downscale(photo.uri, MAX_PICTURE_WIDTH);
        if (shrunk) {
          base64 = shrunk.base64;
          width = shrunk.width;
          height = shrunk.height;
          extraFile = shrunk.file;
        }
      }

      // getLocation() already caches a fix for 15 s, so this is not a GPS
      // request per frame; it is a cache read. The frame still carries the
      // coordinates, which is what lets a hot-list sighting be placed on a map.
      const loc = await this._location();
      if (!this._running) return;
      client.sendFrame(width, height, loc.latitude, loc.longitude, base64);
      this._frames += 1;
    } finally {
      for (const f of [tempFile, extraFile]) {
        if (!f) continue;
        try {
          if (f.exists) f.delete();
        } catch (e) {
          // A cache file we failed to delete is not worth breaking the stream.
        }
      }
    }

    // Wait for the answer before capturing again: that is what keeps exactly one
    // frame in flight, and it is also what makes the round trip the pace.
    await client.whenIdle(IDLE_TIMEOUT_MS);
    if (!this._running) return;

    // Pace off the round trip the server just reported.
    const rt = client.lastRoundTripMs;
    const target = rt > SLOW_ROUND_TRIP_MS ? SLOW_INTERVAL_MS : MIN_INTERVAL_MS;
    this._intervalMs = target;
    const elapsed = Date.now() - startedAt;
    if (elapsed < target) await sleep(target - elapsed);

    this._publishStats(rt);
  }

  async _location() {
    try {
      return await getLocation();
    } catch (e) {
      // A location fault is not a reason to drop the frame. getLocation already
      // falls back to a last-known or demo coordinate, and the server ignores
      // anything it considers implausible rather than storing a bogus sighting.
      return { latitude: 0, longitude: 0 };
    }
  }

  /**
   * Resize + recompress one capture through expo-image-manipulator.
   *
   * Returns null on any failure: an oversized frame the server then refuses with
   * "frame too large" is a far better outcome than an exception that kills the
   * capture loop.
   */
  async _downscale(uri, width) {
    if (!uri) return null;
    try {
      const { manipulateAsync, SaveFormat } = await import("expo-image-manipulator");
      const out = await manipulateAsync(uri, [{ resize: { width } }], {
        compress: CAPTURE_OPTIONS.quality,
        format: SaveFormat.JPEG,
      });
      if (!out?.uri) return null;
      const file = new File(out.uri);
      const base64 = file.base64();
      if (!base64) return null;
      return { base64, width: out.width || width, height: out.height || 0, file };
    } catch (e) {
      this.onError("Could not downscale an oversized frame");
      return null;
    }
  }

  _publishStats(roundTripMs) {
    const now = Date.now();
    if (now - this._lastStatsAt < 1000) return;
    const fps = Math.round((this._frames * 1000) / (now - this._lastStatsAt));
    this._frames = 0;
    this._lastStatsAt = now;
    this.onStats({ fps, roundTripMs, intervalMs: this._intervalMs });
  }
}

export { CAPTURE_OPTIONS, MIN_PICTURE_WIDTH, MAX_PICTURE_WIDTH, SLOW_ROUND_TRIP_MS, SOFT_FRAME_BYTES };
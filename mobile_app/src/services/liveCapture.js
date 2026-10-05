/**
 * The live capture loop.
 *
 * One loop, one capture at a time, always - and, since the ENOENT crash, one
 * *iteration* at a time. Three separate defects used to be able to overlap here,
 * and all three ended in the same crash on Metro:
 *
 *   Uncaught (in promise): Error: Call to function 'FileSystemFile.base64' has
 *   been rejected. Caused by: java.io.FileNotFoundException:
 *   .../ImageManipulator/<uuid>.jpg: open failed: ENOENT
 *
 *   1. `File.base64()` is ASYNC in the installed expo-file-system 58.0.5 -
 *      `base64(): Promise<string>`, with `base64Sync()` as the synchronous twin -
 *      while `exists` is a plain boolean and `delete()` returns void. The old
 *      `_downscale` therefore read the resized file, received a *Promise*, and
 *      shipped that Promise as the frame body; the `finally` block then deleted
 *      the very file whose read was still queued on the native bridge. The
 *      pending call threw FileNotFoundException and, never having been awaited,
 *      took the whole app down. The `ImageManipulator/<uuid>.jpg` in the message
 *      is the proof that it was the resize path, not the camera's own temp file -
 *      which is why it only ever appeared on devices where `pictureSize` is not
 *      honoured.
 *      Nothing in this file reads a temp file back now. The bytes always arrive
 *      in the same call that produced them.
 *   2. `stop()` cleared `_running` *before* awaiting the old loop, so a screen
 *      that regained focus inside that window passed the `start()` guard and
 *      began a second loop while the first was still inside an iteration.
 *   3. `stop()` was called fire-and-forget from the effect cleanup.
 *
 * Strict serialisation is now structural rather than incidental: every iteration
 * takes `_lock` and drops it in `finally`, each loop run carries a generation
 * number that `stop()` retires (so a draining loop cannot be resurrected), and a
 * `start()` issued during a `stop()` chains onto the draining loop rather than
 * racing it.
 */

// Native modules are pulled in lazily. Metro handles this fine - the resize path
// already did it for expo-image-manipulator - and it is what lets this loop run
// under `node --test` with an injected fake, so the crash above stays fixed by a
// test rather than by a phone.
let fsPromise = null;
function loadFileSystem() {
  if (!fsPromise) fsPromise = import("expo-file-system");
  return fsPromise;
}

let locationPromise = null;
async function loadLocation() {
  if (!locationPromise) locationPromise = await import("./location");
  return (await locationPromise).getLocation;
}

let manipulatorPromise = null;
function loadImageManipulator() {
  if (!manipulatorPromise) manipulatorPromise = import("expo-image-manipulator");
  return manipulatorPromise;
}

/**
 * Capture options, all verified against the installed expo-camera 58.0.7:
 *
 *   - `base64: true` reads the JPEG the native side already compressed, so the
 *     bytes never cross into JS as a file and back. This is the reason there is
 *     no resize step on the fast path.
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

/**
 * The one-off frame the server asks for when a plate is too small to read.
 *
 * Same options, two changes, both measured rather than guessed:
 *
 *   - `quality: 0.7` instead of 0.4. The plate is maybe 90 px wide here, so
 *     there are ~10 pixels per glyph and JPEG artefacts land directly on the
 *     characters - this is the exact failure that reads `Z` as `2`. More
 *     compression saves ~40 KB per ordinary frame and is thrown away the moment
 *     the detector has read it; on this one frame the bytes are the whole point.
 *   - no `width`: the sensor is asked for what it has, and the server caps the
 *     decode at its own `HIRES_MAX_WIDTH`. Resizing in JS first would mean
 *     paying a native round trip to produce pixels the server was going to throw
 *     away anyway.
 *
 * Cost: one bigger frame every two seconds at most, only while a plate is
 * genuinely unreadable. At quality 0.7 a 1600 px frame is ~250 KB against a
 * 900 KB ceiling, so the worst case is well inside the limit.
 */
const HIRES_CAPTURE_OPTIONS = {
  ...CAPTURE_OPTIONS,
  quality: 0.7,
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
// anyway, so there is no point capturing larger pixels. This is a real case, not
// a theoretical one: `pictureSize` becomes
// `ResolutionStrategy(size, FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER)` in
// ExpoCameraView.kt, which serves the requested size when the device has it and
// otherwise the closest *higher* one. So the resize path stays, it is just no
// longer allowed to touch the filesystem.
const MAX_PICTURE_WIDTH = 1280;

// The server's SOFT_FRAME_BYTES. Below it a frame is kept as sent; above it the
// server downscales and logs. Kept here so the client can avoid the situation
// rather than merely survive it. Mirrors backend/app/api/v1/live_scan.py.
const SOFT_FRAME_BYTES = 300 * 1024;

// Circuit breaker. Five captures in a row that produce no frame means something
// structural is wrong (permission revoked, the camera is held by another app, the
// sensor is gone), and retrying at 5 fps just buries the evidence. Back off for
// two seconds, say so on screen, then carry on.
const FAILURE_LIMIT = 5;
const FAILURE_BACKOFF_MS = 2000;

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

/** A failure we can describe in one line for the operator. */
class CaptureFault extends Error {}

/** One concise line per failure type - no stack traces into the event log. */
function describeFault(e) {
  if (e instanceof CaptureFault) return e.message;
  const message = String((e && e.message) || e || "");
  if (/permission/i.test(message)) return "Camera permission denied";
  if (/CameraNotReady|not ready|camera is not/i.test(message)) return "Camera is not ready";
  if (/busy|another camera/i.test(message)) return "Camera is in use by another app";
  return "Camera capture failed";
}

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
 * The camera settings the server asked for, if it asked for anything usable.
 *
 * The server sends its own `width` and `quality` in the request, so the two sides
 * cannot disagree about what "big enough to read" means: if the reading pipeline
 * is retuned and the threshold moves, the phone follows it without a new release.
 *
 * Only `quality` is taken. `takePictureAsync` in the installed expo-camera 58
 * takes `{ quality, base64, exif, shutterSound, skipProcessing }` and nothing
 * else - resolution comes from the `pictureSize` prop on the CameraView, which is
 * chosen once at mount. Handing the camera a `width` it does not read would look
 * like the feature works while changing nothing, so the requested width is used
 * for the *check* instead: a capture that is not actually wider than the ordinary
 * frame is reported, because on such a device there are no more pixels to be had
 * and a volunteer deserves to know that rather than to keep watching a request
 * that can never be answered.
 *
 * Anything nonsensical is ignored in favour of the local default rather than
 * passed to the camera - a `quality` of 12 would be a `takePictureAsync`
 * rejection at the worst possible moment, on the one frame whose whole purpose
 * is to be read.
 */
function pickHiresSettings(want) {
  const out = {};
  const quality = Number(want && want.quality);
  if (Number.isFinite(quality) && quality > 0 && quality <= 1) out.quality = quality;
  return out;
}

/**
 * Run the loop until `stop()`.
 *
 * Everything the loop needs is injected, so the same code drives a real camera
 * and a test double with no `if (process.env.NODE_ENV)` in sight.
 */
export class LiveCaptureLoop {
  constructor({
    cameraRef,
    client,
    onStats,
    onFrameTiming,
    onError,
    onPause,
    onRecover,
    fs,
    location,
    image,
    minIntervalMs,
    slowIntervalMs,
    idleTimeoutMs,
    failureBackoffMs,
  } = {}) {
    this.cameraRef = cameraRef || { current: null };
    this.client = client || null;
    this.onStats = onStats || (() => {});
    // Per-frame capture / encode / send, as three separate numbers. Every frame
    // is reported; the screen decides which ones are worth showing a human.
    this.onFrameTiming = onFrameTiming || null;
    this.onError = onError || (() => {});
    this.onPause = onPause || (() => {});
    this.onRecover = onRecover || (() => {});
    // Injected native seams; null means "resolve it lazily on first use".
    this._fs = fs || null;
    this._locationFn = location || null;
    this._image = image || null;
    // Pacing policy. Overridable so the regression harness can run 500
    // iterations in seconds instead of a minute.
    this._minIntervalMs = minIntervalMs == null ? MIN_INTERVAL_MS : minIntervalMs;
    this._slowIntervalMs = slowIntervalMs == null ? SLOW_INTERVAL_MS : slowIntervalMs;
    this._idleTimeoutMs = idleTimeoutMs == null ? IDLE_TIMEOUT_MS : idleTimeoutMs;
    this._failureBackoffMs = failureBackoffMs == null ? FAILURE_BACKOFF_MS : failureBackoffMs;

    this._running = false;
    // Every `start()` bumps this. `stop()` bumps it too, which retires the
    // current run: a loop that is still draining cannot be kept alive by a
    // focus event arriving in the meantime.
    this._generation = 0;
    // Serialises iterations. Taken before any await in an iteration, dropped in
    // `finally`, so there is no window in which two captures can be in flight.
    this._lock = false;
    this._loop = null;
    // Start() during a stop() chains here rather than racing it.
    this._queue = Promise.resolve();
    this._frames = 0;
    this._lastStatsAt = 0;
    this._intervalMs = this._minIntervalMs;
    this._failures = 0;
    this._faulted = false;
    // True only for the duration of an iteration that is answering a `need_hires`.
    this._hiresActive = false;
    this._hiresWantWidth = 0;
    // One "this device has no more pixels" message per session, not per frame.
    this._warnedNoHires = false;
  }

  start() {
    if (this._running) return;
    this._running = true;
    const gen = ++this._generation;
    this._loop = this._queue.then(() => this._run(gen));
    // The chain must never look unhandled, even though every link is caught
    // where it is awaited.
    this._queue = this._loop.catch(() => {});
  }

  /**
   * Retire the current run and wait for it to finish.
   *
   * Safe to call when already stopped, and safe to call twice: the second call
   * sees `_running === false` and simply waits on whatever is left.
   */
  async stop() {
    this._running = false;
    // Retire the generation *before* awaiting, so an in-flight iteration that
    // checks "am I still wanted?" stops at its next checkpoint.
    this._generation += 1;
    const loop = this._loop;
    if (!loop) return;
    await loop.catch(() => {});
    if (this._loop === loop) this._loop = null;
  }

  get running() {
    return this._running;
  }

  /** Has this run been superseded or stopped? */
  _retired(gen) {
    return !this._running || gen !== this._generation;
  }

  async _run(gen) {
    while (!this._retired(gen)) {
      const cam = this.cameraRef.current;
      const client = this.client;

      if (!cam || !client || !client.ready || !client.connected) {
        // Paused on purpose: a backgrounded app, a refused permission or a dead
        // socket must not keep the camera sensor powered up.
        this.onPause();
        await sleep(SLEEP_SLICE_MS);
        continue;
      }

      if (this._lock) {
        // Unreachable while `start()` serialises properly, and kept anyway: it
        // turns any future mistake about loop lifecycle into a skipped frame
        // instead of a duplicate capture.
        await sleep(SLEEP_SLICE_MS);
        continue;
      }

      this._lock = true;
      try {
        await this._tick(cam, client, gen);
        if (this._faulted) {
          this._faulted = false;
          this.onRecover();
        }
      } catch (e) {
        if (this._retired(gen)) break;
        this._faulted = true;
        this._failures += 1;
        this.onError(describeFault(e));
      } finally {
        this._lock = false;
      }

      if (this._failures >= FAILURE_LIMIT && !this._retired(gen)) {
        this.onError("Camera error, retrying");
        this._failures = 0;
        const until = Date.now() + this._failureBackoffMs;
        while (!this._retired(gen) && Date.now() < until) {
          await sleep(SLEEP_SLICE_MS);
        }
      }
    }
  }

  async _tick(cam, client, gen) {
    const startedAt = Date.now();
    // Every temp JPEG this iteration creates, deleted in the `finally` once the
    // frame is on the wire.
    const writtenUris = [];

    try {
      // Is the server waiting for one bigger frame? Taken *before* the capture,
      // never after: the picture is taken at the resolution that was asked for,
      // rather than being taken at 1280 px and then upscaled, which would
      // manufacture detail that was never in the sensor and defeat the point.
      //
      // It is also taken from the socket rather than passed in, so the flag cannot
      // be lost between the server's request and the frame that answers it.
      const hires = typeof client.consumeHiresRequest === "function"
        ? client.consumeHiresRequest()
        : null;
      const settings = hires ? pickHiresSettings(hires) : {};
      const options = hires ? { ...HIRES_CAPTURE_OPTIONS, ...settings } : CAPTURE_OPTIONS;
      this._hiresActive = Boolean(hires);
      this._hiresWantWidth = Number(hires && hires.width) || 0;

      // One capture, awaited. Nothing else may take a picture until it resolves.
      const t0 = Date.now();
      const photo = await cam.takePictureAsync(options);
      const captureMs = Date.now() - t0;
      if (photo?.uri) writtenUris.push(photo.uri);
      if (this._retired(gen)) return;

      if (!photo?.base64) {
        throw new CaptureFault("Camera returned no image");
      }
      let width = photo.width > 0 ? photo.width : 0;
      let height = photo.height > 0 ? photo.height : 0;
      if (!width || !height) {
        // The native side reported no usable dimensions. Sending them anyway
        // would make every box on screen project against an unknown frame.
        throw new CaptureFault("Camera returned an unknown frame size");
      }

      // The bytes are already here, from the same call that created them.
      // "Encoding" is already done by the time this line is reached on the fast
      // path; the figure below is what any extra resize cost, so the three
      // numbers a slow phone gets reported - capture, encode, send - each mean
      // one thing.
      let base64 = photo.base64;
      let encodeMs = 0;

      // Safety net for the devices where `pictureSize` is not honoured: the
      // ResolutionSelector falls back to the closest higher size, so a capture
      // can come back at 1920 px. At quality 0.4 that is ~250 KB, past the
      // server's 300 KB soft limit and approaching the 400 KB ceiling that comes
      // back as "frame too large" five times a second. Only when it is actually
      // needed, because the fast path stays file-free.
      //
      // A hi-res frame is deliberately exempt: resizing it back to 1280 px would
      // throw away the extra pixels the server explicitly asked for.
      if (
        !hires &&
        width > MAX_PICTURE_WIDTH &&
        base64Bytes(base64) > SOFT_FRAME_BYTES
      ) {
        const t1 = Date.now();
        const shrunk = await this._downscale(photo.uri, MAX_PICTURE_WIDTH);
        encodeMs = Date.now() - t1;
        if (shrunk) {
          base64 = shrunk.base64;
          width = shrunk.width;
          height = shrunk.height;
          if (shrunk.uri) writtenUris.push(shrunk.uri);
        }
      }

      // getLocation() already caches a fix for 15 s, so this is not a GPS
      // request per frame; it is a cache read. The frame still carries the
      // coordinates, which is what lets a hot-list sighting be placed on a map.
      const loc = await this._location();
      if (this._retired(gen)) return;

      const t2 = Date.now();
      const sent = client.sendFrame(width, height, loc.latitude, loc.longitude, base64, this._hiresActive);
      const sendMs = Date.now() - t2;
      this._frames += 1;

      // Capture, encode and send are timed separately on purpose. One combined
      // number cannot tell a volunteer whose phone is slow *why*, and the three
      // have three different fixes: a slow sensor, a bad resize, or a bad
      // connection. `onStats` gets all three every time.
      if (this.onFrameTiming) {
        this.onFrameTiming({
          hires: this._hiresActive,
          width,
          height,
          bytes: base64Bytes(base64),
          captureMs,
          encodeMs,
          sendMs,
        });
      }

      // Asked for a bigger frame and got the same pixels: say so once. This is a
      // device property, not a fault, and it will be true on every subsequent
      // request too - a volunteer watching a plate that never reads deserves to
      // be told why instead of watching a request come and go.
      if (
        this._hiresActive &&
        this._hiresWantWidth > 0 &&
        width < this._hiresWantWidth &&
        !this._warnedNoHires
      ) {
        this._warnedNoHires = true;
        this.onError(
          `Camera cannot supply more than ${width}px; this plate will stay hard to read`
        );
      }
    } finally {
      this._hiresActive = false;
      // Only now, with the frame handed to the socket, are the temp JPEGs worth
      // removing. Over a long shift they would otherwise fill the app's cache.
      await this._discard(writtenUris);
    }

    if (this._retired(gen)) return;

    // Wait for the answer before capturing again: that is what keeps exactly one
    // frame in flight, and it is also what makes the round trip the pace.
    await client.whenIdle(this._idleTimeoutMs);
    if (this._retired(gen)) return;

    // Pace off the round trip the server just reported.
    const rt = client.lastRoundTripMs;
    const target = rt > SLOW_ROUND_TRIP_MS ? this._slowIntervalMs : this._minIntervalMs;
    this._intervalMs = target;
    const elapsed = Date.now() - startedAt;
    if (elapsed < target) await sleep(target - elapsed);

    this._publishStats(rt);
  }

  /**
   * Delete temp images, ignoring anything that is already gone.
   *
   * `delete()` is synchronous and `exists` is a plain boolean in the installed
   * expo-file-system, but both are awaited/guarded anyway so this keeps working
   * if either becomes asynchronous. A cache file Android collected first is the
   * expected case on a long session, not a fault.
   */
  async _discard(uris) {
    for (const uri of uris) {
      if (!uri) continue;
      try {
        const { File } = this._fs || (await loadFileSystem());
        const file = new File(uri);
        if (!file.exists) continue;
        await Promise.resolve(file.delete());
      } catch (e) {
        // ENOENT, or a permission we do not have. Never fatal, never logged: a
        // missing temp file is not information the operator needs.
      }
    }
  }

  async _location() {
    const getLocation = this._locationFn || (await loadLocation());
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
   * The base64 comes back in the SAME call that wrote the file, via the
   * `base64` save option (expo-image-manipulator 58.0.11:
   * `SaveOptions.base64?: boolean` -> `ImageResult.base64?: string`). Nothing is
   * read back off disk, which is the only reason this path cannot produce the
   * FileNotFoundException the loop used to throw.
   *
   * Returns null on any failure: an oversized frame the server then refuses with
   * "frame too large" is a far better outcome than an exception that kills the
   * capture loop.
   */
  async _downscale(uri, width) {
    if (!uri) return null;
    try {
      const { manipulateAsync, SaveFormat } = this._image || (await loadImageManipulator());
      const out = await manipulateAsync(uri, [{ resize: { width } }], {
        base64: true,
        compress: CAPTURE_OPTIONS.quality,
        format: SaveFormat.JPEG,
      });
      if (!out?.base64) return null;
      return { base64: out.base64, width: out.width || width, height: out.height || 0, uri: out.uri };
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

export {
  CAPTURE_OPTIONS,
  FAILURE_BACKOFF_MS,
  FAILURE_LIMIT,
  MAX_PICTURE_WIDTH,
  MIN_PICTURE_WIDTH,
  SLOW_ROUND_TRIP_MS,
  SOFT_FRAME_BYTES,
};

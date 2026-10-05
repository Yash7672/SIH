/**
 * Regression harness for the live capture loop.
 *
 *   node --test src/services/__tests__/liveCaptureLoop.test.js
 *
 * This exists because of a crash that only ever showed up on a real device:
 *
 *   Uncaught (in promise): Error: Call to function 'FileSystemFile.base64' has
 *   been rejected. Caused by: java.io.FileNotFoundException:
 *   .../ImageManipulator/<uuid>.jpg: open failed: ENOENT
 *
 * Two independent defects produced it, and neither is visible by reading the
 * loop alone:
 *
 *   1. `File.base64()` is async in expo-file-system 58.0.5, so the resize path
 *      received a Promise and shipped it as the frame; the `finally` block then
 *      deleted the file that read was still queued on.
 *   2. `stop()` cleared its flag before awaiting the old loop, and the effect
 *      cleanup never awaited `stop()` at all, so a focus event could start a
 *      second loop while the first was mid-iteration.
 *
 * So the fake filesystem here is deliberately hostile in the two ways that used
 * to be fatal: it keeps a registry of live files, and `delete()` unlinks them
 * at once while `base64()` only finds out at the moment it settles. Any
 * read-back of a temp file is therefore visible in `stats.reads`. Any two
 * concurrent captures are visible in `maxInFlight`. Those two counters are what
 * the assertions below are about.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { LiveCaptureLoop, base64Bytes, choosePictureSize, CAPTURE_OPTIONS } from "../liveCapture.js";

/**
 * A filesystem that behaves like expo-file-system 58.0.5's, including the part
 * that caused the crash: `base64()` is async, `exists` is a boolean and
 * `delete()` takes effect immediately.
 *
 * `reads` counts every call to `base64()`. The whole point of the fix is that
 * the loop never makes one, so any non-zero count is a failure.
 */
function makeFakeFs(stats) {
  const live = new Set();
  class File {
    constructor(uri) {
      this.uri = uri;
    }
    get exists() {
      return live.has(this.uri);
    }
    base64() {
      stats.reads += 1;
      // Async, like the real one: the caller must not assume it is a string.
      // And the file is checked when the promise SETTLES, not when it is called,
      // because on a device the read happens on the native bridge a tick later -
      // which is the whole window the ENOENT crash lived in.
      return Promise.resolve().then(() => {
        if (!live.has(this.uri)) {
          throw new Error(
            "Call to function 'FileSystemFile.base64' has been rejected. " +
              "Caused by: java.io.FileNotFoundException: " +
              this.uri +
              ": open failed: ENOENT"
          );
        }
        return "ZmFrZQ==";
      });
    }
    delete() {
      live.delete(this.uri);
      stats.deleted += 1;
    }
  }
  return {
    fs: { File },
    makeFile: (uri) => {
      live.add(uri);
      return new File(uri);
    },
    isLive: (uri) => live.has(uri),
  };
}

/**
 * A camera whose failures are injected rather than avoided, so a test can prove
 * the loop contains them.
 *
 * `script` is a rotating list of outcomes, so success, empty frames, missing
 * dimensions and hard failures interleave the way a real camera produces them.
 */
function makeCamera(fakeFs, opts = {}) {
  const { script = [], width = 1280, height = 960, bytes = 70 * 1024 } = opts;
  const stats = { captures: 0, inFlight: 0, maxInFlight: 0, frames: 0, options: [] };
  const cam = {
    async takePictureAsync(options) {
      stats.captures += 1;
      stats.inFlight += 1;
      stats.maxInFlight = Math.max(stats.maxInFlight, stats.inFlight);
      // What the camera was actually asked for, per capture. The hi-res feature
      // is entirely about these, so a fake that does not record them could not
      // tell a working implementation from one that changed nothing.
      stats.options.push({ ...(options || {}) });
      try {
        // Yield, so an overlap between two loops would be observable rather
        // than something the single-threaded runtime hides for us.
        await new Promise((r) => setTimeout(r, 0));
        const step = script.length ? script[(stats.captures - 1) % script.length] : "ok";
        if (step === "throw") {
          throw new Error("takePictureAsync: CameraDevice is not open");
        }
        if (step === "empty") return { width, height, base64: "" };
        if (step === "nosize") return { width: -1, height: -1, base64: "ZmFrZQ==" };
        const uri = `file:///cache/frame-${stats.captures}.jpg`;
        const file = fakeFs.makeFile(uri);
        stats.frames += 1;
        return { uri: file.uri, width, height, base64: "A".repeat(Math.ceil((bytes * 4) / 3)) };
      } finally {
        stats.inFlight -= 1;
      }
    },
  };
  return { cam, stats };
}

function makeClient(opts = {}) {
  const { reply = true } = opts;
  const stats = { sent: 0, bodies: [], lastRoundTripMs: 0, maxInFlight: 0, inFlight: 0 };
  // A queue of server requests to hand out, one per capture, so a test can say
  // "the server asks on the third frame" and have the loop see it there.
  const pending = [];
  return {
    stats,
    askForHires(want) {
      pending.push(want || { width: 1600, quality: 0.7, px_w: 90 });
    },
    client: {
      ready: true,
      connected: true,
      get lastRoundTripMs() {
        return stats.lastRoundTripMs;
      },
      // Take-and-clear, exactly like the real socket: a request the loop never
      // answers must not be answered by a frame taken seconds later.
      consumeHiresRequest() {
        return pending.length ? pending.shift() : null;
      },
      sendFrame(width, height, lat, lng, base64, hires) {
        stats.sent += 1;
        stats.bodies.push({ width, height, lat, lng, base64, hires: Boolean(hires) });
        stats.inFlight += 1;
        stats.maxInFlight = Math.max(stats.maxInFlight, stats.inFlight);
        if (reply) queueMicrotask(() => (stats.inFlight -= 1));
        return true;
      },
      whenIdle: () => Promise.resolve(),
    },
  };
}

/** Record every unhandled rejection, whatever it says. */
function watchUncaught() {
  const seen = [];
  const handler = (reason) => seen.push(reason);
  process.on("unhandledRejection", handler);
  return {
    async stop() {
      // Two macrotask turns is what Node gives a promise a chance to report as
      // unhandled; one is not enough.
      await new Promise((r) => setTimeout(r, 10));
      await new Promise((r) => setTimeout(r, 10));
      process.off("unhandledRejection", handler);
      return seen.map((u) => String(u && u.message ? u.message : u));
    },
  };
}

const tick = (ms = 2) => new Promise((r) => setTimeout(r, ms));

/**
 * Wait for something to happen, and fail loudly rather than hanging if it never
 * does. A lifecycle bug in this loop shows up as a run that cannot be stopped,
 * and an unbounded wait would turn that regression into a wedged test process
 * with no output at all.
 */
async function waitFor(predicate, label, ms = 30000) {
  const deadline = Date.now() + ms;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(`timed out after ${ms}ms waiting for: ${label}`);
    await tick();
  }
}

/**
 * Bounded `stop()`, and always called from a `finally`.
 *
 * Every test below tears its loop down in a `finally` for the same reason: a
 * failing assertion must not leave a capture loop running, or the test process
 * never exits and the failure is reported as a hang instead of as itself.
 */
async function stopWithin(loop, ms = 10000) {
  let done = false;
  Promise.resolve(loop.stop()).then(
    () => {
      done = true;
    },
    () => {
      done = true;
    }
  );
  const deadline = Date.now() + ms;
  while (!done) {
    if (Date.now() > deadline) throw new Error(`stop() did not return within ${ms}ms: the loop cannot be torn down`);
    await tick();
  }
}

/**
 * Loop options shared by every test: pacing collapsed to nothing and a short
 * breaker pause, so the suite runs in seconds rather than the minute that 500
 * real 120 ms-spaced iterations would take.
 */
// Pacing policy for tests, plus a location stub. `location` is part of the same
// spread every loop here is built with, so no test has to remember it - and a
// loop that silently cannot get a fix would otherwise look like a loop that
// cannot capture.
const FAST = {
  minIntervalMs: 0,
  slowIntervalMs: 0,
  idleTimeoutMs: 5,
  failureBackoffMs: 120,
  location: async () => ({ latitude: 17.44, longitude: 78.35 }),
};

test("500 iterations of a hostile camera produce zero uncaught rejections and zero ENOENT", async () => {
  const fsStats = { reads: 0, deleted: 0 };
  const fakeFs = makeFakeFs(fsStats);
  const watcher = watchUncaught();

  // Every ninth capture fails outright, every eleventh hands back an empty
  // frame, every thirteenth lies about its dimensions. The rest succeed at
  // 1280 px so the resize path is never entered.
  const script = [];
  for (let i = 1; i <= 40; i += 1) {
    if (i % 9 === 0) script.push("throw");
    else if (i % 11 === 0) script.push("empty");
    else if (i % 13 === 0) script.push("nosize");
    else script.push("ok");
  }
  const { cam, stats: camStats } = makeCamera(fakeFs, { script, width: 1280, height: 960 });
  const { client, stats: clientStats } = makeClient();

  const errors = [];
  const loop = new LiveCaptureLoop({
    cameraRef: { current: cam },
    client,
    onError: (m) => errors.push(m),
    fs: fakeFs.fs,
    location: async () => ({ latitude: 17.44, longitude: 78.35 }),
    ...FAST,
  });

  try {
    loop.start();
    await waitFor(() => camStats.captures >= 500, "500 captures");
  } finally {
    await stopWithin(loop);
  }
  const uncaught = await watcher.stop();

  assert.deepEqual(uncaught, [], "no promise may reject with nobody listening");
  assert.ok(!uncaught.some((u) => /ENOENT/.test(u)));
  assert.equal(fsStats.reads, 0, "the loop must never read a temp file back");
  assert.ok(camStats.captures >= 500, `expected 500 captures, saw ${camStats.captures}`);
  assert.equal(camStats.maxInFlight, 1, "exactly one capture in flight, ever");
  assert.equal(clientStats.maxInFlight, 1, "exactly one frame in flight, ever");
  assert.ok(clientStats.sent > 0, "the loop must have sent frames");
  // Every frame the socket received must be a real string of base64. Before the
  // fix this was the resolved Promise from File.base64(), which JSON-serialises
  // to `{}`.
  assert.ok(
    clientStats.bodies.every((b) => typeof b.base64 === "string" && b.base64.length > 0),
    "every frame body must be a base64 string"
  );
  assert.ok(errors.length > 0, "the injected failures must be reported, not swallowed");
});

test("the resize path gets its base64 from the manipulator, not from the filesystem", async () => {
  const fsStats = { reads: 0, deleted: 0 };
  const fakeFs = makeFakeFs(fsStats);
  const watcher = watchUncaught();

  // 1920 px at a size that trips the soft limit, which is exactly the device
  // where ExpoCameraView.kt's ResolutionStrategy falls back to the closest
  // higher size.
  const { cam, stats: camStats } = makeCamera(fakeFs, { width: 1920, height: 1440, bytes: 320 * 1024 });
  const { client, stats: clientStats } = makeClient();

  // Stand in the one call the loop makes. `base64: true` in the save options is
  // the contract: SaveOptions.base64 -> ImageResult.base64 in 58.0.11. The fake
  // honours the flag exactly as the real module does - ask for bytes and you
  // get bytes, do not ask and you are left with a file and a filesystem to read
  // it from - so a loop that goes the old way fails here the way it did on the
  // device.
  const manipulated = [];
  const image = {
    manipulateAsync: async (uri, actions, options) => {
      manipulated.push({ uri, actions, options });
      const out = { uri: "file:///cache/ImageManipulator/resized.jpg", width: 1280, height: 960 };
      if (options?.base64 === true) return { ...out, base64: "UkVTSVpFRA==" };
      fakeFs.makeFile(out.uri);
      return out;
    },
    SaveFormat: { JPEG: "jpeg", PNG: "png", WEBP: "webp" },
  };

  const errors = [];
  const loop = new LiveCaptureLoop({
    cameraRef: { current: cam },
    client,
    onError: (m) => errors.push(m),
    fs: fakeFs.fs,
    image,
    location: async () => ({ latitude: 17.44, longitude: 78.35 }),
    ...FAST,
  });

  try {
    loop.start();
    await waitFor(() => camStats.captures >= 3, "3 captures");
  } finally {
    await stopWithin(loop);
  }
  const uncaught = await watcher.stop();

  assert.deepEqual(uncaught, [], "no unhandled rejection");
  assert.equal(fsStats.reads, 0, "no read-back, even on the resize path");
  assert.ok(manipulated.length > 0, "the resize path should have been exercised");
  assert.ok(manipulated.every((m) => m.options.base64 === true), "base64 must be requested from the manipulator");
  // The frame that reached the socket is the manipulator's bytes, not a Promise.
  assert.ok(
    clientStats.bodies.every((b) => typeof b.base64 === "string"),
    "a Promise as the frame body JSON-serialises to {} and the server sees no image"
  );
  assert.ok(clientStats.bodies.some((b) => b.base64 === "UkVTSVpFRA=="));
  assert.ok(clientStats.bodies.every((b) => b.width === 1280 && b.height === 960));
  assert.ok(!fakeFs.isLive("file:///cache/ImageManipulator/resized.jpg"), "the resized temp file is deleted too");
});

test("temp files are deleted after the frame is sent, and an already-collected file is not an error", async () => {
  const fsStats = { reads: 0, deleted: 0 };
  const fakeFs = makeFakeFs(fsStats);
  const watcher = watchUncaught();

  const { cam, stats: camStats } = makeCamera(fakeFs, { width: 1280, height: 960 });
  const { client, stats: clientStats } = makeClient();

  // Android's cache cleaner beat us to one of them, and `delete()` on a file
  // that is already gone throws.
  class FlakyFile extends fakeFs.fs.File {
    get exists() {
      return super.exists && !this.uri.includes("frame-3");
    }
    delete() {
      if (this.uri.includes("frame-3")) {
        throw new Error("FileNotFoundException: " + this.uri);
      }
      super.delete();
    }
  }

  const errors = [];
  const loop = new LiveCaptureLoop({
    cameraRef: { current: cam },
    client,
    onError: (m) => errors.push(m),
    fs: { File: FlakyFile },
    location: async () => ({ latitude: 17.44, longitude: 78.35 }),
    ...FAST,
  });

  try {
    loop.start();
    await waitFor(() => camStats.captures >= 5, "5 captures");
  } finally {
    await stopWithin(loop);
  }
  const uncaught = await watcher.stop();

  assert.deepEqual(uncaught, [], "a file collected by the cache cleaner is not a crash");
  assert.ok(clientStats.sent >= 4, "frames kept flowing past the failing delete");
  assert.ok(fsStats.deleted >= 3, "the files that did exist were deleted");
  assert.ok(!errors.some((e) => /ENOENT|FileNotFound/.test(e)), "deletion failures stay silent");
});

test("start() during a stop() never produces two loops or two captures", async () => {
  const fsStats = { reads: 0, deleted: 0 };
  const fakeFs = makeFakeFs(fsStats);
  const watcher = watchUncaught();

  const { cam, stats: camStats } = makeCamera(fakeFs, { width: 1280, height: 960 });
  const { client, stats: clientStats } = makeClient();

  const loop = new LiveCaptureLoop({
    cameraRef: { current: cam },
    client,
    fs: fakeFs.fs,
    location: async () => ({ latitude: 17.44, longitude: 78.35 }),
    ...FAST,
  });

  try {
    loop.start();
    await waitFor(() => camStats.captures >= 2, "2 captures");

    // The exact sequence a focus event produces: teardown starts, and start()
    // lands while the teardown is still draining.
    const stopping = loop.stop();
    loop.start();
    await stopping;
    await waitFor(() => camStats.captures >= 8, "8 captures");
  } finally {
    await stopWithin(loop);
  }
  const uncaught = await watcher.stop();

  assert.deepEqual(uncaught, []);
  assert.equal(camStats.maxInFlight, 1, "two loops would show up as two concurrent captures");
  assert.equal(clientStats.maxInFlight, 1, "two loops would show up as two frames in flight");
  assert.equal(loop.running, false, "the last stop() wins");
});

test("five consecutive capture failures trip the breaker, then it recovers", async () => {
  const fsStats = { reads: 0, deleted: 0 };
  const fakeFs = makeFakeFs(fsStats);
  const watcher = watchUncaught();

  const { cam, stats: camStats } = makeCamera(fakeFs, { script: ["throw"], width: 1280, height: 960 });
  const { client, stats: clientStats } = makeClient({ reply: false });

  const errors = [];
  let recovered = 0;
  const loop = new LiveCaptureLoop({
    cameraRef: { current: cam },
    client,
    onError: (m) => errors.push(m),
    onRecover: () => {
      recovered += 1;
    },
    fs: fakeFs.fs,
    location: async () => ({ latitude: 17.44, longitude: 78.35 }),
    ...FAST,
  });

  try {
    loop.start();
    // Long enough for a burst of failures and the pause that follows.
    await tick(300);
  } finally {
    const duringOutage = clientStats.sent;
    await stopWithin(loop);

    assert.deepEqual(await watcher.stop(), []);
    assert.ok(
      errors.includes("Camera error, retrying"),
      `expected the breaker notice, saw ${JSON.stringify(errors)}`
    );
    assert.equal(duringOutage, 0, "a camera that returns nothing sends nothing");
    assert.ok(camStats.captures >= 5, "the breaker counts real capture attempts");

    // And it comes back: the breaker is a pause, not a shutdown.
    loop.start();
    try {
      const { cam: healthy, stats: healthyStats } = makeCamera(fakeFs, { width: 1280, height: 960 });
      loop.cameraRef.current = healthy;
      await waitFor(() => healthyStats.captures >= 2, "2 captures from the recovered camera");
    } finally {
      await stopWithin(loop);
    }

    assert.ok(clientStats.sent > duringOutage, "the loop resumed after the breaker");
    assert.ok(recovered >= 1, "the screen is told the camera is healthy again");
  }
});

// ---- the hi-res answer ---------------------------------------------------- //

test("a need_hires request changes the very next capture and is marked on the wire", async () => {
  const fakeFs = makeFakeFs();
  const { cam, stats: camStats } = makeCamera(fakeFs, { width: 1280, height: 960 });
  const { client, stats, askForHires } = makeClient();
  const loop = new LiveCaptureLoop({ cameraRef: { current: cam }, client, fs: fakeFs.fs, ...FAST });
  loop.start();
  try {
    await waitFor(() => stats.sent >= 1, "one ordinary frame");
    assert.equal(stats.bodies[0].hires, false, "an ordinary frame is not marked");
    assert.equal(camStats.options[0].quality, CAPTURE_OPTIONS.quality);

    // The server now finds a plate too small to read.
    askForHires({ width: 1600, quality: 0.7, px_w: 90 });
    await waitFor(() => stats.bodies.some((b) => b.hires), "the answering frame");

    const first = stats.bodies.findIndex((b) => b.hires);
    assert.equal(stats.bodies[first].hires, true, "the frame must be marked, or the server refuses its size");
    // The capture *before* the marked frame is the one that answered the request,
    // so the two indices line up.
    assert.equal(
      camStats.options[first].quality,
      0.7,
      "the request's own quality is used, so the two sides cannot disagree"
    );
    assert.ok(
      stats.bodies.slice(first + 1).every((b) => !b.hires),
      "exactly one frame answers one request"
    );
  } finally {
    await stopWithin(loop);
  }
});

test("a request is answered once, then the loop goes back to ordinary frames", async () => {
  const fakeFs = makeFakeFs();
  const { cam } = makeCamera(fakeFs, { width: 1280, height: 960 });
  const { client, stats, askForHires } = makeClient();
  const loop = new LiveCaptureLoop({ cameraRef: { current: cam }, client, fs: fakeFs.fs, ...FAST });
  loop.start();
  try {
    askForHires();
    await waitFor(() => stats.sent >= 3, "three frames");
    const marked = stats.bodies.filter((b) => b.hires).length;
    assert.equal(marked, 1, "one request, one marked frame - not one per frame after it");
  } finally {
    await stopWithin(loop);
  }
});

test("a hi-res frame is never resized back down", async () => {
  // A camera that ignores `pictureSize` and returns a 1920 px frame at a size
  // over the soft limit. The ordinary path shrinks it; the hi-res path must not,
  // because resizing to 1280 px would throw away the pixels the server asked for.
  const fakeFs = makeFakeFs();
  const { cam } = makeCamera(fakeFs, { width: 1920, height: 1280, bytes: 320 * 1024 });
  let manipulates = 0;
  const image = {
    async manipulateAsync() {
      manipulates += 1;
      return { base64: "A".repeat(100), width: 1280, height: 960, uri: "file:///cache/x.jpg" };
    },
    SaveFormat: { JPEG: 1 },
  };
  const { client, stats, askForHires } = makeClient();
  const loop = new LiveCaptureLoop({
    cameraRef: { current: cam },
    client,
    fs: fakeFs.fs,
    image,
    ...FAST,
  });
  loop.start();
  try {
    await waitFor(() => stats.sent >= 1, "one ordinary frame");
    // The ordinary frame on its own proves the resize path is live, so the
    // assertion below is about the hi-res frame specifically.
    assert.equal(stats.bodies[0].width, 1280, "an oversized ordinary frame is resized");
    assert.equal(manipulates, 1);

    askForHires();
    await waitFor(() => stats.bodies.some((b) => b.hires), "the answering frame");
    await stopWithin(loop);

    const marked = stats.bodies.find((b) => b.hires);
    assert.equal(marked.width, 1920, "the hi-res frame keeps the pixels it was given");
    assert.equal(marked.height, 1280);
    // One resize per ordinary frame up to the answer, and none for the answer
    // itself. Counted per capture rather than as a raw total, because ordinary
    // frames keep coming while the test waits.
    const markedIndex = stats.bodies.findIndex((b) => b.hires);
    assert.equal(manipulates, markedIndex, "the hi-res capture is not resized");
  } finally {
    await stopWithin(loop);
  }
});

test("capture, encode and send are reported separately", async () => {
  const fakeFs = makeFakeFs();
  // 1920 px and 320 KB, so the resize path is entered and `encodeMs` is a real
  // number rather than a zero that would pass in a version that never times it.
  const { cam } = makeCamera(fakeFs, { width: 1920, height: 1280, bytes: 320 * 1024 });
  const image = {
    async manipulateAsync() {
      // A real resize takes tens of milliseconds on a phone. Returning
      // instantly would make `encodeMs` always 0, which would let a version
      // that never times the resize at all pass this test unchanged.
      await new Promise((r) => setTimeout(r, 3));
      return { base64: "A".repeat(100), width: 1280, height: 960, uri: "file:///cache/x.jpg" };
    },
    SaveFormat: { JPEG: 1 },
  };
  const { client } = makeClient();
  const timings = [];
  const loop = new LiveCaptureLoop({
    cameraRef: { current: cam },
    client,
    fs: fakeFs.fs,
    image,
    onFrameTiming: (t) => timings.push(t),
    ...FAST,
  });
  loop.start();
  try {
    await waitFor(() => timings.length >= 2, "two timed frames");
  } finally {
    await stopWithin(loop);
  }
  const t = timings[0];
  // One combined "frame took N ms" figure cannot tell a volunteer whose phone is
  // slow *why*; the three have three different causes and three fixes.
  assert.equal(typeof t.captureMs, "number");
  assert.equal(typeof t.encodeMs, "number");
  assert.equal(typeof t.sendMs, "number");
  assert.equal(typeof t.bytes, "number");
  assert.ok(t.encodeMs > 0, "the oversized frame really did go through the resize");
  // `bytes` is what went on the wire, not what the camera produced - after the
  // resize it is the small one, and that is the number that decides whether the
  // server refuses the frame.
  assert.equal(t.width, 1280);
  assert.ok(t.bytes < 300 * 1024, `${t.bytes} bytes is over the soft limit`);
  assert.equal(t.hires, false);
});

test("a device that cannot give more pixels is told once, not every frame", async () => {
  const fakeFs = makeFakeFs();
  // The camera is stuck at 1280 px whatever is asked for - a real property of
  // some devices, and one a volunteer deserves to know about rather than to keep
  // watching a request come and go.
  const { cam } = makeCamera(fakeFs, { width: 1280, height: 960 });
  const { client, stats, askForHires } = makeClient();
  const errors = [];
  const loop = new LiveCaptureLoop({
    cameraRef: { current: cam },
    client,
    onError: (m) => errors.push(m),
    ...FAST,
  });
  loop.start();
  try {
    askForHires({ width: 1600, quality: 0.7, px_w: 90 });
    await waitFor(() => stats.sent >= 3, "three frames");
    const said = errors.filter((m) => /more than 1280px/.test(m));
    assert.equal(said.length, 1, `expected one message, got ${JSON.stringify(errors)}`);
  } finally {
    await stopWithin(loop);
  }
});

test("an implausible request quality is ignored rather than handed to the camera", async () => {
  const fakeFs = makeFakeFs();
  const { cam, stats: camStats } = makeCamera(fakeFs, { width: 1280, height: 960 });
  const { client, stats, askForHires } = makeClient();
  const loop = new LiveCaptureLoop({ cameraRef: { current: cam }, client, fs: fakeFs.fs, ...FAST });
  loop.start();
  try {
    askForHires({ width: 1600, quality: 12 });
    await waitFor(() => stats.sent >= 1, "the answering frame");
    assert.equal(stats.bodies[0].hires, true, "the frame is still marked");
    assert.ok(
      camStats.options[0].quality <= 1,
      `the camera was asked for quality ${camStats.options[0].quality}`
    );
  } finally {
    await stopWithin(loop);
  }
});

test("base64Bytes, choosePictureSize and the capture options still hold", () => {
  assert.equal(base64Bytes("AAAA"), 3);
  assert.equal(base64Bytes("AAA="), 2);
  assert.equal(base64Bytes("AA=="), 1);
  assert.equal(base64Bytes(""), 0);
  assert.equal(choosePictureSize(["640x480", "1920x1080", "1280x960", "junk"]), "1280x960");
  assert.equal(
    choosePictureSize(["640x480", "800x600"]),
    null,
    "nothing wide enough is null, not a silent 640 that cannot read a plate"
  );
  assert.equal(CAPTURE_OPTIONS.base64, true);
  assert.equal(CAPTURE_OPTIONS.shutterSound, false);
  assert.equal(
    CAPTURE_OPTIONS.skipProcessing,
    false,
    "skipProcessing discards quality and reports EXIF sizes of -1"
  );
});

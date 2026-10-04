import { useEffect, useRef, useState } from "react";
import { AppState, Vibration } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { manipulateAsync, SaveFormat } from "expo-image-manipulator";
import { API_BASE, frameToBase64, healthCheck, normalizePlate, registerDevice, reportSighting, scanImage, scanTimeoutMs } from "../services/api";
import { getLocation } from "../services/location";
import ScannerScreenView from "../components/ScannerScreenNew";
import { LiveScanClient } from "../services/liveScan";

const DEVICE_KEY = "rakshak_device_id";
const SIM_PLATES = ["TS09AB1234", "MH12JK4567"];
// ~1280 px wide at JPEG 0.8 keeps a frame near 120-250 KB. The detector finds
// the plate in the whole frame, so there is no reason to ship a 12 MP original
// over the LAN, and base64 encoding would inflate it by another 4/3.
const MAX_UPLOAD_WIDTH = 1280;
const CAPTURE_QUALITY = 0.8;
const VOTE_FRAMES = 3;
const VOTE_INTERVAL_MS = 350;
const EARLY_EXIT_CONFIDENCE = 0.85;

// Live-path frame budget.
//
// The server refuses frames over 300 KB of decoded JPEG. The auto-detect loop used
// to call takePictureAsync with no resize at all, so a 12 MP sensor produced
// ~500 KB even at quality 0.3 and *every* live frame was rejected — which looked
// exactly like "detection does not work". The manual snap path was unaffected
// because prepareFrame() resizes, which is why /scanner/scan kept working while
// live detection silently did not.
const LIVE_FRAME_BUDGET_BYTES = 260 * 1024; // headroom under the server's 300 KB
const LIVE_QUALITY_LADDER = [0.7, 0.55, 0.4, 0.3];
const LIVE_WIDTH_LADDER = [1280, 960, 800];
const LIVE_MIN_INTERVAL_MS = 120;

/** Decoded JPEG size of a bare base64 payload, without decoding it. */
function base64Bytes(b64) {
  const padding = b64.endsWith("==") ? 2 : b64.endsWith("=") ? 1 : 0;
  return Math.floor((b64.length * 3) / 4) - padding;
}

/**
 * Map an exception to a distinct, honest state.
 *
 * Every branch keeps the underlying reason. The old version collapsed anything
 * unmatched into the same "Cannot reach backend" text, which hid the actual
 * cause behind a generic network message and made a capture fault look like a
 * firewall problem.
 */
function mapErrorToState(e) {
  const msg = String((e && e.message) || e || "unknown error");

  // Camera-side faults happen before any request leaves the phone.
  if (/camera is not ready/i.test(msg)) {
    return { state: "camera", message: "Camera not ready — wait for the preview, then scan." };
  }
  if (/did not return an image/i.test(msg)) {
    return { state: "camera", message: "Camera returned no image — hold steady and try again." };
  }
  if (/capture failed/i.test(msg)) {
    return { state: "camera", message: `Could not capture photo: ${msg.replace(/^.*?:\s*/, "")}` };
  }
  if (/could not prepare image/i.test(msg)) {
    return { state: "camera", message: `Could not prepare image: ${msg.replace(/^.*?:\s*/, "")}` };
  }

  // Session / auth.
  if (/HTTP 401/i.test(msg)) {
    return { state: "session", message: "Session expired, log in again." };
  }
  if (/HTTP 403/i.test(msg)) {
    return { state: "forbidden", message: "Not allowed — this account cannot file sightings." };
  }

  // Server answered with a status: report it verbatim, detail included.
  const m = msg.match(/HTTP (\d{3})/);
  if (m) {
    const detail = msg.slice(msg.indexOf(":") + 1).trim();
    return { state: "server", message: `Server error ${m[1]}: ${detail || "no detail returned"}` };
  }

  // Timeout is not a network fault; say which call ran out of time.
  if (/timeout after/i.test(msg)) {
    return { state: "slow", message: "Scan timed out, try again." };
  }

  // Thrown while BUILDING the request, so nothing was sent. Reporting this as a
  // network failure is what made the FormDataPart bug look like a dead backend.
  if (/request error/i.test(msg)) {
    const reason = msg.replace(/^.*?Request error:\s*/, "");
    return { state: "request", message: `Request error: ${reason}` };
  }

  // Genuine transport failure: the request went out and the link failed.
  if (/network error/i.test(msg)) {
    const reason = (msg.match(/Reason=([^|]*)/) || [, "unknown"])[1].trim();
    return { state: "network", message: `Network error: ${reason} (${API_BASE})` };
  }

  // Anything else keeps its own text rather than borrowing the network message.
  return { state: "error", message: msg };
}

/**
 * ScannerScreen owns all I/O: camera frames, AI inference calls, device
 * registration, location and vibration. It renders the presentational view so
 * the UI is memoised and the camera does not re-render on every log change.
 */
export default function ScannerScreen({ user, onLogout }) {
  const [deviceReady, setDeviceReady] = useState(false);
  const [deviceId, setDeviceId] = useState(null);
  // react-navigation is not installed, so focus is tracked through AppState:
  // the camera must not run inference while the app sits in the background.
  const [focused, setFocused] = useState(true);
  const processing = useRef(false);

  const [autoDetect, setAutoDetect] = useState(true);
  const [boxes, setBoxes] = useState(null);
  const [liveScanState, setLiveScanState] = useState({ status: 'OFFLINE', fps: 0, ms: 0 });
  const liveScanRef = useRef(null);
  const camRef = useRef(null);
  const frameCount = useRef(0);
  const lastFpsTime = useRef(Date.now());
  const onAutoResultRef = useRef(null);
  // Hot-list match banner: set from the plate reply, cleared after a few seconds.
  const [stolenPlate, setStolenPlate] = useState(null);
  const stolenTimerRef = useRef(null);

  useEffect(() => {
    const sub = AppState.addEventListener("change", (state) => {
      setFocused(state === "active");
    });
    return () => sub?.remove?.();
  }, []);

  useEffect(() => {
    if (!deviceReady || !focused) return;
    
    const client = new LiveScanClient({
      onStateChange: (status) => setLiveScanState(s => ({...s, status})),
      onBoxes: (data) => {
        setBoxes(data);
        setLiveScanState(s => ({...s, ms: data.ms}));
      },
      onPlate: (data) => {
        if (data.plate_id && data.text && data.valid && data.conf >= 0.6) {
           const result = {
             state: "success",
             plate: data.norm,
             confidence: data.conf,
             hotlist: data.stolen,
             raw: data.text,
             normalized: data.norm,
             valid: true,
             frames: 1,
             message: data.stolen ? "Reported to police — active hotlist match." : "Sighting recorded — not on the hotlist.",
           };
           if (data.stolen) {
             try { Vibration.vibrate([40, 120, 80]); } catch {}
             // The banner stays up for a few seconds rather than flashing past:
             // the phone is held at arm's length from the car being checked.
             setStolenPlate(data.norm);
             if (stolenTimerRef.current) clearTimeout(stolenTimerRef.current);
             stolenTimerRef.current = setTimeout(() => setStolenPlate(null), 6000);
           }
           if (onAutoResultRef.current) onAutoResultRef.current(result);
        }
      },
      // Errors go to the on-screen status too, not just the console. A frame
      // rejected server-side used to be invisible in the UI, so the scanner looked
      // broken with no indication of why.
      onError: (err) => {
        console.log("LiveScan error:", err);
        setLiveScanState(s => ({ ...s, status: `error: ${err?.message || err}` }));
      }
    });
    client.connect();
    liveScanRef.current = client;
    
    return () => {
      client.disconnect();
      liveScanRef.current = null;
      if (stolenTimerRef.current) clearTimeout(stolenTimerRef.current);
      setStolenPlate(null);
    };
  }, [deviceReady, focused]);

  useEffect(() => {
    let active = true;
    const loop = async () => {
      let lastSentAt = 0;
      while (active) {
        if (autoDetect && focused && camRef.current && liveScanRef.current?.ready) {
          try {
            // No base64 here on purpose: takePictureAsync base64-encodes the FULL
            // sensor image, so we ask for the uri and downscale before reading it.
            const photo = await camRef.current.takePictureAsync({
              quality: 0.7,
              skipProcessing: true,
              exif: false,
            });
            if (photo && photo.uri && active && autoDetect) {
              const payload = await prepareLiveFrame(photo);
              if (payload && active && autoDetect) {
                const loc = await getLocation().catch(() => ({}));
                liveScanRef.current.sendFrame(
                  payload.width, payload.height,
                  loc.latitude || 0, loc.longitude || 0,
                  payload.base64,
                );

                frameCount.current += 1;
                const now = Date.now();
                if (now - lastFpsTime.current > 1000) {
                  setLiveScanState(s => ({ ...s, fps: frameCount.current }));
                  frameCount.current = 0;
                  lastFpsTime.current = now;
                }
              }
            }
          } catch (e) {
            // A single bad capture must not kill the loop.
            if (e?.message) console.log("LiveScan capture:", e.message);
          }
        }
        // Pace the loop. Without this the camera is driven flat out, which starves
        // the JS thread and drops the location fix that every frame rides along on.
        const since = Date.now() - lastSentAt;
        if (since < LIVE_MIN_INTERVAL_MS) {
          await new Promise((r) => setTimeout(r, LIVE_MIN_INTERVAL_MS - since));
        }
        lastSentAt = Date.now();
      }
    };
    loop();
    return () => { active = false; };
  }, [autoDetect, focused, deviceReady]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      let id = await AsyncStorage.getItem(DEVICE_KEY);
      if (!id) {
        try {
          const dev = await registerDevice(`Scanner-${Date.now() % 10000}`);
          id = dev.id;
          await AsyncStorage.setItem(DEVICE_KEY, id);
        } catch (e) {
          // If registration fails, the view will show a network banner via
          // onTestBackend/onScan; still allow the user to test.
        }
      }
      if (!cancelled) {
        setDeviceId(id);
        setDeviceReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * Resize to MAX_UPLOAD_WIDTH, compress, and return bare base64.
   *
   * The resize step also normalises EXIF rotation, which matters because a
   * portrait phone frame arrives rotated and OCR would then read garbage.
   * The WHOLE frame is sent: the backend detector finds the plate itself, so no
   * crop of the on-screen guide rectangle is applied.
   */
  async function prepareFrame(photo) {
    const uri = photo?.uri;
    if (!uri) throw new Error("Camera did not return an image");
    try {
      const actions = photo.width && photo.width > MAX_UPLOAD_WIDTH ? [{ resize: { width: MAX_UPLOAD_WIDTH } }] : [];
      const result = await manipulateAsync(uri, actions, { compress: CAPTURE_QUALITY, format: SaveFormat.JPEG });
      return await frameToBase64(result?.uri || uri);
    } catch (error) {
      // prepareFrame's own errors already say "Could not prepare image"; do not
      // relabel a manipulator fault as a read fault.
      throw String(error?.message || "").startsWith("Could not prepare image")
        ? error
        : new Error(`Could not prepare image: ${error?.message || error}`);
    }
  }

  /**
   * Resize + compress a live frame until it fits the server's frame budget.
   *
   * Walks a width ladder and, within each width, a JPEG quality ladder, returning
   * the first combination that lands under LIVE_FRAME_BUDGET_BYTES. Measuring the
   * real payload beats guessing a fixed quality, because the same setting yields
   * very different sizes depending on scene content — a frame full of sky and
   * tarmac compresses far past the budget, a cluttered street does not.
   *
   * If nothing fits, the smallest attempt is returned rather than dropped, so a
   * marginal frame still gets a chance instead of vanishing silently.
   */
  async function prepareLiveFrame(photo) {
    const uri = photo?.uri;
    if (!uri) throw new Error("Camera did not return an image");

    let smallest = null;
    try {
      for (const width of LIVE_WIDTH_LADDER) {
        for (const quality of LIVE_QUALITY_LADDER) {
          const resized =
            photo.width && photo.width > width
              ? await manipulateAsync(uri, [{ resize: { width } }], { compress: 1, format: SaveFormat.JPEG })
              : null;
          const target = resized?.uri || uri;
          const result = await manipulateAsync(
            target,
            [],
            { compress: quality, format: SaveFormat.JPEG },
          );
          const base64 = await frameToBase64(result?.uri || target);
          const bytes = base64Bytes(base64);
          const candidate = { width: result?.width || width, height: result?.height || photo.height, base64, bytes };

          if (!smallest || bytes < smallest.bytes) smallest = candidate;
          if (bytes <= LIVE_FRAME_BUDGET_BYTES) return candidate;
        }
      }
      if (smallest) {
        console.log(
          `[RAKSHAK LIVE] frame ${Math.round(smallest.bytes / 1024)} KB still over budget after compression`,
        );
      }
      return smallest;
    } catch (error) {
      throw String(error?.message || "").startsWith("Could not prepare image")
        ? error
        : new Error(`Could not prepare image: ${error?.message || error}`);
    }
  }

  async function captureFrames(camRef) {
    const frames = [];
    for (let i = 0; i < VOTE_FRAMES; i += 1) {
      let photo;
      try {
        photo = await camRef.takePictureAsync({ quality: CAPTURE_QUALITY });
      } catch (e) {
        // takePictureAsync rejects for camera-side reasons (viewfinder not
        // mounted, ref released, hardware busy). Say so instead of letting the
        // generic network mapper claim the backend is unreachable.
        throw new Error(`Capture failed: ${e?.message || e}`);
      }
      if (!photo || !photo.uri) {
        throw new Error(i === 0 ? "Camera did not return an image" : `Camera returned no image on frame ${i + 1}`);
      }
      frames.push(await prepareFrame(photo));
      if (i < VOTE_FRAMES - 1) await new Promise((r) => setTimeout(r, VOTE_INTERVAL_MS));
    }
    return frames;
  }

  function voteReads(reads) {
    const usable = reads.filter((r) => r && r.plate);
    if (!usable.length) return null;
    const counts = {};
    for (const r of usable) counts[r.plate] = (counts[r.plate] || 0) + 1;
    const score = (r) => [
      counts[r.plate],
      r.valid ? 1 : 0,
      r.uncertain ? 0 : 1,
      Number(r.confidence || 0),
    ];
    return usable
      .slice()
      .sort((a, b) => {
        const sa = score(a);
        const sb = score(b);
        for (let i = 0; i < sa.length; i += 1) if (sa[i] !== sb[i]) return sb[i] - sa[i];
        return 0;
      })
      // Attach the tally to the winner. Without this the caller read
      // `best.votes`, which the sorted read object never had, so the activity
      // log always printed an empty/undefined vote count even on a real vote.
      .map((winner) => ({ ...winner, votes: { ...counts } }))[0];
  }

  async function submitSighting(rawPlate, confidence, jitter = 0) {
    const normalized = normalizePlate(rawPlate);
    const loc = await getLocation();
    const lat = (loc.latitude || 17.385) + (Math.random() - 0.5) * jitter;
    const lng = (loc.longitude || 78.4867) + (Math.random() - 0.5) * jitter;
    const payload = {
      plate: normalized,
      latitude: lat,
      longitude: lng,
      timestamp: new Date().toISOString(),
      confidence,
      device_id: deviceId,
    };
    const resp = await reportSighting(payload);
    return { normalized, resp, lat, lng };
  }

  async function onScan(camRef) {
    if (processing.current || !focused) throw new Error("Camera is not ready");
    processing.current = true;
    try {
      const frames = await captureFrames(camRef);
      if (!frames.length) throw new Error("Camera did not return an image");
      const reads = [];
      for (const frame of frames) {
        const res = await scanImage([frame]);
        if (res && res.plate) {
          reads.push(res);
          if (!res.uncertain && Number(res.confidence || 0) >= EARLY_EXIT_CONFIDENCE) break;
        }
      }
      const best = voteReads(reads);
      const raw = String(best?.plate || "");
      const normalized = normalizePlate(raw);
      const confidence = Number(best?.confidence || 0);
      const uncertain = Boolean(best?.uncertain) || !best?.valid || confidence < 0.5;
      if (!raw) return { state: "none", frames: reads.length, message: "No plate found, move closer and hold steady." };
      if (uncertain) {
        return {
          state: "uncertain",
          plate: normalized || raw,
          confidence,
          raw,
          normalized,
          frames: reads.length,
          votes: best?.votes,
          reason: best?.reason || "low confidence",
          elapsedMs: best?.elapsedMs,
          uploadVia: best?.uploadVia,
          message: "Hold the plate inside the frame and scan again.",
        };
      }
      try {
        const s = await submitSighting(raw, confidence, 0);
        const hotlist = !(s.resp.accepted === false || s.resp.detail === "accepted_no_match" || s.resp.note);
        if (hotlist) {
          // expo-haptics is not installed; Vibration ships with React Native.
          try { Vibration.vibrate([40, 120, 80]); } catch {}
        }
        return {
          state: "success",
          plate: s.normalized,
          confidence,
          hotlist,
          // Surfaced for the ANPR DEBUG card; the voting decision above is
          // unchanged.
          raw,
          normalized,
          valid: true,
          frames: reads.length,
          votes: best?.votes,
          elapsedMs: best?.elapsedMs,
          uploadVia: best?.uploadVia,
          message: hotlist ? "Reported to police — active hotlist match." : "Sighting recorded — not on the hotlist.",
        };
      } catch (e) {
        throw e;
      }
    } catch (e) {
      return mapErrorToState(e);
    } finally {
      processing.current = false;
    }
  }

  async function onSimulate() {
    const plate = SIM_PLATES[Math.floor(Math.random() * SIM_PLATES.length)];
    const jitter = 0.012;
    try {
      const s = await submitSighting(plate, 0.97, jitter);
      const hotlist = !(s.resp.accepted === false || s.resp.detail === "accepted_no_match" || s.resp.note);
      if (hotlist) {
        try { Vibration.vibrate([40, 120, 80]); } catch {}
      }
      return {
        state: "success",
        plate: s.normalized,
        confidence: 0.97,
        hotlist,
        raw: plate,
        normalized: s.normalized,
        valid: true,
        frames: 0,
        votes: { [plate]: 1 },
        message: hotlist ? "Simulated hotlist match — reported to police." : "Simulated sighting recorded.",
      };
    } catch (e) {
      return mapErrorToState(e);
    }
  }

  async function onTestBackend() {
    try {
      const status = await healthCheck();
      const ok = status?.status === "ok" || status?.status === "degraded";
      return { status: ok ? "CONNECTED" : "FAILED", error: ok ? null : JSON.stringify(status) };
    } catch (e) {
      return { status: "FAILED", error: e.message };
    }
  }

  async function onLogoutClear() {
    await AsyncStorage.removeItem(DEVICE_KEY);
    onLogout();
  }

  return (
    <ScannerScreenView
      user={user}
      deviceReady={deviceReady}
      onScan={onScan}
      onSimulate={onSimulate}
      onTestBackend={onTestBackend}
      onLogout={onLogoutClear}
      liveScanState={liveScanState}
      boxes={boxes}
      stolenPlate={stolenPlate}
      autoDetect={autoDetect}
      onToggleAutoDetect={() => setAutoDetect(!autoDetect)}
      externalCamRef={camRef}
      onAutoResultRef={onAutoResultRef}
    />
  );
}

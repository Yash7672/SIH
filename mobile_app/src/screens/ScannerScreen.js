import { useEffect, useRef, useState } from "react";
import { AppState, Vibration } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { manipulateAsync, SaveFormat } from "expo-image-manipulator";
import { API_BASE, frameToBase64, healthCheck, normalizePlate, registerDevice, reportSighting, scanImage, scanTimeoutMs } from "../services/api";
import { getLocation } from "../services/location";
import ScannerScreenView from "../components/ScannerScreenNew";

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

  useEffect(() => {
    const sub = AppState.addEventListener("change", (state) => {
      setFocused(state === "active");
    });
    return () => sub?.remove?.();
  }, []);

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
    />
  );
}

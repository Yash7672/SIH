import { useEffect, useRef, useState } from "react";
import { AppState, Vibration } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { manipulateAsync, SaveFormat } from "expo-image-manipulator";
import { API_BASE, healthCheck, normalizePlate, registerDevice, reportSighting, scanImage } from "../services/api";
import { getLocation } from "../services/location";
import ScannerScreenView from "../components/ScannerScreenNew";

const DEVICE_KEY = "rakshak_device_id";
const SIM_PLATES = ["TS09AB1234", "MH12JK4567"];
const MAX_UPLOAD_WIDTH = 1600;
const CAPTURE_QUALITY = 0.85;
const VOTE_FRAMES = 3;
const VOTE_INTERVAL_MS = 350;
const EARLY_EXIT_CONFIDENCE = 0.85;

/** Map an exception to the sheet's state and message. */
function mapErrorToState(e) {
  const msg = String((e && e.message) || e || "unknown error");
  if (/timeout after/i.test(msg)) {
    return { state: "network", message: "Server took too long — first scan can load the AI models, try again." };
  }
  if (/network error/i.test(msg)) {
    return { state: "network", message: `Cannot reach backend at ${API_BASE}. Check Wi-Fi and firewall.` };
  }
  const m = msg.match(/HTTP (\d{3})/);
  if (m) {
    if (m[1] === "413") return { state: "server", message: "Photo too large — the scanner resized it, try again." };
    if (m[1] === "401") return { state: "session", message: "Session expired — log in again." };
    if (m[1] === "403") return { state: "forbidden", message: "Not allowed — this account cannot file sightings." };
    return { state: "server", message: `Server error ${m[1]}.` };
  }
  return { state: "network", message: msg };
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

  async function shrinkForUpload(photo) {
    const uri = photo?.uri;
    if (!uri) return uri;
    try {
      const actions = photo.width && photo.width > MAX_UPLOAD_WIDTH ? [{ resize: { width: MAX_UPLOAD_WIDTH } }] : [];
      const result = await manipulateAsync(uri, actions, { compress: CAPTURE_QUALITY, format: SaveFormat.JPEG });
      return result?.uri || uri;
    } catch {
      return uri;
    }
  }

  async function captureFrames(camRef) {
    const uris = [];
    for (let i = 0; i < VOTE_FRAMES; i += 1) {
      const photo = await camRef.takePictureAsync({ quality: CAPTURE_QUALITY });
      if (!photo || !photo.uri) break;
      uris.push(await shrinkForUpload(photo));
      if (i < VOTE_FRAMES - 1) await new Promise((r) => setTimeout(r, VOTE_INTERVAL_MS));
    }
    return uris;
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
      })[0];
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
      const uris = await captureFrames(camRef);
      if (!uris.length) throw new Error("Camera did not return an image");
      const reads = [];
      for (const uri of uris) {
        const res = await scanImage(uri);
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
      if (!raw) return { state: "none", message: "No plate detected — move closer and hold steady." };
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

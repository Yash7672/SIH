import { useCallback, useEffect, useRef, useState } from "react";
import { AppState, Vibration } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { normalizePlate, registerDevice, reportSighting, healthCheck } from "../services/api";
import { getLocation } from "../services/location";
import ScannerScreenView from "../components/ScannerScreenNew";
import { LiveScanClient, socketStatusLabel } from "../services/liveScan";
import { LiveCaptureLoop, choosePictureSize } from "../services/liveCapture";
import { liveOverlayStore } from "../components/liveOverlayStore";
import { CameraView } from "expo-camera";

const DEVICE_KEY = "rakshak_device_id";
const SIM_PLATES = ["TS09AB1234", "MH12JK4567"];

// A read below this is shown but never treated as an identification. The server
// does its own validation; this is the UI's own line in the sand.
const MIN_TRUSTED_CONFIDENCE = 0.6;
// How long the stolen banner stays up after the last matching read. Long enough
// for a volunteer to look up from the road and read it.
const STOLEN_HOLD_MS = 8000;
const LOG_LIMIT = 12;

function formatClock(d) {
  const h = String(d.getHours()).padStart(2, "0");
  const m = String(d.getMinutes()).padStart(2, "0");
  const s = String(d.getSeconds()).padStart(2, "0");
  return `${h}:${m}:${s}`;
}

/**
 * ScannerScreen owns all I/O: the socket, the camera capture loop, device
 * registration, location and vibration. It renders the presentational view so
 * the UI is memoised and the camera does not re-render on every log change.
 *
 * Detection is continuous. There is no scan button and no single-shot path: the
 * only thing this screen does is keep one frame in flight at a time and turn the
 * server's answers into geometry (via the overlay store, which the camera
 * subtree does not subscribe to) and into a short event log.
 */
export default function ScannerScreen({ user, onLogout }) {
  const [deviceReady, setDeviceReady] = useState(false);
  const [deviceId, setDeviceId] = useState(null);
  // react-navigation is not installed, so focus is tracked through AppState:
  // the camera must not stream while the app sits in the background.
  const [focused, setFocused] = useState(true);

  const [streaming, setStreaming] = useState(true);
  const [liveScan, setLiveScan] = useState({ state: "OFFLINE", fps: 0, serverMs: 0, roundTripMs: 0 });
  const [stalePlate, setStalePlate] = useState(null);
  const [cameraFault, setCameraFault] = useState(null);
  const [cameraActive, setCameraActive] = useState(false);
  const [pictureSize, setPictureSize] = useState(undefined);
  const [log, setLog] = useState([]);

  const camRef = useRef(null);
  const clientRef = useRef(null);
  const loopRef = useRef(null);
  const stolenTimer = useRef(null);

  const appendLog = useCallback((line) => {
    setLog((prev) => [line, ...prev].slice(0, LOG_LIMIT));
  }, []);

  useEffect(() => {
    const sub = AppState.addEventListener("change", (state) => setFocused(state === "active"));
    return () => sub?.remove?.();
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      // Every await in here is guarded: an IIFE that rejects with nobody
      // listening is exactly the "Uncaught (in promise)" noise this screen
      // used to produce.
      let id = null;
      try {
        id = await AsyncStorage.getItem(DEVICE_KEY);
      } catch (e) {
        id = null;
      }
      if (!id) {
        try {
          const dev = await registerDevice(`Scanner-${Date.now() % 10000}`);
          id = dev.id;
          await AsyncStorage.setItem(DEVICE_KEY, id);
        } catch (e) {
          // Registration failing is not fatal: the socket will still connect and
          // the health check will say so on screen.
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

  // Pick the smallest picture size the detector can still read. A 12 MP sensor
  // otherwise hands the loop a ~4 MB frame to compress down to ~45 KB.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const sizes = await CameraView.getAvailablePictureSizesAsync();
        if (cancelled) return;
        const chosen = choosePictureSize(sizes);
        if (chosen) setPictureSize(chosen);
      } catch (e) {
        // Not every platform/device reports sizes; the default is fine.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // ---- the socket -------------------------------------------------------- //
  useEffect(() => {
    if (!deviceReady) return undefined;

    const client = new LiveScanClient({
      onStateChange: (status) => {
        setLiveScan((s) => ({ ...s, state: socketStatusLabel(status) }));
        if (status === "OFFLINE") {
          // Nothing tracked across a reconnect: the cars in the new stream are
          // different cars, and keeping the old boxes would point at empty road.
          liveOverlayStore.clear();
          setStalePlate(null);
          setLiveScan((s) => ({ ...s, fps: 0, serverMs: 0, roundTripMs: 0 }));
        }
      },
      onBoxes: (data) => {
        liveOverlayStore.applyBoxes(data);
        setLiveScan((s) => ({ ...s, serverMs: data.ms ?? 0 }));
      },
      onPlates: (data) => {
        liveOverlayStore.applyPlates(data);
      },
      onPlate: (data) => {
        liveOverlayStore.applyPlateText(data);
        const conf = Math.round((data.conf || 0) * 100);
        const valid = Boolean(data.valid);

        // Only a valid, confident read is allowed to raise an alert. A garbled
        // low-confidence read is shown and otherwise ignored - the alternative is
        // a red banner naming a plate nobody can read.
        if (!valid || !data.text || (data.conf || 0) < MIN_TRUSTED_CONFIDENCE) {
          appendLog(
            `${data.label || "vehicle"}: unreadable${data.text ? ` (${data.text} ${conf}%)` : ""}`
          );
          return;
        }

        const plate = {
          plate: data.norm || normalizePlate(data.text),
          confidence: conf,
          stolen: Boolean(data.stolen),
          at: Date.now(),
        };
        setStalePlate(plate);
        appendLog(
          `${formatClock(new Date())}  ${plate.plate}  ${conf}%${plate.stolen ? "  HOTLIST MATCH" : ""}`
        );

        if (plate.stolen) {
          try {
            Vibration.vibrate([40, 120, 80]);
          } catch {}
          if (stolenTimer.current) clearTimeout(stolenTimer.current);
          stolenTimer.current = setTimeout(() => setStalePlate(null), STOLEN_HOLD_MS);
        }
      },
      onRoundTrip: (roundTripMs) => {
        setLiveScan((s) => ({ ...s, roundTripMs }));
      },
      // Errors reach the screen, not just the console: a frame rejected
      // server-side used to be invisible, so the scanner looked broken with no
      // indication of why.
      onError: (message, code) => {
        const text = typeof message === "string" ? message : message?.message || String(message);
        if (code === "size_limit") {
          setCameraFault("Frame too large for the server — try again.");
        } else if (code === "missing_model") {
          setCameraFault("Server has no vehicle model — restart the backend.");
        } else if (code === "bad_frame") {
          setCameraFault("A frame arrived corrupted — try again.");
        }
        appendLog(`error: ${text}`);
      },
      onSessionExpired: () => {
        setLiveScan({ state: "OFFLINE", fps: 0, serverMs: 0, roundTripMs: 0 });
        appendLog("session expired — log in again");
        onLogout();
      },
    });
    client.setDeviceId(deviceId);
    // connect() is async and constructs the socket; a malformed base URL would
    // reject here with nobody listening, so it is settled explicitly.
    Promise.resolve(client.connect()).catch(() => {});
    clientRef.current = client;

    return () => {
      client.disconnect();
      clientRef.current = null;
      liveOverlayStore.clear();
      if (stolenTimer.current) clearTimeout(stolenTimer.current);
    };
  }, [deviceReady, deviceId, onLogout, appendLog]);

  // ---- the capture loop -------------------------------------------------- //
  // Started and stopped rather than toggled per frame: there must never be two
  // loops, and there must never be two captures inside one.
  useEffect(() => {
    if (!deviceReady || !streaming || !focused) return undefined;
    const loop = new LiveCaptureLoop({
      cameraRef: camRef,
      client: clientRef.current,
      onStats: ({ fps, roundTripMs }) => {
        setLiveScan((s) => ({ ...s, fps, roundTripMs }));
      },
      onPause: () => {
        // The socket is not ready yet, or dropped. That is not an error, but it
        // is worth saying on screen rather than showing a stalled feed.
        setCameraActive(false);
      },
      onError: (message) => {
        setCameraFault(message);
      },
      // The loop only calls this once it has produced a frame again, so the
      // "Camera error, retrying" notice disappears the moment the camera is
      // healthy instead of hanging around until the next unrelated fault.
      onRecover: () => {
        setCameraFault(null);
      },
    });
    loopRef.current = loop;
    loop.start();
    setCameraActive(true);
    setCameraFault(null);

    return () => {
      // React does not await an effect cleanup, so the stop promise is settled
      // here rather than left to surface as an unhandled rejection. The loop
      // itself chains a start() that lands mid-teardown onto this one, so the
      // "never two loops" guarantee holds even if focus flaps.
      const stopping = loop.stop();
      loopRef.current = null;
      setCameraActive(false);
      Promise.resolve(stopping).catch(() => {});
    };
  }, [deviceReady, streaming, focused]);

  const onToggleStreaming = useCallback(() => setStreaming((v) => !v), []);

  async function onSimulate() {
    const plate = SIM_PLATES[Math.floor(Math.random() * SIM_PLATES.length)];
    // A little jitter so repeated taps do not stack sightings on one exact point
    // on the map.
    const jitter = 0.012;
    const loc = await getLocationSafe();
    const resp = await reportSighting({
      plate,
      latitude: loc.latitude + (Math.random() - 0.5) * jitter,
      longitude: loc.longitude + (Math.random() - 0.5) * jitter,
      timestamp: new Date().toISOString(),
      confidence: 0.97,
      device_id: deviceId,
    });
    // "accepted" is not the same as "matched": the API accepts every sighting
    // and reports a match separately, and treating a plain acceptance as a
    // hotlist hit made the button lie about what it found.
    const hotlist = !(resp.accepted === false || resp.detail === "accepted_no_match" || resp.note);
    if (hotlist) {
      try {
        Vibration.vibrate([40, 120, 80]);
      } catch {}
    }
    return { plate, hotlist };
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

  // React Native does not await `onPress`, so this handler must be unable to
  // reject: losing the logout over a failed key removal would be a bad trade.
  const onLogoutClear = useCallback(() => {
    Promise.resolve(AsyncStorage.removeItem(DEVICE_KEY))
      .catch(() => {})
      .then(() => onLogout());
  }, [onLogout]);

  return (
    <ScannerScreenView
      user={user}
      deviceReady={deviceReady}
      onSimulate={onSimulate}
      onTestBackend={onTestBackend}
      onLogout={onLogoutClear}
      liveScan={liveScan}
      stalePlate={stalePlate}
      streaming={streaming}
      onToggleStreaming={onToggleStreaming}
      externalCamRef={camRef}
      cameraActive={cameraActive}
      cameraFault={cameraFault}
      pictureSize={pictureSize}
    />
  );
}

// The simulate path needs coordinates to file a sighting, but a location fault
// must not stop the button working: the demo hotspot fix is good enough.
async function getLocationSafe() {
  try {
    const fix = await getLocation();
    return { latitude: fix.latitude || 17.44, longitude: fix.longitude || 78.35 };
  } catch (e) {
    // The demo hotspot fix: the button has to work on a phone with location
    // switched off.
    return { latitude: 17.44, longitude: 78.35 };
  }
}
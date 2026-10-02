import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AppState,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  Vibration,
  View,
  useWindowDimensions,
} from "react-native";
import { CameraView, useCameraPermissions } from "expo-camera";
import { StatusBar } from "expo-status-bar";
// Import the single icon set directly: the package root pulls in all six font
// families (~2.2 MB of assets) even though the scanner only uses Ionicons.
import Ionicons from "@expo/vector-icons/Ionicons";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { ScannerGuideFrame } from "./ScannerGuideFrame";
import Button from "./ui/Button";
import { Card } from "./ui/Card";
import { LoadingState } from "./ui/Banner";
import { fontSize, fontWeight, radius, spacing } from "../theme";
import { useTheme } from "../theme/ThemeContext";

/**
 * The scanner is deliberately always dark in both themes: a bright chrome next
 * to the viewfinder makes the plate the least readable thing on screen, and the
 * volunteer is holding the phone outdoors. Only the chrome tints follow the
 * palette.
 *
 * Everything lives on ONE scrolling page - debug card, actions, backend check,
 * last detection and the activity log - so nothing useful hides behind a menu.
 *
 * No watermark over the viewfinder: text on top of the plate preview lowers
 * plate-read accuracy.
 */

const LOG_LIMIT = 20;
const HEALTH_INTERVAL_MS = 30000;
const CAMERA_HEIGHT_RATIO = 0.4;
// The 40% share is too small to aim with on a short phone, so the preview never
// drops below this. 360x640 would otherwise get a 256 px strip.
const MIN_CAMERA_HEIGHT = 260;

export default function ScannerScreen({ onScan, onSimulate, onTestBackend, onLogout, deviceReady, user }) {
  const { colors, isDark, setMode } = useTheme();
  const insets = useSafeAreaInsets();
  const [perm, requestPerm] = useCameraPermissions();
  const camRef = useRef(null);
  const [camReady, setCamReady] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [testing, setTesting] = useState(false);
  const [backendState, setBackendState] = useState({ status: "CHECKING", error: null });
  const [result, setResult] = useState(null);
  const [log, setLog] = useState([]);
  const [showDebug, setShowDebug] = useState(true);
  // react-navigation is not installed, so focus comes from AppState: the camera
  // must not run inference while the app sits in the background.
  const [focused, setFocused] = useState(true);
  const { height } = useWindowDimensions();

  const s = useMemo(() => makeStyles(colors), [colors]);

  const appendLog = useCallback((line) => {
    setLog((prev) => [line, ...prev].slice(0, LOG_LIMIT));
  }, []);

  useEffect(() => {
    const sub = AppState.addEventListener("change", (state) => setFocused(state === "active"));
    return () => sub?.remove?.();
  }, []);

  // Losing focus invalidates the camera handle: the preview unmounts, so a
  // stale "ready" flag would let a scan press fire against a dead ref.
  useEffect(() => {
    if (!focused) setCamReady(false);
  }, [focused]);

  // Backend health: first call on mount, again on every focus, then every 30 s
  // while focused and in the foreground. The interval is torn down on blur and
  // on unmount so a backgrounded app never polls.
  const runHealth = useCallback(
    async (logIt) => {
      if (logIt) setTesting(true);
      setBackendState((prev) => (prev.status === "CONNECTED" ? prev : { status: "CHECKING", error: null }));
      try {
        const state = await onTestBackend();
        const next = { status: state.status === "CONNECTED" ? "CONNECTED" : "OFFLINE", error: state.error || null };
        setBackendState(next);
        if (logIt) appendLog(`backend health -> ${next.status}${next.error ? ` ${next.error}` : ""}`);
      } catch (e) {
        setBackendState({ status: "OFFLINE", error: e.message });
        if (logIt) appendLog(`backend health -> OFFLINE ${e.message}`);
      } finally {
        if (logIt) setTesting(false);
      }
    },
    [onTestBackend, appendLog]
  );

  useEffect(() => {
    if (!focused) return undefined;
    runHealth(false);
    const id = setInterval(() => runHealth(false), HEALTH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [focused, runHealth]);

  const handleTestBackend = useCallback(() => runHealth(true), [runHealth]);

  const capture = useCallback(async () => {
    if (scanning || !camRef.current || !camReady || !focused) return;
    setScanning(true);
    setResult(null);
    const startedAt = Date.now();
    try {
      const scanResult = await onScan(camRef.current);
      const totalMs = Date.now() - startedAt;
      setResult(scanResult);
      const pct = Math.round((scanResult.confidence || 0) * 100);
      const ok = scanResult.state === "success" || scanResult.state === "uncertain" || scanResult.state === "none";
      // The status pill belongs to the health check alone, never to one scan. A
      // connection-level scan failure is confirmed with an immediate re-check:
      // only if that also fails does the backend count as unreachable.
      if (scanResult.state === "network") {
        appendLog("scan: connection failed - confirming with a health check");
        await runHealth(false);
      }
      appendLog(
        `scan: ${scanResult.plate || "no plate"} ${pct}% frames=${scanResult.frames ?? 0} votes={${formatVotes(scanResult.votes)}} ` +
          `state=${scanResult.state} ` +
          `${scanResult.elapsedMs ? `${scanResult.elapsedMs}ms via=${scanResult.uploadVia}` : `${totalMs}ms`}` +
          `${scanResult.message && !ok ? ` | ${scanResult.message}` : ""}`
      );
      if (scanResult.hotlist) {
        try {
          Vibration.vibrate([40, 120, 80]);
        } catch {}
      }
    } catch (e) {
      setResult({ state: "error", message: e.message });
      appendLog(`scan FAILED total=${Date.now() - startedAt}ms | ${e.message}`);
    } finally {
      setScanning(false);
    }
  }, [scanning, camReady, focused, onScan, appendLog, runHealth]);

  const simulate = useCallback(async () => {
    try {
      const r = await onSimulate();
      setResult(r);
      appendLog(`simulate: ${r.plate} votes={${formatVotes(r.votes)}}`);
      if (r.hotlist) {
        try {
          Vibration.vibrate([40, 120, 80]);
        } catch {}
      }
    } catch (e) {
      setResult({ state: "network", message: e.message });
      appendLog(`error: ${e.message}`);
    }
  }, [onSimulate, appendLog]);

  if (!deviceReady) return <LoadingState label="Registering device…" />;

  if (!perm?.granted) {
    return (
      <View style={[s.fill, { backgroundColor: colors.surface.bg, paddingTop: insets.top, paddingBottom: insets.bottom }]}>
        <View style={s.permission}>
          <Text style={[s.permissionTitle, { color: colors.surface.text }]}>Camera access needed</Text>
          <Text style={[s.permissionText, { color: colors.surface.muted }]}>
            The scanner needs the camera to read vehicle number plates. No video is uploaded — only a single still
            frame per scan.
          </Text>
          <Button
            label="Grant camera access"
            size="lg"
            appearance="surface"
            onPress={requestPerm}
            icon={<Ionicons name="camera" size={18} color={colors.solid} />}
          />
        </View>
      </View>
    );
  }

  const pill = resultPill(result);
  const cameraHeight = Math.round(Math.max(MIN_CAMERA_HEIGHT, height * CAMERA_HEIGHT_RATIO));

  return (
    <View style={[s.fill, { backgroundColor: colors.dark[900] }]}>
      {/* Edge-to-edge on Android: the header applies insets.top itself. */}
      <StatusBar style="light" translucent />

      <View style={[s.header, { backgroundColor: colors.dark[800], borderBottomColor: colors.dark[600], paddingTop: insets.top + spacing.sm }]}>
        {/* Row 1: title only, so it never has to share width with buttons. */}
        <View style={s.headerRow}>
          <Text style={[s.title, { color: colors.onDark.strong }]} numberOfLines={1}>
            RAKSHAK Scanner
          </Text>
          <View style={s.headerActions}>
            <Pressable
              onPress={() => setMode(isDark ? "light" : "dark")}
              hitSlop={8}
              accessibilityRole="button"
              accessibilityLabel={isDark ? "Switch to light theme" : "Switch to dark theme"}
              style={({ pressed }) => [s.iconBtn, { opacity: pressed ? 0.6 : 1 }]}
            >
              <Ionicons name={isDark ? "sunny" : "moon"} size={19} color={colors.onDark.body} />
            </Pressable>
            <Pressable
              onPress={onLogout}
              hitSlop={8}
              accessibilityRole="button"
              accessibilityLabel="Log out"
              style={({ pressed }) => [s.iconBtn, { opacity: pressed ? 0.6 : 1 }]}
            >
              <Ionicons name="log-out-outline" size={19} color={colors.onDark.body} />
            </Pressable>
          </View>
        </View>

        {/* Row 2: who is scanning, and whether the backend is answering. */}
        <View style={s.headerRow}>
          <Text style={[s.subtitle, { color: colors.onDark.muted }]} numberOfLines={1}>
            {user?.name} · {String(user?.role || "VOLUNTEER").toUpperCase()}
          </Text>
          <ConnPill status={backendState.status} s={s} />
        </View>
      </View>

      <CameraSection
        height={cameraHeight}
        colors={colors}
        s={s}
        focused={focused}
        scanning={scanning}
        pill={pill}
        camRef={camRef}
        onCameraReady={() => setCamReady(true)}
      />

      <ScrollView
        style={s.scroll}
        contentContainerStyle={{ paddingBottom: insets.bottom + 24 }}
        keyboardShouldPersistTaps="handled"
      >
        {backendState.status === "OFFLINE" ? (
          <View style={[s.offlineBanner, { backgroundColor: colors.dark[700], borderColor: colors.danger }]}>
            <Ionicons name="warning-outline" size={15} color={colors.danger} />
            <Text style={[s.offlineText, { color: colors.onDark.body }]} numberOfLines={2}>
              Backend unreachable{backendState.error ? `: ${backendState.error}` : ""}
            </Text>
          </View>
        ) : null}

        {/* Scan faults get their own block. It sits inside the scroll content
            with a top margin, so it can never slide under the fixed-height
            camera container the way the old absolutely-positioned banner did. */}
        {isErrorState(result) ? (
          <View style={[s.errorBanner, { backgroundColor: colors.dark[700], borderColor: colors.danger }]}>
            <Ionicons name="alert-circle-outline" size={15} color={colors.danger} />
            <Text style={[s.offlineText, { color: colors.onDark.body }]}>{result.message}</Text>
          </View>
        ) : null}

        <AnprDebugCard result={result} visible={showDebug} onToggle={() => setShowDebug((v) => !v)} colors={colors} s={s} />

        <View style={s.actions}>
          <Button
            label="Scan plate"
            size="lg"
            onPress={capture}
            disabled={!camReady || !focused}
            loading={scanning}
            loadingLabel="Reading plate..."
            style={s.flex}
            icon={<Ionicons name="camera" size={19} color="#FFFFFF" />}
          />
          <Button
            label="Simulate"
            size="lg"
            variant="secondary"
            onPress={simulate}
            disabled={scanning}
            style={s.flex}
            icon={<Ionicons name="dice" size={19} color={colors.onDark.body} />}
          />
        </View>

        <View style={s.backendRow}>
          <Button
            label="TEST BACKEND CONNECTION"
            size="md"
            variant="secondary"
            onPress={handleTestBackend}
            loading={testing}
            loadingLabel="Testing..."
            style={s.flex}
            icon={<Ionicons name="pulse" size={17} color={colors.accent[300]} />}
          />
          <Text
            style={[
              s.backendStatus,
              {
                color:
                  backendState.status === "CONNECTED"
                    ? colors.success
                    : backendState.status === "CHECKING"
                      ? colors.warning
                      : colors.danger,
              },
            ]}
          >
            {backendState.status}
          </Text>
        </View>

        <LastDetectionCard result={result} colors={colors} s={s} />

        <ActivityLog log={log} colors={colors} s={s} />
      </ScrollView>
    </View>
  );
}

function formatVotes(votes) {
  if (!votes || typeof votes !== "object") return "";
  const parts = Object.keys(votes).map((k) => `${k}:${votes[k]}`);
  return parts.length ? parts.join(",") : "";
}

/** Three-state backend pill: CHECKING amber, CONNECTED green, OFFLINE red. */
function ConnPill({ status, s }) {
  const { colors } = useTheme();
  const tone = status === "CONNECTED" ? colors.success : status === "CHECKING" ? colors.warning : colors.danger;
  return (
    <View style={[s.pill, { borderColor: tone }]}>
      <View style={[s.pillDot, { backgroundColor: tone }]} />
      <Text style={[s.pillText, { color: tone }]}>{status}</Text>
    </View>
  );
}

/**
 * Camera preview plus its overlays, memoised so a new activity-log line never
 * re-renders the camera. Only `pill` and `scanning` reach it.
 */
const CameraSection = memo(function CameraSection({ height, colors, s, focused, scanning, pill, camRef, onCameraReady }) {
  // The overlay must be laid out against the preview box, so the box measures
  // itself and hands the result down. `height` is the intended height, used
  // until the first onLayout lands.
  const [box, setBox] = useState({ width: 0, height });
  const measure = useCallback(
    (e) => {
      const { width: w, height: h } = e.nativeEvent.layout;
      if (w > 0 && h > 0 && (w !== box.width || h !== box.height)) setBox({ width: w, height: h });
    },
    [box.width, box.height]
  );

  return (
    <View style={[s.cameraWrap, { height }]} onLayout={measure}>
      {focused ? (
        <CameraView
          ref={camRef}
          style={StyleSheet.absoluteFill}
          facing="back"
          autofocus="on"
          zoom={0}
          onCameraReady={onCameraReady}
        />
      ) : null}

      {pill ? (
        <View style={[s.resultPill, { borderColor: pill.tone, backgroundColor: "rgba(4,8,20,0.66)" }]}>
          <Text style={[s.resultPillText, { color: pill.tone }]} numberOfLines={1}>
            {pill.text}
          </Text>
        </View>
      ) : null}

      {/* Sized from the measured preview box, so the frame can never spill over
          the cards below. */}
      <ScannerGuideFrame
        scanning={scanning}
        hint={null}
        containerWidth={box.width || undefined}
        containerHeight={box.height || height}
      />

      <View style={s.hintStrip} pointerEvents="none">
        <Text style={s.hintStripText} numberOfLines={1}>
          Align plate in the viewfinder, then press Scan
        </Text>
      </View>
    </View>
  );
});

function resultPill(result) {
  if (!result) return null;
  if (result.state === "success" && result.plate) return { tone: "#22C55E", text: result.plate };
  if (result.state === "uncertain") return { tone: "#F59E0B", text: `${result.plate || "?"} · uncertain` };
  if (result.state === "none") return { tone: "#EF4444", text: "NO PLATE" };
  if (result.state === "network") return { tone: "#EF4444", text: "NO CONNECTION" };
  if (result.message) return { tone: "#EF4444", text: result.state === "camera" ? "CAMERA" : "REQUEST FAILED" };
  return null;
}

/** True for states that represent a fault rather than a scan outcome. */
function isErrorState(result) {
  if (!result) return false;
  return ["camera", "network", "request", "server", "slow", "session", "forbidden", "error"].includes(result.state);
}

function DebugRow({ label, value, valueColor, s, colors }) {
  return (
    <View style={s.debugRow}>
      <Text style={[s.debugLabel, { color: colors.onDark.muted }]}>{label}</Text>
      <Text style={[s.debugValue, { color: valueColor || colors.onDark.strong }]} numberOfLines={2}>
        {value}
      </Text>
    </View>
  );
}

function AnprDebugCard({ result, visible, onToggle, colors, s }) {
  const detected = Boolean(result && result.plate);
  const pct = result && result.confidence ? `${Math.round(result.confidence * 100)}%` : "—";
  const yes = (v) => (v ? "YES" : "NO");
  return (
    <Card tone="dark" style={s.block}>
      <View style={s.blockHeader}>
        <Text style={[s.blockTitle, { color: colors.onDark.strong }]}>ANPR DEBUG</Text>
        <Pressable onPress={onToggle} hitSlop={8} accessibilityRole="button" accessibilityLabel="Toggle ANPR debug card">
          <Ionicons name={visible ? "chevron-up" : "chevron-down"} size={17} color={colors.onDark.muted} />
        </Pressable>
      </View>
      {visible ? (
        <View style={s.debugBody}>
          <DebugRow
            label="Plate detected"
            value={yes(detected)}
            valueColor={detected ? colors.success : colors.danger}
            s={s}
            colors={colors}
          />
          <DebugRow label="Raw OCR" value={result?.raw || "—"} s={s} colors={colors} />
          <DebugRow label="Normalized" value={result?.normalized || result?.plate || "—"} s={s} colors={colors} />
          <DebugRow label="Confidence" value={pct} s={s} colors={colors} />
          <DebugRow
            label="Valid plate"
            value={yes(Boolean(result?.valid))}
            valueColor={result?.valid ? colors.success : colors.danger}
            s={s}
            colors={colors}
          />
        </View>
      ) : null}
    </Card>
  );
}

function LastDetectionCard({ result, colors, s }) {
  if (!result) {
    return (
      <Card tone="dark" style={s.block}>
        <Text style={[s.blockTitle, { color: colors.onDark.strong }]}>LAST DETECTION</Text>
        <Text style={[s.emptyText, { color: colors.onDark.muted }]}>No detection yet — press Scan plate.</Text>
      </Card>
    );
  }

  const pct = result.confidence ? `${Math.round(result.confidence * 100)}% confidence` : "";
  const valid = Boolean(result.valid);
  const sub = result.state === "success" ? `${pct} · valid plate` : result.state === "uncertain" ? `${pct} · uncertain, scan again` : result.message || "";

  return (
    <Card tone="dark" style={s.block}>
      <Text style={[s.blockTitle, { color: colors.onDark.strong }]}>LAST DETECTION</Text>
      {result.plate ? (
        <Text style={[s.plateMono, { color: colors.onDark.strong }]} numberOfLines={1} adjustsFontSizeToFit>
          {result.plate}
        </Text>
      ) : null}
      <Text style={[s.detSub, { color: valid ? colors.success : colors.warning }]}>{sub}</Text>

      {result.hotlist ? (
        <View style={[s.hotlist, { backgroundColor: colors.solidDanger, borderColor: colors.danger }]}>
          <Ionicons name="shield" size={16} color="#FFFFFF" />
          <Text style={s.hotlistText}>HOTLIST MATCH, reported to police</Text>
        </View>
      ) : null}

      {!valid && result.state === "none" ? (
        <Text style={[s.detNote, { color: colors.danger }]}>
          No plate detected{result.frames ? ` across ${result.frames} frame(s)` : ""} — move closer and hold steady.
        </Text>
      ) : null}
    </Card>
  );
}

function ActivityLog({ log, colors, s }) {
  return (
    <Card tone="dark" style={s.block}>
      <Text style={[s.blockTitle, { color: colors.onDark.strong }]}>ACTIVITY LOG</Text>
      {log.length === 0 ? (
        <Text style={[s.emptyText, { color: colors.onDark.muted }]}>Nothing yet.</Text>
      ) : (
        <View style={s.logBody}>
          {log.map((line, i) => (
            <Text key={`${i}-${line.slice(0, 24)}`} style={[s.logLine, { color: colors.onDark.muted }]}>
              {line}
            </Text>
          ))}
        </View>
      )}
    </Card>
  );
}

function makeStyles(colors) {
  return StyleSheet.create({
    fill: { flex: 1 },

    header: {
      paddingHorizontal: spacing.lg,
      paddingBottom: spacing.sm,
      borderBottomWidth: 1,
      gap: spacing.xs,
    },
    // Row 1 = title, row 2 = identity + connection. Splitting them is what
    // stops "RAKSHAK Scanner" being truncated by the buttons beside it.
    headerRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: spacing.sm },
    title: { flex: 1, fontSize: fontSize.lg, fontWeight: fontWeight.heavy, letterSpacing: 0.3 },
    subtitle: { flex: 1, fontSize: fontSize.xs },
    headerActions: { flexDirection: "row", alignItems: "center", gap: spacing.xs },
    iconBtn: {
      width: 44,
      height: 44,
      borderRadius: 12,
      alignItems: "center",
      justifyContent: "center",
    },
    pill: {
      flexDirection: "row",
      alignItems: "center",
      gap: 5,
      borderWidth: 1,
      borderRadius: radius.pill,
      paddingHorizontal: spacing.sm,
      paddingVertical: 3,
    },
    pillDot: { width: 7, height: 7, borderRadius: 4 },
    pillText: { fontSize: 10, fontWeight: fontWeight.bold, letterSpacing: 0.4 },

    cameraWrap: { backgroundColor: "#000", width: "100%", overflow: "hidden" },
    resultPill: {
      position: "absolute",
      top: spacing.md,
      alignSelf: "center",
      borderWidth: 2,
      borderRadius: radius.pill,
      paddingHorizontal: spacing.lg,
      paddingVertical: 5,
      zIndex: 2,
    },
    resultPillText: { fontSize: fontSize.base, fontWeight: fontWeight.bold, letterSpacing: 1 },
    hintStrip: {
      position: "absolute",
      left: 0,
      right: 0,
      bottom: 0,
      backgroundColor: "rgba(4,8,20,0.66)",
      paddingVertical: spacing.sm,
      alignItems: "center",
    },
    hintStripText: { color: "#E2E8F0", fontSize: fontSize.sm, fontWeight: fontWeight.medium },

    scroll: { flex: 1 },
    flex: { flex: 1 },

    offlineBanner: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.sm,
      margin: spacing.lg,
      marginBottom: 0,
      padding: spacing.md,
      borderRadius: radius.md,
      borderWidth: 1,
    },
    // Normal flow block: a real top margin, no negative offset, so the banner
    // starts below the camera rather than underneath it.
    errorBanner: {
      flexDirection: "row",
      alignItems: "flex-start",
      gap: spacing.sm,
      marginHorizontal: spacing.lg,
      marginTop: spacing.md,
      padding: spacing.md,
      borderRadius: radius.md,
      borderWidth: 1,
    },
    offlineText: { flex: 1, fontSize: fontSize.sm },

    block: { marginHorizontal: spacing.lg, marginTop: spacing.md },
    blockHeader: { flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
    blockTitle: {
      fontSize: fontSize.sm,
      fontWeight: fontWeight.bold,
      letterSpacing: 1.1,
    },
    emptyText: { marginTop: spacing.sm, fontSize: fontSize.sm },

    debugBody: { marginTop: spacing.md, gap: spacing.xs },
    debugRow: { flexDirection: "row", alignItems: "flex-start", justifyContent: "space-between", gap: spacing.md },
    debugLabel: { fontSize: fontSize.sm },
    debugValue: {
      flex: 1,
      textAlign: "right",
      fontSize: fontSize.sm,
      fontWeight: fontWeight.semibold,
      fontFamily: "monospace",
    },

    actions: { flexDirection: "row", gap: spacing.md, marginHorizontal: spacing.lg, marginTop: spacing.md },

    backendRow: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.md,
      marginHorizontal: spacing.lg,
      marginTop: spacing.md,
    },
    backendStatus: { fontSize: fontSize.sm, fontWeight: fontWeight.bold, letterSpacing: 0.4 },

    plateMono: {
      marginTop: spacing.md,
      fontSize: fontSize.display,
      fontWeight: fontWeight.heavy,
      fontFamily: "monospace",
      letterSpacing: 2,
    },
    detSub: { marginTop: spacing.xs, fontSize: fontSize.sm, fontWeight: fontWeight.semibold },
    detNote: { marginTop: spacing.sm, fontSize: fontSize.sm },
    hotlist: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.sm,
      marginTop: spacing.md,
      padding: spacing.md,
      borderRadius: radius.md,
      borderWidth: 1,
    },
    hotlistText: { color: "#FFFFFF", fontSize: fontSize.sm, fontWeight: fontWeight.bold, letterSpacing: 0.3, flex: 1 },

    logBody: { marginTop: spacing.md, gap: 3 },
    logLine: { fontSize: 12.5, lineHeight: 18, fontFamily: "monospace" },

    permission: { flex: 1, justifyContent: "center", paddingHorizontal: spacing.xl, gap: spacing.lg, alignItems: "center" },
    permissionTitle: { fontSize: fontSize.xl, fontWeight: fontWeight.bold },
    permissionText: { textAlign: "center", lineHeight: 22 },
  });
}

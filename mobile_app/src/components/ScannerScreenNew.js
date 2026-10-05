import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Animated,
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
import { useKeepAwake } from "expo-keep-awake";
import Button from "./ui/Button";
import LiveDetectionOverlay from "./LiveDetectionOverlay";
import { Card } from "./ui/Card";
import { LoadingState } from "./ui/Banner";
import { fontSize, fontWeight, radius, spacing } from "../theme";
import { useTheme } from "../theme/ThemeContext";

/**
 * The live scanner.
 *
 * The camera is the page. It fills the top ~65% of the screen at all times and
 * detection runs by itself - there is no shutter button, because a live feed
 * that waits for a tap is just a camera app with extra steps. Everything the
 * volunteer needs to *interpret* the feed sits in a compact panel underneath:
 * the last plate and its confidence, the stolen banner, a short event log, the
 * connection state and the frame rate.
 *
 * The scanner is deliberately always dark in both themes: a bright chrome next
 * to the viewfinder makes the plate the least readable thing on screen, and the
 * volunteer is holding the phone outdoors. Only the chrome tints follow the
 * palette.
 *
 * No watermark or guide frame over the viewfinder: text on top of the plate
 * preview lowers plate-read accuracy, and live detection already draws real
 * boxes over the feed.
 */

const LOG_LIMIT = 12;
const HEALTH_INTERVAL_MS = 30000;
const CAMERA_HEIGHT_RATIO = 0.65;
// A share this small is impossible to aim with on a short phone, so the preview
// never drops below this. 360x640 would otherwise get a 256 px strip.
const MIN_CAMERA_HEIGHT = 260;

export default function ScannerScreen({
  onSimulate,
  onTestBackend,
  onLogout,
  deviceReady,
  user,
  liveScan,
  stalePlate,
  streaming,
  onToggleStreaming,
  externalCamRef,
  cameraActive,
  cameraFault,
  pictureSize,
}) {
  const { colors, isDark, setMode } = useTheme();
  const insets = useSafeAreaInsets();
  const [perm, requestPerm] = useCameraPermissions();
  const internalCamRef = useRef(null);
  const camRef = externalCamRef || internalCamRef;
  const [testing, setTesting] = useState(false);
  const [backendState, setBackendState] = useState({ status: "CHECKING", error: null });
  const [log, setLog] = useState([]);
  const [focused, setFocused] = useState(true);
  const { height } = useWindowDimensions();

  // Themed stylesheet. Rebuilt only when the palette changes, not per frame: the
  // overlay and the panels below the viewfinder read from this object, and a new
  // object every render would re-render the whole screen several times a second.
  const s = useMemo(() => makeStyles(colors), [colors]);

  // A phone that sleeps mid-scan stops detecting, and nobody notices until the
  // screen is dark in their hand. Held only while the stream is actually
  // running, so the rest of the app still sleeps normally.
  useKeepAwake(streaming && focused && cameraActive ? "rakshak-live-scan" : undefined, { allowed: true });

  const appendLog = useCallback((line) => {
    setLog((prev) => [line, ...prev].slice(0, LOG_LIMIT));
  }, []);

  // The app being backgrounded unmounts the camera preview, so focus is what
  // gates both the capture loop and the socket.
  useEffect(() => {
    const sub = AppState.addEventListener("change", (state) => setFocused(state === "active"));
    return () => sub?.remove?.();
  }, []);

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

  const simulate = useCallback(async () => {
    try {
      const r = await onSimulate();
      appendLog(`simulate: ${r.plate}${r.hotlist ? " - HOTLIST MATCH" : ""}`);
    } catch (e) {
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
            The scanner needs the camera to read vehicle number plates. It streams detection frames to the server
            over your own network — no video is stored.
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

  const cameraHeight = Math.round(Math.max(MIN_CAMERA_HEIGHT, height * CAMERA_HEIGHT_RATIO));

  return (
    <View style={[s.fill, { backgroundColor: colors.dark[900] }]}>
      {/* Edge-to-edge on Android: the header applies insets.top itself. */}
      <StatusBar style="light" translucent />

      <View style={[s.header, { backgroundColor: colors.dark[800], borderBottomColor: colors.dark[600], paddingTop: insets.top + spacing.sm }]}>
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

        <View style={s.headerRow}>
          <Text style={[s.subtitle, { color: colors.onDark.muted }]} numberOfLines={1}>
            {user?.name} · {String(user?.role || "VOLUNTEER").toUpperCase()}
          </Text>
          <LivePill live={liveScan} colors={colors} s={s} />
        </View>
      </View>

      {/* Memoised on purpose: this subtree holds the native camera, and the
          message rate is several per second. Only these props may cross into
          it - the boxes come through the overlay's own store instead. */}
      <CameraSection
        height={cameraHeight}
        colors={colors}
        s={s}
        focused={focused}
        camRef={camRef}
        streaming={streaming}
        pictureSize={pictureSize}
      />

      <ScrollView
        style={s.scroll}
        contentContainerStyle={{ paddingBottom: insets.bottom + 24 }}
        keyboardShouldPersistTaps="handled"
      >
        <StolenBanner plate={stalePlate} />

        {cameraFault ? (
          <View style={[s.faultBanner, { backgroundColor: colors.dark[700], borderColor: colors.danger }]}>
            <Ionicons name="alert-circle-outline" size={15} color={colors.danger} />
            <Text style={[s.faultText, { color: colors.onDark.body }]}>{cameraFault}</Text>
          </View>
        ) : null}

        {backendState.status === "OFFLINE" ? (
          <View style={[s.faultBanner, { backgroundColor: colors.dark[700], borderColor: colors.danger }]}>
            <Ionicons name="warning-outline" size={15} color={colors.danger} />
            <Text style={[s.faultText, { color: colors.onDark.body }]} numberOfLines={2}>
              Backend unreachable{backendState.error ? `: ${backendState.error}` : ""}
            </Text>
          </View>
        ) : null}

        <InfoPanel
          liveScan={liveScan}
          stalePlate={stalePlate}
          streaming={streaming}
          onToggleStreaming={onToggleStreaming}
          colors={colors}
          s={s}
        />

        <View style={s.actions}>
          <Button
            label="Simulate"
            size="lg"
            variant="secondary"
            onPress={simulate}
            style={s.flex}
            icon={<Ionicons name="dice" size={19} color={colors.onDark.body} />}
          />
          <Button
            label="TEST BACKEND"
            size="lg"
            variant="secondary"
            onPress={handleTestBackend}
            loading={testing}
            loadingLabel="Testing..."
            style={s.flex}
            icon={<Ionicons name="pulse" size={19} color={colors.accent[300]} />}
          />
        </View>

        <ActivityLog log={log} colors={colors} s={s} />
      </ScrollView>
    </View>
  );
}

/** LIVE / RECONNECTING / OFFLINE. The one status the volunteer acts on. */
function LivePill({ live, colors, s }) {
  const state = live?.state || "OFFLINE";
  const tone = state === "LIVE" ? colors.success : state === "RECONNECTING" ? colors.warning : colors.danger;
  return (
    <View style={[s.pill, { borderColor: tone }]}>
      <View style={[s.pillDot, { backgroundColor: tone }]} />
      <Text style={[s.pillText, { color: tone }]}>{state}</Text>
    </View>
  );
}

/**
 * Camera preview plus its overlays, memoised so a new log line never re-renders
 * the camera. Nothing that changes per message may be passed in here.
 */
const CameraSection = memo(function CameraSection({ height, colors, s, focused, camRef, streaming, pictureSize }) {
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
          flash="off"
          animateShutter={false}
          // The smallest size the plate is still readable at. Without it the
          // sensor hands the loop a 12 MP frame to compress down to 45 KB, and
          // that is where the frame rate goes.
          pictureSize={pictureSize}
        />
      ) : null}

      {/* Detection boxes live in their own memoised layer: the camera preview
          above has no state and must not re-render just to move a rectangle. */}
      <LiveDetectionOverlay preview={box} />

      {!streaming ? (
        <View style={s.pausedVeil} pointerEvents="none">
          <Ionicons name="pause-circle" size={34} color="#FFFFFF" />
          <Text style={s.pausedText}>Detection paused</Text>
        </View>
      ) : null}
    </View>
  );
});

/**
 * The alert banner. Two states, both from the server's verdict, nothing else.
 *
 * Red and pulsing means the server has said STOLEN: strict syntax, an exact
 * hot-list match, and either two independent frames agreeing or one frame read at
 * 0.92+ with every glyph above 0.8. That is a car a volunteer should act on.
 *
 * Amber and still means POSSIBLE: it resembles a listed plate but no rule has
 * been met. It deliberately does not pulse, does not vibrate and does not say
 * "police alerted" - there is no alert, because nothing has been filed. Buzzing
 * and shouting for every car that merely looks similar is how a volunteer learns
 * to ignore both, and then the red one is ignored too.
 *
 * The banner is driven only by `plate.state`. It used to be driven by "is there
 * any plate at all", which painted it red for an ordinary clear read - the
 * reported symptom of a STOLEN banner sitting above a card reading "not on the
 * hotlist".
 */
const StolenBanner = memo(function StolenBanner({ plate }) {
  const pulse = useRef(new Animated.Value(1)).current;
  const state = plate?.state;
  const alarming = state === "STOLEN";

  useEffect(() => {
    // Only the red one pulses. An amber banner that pulses at the same rate is
    // an alarm with none of the information.
    if (!plate || !alarming) {
      pulse.setValue(1);
      return undefined;
    }
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(pulse, { toValue: 0.35, duration: 420, useNativeDriver: true }),
        Animated.timing(pulse, { toValue: 1, duration: 420, useNativeDriver: true }),
      ])
    );
    loop.start();
    return () => loop.stop();
  }, [plate, alarming, pulse]);

  if (!plate || (state !== "STOLEN" && state !== "POSSIBLE")) return null;

  const label =
    state === "STOLEN"
      ? `STOLEN VEHICLE ${plate.plate}`
      : `POSSIBLE MATCH · ${plate.plate || "plate unread"}`;

  const sub =
    state === "STOLEN"
      ? `${plate.confidence}%${plate.reads > 1 ? ` · seen in ${plate.reads} frames` : ""} · ${
          plate.alerted ? "police alerted" : "match confirmed"
        }`
      : `${plate.confidence}%${plate.reads > 1 ? ` · seen in ${plate.reads} frames` : ""} · not confirmed, no alert sent`;

  return (
    <Animated.View
      accessibilityRole="alert"
      accessibilityLabel={
        state === "STOLEN"
          ? `Stolen vehicle detected, plate ${plate.plate}, police alerted`
          : `Possible match, plate ${plate.plate}, not confirmed`
      }
      pointerEvents="none"
      style={[
        stolenStyles.banner,
        { backgroundColor: alarming ? "#FF1F1F" : "#B45309", opacity: alarming ? pulse : 1 },
      ]}
    >
      <Ionicons
        name={alarming ? "warning" : "help-circle"}
        size={18}
        color="#FFFFFF"
      />
      <View style={stolenStyles.textWrap}>
        <Text style={stolenStyles.title} numberOfLines={1}>
          {label}
        </Text>
        <Text style={stolenStyles.sub} numberOfLines={1}>
          {sub}
        </Text>
      </View>
    </Animated.View>
  );
});

const stolenStyles = StyleSheet.create({
  banner: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    marginHorizontal: spacing.lg,
    marginTop: spacing.md,
    paddingVertical: 10,
    paddingHorizontal: 12,
    borderRadius: radius.md,
    borderWidth: 2,
    borderColor: "#FFFFFF",
  },
  textWrap: { flex: 1 },
  title: { color: "#FFFFFF", fontSize: 12, fontWeight: "800", letterSpacing: 0.6 },
  sub: { color: "#FFFFFF", fontSize: 11, fontWeight: "700", opacity: 0.92 },
});

// One row per verdict state, used by both the banner and the card. The card
// showing "not on the hotlist" under a red banner is what this replaces, so the
// wording for each state is written once and cannot drift between the two.
const VERDICT_LINE = {
  STOLEN: "on the hot-list · police alerted",
  POSSIBLE: "resembles a listed plate · not confirmed",
  CLEAR: "not on the hot-list",
  UNREAD: "could not read the plate",
};

/**
 * The plate with grey "?" chips on the characters the server was unsure about.
 *
 * This is the one thing the phone can show that the verdict cannot: *which*
 * glyph is doubtful. A server that says POSSIBLE on `DL1ZA9092` because of the
 * `Z` is a different situation from one that says POSSIBLE because of four
 * characters, and a volunteer deciding whether to walk up to the car needs to
 * tell them apart. `weak` is a list of character indices, so a dropped character
 * does not shift the ones after it into the wrong place.
 */
function PlateWithWeakChips({ plate, chars, confidence, s }) {
  const glyphs = String(plate || "").split("");
  const weak = new Set((chars || []).filter((i) => Number.isInteger(i) && i >= 0 && i < glyphs.length));
  return (
    <Text
      style={[s.plateMono, { color: confidence }]}
      numberOfLines={1}
      adjustsFontSizeToFit
    >
      {glyphs.map((g, i) =>
        weak.has(i) ? (
          <Text key={i} style={styles.weakChip}>
            {g === " " ? "_" : g}
            <Text style={styles.weakMark}>?</Text>
          </Text>
        ) : (
          <Text key={i}>{g === " " ? "·" : g}</Text>
        )
      )}
    </Text>
  );
}

/**
 * Everything under the camera, in one panel: last plate, stats, the pause
 * toggle and the recent events. Kept compact so the preview can have the top
 * 65% of the screen instead of fighting a stack of cards.
 *
 * The card and the banner read the *same* `stalePlate` object, so they cannot
 * disagree. That is the second half of the fix: they used to be computed
 * independently, and the two answers could be different at the same instant on
 * the same car.
 */
function InfoPanel({ liveScan, stalePlate, streaming, onToggleStreaming, colors, s }) {
  const last = stalePlate;
  const state = last?.state;
  const chipColor =
    state === "STOLEN" ? "#FF6B6B" : state === "POSSIBLE" ? "#F59E0B" : colors.onDark.strong;
  return (
    <Card tone="dark" style={s.block}>
      <View style={s.blockHeader}>
        <Text style={[s.blockTitle, { color: colors.onDark.strong }]}>LAST PLATE</Text>
        <Pressable
          onPress={onToggleStreaming}
          hitSlop={8}
          accessibilityRole="switch"
          accessibilityState={{ checked: streaming }}
          accessibilityLabel={streaming ? "Pause live detection" : "Resume live detection"}
          style={({ pressed }) => [styles.detectToggle, { opacity: pressed ? 0.7 : 1 }]}
        >
          <View style={[styles.detectTrack, { backgroundColor: streaming ? colors.success : colors.dark[600] }]}>
            <View style={[styles.detectKnob, { alignSelf: streaming ? "flex-end" : "flex-start" }]} />
          </View>
        </Pressable>
      </View>

      {last && last.plate ? (
        <View>
          <PlateWithWeakChips plate={last.plate} chars={last.weak} confidence={chipColor} s={s} />
          <Text style={s.detSub}>
            {`${last.confidence}% · ${VERDICT_LINE[state] || VERDICT_LINE.UNREAD}`}
            {last.reads > 1 ? ` · seen in ${last.reads} frames` : ""}
          </Text>
        </View>
      ) : (
        <View>
          <Text style={[s.plateMono, { color: colors.onDark.muted }]} numberOfLines={1}>
            {last ? "unsure" : "—"}
          </Text>
          <Text style={s.detSub}>
            {last?.reason
              ? `${VERDICT_LINE.UNREAD} · ${last.reason}`
              : streaming
                ? "Point the camera at a vehicle plate."
                : "Live detection is paused."}
          </Text>
        </View>
      )}

      <View style={[s.statRow, { borderTopColor: colors.dark[600] }]}>
        <Stat label="FPS" value={liveScan?.fps ? String(liveScan.fps) : "-"} colors={colors} s={s} />
        <Stat label="SERVER" value={liveScan?.serverMs ? `${liveScan.serverMs}ms` : "-"} colors={colors} s={s} />
        <Stat label="ROUND TRIP" value={liveScan?.roundTripMs ? `${liveScan.roundTripMs}ms` : "-"} colors={colors} s={s} />
      </View>
    </Card>
  );
}

function Stat({ label, value, colors, s }) {
  return (
    <View style={s.stat}>
      <Text style={[s.statLabel, { color: colors.onDark.muted }]}>{label}</Text>
      <Text style={[s.statValue, { color: colors.onDark.strong }]}>{value}</Text>
    </View>
  );
}

function ActivityLog({ log, colors, s }) {
  return (
    <Card tone="dark" style={s.block}>
      <Text style={[s.blockTitle, { color: colors.onDark.strong }]}>RECENT EVENTS</Text>
      {log.length === 0 ? (
        <Text style={[s.emptyText, { color: colors.onDark.muted }]}>Nothing yet.</Text>
      ) : (
        <View style={s.logBody}>
          {log.map((line, i) => (
            <Text key={`${i}-${line.slice(0, 24)}`} style={[s.logLine, { color: colors.onDark.muted }]} numberOfLines={2}>
              {line}
            </Text>
          ))}
        </View>
      )}
    </Card>
  );
}

const styles = StyleSheet.create({
  // A character the server was unsure about: grey, underlined, and marked with a
  // trailing "?" so it is legible without colour and distinguishable from a real
  // character in a list of them.
  weakChip: { color: "#9CA3AF", textDecorationLine: "underline" },
  weakMark: { color: "#6B7280", fontSize: 9 },
  detectToggle: { paddingVertical: 2 },
  detectTrack: {
    width: 46,
    height: 28,
    borderRadius: 14,
    justifyContent: "center",
    paddingHorizontal: 3,
  },
  detectKnob: { width: 22, height: 22, borderRadius: 11, backgroundColor: "#FFFFFF" },
});

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
    // Shown over the preview while paused. Opacity rather than a blur, so it
    // costs nothing on a device that is already struggling to encode frames.
    pausedVeil: {
      ...StyleSheet.absoluteFillObject,
      backgroundColor: "rgba(4,8,20,0.55)",
      alignItems: "center",
      justifyContent: "center",
      gap: 6,
    },
    pausedText: { color: "#FFFFFF", fontSize: fontSize.sm, fontWeight: fontWeight.semibold, letterSpacing: 0.6 },

    scroll: { flex: 1 },
    flex: { flex: 1 },

    faultBanner: {
      flexDirection: "row",
      alignItems: "flex-start",
      gap: spacing.sm,
      marginHorizontal: spacing.lg,
      marginTop: spacing.md,
      padding: spacing.md,
      borderRadius: radius.md,
      borderWidth: 1,
    },
    faultText: { flex: 1, fontSize: fontSize.sm },

    block: { marginHorizontal: spacing.lg, marginTop: spacing.md },
    blockHeader: { flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
    blockTitle: {
      fontSize: fontSize.sm,
      fontWeight: fontWeight.bold,
      letterSpacing: 1.1,
    },
    emptyText: { marginTop: spacing.sm, fontSize: fontSize.sm },

    plateMono: {
      marginTop: spacing.sm,
      fontSize: fontSize.xxl,
      fontWeight: fontWeight.heavy,
      fontFamily: "monospace",
      letterSpacing: 2,
    },
    detSub: { marginTop: 2, fontSize: fontSize.xs, color: "#94A3B8" },

    statRow: {
      flexDirection: "row",
      marginTop: spacing.md,
      paddingTop: spacing.sm,
      borderTopWidth: 1,
      gap: spacing.md,
    },
    stat: { flex: 1 },
    statLabel: { fontSize: 10, fontWeight: fontWeight.semibold, letterSpacing: 0.8 },
    statValue: { marginTop: 2, fontSize: fontSize.sm, fontFamily: "monospace" },

    actions: { flexDirection: "row", gap: spacing.md, marginHorizontal: spacing.lg, marginTop: spacing.md },

    logBody: { marginTop: spacing.sm, gap: 3 },
    logLine: { fontSize: 12, lineHeight: 17, fontFamily: "monospace" },

    permission: { flex: 1, justifyContent: "center", paddingHorizontal: spacing.xl, gap: spacing.lg, alignItems: "center" },
    permissionTitle: { fontSize: fontSize.xl, fontWeight: fontWeight.bold },
    permissionText: { textAlign: "center", lineHeight: 22 },
  });
}
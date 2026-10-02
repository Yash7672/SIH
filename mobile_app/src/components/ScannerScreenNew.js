import { useEffect, useRef, useState } from "react";
import {
  AppState,
  FlatList,
  Modal,
  Pressable,
  SafeAreaView,
  StyleSheet,
  Text,
  Vibration,
  View,
  useWindowDimensions,
} from "react-native";
import { CameraView, useCameraPermissions } from "expo-camera";
import { StatusBar } from "expo-status-bar";
import { ScannerGuideFrame } from "../components/ScannerGuideFrame";
import { ScanResultSheet } from "../components/ScanResultSheet";
import Button from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { ConnectionDot } from "../components/ui/StatusChip";
import { Banner, LoadingState } from "../components/ui/Banner";
import { ThemeModePicker } from "../components/ThemeToggle";
import { fontSize, fontWeight, radius, spacing } from "../theme";
import { useTheme } from "../theme/ThemeContext";

/**
 * The scanner is deliberately always dark in both themes: a bright chrome next
 * to the viewfinder makes the plate the least readable thing on screen, and the
 * volunteer is holding the phone outdoors. Only the chrome tints follow the
 * palette, and the theme preference itself is chosen from the settings sheet.
 *
 * No watermark here on purpose - text over the viewfinder can lower plate-read
 * accuracy. The result sheet has none either, for the same reason.
 */
export default function ScannerScreen({ onScan, onSimulate, onTestBackend, onLogout, deviceReady, user }) {
  const { colors } = useTheme();
  const [perm, requestPerm] = useCameraPermissions();
  const camRef = useRef(null);
  const [camReady, setCamReady] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [testing, setTesting] = useState(false);
  const [backendState, setBackendState] = useState({ status: "UNKNOWN", error: null });
  const [result, setResult] = useState(null);
  const [log, setLog] = useState([]);
  const [tapCount, setTapCount] = useState(0);
  const [devDetails, setDevDetails] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const tapTimer = useRef(null);
  // react-navigation is not installed, so focus comes from AppState: the camera
  // must not run inference while the app sits in the background.
  const [focused, setFocused] = useState(true);
  const { height } = useWindowDimensions();

  useEffect(() => {
    const sub = AppState.addEventListener("change", (state) => setFocused(state === "active"));
    return () => sub?.remove?.();
  }, []);

  useEffect(() => {
    if (!focused) setCamReady(false);
  }, [focused]);

  useEffect(() => {
    return () => {
      if (tapTimer.current) clearTimeout(tapTimer.current);
    };
  }, []);

  const appendLog = (line) => {
    setLog((prev) => [line, ...prev].slice(0, 20));
  };

  const handleTestBackend = async () => {
    setTesting(true);
    try {
      const state = await onTestBackend();
      setBackendState(state);
      appendLog(`backend: ${state.status}${state.error ? ` — ${state.error}` : ""}`);
    } catch (e) {
      setBackendState({ status: "FAILED", error: e.message });
      appendLog(`backend: FAILED — ${e.message}`);
    } finally {
      setTesting(false);
    }
  };

  const capture = async () => {
    if (scanning || !camRef.current || !camReady || !focused) return;
    setScanning(true);
    setResult(null);
    try {
      const scanResult = await onScan(camRef.current);
      setResult(scanResult);
      appendLog(`scan: ${scanResult.plate || "none"} (${Math.round((scanResult.confidence || 0) * 100)}%)`);
      if (scanResult.hotlist) {
        try { Vibration.vibrate([40, 120, 80]); } catch {}
      }
    } catch (e) {
      const r = { state: "network", message: e.message };
      setResult(r);
      appendLog(`error: ${e.message}`);
    } finally {
      setScanning(false);
    }
  };

  const simulate = async () => {
    const r = await onSimulate();
    setResult(r);
    appendLog(`simulate: ${r.plate}`);
    if (r.hotlist) {
      try { Vibration.vibrate([40, 120, 80]); } catch {}
    }
  };

  const onTitleTap = () => {
    const next = tapCount + 1;
    setTapCount(next);
    if (tapTimer.current) clearTimeout(tapTimer.current);
    tapTimer.current = setTimeout(() => setTapCount(0), 2000);
    if (next === 5) {
      setTapCount(0);
      setDevDetails((v) => !v);
      appendLog(`dev details: ${!devDetails ? "on" : "off"}`);
    }
  };

  const renderLogItem = ({ item }) => (
    <Text style={[styles.logLine, { color: colors.onDark.muted }]} numberOfLines={1}>
      {item}
    </Text>
  );

  if (!deviceReady) {
    return <LoadingState label="Registering device…" />;
  }

  if (!perm?.granted) {
    return (
      <SafeAreaView style={[styles.center, { backgroundColor: colors.surface.bg }]}>
        <View style={styles.permission}>
          <Text style={[styles.permissionTitle, { color: colors.surface.text }]}>Camera access needed</Text>
          <Text style={[styles.permissionText, { color: colors.surface.muted }]}>
            The scanner needs the camera to read vehicle number plates. No video is uploaded — only a single still
            frame per scan.
          </Text>
          <Button label="Grant camera access" size="lg" appearance="surface" onPress={requestPerm} />
        </View>
      </SafeAreaView>
    );
  }

  const connected = backendState.status === "CONNECTED";
  const failed = backendState.status === "FAILED";

  return (
    <View style={[styles.container, { backgroundColor: colors.dark[900] }]}>
      <StatusBar style="light" />
      <SafeAreaView style={[styles.safeTop, { backgroundColor: colors.dark[800] }]} edges={["top"]}>
        <View
          style={[
            styles.topBar,
            { backgroundColor: colors.dark[800], borderBottomColor: colors.dark[700] },
          ]}
        >
          <View style={styles.topLeft}>
            <Pressable onPress={onTitleTap} hitSlop={6} accessibilityRole="button" accessibilityLabel="RAKSHAK">
              <Text style={[styles.appName, { color: colors.onDark.strong }]}>RAKSHAK</Text>
            </Pressable>
            <Text style={[styles.user, { color: colors.onDark.muted }]} numberOfLines={1}>
              {user?.name} · {user?.role || "VOLUNTEER"}
            </Text>
          </View>
          <View style={styles.topRight}>
            <ConnectionDot connected={connected} label={backendState.status} />
            <Pressable
              onPress={() => setMenuOpen(true)}
              accessibilityRole="button"
              accessibilityLabel="Open settings menu"
              hitSlop={8}
              style={({ pressed }) => [
                styles.menuBtn,
                { borderColor: colors.dark[500], opacity: pressed ? 0.7 : 1 },
              ]}
            >
              <Text style={[styles.menuBtnText, { color: colors.onDark.body }]}>⋯</Text>
            </Pressable>
          </View>
        </View>

        {failed && backendState.error ? (
          <Banner tone="danger" title="Cannot reach backend" message={backendState.error} style={styles.banner} />
        ) : null}

        {devDetails ? (
          <Card tone="dark" style={styles.devCard}>
            <Text style={[styles.devTitle, { color: colors.onDark.strong }]}>Debug</Text>
            <Text style={[styles.devText, { color: colors.onDark.muted }]}>
              Tap 5 times on the title to toggle this panel.
            </Text>
          </Card>
        ) : null}
      </SafeAreaView>

      <View style={[styles.cameraWrap, { height: height * 0.48 }]}>
        {focused ? (
          <CameraView
            ref={camRef}
            style={StyleSheet.absoluteFill}
            facing="back"
            onCameraReady={() => setCamReady(true)}
          />
        ) : null}
        <ScannerGuideFrame scanning={scanning} hint="Align the number plate inside the frame" />
      </View>

      <SafeAreaView style={[styles.bottom, { backgroundColor: colors.dark[900] }]} edges={["bottom", "left", "right"]}>
        <View style={styles.controls}>
          <Button
            label={scanning ? "Reading plate…" : "Scan"}
            size="lg"
            loading={scanning}
            onPress={capture}
            disabled={!camReady || !focused}
            style={styles.scanBtn}
          />
          <Button label="Simulate" size="lg" variant="secondary" onPress={simulate} style={styles.simBtn} />
        </View>

        <View style={styles.healthRow}>
          <Button
            label={testing ? "Testing…" : "Test backend"}
            size="sm"
            variant="ghost"
            loading={testing}
            onPress={handleTestBackend}
            loadingLabel="Testing…"
          />
          <FlatList
            data={log}
            keyExtractor={(_, i) => String(i)}
            renderItem={renderLogItem}
            horizontal
            showsHorizontalScrollIndicator={false}
            contentContainerStyle={styles.logList}
          />
        </View>
      </SafeAreaView>

      <ScanResultSheet
        result={result}
        onDismiss={() => setResult(null)}
        onRescan={capture}
        onDetailsLongPress={() => setDevDetails((v) => !v)}
      />

      <SettingsSheet
        visible={menuOpen}
        onClose={() => setMenuOpen(false)}
        onTestBackend={() => {
          setMenuOpen(false);
          handleTestBackend();
        }}
        onLogout={() => {
          setMenuOpen(false);
          onLogout();
        }}
        colors={colors}
      />
    </View>
  );
}

/** Bottom sheet holding the three-option theme picker and account actions. */
function SettingsSheet({ visible, onClose, onTestBackend, onLogout, colors }) {
  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <Pressable
        style={[styles.scrim, { backgroundColor: "rgba(2,6,17,0.6)" }]}
        onPress={onClose}
        accessibilityRole="button"
        accessibilityLabel="Close menu"
      />
      <View style={[styles.sheet, { backgroundColor: colors.dark[800], borderColor: colors.dark[600] }]}>
        <View style={[styles.grabber, { backgroundColor: colors.dark[500] }]} />

        <Text style={[styles.sheetTitle, { color: colors.onDark.strong }]}>Appearance</Text>
        <Text style={[styles.sheetHint, { color: colors.onDark.muted }]}>
          Applies to the login screen. The scanner viewfinder stays dark so the plate reads clearly outdoors.
        </Text>
        <ThemeModePicker style={styles.picker} />

        <View style={[styles.sheetActions, { borderTopColor: colors.dark[600] }]}>
          <Button label="Test backend" size="md" variant="secondary" onPress={onTestBackend} style={styles.flex} />
          <Button label="Close" size="md" variant="ghost" onPress={onClose} style={styles.flex} />
          <Button label="Log out" size="md" variant="danger" onPress={onLogout} style={styles.flex} />
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  safeTop: {},
  topBar: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
    borderBottomWidth: 1,
  },
  topLeft: { flex: 1, gap: 2 },
  appName: { fontSize: fontSize.lg, fontWeight: fontWeight.heavy, letterSpacing: 1 },
  user: { fontSize: fontSize.xs },
  topRight: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  menuBtn: {
    width: 34,
    height: 34,
    borderRadius: 10,
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
  },
  menuBtnText: { fontSize: 18, lineHeight: 22, fontWeight: fontWeight.bold },
  banner: { marginHorizontal: spacing.lg, marginBottom: spacing.sm },
  devCard: { marginHorizontal: spacing.lg, marginBottom: spacing.sm },
  devTitle: { fontWeight: fontWeight.bold },
  devText: { fontSize: fontSize.sm },
  cameraWrap: { backgroundColor: "#000" },
  bottom: { flex: 1, justifyContent: "space-between" },
  controls: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.md,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.lg,
  },
  scanBtn: { flex: 1, minHeight: 64 },
  simBtn: { width: 120 },
  healthRow: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
    gap: spacing.md,
  },
  logList: { paddingHorizontal: spacing.sm },
  logLine: { fontSize: fontSize.xs, fontFamily: "monospace" },
  center: { flex: 1, justifyContent: "center" },
  permission: { paddingHorizontal: spacing.xl, gap: spacing.lg, alignItems: "center" },
  permissionTitle: { fontSize: fontSize.xl, fontWeight: fontWeight.bold },
  permissionText: { textAlign: "center", lineHeight: 22 },
  scrim: { ...StyleSheet.absoluteFillObject },
  sheet: {
    marginTop: "auto",
    borderTopLeftRadius: radius.xl,
    borderTopRightRadius: radius.xl,
    borderTopWidth: 1,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.sm,
    paddingBottom: spacing.xl,
    gap: spacing.sm,
  },
  grabber: {
    alignSelf: "center",
    width: 40,
    height: 4,
    borderRadius: 2,
    marginBottom: spacing.sm,
  },
  sheetTitle: { fontSize: fontSize.lg, fontWeight: fontWeight.bold },
  sheetHint: { fontSize: fontSize.xs, lineHeight: 16 },
  picker: { marginTop: spacing.xs },
  sheetActions: {
    flexDirection: "row",
    gap: spacing.sm,
    marginTop: spacing.md,
    paddingTop: spacing.md,
    borderTopWidth: 1,
  },
  flex: { flex: 1 },
});